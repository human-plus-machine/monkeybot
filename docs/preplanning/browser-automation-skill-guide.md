# Social Media Browser Automation: agent-browser + Emonk

## The Approach

Use **`agent-browser`** (Vercel Labs) — a purpose-built browser CLI for AI agents. Your Python skill calls it via subprocess. The agent reads the SKILL.md, knows exactly what shell commands to run in what order, and executes them through the existing terminal executor.

**Why not the alternatives:**
- **Playwright Python directly** — you're writing imperative automation code inside the skill, which means the skill becomes brittle logic rather than a simple command sequence. When X changes their UI, you're editing Python, not just a SKILL.md.
- **Playwright MCP** — 13,700 token schema overhead before doing anything. Full DOM trees dumped into context on every action. Wrong tool for known, repeatable tasks.
- **`browser-use`** — calls the LLM on every browser action. Good for exploration, bad for "post this text to X" which is always the same 8 steps.

**Why `agent-browser`:**
- CLI commands → works exactly like your existing shell skills (`cat`, `ls`, `python`)
- Returns compact element refs (`@e1`, `@e2`) not full DOM — ~200 tokens per page vs thousands
- Persistent profiles for auth — log in once, reuse forever
- The SKILL.md describes what commands to call and in what order — the agent doesn't need to figure it out
- 93% less context than Playwright MCP per Vercel's own benchmarks

---

## How It Fits Into Emonk

The skill structure is exactly the same as any other skill. The difference is that instead of calling a Python API, the skill script calls `agent-browser` commands via subprocess.

```
skills/social/post-to-x/
├── SKILL.md          ← agent reads this, knows what commands to call
└── post_to_x.py      ← Python wrapper that calls agent-browser via subprocess
```

The agent reads the SKILL.md, sees "to post to X, call these commands in this order", invokes `post_to_x.py` with the content argument, and the Python script shells out to `agent-browser`.

---

## Auth: How Login Works

Log in once manually. `agent-browser` saves the browser state (cookies, localStorage) to a profile directory. Every future run reuses it — no re-login.

```bash
# Bootstrap — run this once, locally, on your machine (not on Cloud Run)
agent-browser --profile ~/.emonk/profiles/twitter open https://x.com/login --headed
# Log in manually in the browser window that opens
# Close it — profile state is saved to disk
agent-browser close
```

On Cloud Run, the profile directory lives in GCS and gets synced to `/tmp/` at startup. Same pattern as the existing memory system.

---

## The Skill

### `skills/social/post-to-x/SKILL.md`

```markdown
---
name: post-to-x
description: "Post content to X (Twitter). Use when user asks to tweet, post to X, share on Twitter. Validates character limit before posting. Requires content ≤280 chars."
version: 1.0.0
entry_point: skills/social/post-to-x/post_to_x.py
---

# Post to X (Twitter)

## Usage
```bash
python skills/social/post-to-x/post_to_x.py "Your tweet content here"
```

## What it does
1. Opens X compose URL using saved browser profile (already logged in)
2. Takes a snapshot to get element refs
3. Fills the compose box with content
4. Clicks post
5. Waits for success confirmation
6. Screenshots result as audit log

## Output
```json
{"success": true, "screenshot": "/tmp/x_post_confirm.png"}
{"success": false, "error": "Session expired. Re-run bootstrap."}
{"success": false, "error": "Content too long: 300 chars (max 280)"}
```

## Prerequisites
- `agent-browser` installed globally (`npm install -g agent-browser`)
- Profile bootstrapped at `BROWSER_PROFILE_DIR/twitter`
- On Cloud Run: profile synced from GCS on startup

## Selectors (X uses data-testid — stable but can change)
- Compose box: `[data-testid="tweetTextarea_0"]`
- Post button: `[data-testid="tweetButtonInline"]`
- Success toast: `[data-testid="toast"]`

## Recovery
If you get "Session expired": run `scripts/bootstrap_x_auth.sh` locally, upload profile to GCS.
```

### `skills/social/post-to-x/post_to_x.py`

```python
#!/usr/bin/env python3
import sys
import json
import subprocess
import os
from pathlib import Path

PROFILE_DIR = os.environ.get("BROWSER_PROFILE_DIR", str(Path.home() / ".emonk/profiles"))
X_PROFILE = f"{PROFILE_DIR}/twitter"
SCREENSHOT_PATH = "/tmp/x_post_confirm.png"


def run(cmd: list[str], timeout: int = 30) -> tuple[bool, str]:
    """Run an agent-browser command, return (success, output)."""
    result = subprocess.run(
        ["agent-browser", "--profile", X_PROFILE] + cmd,
        capture_output=True, text=True, timeout=timeout
    )
    return result.returncode == 0, result.stdout + result.stderr


def post_to_x(content: str) -> dict:
    if len(content) > 280:
        return {"success": False, "error": f"Content too long: {len(content)} chars (max 280)"}

    if not Path(X_PROFILE).exists():
        return {"success": False, "error": f"Profile not found at {X_PROFILE}. Run bootstrap first."}

    try:
        # 1. Navigate to compose
        ok, out = run(["open", "https://x.com/compose/tweet"])
        if not ok or "login" in out.lower():
            return {"success": False, "error": "Session expired. Re-run bootstrap_x_auth.sh."}

        # 2. Snapshot to get element refs (for debugging / fallback)
        run(["snapshot", "-i"])

        # 3. Fill compose box using semantic locator (stable across UI changes)
        ok, out = run(["find", "role", "textbox", "fill", content, "--name", "Post text"])
        if not ok:
            return {"success": False, "error": f"Could not fill compose box: {out}"}

        # 4. Click post
        ok, out = run(["find", "role", "button", "click", "--name", "Post"])
        if not ok:
            return {"success": False, "error": f"Could not click Post button: {out}"}

        # 5. Wait for success toast
        ok, out = run(["wait", "--text", "Your post was sent"], timeout=15)
        if not ok:
            run(["screenshot", SCREENSHOT_PATH])
            return {"success": False, "error": "Post may have failed (no toast)", "screenshot": SCREENSHOT_PATH}

        # 6. Screenshot as audit log
        run(["screenshot", SCREENSHOT_PATH])
        run(["close"])

        return {"success": True, "screenshot": SCREENSHOT_PATH}

    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Timed out waiting for browser action"}
    except FileNotFoundError:
        return {"success": False, "error": "agent-browser not found. Run: npm install -g agent-browser"}
    except Exception as e:
        return {"success": False, "error": str(e)}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(json.dumps({"success": False, "error": "Usage: post_to_x.py 'content'"}))
        sys.exit(1)

    result = post_to_x(sys.argv[1])
    print(json.dumps(result))
    sys.exit(0 if result["success"] else 1)
```

