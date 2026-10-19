"""Coding-mode project instructions, repo overview, and the git context hook."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from monkeybot.core.coding.git_context_hook import (
    GitContextHook,
    format_git_status,
    git_status_note,
)
from monkeybot.core.config import reset_runtime_env_state_for_tests
from monkeybot.core.config.snapshot import build_runtime_config
from monkeybot.core.context import TurnContext, build_context
from monkeybot.core.context.epoch import ContextEpochTracker, fingerprint_text
from monkeybot.core.context.project_context import (
    MAX_INSTRUCTIONS_CHARS,
    load_project_instructions,
    repo_overview,
)
from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.prompts.prompt import compose_stable_baseline, compose_system_prompt
from monkeybot.providers._utils import split_system_prompt_for_cache
from tests.core.test_context import FakeMCPClient


def _ctx(**kwargs: object) -> TurnContext:
    base: dict[str, object] = {
        "thread_id": "t",
        "request_id": "r",
        "agent_md": "# Agent",
        "memory_index": [],
        "skills": [],
        "tools": [],
        "user_id": None,
        "parent_run_id": None,
        "model": "gemini-2.5-flash",
    }
    base.update(kwargs)
    return TurnContext(**base)  # type: ignore[arg-type]


def test_instructions_first_hit_wins_in_configured_order(tmp_path: Path) -> None:
    (tmp_path / "CLAUDE.md").write_text("claude rules\n", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("agents rules\n", encoding="utf-8")
    assert load_project_instructions(tmp_path).startswith("Source: AGENTS.md\n\nagents rules")
    assert "claude rules" in load_project_instructions(tmp_path, ("CLAUDE.md", "AGENTS.md"))
    (tmp_path / "AGENTS.md").unlink()
    assert "claude rules" in load_project_instructions(tmp_path)


def test_instructions_nested_default_and_missing(tmp_path: Path) -> None:
    assert load_project_instructions(tmp_path) == ""
    (tmp_path / ".monkeybot").mkdir()
    (tmp_path / ".monkeybot" / "AGENTS.md").write_text("nested\n", encoding="utf-8")
    assert "nested" in load_project_instructions(tmp_path)
    (tmp_path / "AGENTS.md").write_text("   \n", encoding="utf-8")  # blank file is skipped
    assert "nested" in load_project_instructions(tmp_path)


def test_instructions_are_capped_with_a_pointer(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("x" * (MAX_INSTRUCTIONS_CHARS * 2), encoding="utf-8")
    text = load_project_instructions(tmp_path)
    assert "truncated at 12000 chars" in text and "read_file AGENTS.md" in text
    assert len(text) < MAX_INSTRUCTIONS_CHARS + 200


def test_instructions_ignore_symlink_escaping_the_workspace(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("SECRET\n", encoding="utf-8")
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "AGENTS.md").symlink_to(outside)
    assert load_project_instructions(ws) == ""


def test_instruction_headings_cannot_fake_the_volatile_tail(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text(
        "# Rules\n\n## Skills\n- use ruff\n\n## Current date\nnow\n\n## Notes\nkeep\n",
        encoding="utf-8",
    )
    text = load_project_instructions(tmp_path)
    assert "\n\n## Skills\n" not in text and "\n\n## Current date\n" not in text
    assert "### Skills" in text and "## Notes" in text


def test_repo_overview_is_deterministic_and_skips_noise(tmp_path: Path) -> None:
    for d in ("tests", "src/pkg_b", "src/pkg_a", "node_modules/x", ".git", "build"):
        (tmp_path / d).mkdir(parents=True)
    for f in ("pyproject.toml", "README.md", "uv.lock", "scratch.py", "notes.txt"):
        (tmp_path / f).write_text("", encoding="utf-8")
    first = repo_overview(tmp_path)
    assert first == repo_overview(tmp_path)
    assert "python (pyproject.toml, uv.lock) — tests: `uv run pytest -q`" in first
    lines = [ln.strip() for ln in first.splitlines()]
    assert (
        lines.index("src/") < lines.index("pkg_a/") < lines.index("pkg_b/") < lines.index("tests/")
    )
    assert "README.md" in lines and "pyproject.toml" in lines
    for hidden in ("node_modules/", ".git/", "build/", "scratch.py", "notes.txt"):
        assert hidden not in lines
    # An ordinary new source file must not rewrite the (cached) prompt prefix.
    (tmp_path / "scratch2.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "pkg_a" / "mod.py").write_text("", encoding="utf-8")
    assert repo_overview(tmp_path) == first


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"package.json": '{"scripts": {"test": "jest"}}'}, "tests: `npm test`"),
        (
            {"package.json": '{"scripts": {"test": "x"}}', "pnpm-lock.yaml": ""},
            "tests: `pnpm test`",
        ),
        ({"package.json": '{"name": "x"}'}, "node (package.json)"),
        ({"Cargo.toml": ""}, "tests: `cargo test`"),
        ({"go.mod": ""}, "tests: `go test ./...`"),
        ({"Makefile": "test:\n\techo hi\n"}, "tests: `make test`"),
        ({"Makefile": "build:\n\techo hi\n"}, "make (Makefile)"),
        ({"package.json": "{not json"}, "node (package.json)"),
    ],
)
def test_repo_overview_guesses_toolchain(
    tmp_path: Path, files: dict[str, str], expected: str
) -> None:
    for name, body in files.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    assert expected in repo_overview(tmp_path)


def test_repo_overview_empty_workspace_is_empty(tmp_path: Path) -> None:
    assert repo_overview(tmp_path) == ""
    assert repo_overview(tmp_path / "missing") == ""


def _coding_config(tmp_path: Path, *, enabled: bool):
    reset_runtime_env_state_for_tests()
    cfg_dir = tmp_path / "monkeybot_config"
    cfg_dir.mkdir(exist_ok=True)
    (cfg_dir / "monkeybot.yaml").write_text(
        f"coding:\n  enabled: {str(enabled).lower()}\n", encoding="utf-8"
    )
    return build_runtime_config(agent_root=tmp_path)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [True, False])
async def test_build_context_fills_coding_context_only_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    monkeypatch.chdir(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "AGENTS.md").write_text("run make check\n", encoding="utf-8")
    (ws / "pyproject.toml").write_text("", encoding="utf-8")
    agent_md = tmp_path / "AGENT.md"
    agent_md.write_text("You are a bot.\n", encoding="utf-8")
    skills = tmp_path / "skills"
    skills.mkdir()
    try:
        ctx = await build_context(
            "t",
            "r",
            agent_md_path=agent_md,
            memory=None,
            skills_path=skills,
            mcp_client=FakeMCPClient([]),
            workspace_root=ws,
            config=_coding_config(tmp_path, enabled=enabled),
        )
    finally:
        reset_runtime_env_state_for_tests()
    if enabled:
        assert "run make check" in ctx.project_instructions
        assert "python" in ctx.repo_overview
    else:
        assert ctx.project_instructions == "" and ctx.repo_overview == ""


def test_project_context_lives_in_the_cacheable_prefix_in_order() -> None:
    ctx = _ctx(
        project_instructions="Source: AGENTS.md\n\nrun make check",
        repo_overview="Toolchain: python",
    )
    stable, volatile = split_system_prompt_for_cache(compose_system_prompt(ctx))
    assert "## Project instructions" in stable and "run make check" in stable
    assert "## Repo overview" in stable
    assert "run make check" not in volatile
    assert stable.index("monkeybot harness") < stable.index("## Project instructions")
    assert stable.index("## Project instructions") < stable.index("## Repo overview")
    assert compose_stable_baseline(replace(ctx, project_instructions="", repo_overview="")) == (
        compose_stable_baseline(_ctx())
    )


def test_changed_instructions_open_a_new_epoch_but_volatile_noise_does_not() -> None:
    tracker = ContextEpochTracker()

    def admit(instructions: str, skills: str = "s1"):
        ctx = _ctx(project_instructions=instructions)
        stable = compose_stable_baseline(ctx)
        return tracker.reconcile(
            stable_baseline=stable,
            volatile_text=f"\n\n## Skills\n- {skills}",
            stable_fingerprint=fingerprint_text(stable),
            volatile_fingerprint=fingerprint_text(skills),
        )

    assert admit("Source: AGENTS.md\n\nv1").kind == "new_epoch"
    assert admit("Source: AGENTS.md\n\nv1").kind == "unchanged"
    assert admit("Source: AGENTS.md\n\nv1", skills="s2").kind == "volatile_updated"
    assert admit("Source: AGENTS.md\n\nv2").kind == "new_epoch"


def test_format_git_status_variants() -> None:
    assert format_git_status("## main...origin/main\n") == "git: branch main, clean"
    assert (
        format_git_status(
            "## feat/x...origin/feat/x [ahead 2]\n M a.py\nA  b.py\n?? c.py\n?? d.py\n"
        )
        == "git: branch feat/x [ahead 2], 2 modified (a.py, b.py), 2 untracked"
    )
    many = "## main\n" + "".join(f" M f{i}.py\n" for i in range(8))
    assert "8 modified (f0.py, f1.py, f2.py, f3.py, f4.py, +3 more)" in format_git_status(many)
    assert "new.py" in format_git_status("## main\nR  old.py -> new.py\n")
    assert "branch (detached HEAD)" in format_git_status("## HEAD (no branch)\n")
    assert "main (no commits yet)" in format_git_status("## No commits yet on main\n")
    assert format_git_status("") is None and format_git_status("garbage") is None


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
@pytest.mark.asyncio
async def test_git_status_note_on_a_real_repo(tmp_path: Path) -> None:
    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(tmp_path), "-c", "user.email=t@t", "-c", "user.name=t", *args],
            check=True,
            capture_output=True,
        )

    assert await git_status_note(tmp_path) is None  # not a repo yet
    git("init", "-q", "-b", "main")
    (tmp_path / "a.py").write_text("1\n", encoding="utf-8")
    git("add", "a.py")
    git("commit", "-q", "-m", "init")
    assert await git_status_note(tmp_path) == "git: branch main, clean"
    (tmp_path / "a.py").write_text("2\n", encoding="utf-8")
    (tmp_path / "new.py").write_text("x\n", encoding="utf-8")
    assert await git_status_note(tmp_path) == "git: branch main, 1 modified (a.py), 1 untracked"


@pytest.mark.asyncio
async def test_git_status_note_gives_up_when_git_is_slow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / "bin"
    fake.mkdir()
    script = fake / "git"
    script.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}:{__import__('os').environ['PATH']}")
    started = asyncio.get_running_loop().time()
    assert await git_status_note(tmp_path, timeout_s=0.2) is None
    assert asyncio.get_running_loop().time() - started < 2


@pytest.mark.asyncio
async def test_git_hook_appends_to_existing_inject_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_note(root: Path, **_: object) -> str:
        return f"git: branch main, clean ({root.name})"

    monkeypatch.setattr("monkeybot.core.coding.git_context_hook.git_status_note", fake_note)
    mgr = HookManager()

    async def earlier(p: HookPayload) -> None:
        p.inject_text = "earlier note"

    mgr.register(HookEvent.PRE_TURN, earlier)
    GitContextHook().register(mgr)
    payload = HookPayload(
        event=HookEvent.PRE_TURN,
        thread_id="t",
        request_id="r",
        ctx=_ctx(workspace_root=tmp_path),
    )
    await mgr.fire(payload)
    assert payload.inject_text == f"earlier note\ngit: branch main, clean ({tmp_path.name})"

    no_root = HookPayload(event=HookEvent.PRE_TURN, thread_id="t", request_id="r", ctx=_ctx())
    await mgr.fire(no_root)
    assert no_root.inject_text == "earlier note"
