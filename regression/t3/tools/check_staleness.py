"""Fail when a wire cassette's ``sdk`` field differs from the installed SDK.

Re-record only with ``MONKEYBOT_RECORD_CASSETTES=1``. A lockfile change is the
reminder (§5.5.2, §9); this check does not update cassettes. When a named
distribution is not installed in this environment, the version is the pin in
``uv.lock`` (the file a lockfile PR changes). An installed copy always wins.
"""

from __future__ import annotations

import json
import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

WIRE_ROOT = Path(__file__).resolve().parents[1] / "wire"
_REPO_ROOT = Path(__file__).resolve().parents[3]
_LOCK = _REPO_ROOT / "uv.lock"


def _lock_version(dist_name: str) -> str | None:
    if not _LOCK.is_file():
        return None
    data = tomllib.loads(_LOCK.read_text(encoding="utf-8"))
    packages = data.get("package")
    if not isinstance(packages, list):
        return None
    found: list[str] = []
    for package in packages:
        if not isinstance(package, dict) or package.get("name") != dist_name:
            continue
        pinned = package.get("version")
        if isinstance(pinned, str) and pinned:
            found.append(pinned)
    unique = sorted(set(found))
    if len(unique) == 1:
        return unique[0]
    return None


def sdk_version(dist_name: str) -> tuple[str, str]:
    """``(version, source)`` for ``dist_name``. Source is ``installed`` or ``uv.lock``."""
    try:
        return version(dist_name), "installed"
    except PackageNotFoundError:
        pinned = _lock_version(dist_name)
        if pinned is None:
            raise
        return pinned, "uv.lock"


def mismatches(directory: Path | None = None) -> list[str]:
    """One line per cassette whose ``sdk`` field does not match this environment."""
    root = WIRE_ROOT if directory is None else directory
    if not root.is_dir():
        return []
    problems: list[str] = []
    for path in sorted(root.rglob("*.json")):
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            problems.append(f"{path}: {type(exc).__name__}")
            continue
        sdk = loaded.get("sdk") if isinstance(loaded, dict) else None
        if not isinstance(sdk, dict) or not sdk:
            problems.append(f"{path}: sdk field is missing")
            continue
        for name, recorded in sdk.items():
            if not isinstance(name, str) or not isinstance(recorded, str) or not recorded:
                problems.append(f"{path}: sdk entry {name!r} is not a version string")
                continue
            try:
                current, source = sdk_version(name)
            except PackageNotFoundError:
                problems.append(f"{path}: {name} is not installed and is not pinned in uv.lock")
                continue
            if current != recorded:
                problems.append(f"{path}: {name} cassette {recorded} {source} {current}")
    return problems


def main() -> int:
    found = mismatches()
    if found:
        for line in found:
            print(line)
        return 1
    count = len(list(WIRE_ROOT.rglob("*.json"))) if WIRE_ROOT.is_dir() else 0
    print(f"ok: {count} wire cassettes match")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
