from __future__ import annotations

import os
import shutil
import tempfile
import time
from pathlib import Path


def create_scratch_dir(run_id: str, base_dir: str | None = None) -> str:
    """Create and return an isolated temp directory for *run_id*.

    Path: {base_dir or tempfile.gettempdir()}/monkeybot-run-{run_id}
    Created with mode 0o700 (owner-only). Returns absolute path string.
    """
    base = base_dir or tempfile.gettempdir()
    path = os.path.join(base, f"monkeybot-run-{run_id}")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return os.path.abspath(path)


def cleanup_old_runs(base_dir: str, max_age_days: int = 7) -> int:
    """Delete monkeybot-run-* dirs under base_dir older than max_age_days.

    Returns count of directories deleted.
    Silently skips dirs that cannot be removed.
    """
    cutoff = time.time() - (max_age_days * 86400)
    count = 0
    base = Path(base_dir)
    for d in base.glob("monkeybot-run-*"):
        if d.is_dir() and os.path.getmtime(d) < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            count += 1
    return count
