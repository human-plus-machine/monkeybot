from __future__ import annotations

from pathlib import Path

from monkeybot.tools.file_ops import read_file, write_file
from monkeybot.tools.memory_ops import search_memory
from monkeybot.tools.run_command import _safe_env, format_result, run_command
from monkeybot.tools.skill_ops import list_skills


async def test_run_command_echo():
    result = await run_command("echo hello")
    assert result.exit_code == 0
    assert "hello" in result.stdout


async def test_run_command_timeout():
    result = await run_command("sleep 100", timeout=1)
    assert result.exit_code == 124
    assert "timed out" in result.stderr


def test_read_file_exists(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("hello")
    assert read_file(str(f)) == "hello"


def test_read_file_missing(tmp_path):
    result = read_file(str(tmp_path / "nope.txt"))
    assert result.startswith("ERROR: File not found")


def test_read_file_access_denied(tmp_path):
    f = tmp_path / "secret.txt"
    f.write_text("secret")
    other = tmp_path / "other"
    other.mkdir()
    result = read_file(str(f), allowed_roots=[other])
    assert result.startswith("ERROR: Access denied")


def test_write_file_creates(tmp_path):
    path = str(tmp_path / "out.txt")
    result = write_file(path, "hello world")
    assert result.startswith("Success:")
    assert Path(path).read_text() == "hello world"


def test_write_file_append(tmp_path):
    path = str(tmp_path / "out.txt")
    write_file(path, "line1\n")
    write_file(path, "line2\n", append=True)
    assert Path(path).read_text() == "line1\nline2\n"


def test_write_file_access_denied(tmp_path):
    f = tmp_path / "subdir" / "out.txt"
    allowed = tmp_path / "other"
    allowed.mkdir()
    result = write_file(str(f), "hello", allowed_roots=[allowed])
    assert result.startswith("ERROR: Access denied")


def test_write_file_creates_parent_dirs(tmp_path):
    path = str(tmp_path / "a" / "b" / "c" / "out.txt")
    result = write_file(path, "deep content")
    assert result.startswith("Success:")
    assert Path(path).read_text() == "deep content"


def test_list_skills_lists_all(tmp_path):
    for name in ["skill-a", "skill-b"]:
        (tmp_path / name).mkdir()
        (tmp_path / name / "SKILL.md").write_text(f"# {name}\nA skill called {name}.")
    result = list_skills(str(tmp_path))
    assert "skill-a" in result
    assert "skill-b" in result


def test_list_skills_filter(tmp_path):
    (tmp_path / "alpha").mkdir()
    (tmp_path / "alpha" / "SKILL.md").write_text("# Alpha\nDoes alpha things.")
    (tmp_path / "beta").mkdir()
    (tmp_path / "beta" / "SKILL.md").write_text("# Beta\nDoes beta things.")
    result = list_skills(str(tmp_path), filter="alpha")
    assert "alpha" in result
    assert "beta" not in result


def test_list_skills_empty_dir(tmp_path):
    result = list_skills(str(tmp_path))
    assert result == "No skills found."


def test_list_skills_missing_dir(tmp_path):
    result = list_skills(str(tmp_path / "nonexistent"))
    assert "No skills" in result


def test_builtin_skills_discoverable() -> None:
    """All 4 built-in skills are discovered by list_skills."""
    from monkeybot.tools.skill_ops import list_skills
    result = list_skills(skills_path=".agents/skills")
    for name in ("memory-save", "memory-search", "file-ops", "self-improve"):
        assert name in result, f"Skill {name!r} not found in list_skills output"


def test_search_memory_matches(tmp_path):
    (tmp_path / "python.md").write_text("Python is a great language.")
    (tmp_path / "java.md").write_text("Java is verbose.")
    (tmp_path / "other.md").write_text("Other stuff entirely.")
    result = search_memory("python", str(tmp_path))
    assert "python" in result.lower()
    assert "java" not in result.lower()


def test_search_memory_no_match(tmp_path):
    (tmp_path / "test.md").write_text("Nothing relevant here.")
    result = search_memory("xyz_impossible_query", str(tmp_path))
    assert "xyz_impossible_query" in result


def test_search_memory_missing_path(tmp_path):
    result = search_memory("anything", str(tmp_path / "no_such_dir"))
    assert result == "No memory files found."


def test_search_memory_max_results(tmp_path):
    for i in range(5):
        (tmp_path / f"file{i}.md").write_text(f"Contains keyword word {i}")
    result = search_memory("keyword", str(tmp_path), max_results=2)
    # Should have at most 2 "###" headings
    assert result.count("###") <= 2


def test_format_result_with_stderr():
    from monkeybot.tools.run_command import CommandResult
    r = CommandResult(stdout="out", stderr="err", exit_code=1, duration_ms=100)
    fmt = format_result(r)
    assert "stderr: err" in fmt
    assert "exit_code: 1" in fmt


def test_format_result_no_stderr():
    from monkeybot.tools.run_command import CommandResult
    r = CommandResult(stdout="out", stderr="", exit_code=0, duration_ms=50)
    fmt = format_result(r)
    assert "stderr" not in fmt


def test_safe_env_redacts_gemini_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "real-secret-key")
    env = _safe_env()
    assert env["GEMINI_API_KEY"] == "REDACTED"
    assert "real-secret-key" not in env.values()
