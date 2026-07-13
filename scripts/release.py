#!/usr/bin/env python3
"""Release helpers for monkeybot. Two steps, two trust levels:

  prepare  - anyone: bump a package version, cut CHANGELOG.md, open the
             develop -> main PR. Touches `develop` only.
  publish  - CI only, runs on push to `main`: tag whichever package
             versions aren't tagged yet and cut a GitHub Release per tag.
             Emits ``packages`` via ``GITHUB_OUTPUT`` (comma-separated,
             core, browser, then cli) so the same workflow can Trusted-Publish to PyPI.

Branch protection on `main` (require PR + restrict merge to admins) is what
actually gates the promotion - this script just does the busywork around it.
"""
from __future__ import annotations

import argparse
import datetime
import os
import re
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
# Insertion order matters: publish core/browser before CLI (the scaffold uses both).
PACKAGES = {
    "core": ROOT / "pyproject.toml",
    "browser": ROOT / "integrations" / "browser-mcp" / "pyproject.toml",
    "cli": ROOT / "cli" / "pyproject.toml",
}


def run(*args: str, **kw: Any) -> str:
    completed = subprocess.run(args, cwd=ROOT, check=True, text=True, capture_output=True, **kw)
    return str(completed.stdout).strip()


def read_version(pyproject: Path) -> str:
    m = re.search(r'^version = "([^"]+)"', pyproject.read_text(), re.M)
    if not m:
        raise SystemExit(f"no version field in {pyproject}")
    return m.group(1)


def bump(version: str, part: str) -> str:
    major, minor, patch = (int(x) for x in version.split("."))
    if part == "major":
        return f"{major + 1}.0.0"
    if part == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def write_version(pyproject: Path, new_version: str) -> None:
    text = pyproject.read_text()
    text = re.sub(r'^version = "[^"]+"', f'version = "{new_version}"', text, count=1, flags=re.M)
    pyproject.write_text(text)


def cut_changelog(versions: Sequence[str]) -> None:
    """Move [Unreleased] notes into dated sections for one or more packages.

    A coordinated release may bump core, browser MCP, and CLI to different
    semantic versions.  Each package still needs an exact changelog heading so
    ``publish`` can create its tag and release independently.
    """
    unique_versions = tuple(dict.fromkeys(versions))
    if not unique_versions:
        raise SystemExit("no release versions supplied")
    text = CHANGELOG.read_text()
    today = datetime.date.today().isoformat()
    m = re.search(r"## \[Unreleased\]\n(.*?)(?=\n## \[|\Z)", text, re.S)
    if not m:
        raise SystemExit("CHANGELOG.md has no [Unreleased] section to cut")
    body = m.group(1).strip("\n")
    if not body:
        raise SystemExit("Unreleased section is empty - nothing to release")
    sections = "\n\n".join(
        f"## [{version}] - {today}\n\n{body}" for version in unique_versions
    )
    new_section = f"## [Unreleased]\n\n{sections}\n"
    text = text[: m.start()] + new_section + text[m.end() :]
    CHANGELOG.write_text(text)


def changelog_section(version: str) -> str | None:
    text = CHANGELOG.read_text()
    m = re.search(rf"## \[{re.escape(version)}\].*?\n(.*?)(?=\n## \[|\Z)", text, re.S)
    return m.group(1).strip() if m else None


def write_github_output(key: str, value: str) -> None:
    """Append a ``key=value`` line to ``GITHUB_OUTPUT`` when running in Actions."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{key}={value}\n")


def cmd_prepare(args: argparse.Namespace) -> None:
    names = list(PACKAGES) if args.package == "all" else [args.package]
    old_versions = {name: read_version(PACKAGES[name]) for name in names}
    new_versions = {name: bump(version, args.bump) for name, version in old_versions.items()}
    branch = (
        f"release/all-v{new_versions['core']}"
        if args.package == "all"
        else f"release/{args.package}-v{new_versions[args.package]}"
    )

    # Cut on a throwaway branch, not develop directly: if the PR is closed
    # without merging, develop is untouched.
    run("git", "checkout", "-b", branch)
    for name, version in new_versions.items():
        write_version(PACKAGES[name], version)
    cut_changelog(list(new_versions.values()))
    run("git", "add", *(str(PACKAGES[name]) for name in names), str(CHANGELOG))
    release_labels = ", ".join(f"{name} v{version}" for name, version in new_versions.items())
    run("git", "commit", "-m", f"chore(release): {release_labels}")
    run("git", "push", "-u", "origin", branch)
    run(
        "gh", "pr", "create",
        "--base", "main",
        "--head", branch,
        "--title", f"Release {release_labels}",
        "--body", "\n".join(
            f"## {name} v{version}\n\n{changelog_section(version)}"
            for name, version in new_versions.items()
        ),
    )
    print(
        "Opened release PR: "
        + ", ".join(
            f"{name} {old_versions[name]} -> {new_versions[name]}" for name in names
        )
    )


def cmd_publish(_: argparse.Namespace) -> None:
    existing_tags = set(run("git", "tag", "--list").splitlines())
    released: list[str] = []
    for name, pyproject in PACKAGES.items():
        version = read_version(pyproject)
        tag = f"{name}-v{version}"
        if tag in existing_tags:
            continue
        notes = changelog_section(version)
        if notes is None:
            # No changelog entry for this version - it predates this tooling
            # (e.g. the version already on main when this script was added).
            # Skip rather than publish a release with placeholder notes.
            print(f"Skipping {tag}: no changelog entry found (pre-existing version)")
            continue
        run("git", "tag", tag)
        run("git", "push", "origin", tag)
        run("gh", "release", "create", tag, "--title", f"{name} v{version}", "--notes", notes)
        print(f"Published {tag}")
        released.append(name)

    # Always emit packages (possibly empty) so the PyPI job can gate cleanly.
    packages_csv = ",".join(released)
    write_github_output("packages", packages_csv)
    print(f"released_packages={packages_csv or '(none)'}")

    if released:
        # Keep develop from drifting behind main now that releases land via
        # a release/* branch instead of develop itself.
        run("git", "fetch", "origin", "develop")
        run("git", "checkout", "-B", "develop", "origin/develop")
        run("git", "merge", "--no-edit", "origin/main")
        run("git", "push", "origin", "develop")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("prepare", help="bump version + cut changelog + open release PR")
    p.add_argument("package", choices=[*PACKAGES, "all"])
    p.add_argument("bump", choices=["major", "minor", "patch"])
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("publish", help="tag + GitHub Release for any untagged package versions")
    p.set_defaults(func=cmd_publish)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