---

## Bootstrap Script (local, one-time per platform)

```bash
#!/bin/bash
# scripts/bootstrap_x_auth.sh
# Run locally whenever session expires. Uploads profile to GCS.

PROFILE_DIR="${BROWSER_PROFILE_DIR:-$HOME/.emonk/profiles}"
GCS_BUCKET="${GCS_MEMORY_BUCKET:-emonk-memory}"

echo "Opening X login — log in manually, then close the browser window..."
agent-browser --profile "$PROFILE_DIR/twitter" open https://x.com/login --headed
agent-browser close

echo "Uploading profile to GCS..."
gsutil -m cp -r "$PROFILE_DIR/twitter" "gs://$GCS_BUCKET/browser-profiles/twitter"
echo "Done. Profile saved locally and uploaded to GCS."
```

---

## Deployment

### What needs to be installed in Docker

`agent-browser` is a Node.js CLI with its own Chrome download. Add to your Dockerfile:

```dockerfile
FROM python:3.11-slim

# Node.js (required for agent-browser)
RUN apt-get update && apt-get install -y nodejs npm \
    # Chrome system dependencies
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxrandr2 libgbm1 libasound2 \
    && rm -rf /var/lib/apt/lists/*

# Install agent-browser and download Chrome
RUN npm install -g agent-browser && agent-browser install

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .

CMD ["python", "-m", "src.gateway.server"]
```

No Playwright install needed. `agent-browser install` downloads Chrome for Testing (Google's official automation channel) directly.

### Profile Sync on Startup

Profiles live in GCS alongside agent memory. Sync to `/tmp/` at container startup:

```python
# src/core/browser_profiles.py
import os
import subprocess

PROFILE_DIR = os.environ.get("BROWSER_PROFILE_DIR", "/tmp/browser-profiles")
GCS_BUCKET = os.environ["GCS_MEMORY_BUCKET"]

def sync_profiles_from_gcs():
    os.makedirs(PROFILE_DIR, exist_ok=True)
    subprocess.run([
        "gsutil", "-m", "rsync", "-r",
        f"gs://{GCS_BUCKET}/browser-profiles/",
        PROFILE_DIR
    ], check=False)  # Don't fail startup if no profiles yet
```

```python
# src/gateway/server.py — call in startup
from src.core.browser_profiles import sync_profiles_from_gcs

@app.on_event("startup")
async def startup():
    if os.environ.get("GCS_ENABLED") == "true":
        sync_profiles_from_gcs()
```

### Environment Variables

```bash
BROWSER_PROFILE_DIR=/tmp/browser-profiles   # Where profiles live in container
GCS_MEMORY_BUCKET=emonk-memory              # Already exists — profiles go in browser-profiles/ prefix
```

### Cloud Run Notes

`agent-browser` runs a background daemon that stays warm between CLI calls — this is fine in Cloud Run. Two things to set:

```yaml
timeout: 300        # Browser tasks can be slow — set to 5min
concurrency: 1      # Same profile can't be accessed in parallel
```

---

## Adding Other Platforms

Same pattern — different profile name, URL, and selectors in SKILL.md.

**LinkedIn** — uses a Quill `contenteditable` editor, needs `type` not `fill`:
```bash
agent-browser --profile $PROFILE_DIR/linkedin open "https://www.linkedin.com/feed/"
agent-browser find text "Start a post" click
agent-browser find role textbox type "content here"   # type = keystroke events, required
agent-browser find role button click --name "Post"
```

**Threads:**
```bash
agent-browser --profile $PROFILE_DIR/threads open "https://www.threads.net/"
agent-browser find role button click --name "New thread"
agent-browser find role textbox fill "content here"
agent-browser find role button click --name "Post"
```

Each platform gets its own `skills/social/post-to-<platform>/` with its own SKILL.md and bootstrap script.

---

## Failure Modes

| Error | Cause | Fix |
|---|---|---|
| `agent-browser not found` | Not in Docker image | Add to Dockerfile: `npm install -g agent-browser && agent-browser install` |
| `Session expired` | Cookies expired (30-90 days) | Re-run `scripts/bootstrap_x_auth.sh`, re-upload to GCS |
| `Profile not found` | GCS sync failed on startup | Check `BROWSER_PROFILE_DIR` and GCS bucket |
| Post button not found | X changed their UI | Update SKILL.md selectors; semantic locators (`--name "Post"`) are more resilient |
| Daemon port conflict | Previous run didn't clean up | Always call `agent-browser close` at end of skill |
| 2FA triggered | Account security check | Must complete manually; alert via Google Chat |
