"""CI gate: no new ``os.environ.get`` / ``os.getenv`` of ``ENV_MAP`` keys outside allowlisted modules."""

from __future__ import annotations

import re
from pathlib import Path

from monkeybot.core.config.runtime_env import ENV_MAP

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src" / "monkeybot"

# Subprocess transport / pin capture. In-process readers must use current_env / env_value.
_ALLOWED = frozenset(
    {
        _SRC / "core" / "config" / "snapshot.py",
        _SRC / "core" / "layout.py",
    }
)

_GET_RE = re.compile(
    r"""os\.(?:environ\.get|getenv)\(\s*['\"]([^'\"]+)['\"]""",
    re.MULTILINE,
)


def test_no_env_map_os_environ_gets_outside_allowlist() -> None:
    env_names = set(ENV_MAP.values())
    hits: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        if path in _ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        for match in _GET_RE.finditer(text):
            key = match.group(1)
            if key not in env_names:
                continue
            line_no = text.count("\n", 0, match.start()) + 1
            rel = path.relative_to(_REPO_ROOT)
            hits.append(f"{rel}:{line_no}: {match.group(0).strip()}")
    assert not hits, "ENV_MAP keys must be read via current_env/env_value, not os.environ.get:\n" + "\n".join(
        hits
    )
