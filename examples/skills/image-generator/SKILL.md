---
name: image-generator
description: Generate images with Vertex AI Nano Banana Pro (Gemini image models) and display them in chat.
---

# image-generator

Generate images using **Vertex AI** Gemini image models (default: `gemini-3-pro-image-preview`, Nano Banana Pro class). Uses the same GCP project and Application Default Credentials as the agent runtime.

## When to use

- The user asks for a new image, illustration, diagram, or visual asset
- You need a PNG saved under `./generated-media/images/` for follow-up or display

## How to run

Shell commands start in **workspace root**. Use workspace-relative paths only.

1. Confirm `list_skills` includes `image-generator`.
2. Run the generator with `run_command` and `argv`:

```json
{
  "argv": [
    "python3",
    "./skills/image-generator/generate_image.py",
    "--prompt",
    "A minimalist logo of a monkey astronaut, flat vector style",
    "--aspect-ratio",
    "16:9"
  ],
  "timeout": 180
}
```

Supported `--aspect-ratio` values: `1:1`, `2:3`, `3:2`, `3:4`, `4:3`, `4:5`, `5:4`, `9:16`, `16:9`, `21:9`.

3. Parse JSON **stdout** from the command result:
   - Success: `{"ok": true, "path": "./generated-media/images/<uuid>.png", ...}`
   - Failure: `{"ok": false, "error": "..."}`

4. Call **`render_image`** with the returned `path` to show the image inline in chat:

```json
{
  "path": "./generated-media/images/<uuid>.png",
  "caption": "Optional short caption for the user"
}
```

## Notes

- Output files are written under `./generated-media/images/` (allowed by `run_command` policy).
- Do not paste raw base64 in chat; always use `render_image` after generation.
- On failure, report the `error` field and do not call `render_image`.
- If the script reports a missing dependency (e.g. `google-genai package not installed`), **do not** run `pip install`, `uv pip install`, or any other package install via `run_command` — installs are blocked by policy. Install `google-genai` in the agent / sandbox environment (e.g. via `monkeybot[gemini]` or the sandbox worker image) and restart the gateway.
