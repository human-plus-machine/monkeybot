"""T2-FILE-27: ripgrep and the Python grep walker agree, and lookaround stays on Python."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from monkeybot.core.tools.workspace_service import WorkspaceFileService


def _core(result: dict[str, object]) -> dict[str, object]:
    matches = result["matches"]
    assert isinstance(matches, list)
    ordered = sorted(matches, key=lambda row: (row["path"], row["line"], row["text"]))
    return {
        "matches": ordered,
        "match_count": result["match_count"],
        "total_match_count": result["total_match_count"],
        "scan_complete": result["scan_complete"],
    }


@pytest.mark.case("T2-FILE-27")
def test_grep_rg_vs_python_parity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text('print("hostile")\n', encoding="utf-8")
    (root / "notes.txt").write_text("hostile workspace\n", encoding="utf-8")
    service = WorkspaceFileService(root)
    pattern = "hostile"
    rg_bin = shutil.which("rg")
    assert rg_bin, "rg must be on PATH so the ripgrep and Python walkers can be compared"

    with_rg = service.grep(pattern)
    monkeypatch.setattr(
        "monkeybot.core.tools.workspace_service.shutil.which",
        lambda _name: None,
    )
    without_rg = service.grep(pattern)
    assert _core(with_rg) == _core(without_rg)
    assert {match["path"] for match in with_rg["matches"]} == {"src/app.py", "notes.txt"}

    monkeypatch.undo()

    def _refuse_rg(self: object, *args: object, **kwargs: object) -> object:
        del self, args, kwargs
        raise AssertionError("lookaround pattern must use the Python grep walker")

    monkeypatch.setattr(WorkspaceFileService, "_grep_with_rg", _refuse_rg)
    lookaround = r'(?<=print\()"hostile"'
    found = service.grep(lookaround)
    assert found["matches"] == [{"path": "src/app.py", "line": 1, "text": 'print("hostile")'}]
    assert found["total_match_count"] == 1
    assert found["scan_complete"] is True
