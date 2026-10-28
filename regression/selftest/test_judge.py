"""Quick smoke test for the deepeval Gemini judge.

This calls the judge model over the network, so it is marked ``live`` and is
deselected unless ``--live`` is passed:

    uv run pytest regression/selftest/test_judge.py --live

It passes when the judge answers and fails (with the traceback printed) when
something is broken.
"""

from __future__ import annotations

import os
import traceback

import pytest


def main() -> int:
    print("=== deepeval Gemini judge smoke test ===\n")

    # ── env check ──────────────────────────────────────────────────────────────
    project = (
        os.environ.get("GOOGLE_CLOUD_PROJECT")
        or os.environ.get("VERTEX_AI_PROJECT_ID")
        or os.environ.get("GCP_PROJECT_ID")
    )
    location = os.environ.get("VERTEX_AI_LOCATION") or "us-central1"
    creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")

    print(f"  project  : {project or '(NOT SET)'}")
    print(f"  location : {location}")
    print(f"  creds    : {creds or '(NOT SET)'}")
    if creds:
        import pathlib

        p = pathlib.Path(creds)
        print(f"  creds ok : {p.exists()} (size={p.stat().st_size if p.exists() else 'N/A'})")
    print()

    if not project:
        print("FAIL: no GCP project env var set (GOOGLE_CLOUD_PROJECT / VERTEX_AI_PROJECT_ID)")
        return 1

    # ── import check ────────────────────────────────────────────────────────────
    try:
        import google.genai  # noqa: F401

        print("  google.genai import: OK")
    except ImportError as e:
        print(f"FAIL: google.genai not installed: {e}")
        return 1

    try:
        from deepeval.models import GeminiModel

        print("  deepeval GeminiModel import: OK")
    except ImportError as e:
        print(f"FAIL: deepeval GeminiModel not importable: {e}")
        return 1

    # ── build judge ─────────────────────────────────────────────────────────────
    try:
        judge = GeminiModel(model="gemini-2.5-flash", project=project, location=location)
        print(f"  GeminiModel init: OK (vertexai={judge.should_use_vertexai()})")
    except Exception:
        print("FAIL: GeminiModel init raised:")
        traceback.print_exc()
        return 1

    # ── single generate call ────────────────────────────────────────────────────
    try:
        response, cost = judge.generate("Reply with exactly the word: HELLO")
        print(f"  generate: OK (response={repr(response[:80])!s} cost={cost})")
    except Exception:
        print("FAIL: judge.generate raised:")
        traceback.print_exc()
        return 1

    print("\nPASS: Gemini judge is working correctly via Vertex AI.")
    return 0


@pytest.mark.live
def test_gemini_judge_answers() -> None:
    assert main() == 0
