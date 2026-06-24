#!/usr/bin/env python3
"""Generate a PNG via Vertex AI Gemini image models; print JSON result to stdout."""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import sys
import uuid
from pathlib import Path

_VALID_ASPECT_RATIOS = frozenset(
    {"1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9", "21:9"}
)


def _emit(ok: bool, **fields: object) -> None:
    payload = {"ok": ok, **fields}
    print(json.dumps(payload, ensure_ascii=False))
    sys.exit(0 if ok else 1)


def _resolve_workspace_root() -> Path:
    for env_name in ("MONKEYBOT_WORKSPACE_ROOT", "WORKSPACE_ROOT"):
        raw = os.environ.get(env_name, "").strip()
        if raw:
            p = Path(raw)
            return p.resolve() if p.is_absolute() else (Path.cwd() / p).resolve()
    nested = (Path.cwd() / "workspace").resolve()
    if nested.is_dir():
        return nested
    return Path.cwd().resolve()


def _output_dir() -> Path:
    return _resolve_workspace_root() / "generated-media" / "images"


def _vertex_project_and_location(model_id: str) -> tuple[str, str]:
    project = (
        os.environ.get("GCP_PROJECT_ID")
        or os.environ.get("VERTEX_AI_PROJECT_ID")
        or os.environ.get("GOOGLE_CLOUD_PROJECT")
    )
    if not project or not str(project).strip():
        raise ValueError(
            "Set VERTEX_AI_PROJECT_ID, GCP_PROJECT_ID, or GOOGLE_CLOUD_PROJECT"
        )
    explicit = os.environ.get("VERTEX_AI_LOCATION") or os.environ.get("GOOGLE_CLOUD_LOCATION")
    if explicit and str(explicit).strip():
        return str(project).strip(), str(explicit).strip()
    if "preview" in model_id.lower():
        return str(project).strip(), "global"
    return str(project).strip(), "us-central1"


def _prepare_google_application_credentials() -> None:
    """Resolve ADC credentials file path (same contract as :class:`GeminiProvider`).

    The gateway sets ``GOOGLE_APPLICATION_CREDENTIALS`` to a service-account JSON
    file path (see playground ``.env`` / ``GCP_AUTH_FILE``). ``genai.Client`` uses
    Application Default Credentials from that path — no inline JSON in env.
    """
    raw = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not raw:
        raw = os.environ.get("GCP_AUTH_FILE", "").strip()
    if not raw:
        return
    cred_path = Path(raw).expanduser()
    if not cred_path.is_absolute():
        cred_path = (Path.cwd() / cred_path).resolve()
    else:
        cred_path = cred_path.resolve()
    if not cred_path.is_file():
        raise ValueError(f"GOOGLE_APPLICATION_CREDENTIALS file not found: {cred_path}")
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(cred_path)


def _image_bytes_from_response(response: object) -> bytes | None:
    parts = getattr(response, "parts", None)
    if parts is None:
        candidates = getattr(response, "candidates", None) or []
        if candidates:
            content = getattr(candidates[0], "content", None)
            parts = getattr(content, "parts", None) if content is not None else None
    for part in parts or []:
        inline = getattr(part, "inline_data", None)
        if inline is None:
            continue
        data = getattr(inline, "data", None)
        if not data:
            continue
        if isinstance(data, str):
            try:
                return base64.b64decode(data)
            except binascii.Error:
                continue
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
        as_image = getattr(part, "as_image", None)
        if callable(as_image):
            try:
                img = as_image()
                from io import BytesIO

                buf = BytesIO()
                img.save(buf, format="PNG")
                return buf.getvalue()
            except Exception:
                continue
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate an image with Vertex AI Gemini.")
    parser.add_argument("--prompt", required=True, help="Full image description")
    parser.add_argument(
        "--aspect-ratio",
        default="16:9",
        help="Aspect ratio (default 16:9)",
    )
    args = parser.parse_args()

    prompt = str(args.prompt or "").strip()
    if not prompt:
        _emit(False, error="prompt is empty")

    aspect = str(args.aspect_ratio or "16:9").strip()
    if aspect not in _VALID_ASPECT_RATIOS:
        _emit(False, error=f"invalid aspect-ratio: {aspect}")

    try:
        from google import genai
        from google.genai import types
    except ImportError:
        _emit(False, error="google-genai package not installed (pip install google-genai)")

    model_id = (os.environ.get("IMAGE_GEN_MODEL") or "gemini-3-pro-image-preview").strip()
    if "/" in model_id:
        _emit(False, error="IMAGE_GEN_MODEL must be a model id without vertexai/ prefix")

    try:
        project, location = _vertex_project_and_location(model_id)
    except ValueError as exc:
        _emit(False, error=str(exc))

    try:
        _prepare_google_application_credentials()
    except ValueError as exc:
        _emit(False, error=str(exc))

    client = genai.Client(vertexai=True, project=project, location=location)
    config = types.GenerateContentConfig(
        image_config=types.ImageConfig(aspect_ratio=aspect),
    )

    try:
        response = client.models.generate_content(
            model=model_id,
            contents=prompt,
            config=config,
        )
    except Exception as exc:
        _emit(False, error=str(exc))

    raw = _image_bytes_from_response(response)
    if not raw:
        _emit(False, error="No image bytes in model response")

    output_dir = _output_dir()
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.png"
    out_path = output_dir / filename
    try:
        out_path.write_bytes(raw)
    except OSError as exc:
        _emit(False, error=f"Failed to write {out_path}: {exc}")

    rel = f"./generated-media/images/{filename}"
    _emit(True, path=rel, model=model_id, aspect_ratio=aspect)


if __name__ == "__main__":
    main()
