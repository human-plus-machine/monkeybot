#!/usr/bin/env python3
"""Block pull-request creation until local CI has passed for this repo."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
REPO_NAME = os.path.basename(ROOT)
CREATE_PR = re.compile(r"(?:^|[^\w./-])gh\s+pr\s+create\b")
REPO_FLAG = re.compile(r"(?:^|\s)(?:-R|--repo)(?:=|\s+)(\S+)")
CD = re.compile(r"(?:^|[;&|]\s*)cd\s+([^;&|\n]+)")


def emit(permission: str, message: str | None = None) -> None:
    payload: dict[str, str] = {"permission": permission}
    if message:
        payload["user_message"] = message
        payload["agent_message"] = message
    json.dump(payload, sys.stdout)
    sys.stdout.write("\n")


def allow() -> None:
    emit("allow")
    raise SystemExit(0)


def deny(message: str) -> None:
    emit("deny", message)
    raise SystemExit(0)


def in_repo(path: str) -> bool:
    try:
        return os.path.commonpath(
            [os.path.realpath(path), os.path.realpath(ROOT)]
        ) == os.path.realpath(ROOT)
    except ValueError:
        return False


def is_this_repo(value: str) -> bool:
    name = value.rstrip("/").split("/")[-1]
    if name.endswith(".git"):
        name = name[: -len(".git")]
    return name == REPO_NAME


def effective_cwd(cwd: str, command: str) -> str:
    base = cwd or ROOT
    matches = CD.findall(command)
    if not matches:
        return base
    target = matches[-1].strip().strip('"').strip("'")
    target = os.path.expanduser(target)
    if not os.path.isabs(target):
        target = os.path.normpath(os.path.join(base, target))
    return target


def run_ci() -> int:
    completed = subprocess.run(
        ["bash", "scripts/ci-local.sh", "--if-needed"],
        cwd=ROOT,
        stdout=sys.stderr,
    )
    return completed.returncode


def gate_shell(data: dict) -> None:
    command = str(data.get("command") or "")
    if CREATE_PR.search(command) is None:
        allow()

    flagged = REPO_FLAG.search(command)
    if flagged is not None:
        if not is_this_repo(flagged.group(1)):
            allow()
    else:
        cwd = str(data.get("cwd") or "")
        if cwd or CD.search(command):
            if not in_repo(effective_cwd(cwd, command)):
                allow()
        else:
            roots = data.get("workspace_roots") or []
            if len(roots) != 1 or not in_repo(str(roots[0])):
                deny(
                    "Set the shell working directory to the repository before creating a pull request "
                    "so local CI runs in the right project."
                )

    if run_ci() != 0:
        deny("Local CI failed, so the pull request was not created.")
    allow()


def gate_mcp(data: dict) -> None:
    if data.get("tool_name") != "create_pull_request":
        allow()
    raw = data.get("tool_input") or {}
    if isinstance(raw, str):
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            deny("create_pull_request input was not valid JSON, so local CI did not run.")
    elif isinstance(raw, dict):
        payload = raw
    else:
        payload = {}
    repo = str(payload.get("repo") or "")
    if not repo:
        deny(
            "create_pull_request is missing a repo, so local CI cannot tell which project to test."
        )
    if not is_this_repo(repo):
        allow()
    if run_ci() != 0:
        deny("Local CI failed, so the pull request was not created.")
    allow()


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        deny("Local CI hook received invalid input.")
    event = str(data.get("hook_event_name") or "")
    if event == "beforeMCPExecution" or (
        not event and data.get("tool_name") == "create_pull_request"
    ):
        gate_mcp(data)
    else:
        gate_shell(data)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as exc:
        deny(f"Local CI hook failed: {exc}")
