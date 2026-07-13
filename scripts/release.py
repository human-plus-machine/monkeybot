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
from collections.abc import Mapping
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
PACKAGE_CHANGELOG_TITLES = {
    "core": "Core",
    "browser": "Browser MCP",
    "cli": "CLI",
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


def _split_package_notes(body: str) -> dict[str, str]:
    """Return package-specific notes from the structured Unreleased section."""
    titles = "|".join(re.escape(title) for title in PACKAGE_CHANGELOG_TITLES.values())
    matches = list(re.finditer(rf"^### ({titles})\n(.*?)(?=^### (?:{titles})\n|\Z)", body, re.M | re.S))
    if not matches:
        raise SystemExit("Unreleased notes must be grouped under ### Core, ### Browser MCP, or ### CLI")
    notes = {title: content.strip() for title, content in (m.groups() for m in matches)}
    if len(notes) != len(matches):
        raise SystemExit("Unreleased package headings must not be repeated")
    return {
        package: notes.get(title, "")
        for package, title in PACKAGE_CHANGELOG_TITLES.items()
    }


def _format_unreleased(notes: Mapping[str, str]) -> str:
    blocks = [
        f"### {PACKAGE_CHANGELOG_TITLES[package]}\n\n{body}"
        for package, body in notes.items()
        if body
    ]
    return "## [Unreleased]" + ("\n\n" + "\n\n".join(blocks) if blocks else "")


def _changelog_heading(package: str, version: str) -> str:
    return f"{package} v{version}"


def cut_changelog(versions: Mapping[str, str]) -> None:
    """Cut only each package's own Unreleased notes into its version section."""
    if not versions:
        raise SystemExit("no release versions supplied")
    unknown = set(versions) - set(PACKAGES)
    if unknown:
        raise SystemExit(f"unknown packages: {', '.join(sorted(unknown))}")
    text = CHANGELOG.read_text()
    today = datetime.date.today().isoformat()
    m = re.search(r"## \[Unreleased\]\n(.*?)(?=\n## \[|\Z)", text, re.S)
    if not m:
        raise SystemExit("CHANGELOG.md has no [Unreleased] section to cut")
    notes = _split_package_notes(m.group(1).strip("\n"))
    selected = {package: notes[package] for package in versions}
    if not all(selected.values()):
        raise SystemExit("each released package needs notes in the Unreleased section")
    sections = "\n\n".join(
        f"## [{_changelog_heading(package, version)}] - {today}\n\n{selected[package]}"
        for package, version in versions.items()
    )
    remaining = {package: body for package, body in notes.items() if package not in versions}
    new_section = f"{_format_unreleased(remaining)}\n\n{sections}\n"
    text = text[: m.start()] + new_section + text[m.end() :]
    CHANGELOG.write_text(text)


def changelog_section(package: str, version: str) -> str | None:
    text = CHANGELOG.read_text()
    heading = re.escape(_changelog_heading(package, version))
    m = re.search(rf"## \[{heading}\].*?\n(.*?)(?=\n## \[|\Z)", text, re.S)
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
    cut_changelog(new_versions)
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
            f"## {name} v{version}\n\n{changelog_section(name, version)}"
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
        notes = changelog_section(name, version)
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
