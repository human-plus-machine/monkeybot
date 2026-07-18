---
name: image-generator
description: Generate images with Gemini image models (Nano Banana Pro) and display them in chat.
---

# image-generator

Generate images using Gemini image models (default: `gemini-3-pro-image-preview`, Nano Banana Pro class).

**Auth (preferred):** set `GEMINI_API_KEY` or `GOOGLE_API_KEY` in the agent `.env` (Google AI Studio).  
**Fallback:** Vertex AI via `GCP_PROJECT_ID` / `VERTEX_AI_PROJECT_ID` / `GOOGLE_CLOUD_PROJECT` plus Application Default Credentials.

## When to use

- The user asks for a new image, illustration, diagram, or visual asset
- You need a PNG saved under `./generated-media/images/` for follow-up or display

## How to run

Shell commands start in **workspace root**. The trusted generator script is in
the separate read-only skills root from `list_skills`; generated output remains
workspace-relative.

1. Confirm `list_skills` includes `image-generator` and note `skills_path`.
2. Run the generator with `run_command` and **direct** `python3` argv (do **not**
   wrap in `bash -c` — that can pick a system Python without `google-genai`):

```json
{
  "argv": [
    "python3",
    "<skills_path>/image-generator/generate_image.py",
    "--prompt",
    "A minimalist logo of a monkey astronaut, flat vector style",
    "--aspect-ratio",
    "16:9"
  ],
  "timeout": 180
}
```

Replace `<skills_path>` with the absolute `skills_path` returned by `list_skills`.

Supported `--aspect-ratio` values: `1:1`, `2:3`, `3:2`, `3:4`, `4:3`, `4:5`, `5:4`, `9:16`, `16:9`, `21:9`.

3. Parse JSON **stdout** from the command result:
   - Success: `{"ok": true, "path": "./generated-media/images/<uuid>.png", "prompt": "...", ...}`
   - Failure: `{"ok": false, "error": "..."}`

4. Call **`load_file`** with the returned `path` only. That emits an `ImageBlock` to the
   chat UI (and loads pixels into model context on vision-capable providers):

```
load_file(path="<path from script>")
```

Do **not** use `read_file` on the PNG — that is for text files only.

5. Caption the image using the `--prompt` you submitted (or the echoed `prompt` field).
   Do **not** invent a different subject from memory or earlier chats. On text-only
   providers you will not see pixels — still describe from this turn's prompt.

## Notes

- Output files are written under `./generated-media/images/` (allowed by `run_command` policy).
- Do not paste raw base64 in chat; always use `load_file` after generation.
- On failure, report the `error` field and do not call `load_file`. Never `glob` for
  `**/*.png` and load an arbitrary older file as a substitute.
- Do **not** use the `task` tool for this skill — run the steps above yourself.
- `python3` / `python` in `run_command` use the gateway interpreter (which includes
  `google-genai` via monkeybot). Do not run `pip install` / `uv pip install` via
  `run_command` — installs are blocked by policy.
