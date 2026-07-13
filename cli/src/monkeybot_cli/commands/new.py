"""monkeybot new — scaffold a bot workspace."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from monkeybot_cli.extras_catalog import (
    FEATURE_CHOICES,
    PROVIDER_CHOICES,
    ExtraChoice,
    additional_provider_extra_choices,
    normalize_extra_token,
)
from monkeybot_cli.scaffold import run_new

_DEFAULT_PROVIDER = "gemini"
_DEFAULT_MODEL = "gemini-3-flash"


def _prompt_line(prompt: str) -> str:
    try:
        return input(prompt)
    except EOFError:
        return ""


def _prompt_single(choices: tuple[ExtraChoice, ...], *, title: str, default_key: str) -> str:
    """Numbered single-select; Enter keeps default_key."""
    print(title)
    default_idx = 1
    for i, choice in enumerate(choices, start=1):
        marker = " (default)" if choice.key == default_key else ""
        if choice.key == default_key:
            default_idx = i
        print(f"  {i}) {choice.key} — {choice.label}{marker}")
    raw = _prompt_line(f"Choose provider [{default_idx}]: ").strip()
    if not raw:
        return default_key
    if raw.isdigit():
        idx = int(raw)
        if 1 <= idx <= len(choices):
            return choices[idx - 1].key
        print(f"  invalid number; using {default_key}", file=sys.stderr)
        return default_key
    # Allow typing the key / alias directly
    for choice in choices:
        if raw.lower() == choice.key.lower():
            return choice.key
    print(f"  unknown provider {raw!r}; using {default_key}", file=sys.stderr)
    return default_key


def _prompt_multi(choices: tuple[ExtraChoice, ...], *, title: str) -> list[str]:
    """Numbered multi-select (comma-separated). Enter = none."""
    if not choices:
        return []
    print(title)
    print("  (comma-separated numbers or names; Enter for none)")
    for i, choice in enumerate(choices, start=1):
        print(f"  {i}) {choice.key} — {choice.label}")
    raw = _prompt_line("Select: ").strip()
    if not raw:
        return []
    selected: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        token = part.strip()
        if not token:
            continue
        key: str | None = None
        if token.isdigit():
            idx = int(token)
            if 1 <= idx <= len(choices):
                key = choices[idx - 1].key
            else:
                print(f"  skipping invalid number {token}", file=sys.stderr)
                continue
        else:
            for choice in choices:
                if token.lower() == choice.key.lower():
                    key = choice.key
                    break
            if key is None:
                normalized = normalize_extra_token(token)
                if normalized is not None:
                    key = normalized
                else:
                    print(f"  skipping unknown option {token!r}", file=sys.stderr)
                    continue
        if key not in seen:
            seen.add(key)
            selected.append(key)
    return selected


def _resolve_extras_from_args(with_args: list[str] | None) -> tuple[list[str], list[str]]:
    """Return (extras, unknown_tokens)."""
    extras: list[str] = []
    unknown: list[str] = []
    seen: set[str] = set()
    for raw in with_args or ():
        for part in raw.split(","):
            token = part.strip()
            if not token:
                continue
            normalized = normalize_extra_token(token)
            if normalized is None:
                # fake maps to None — treat as skip only if token is fake
                if token.lower() in {"fake"}:
                    continue
                unknown.append(token)
                continue
            if normalized not in seen:
                seen.add(normalized)
                extras.append(normalized)
    return extras, unknown


def run_new_command(args: argparse.Namespace) -> int:
    dest = Path(args.dest).expanduser().resolve()
    if not dest.exists():
        dest.mkdir(parents=True, exist_ok=True)
    if not dest.is_dir():
        print(f"error: --dest is not a directory: {dest}", file=sys.stderr)
        return 2

    provider = args.provider
    model = args.model
    extras, unknown = _resolve_extras_from_args(getattr(args, "with_extras", None))
    if unknown:
        print(
            f"error: unknown --with value(s): {', '.join(unknown)}",
            file=sys.stderr,
        )
        return 2

    if not args.yes:
        if provider is None:
            provider = _prompt_single(
                PROVIDER_CHOICES,
                title="Model provider:",
                default_key=_DEFAULT_PROVIDER,
            )
        if model is None:
            model = (
                _prompt_line(f"Model name [{_DEFAULT_MODEL}]: ").strip() or _DEFAULT_MODEL
            )
        # Only prompt for optional deps when --with was not already given.
        if not extras:
            more_providers = additional_provider_extra_choices(provider)
            extras.extend(
                _prompt_multi(
                    more_providers,
                    title="Additional providers to install (optional):",
                )
            )
            extras.extend(
                _prompt_multi(
                    FEATURE_CHOICES,
                    title="Optional features to install:",
                )
            )

    report = run_new(
        dest=dest,
        force=args.force,
        provider=provider,
        model=model,
        extras=extras or None,
    )
    print(f"monkeybot scaffold under {dest}:")
    print("\n".join(report))
    print()
    print("Next:")
    print(f"  cd {dest}")
    print("  uv sync")
    print("  cp .env.example .env   # then add provider keys")
    print("  monkeybot doctor")
    print("  monkeybot chat")
    return 0


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    p = subparsers.add_parser(
        "new",
        help="Scaffold monkeybot_config/, workspace dirs, and agent pyproject.toml",
    )
    p.add_argument("--dest", type=Path, default=Path.cwd(), help="Root directory for the bot")
    p.add_argument("--provider", help="model.provider value for monkeybot.yaml")
    p.add_argument("--model", help="model.name value for monkeybot.yaml")
    p.add_argument(
        "--with",
        dest="with_extras",
        action="append",
        default=[],
        metavar="EXTRA",
        help=(
            "Optional dependency to include in agent pyproject.toml "
            "(repeatable or comma-separated). Examples: postgres, sandbox, "
            "observability, openai, bedrock, claude"
        ),
    )
    p.add_argument("--force", action="store_true", help="Overwrite existing scaffold files")
    p.add_argument("--yes", "-y", action="store_true", help="Skip interactive prompts")
    p.set_defaults(func=run_new_command)
