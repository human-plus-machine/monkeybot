"""monkeybot loop — manage prompt-first scheduled loops."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

from monkeybot_cli.config_resolve import load_agent_dotenv, resolve_agent_root, resolve_config


def _gateway_url(args: argparse.Namespace) -> str:
    if args.url:
        return args.url.rstrip("/")
    port = args.port or 8080
    return f"http://127.0.0.1:{port}"


def _read_prompt(args: argparse.Namespace) -> str:
    if args.prompt_file:
        return Path(args.prompt_file).read_text(encoding="utf-8").strip()
    if args.prompt:
        return args.prompt.strip()
    if not sys.stdin.isatty():
        return sys.stdin.read().strip()
    raise SystemExit("Provide --prompt, --prompt-file, or pipe the plan on stdin.")


def _post_json(url: str, path: str, payload: dict[str, object]) -> dict[str, object]:
    with httpx.Client(timeout=60.0) as client:
        resp = client.post(f"{url}{path}", json=payload)
        if resp.status_code >= 400:
            raise SystemExit(f"{resp.status_code} {resp.text}")
        data = resp.json()
        if not isinstance(data, dict):
            raise SystemExit("unexpected response shape")
        return data


def _get_json(url: str, path: str) -> dict[str, object]:
    with httpx.Client(timeout=60.0) as client:
        resp = client.get(f"{url}{path}")
        if resp.status_code >= 400:
            raise SystemExit(f"{resp.status_code} {resp.text}")
        data = resp.json()
        if not isinstance(data, dict):
            raise SystemExit("unexpected response shape")
        return data


def run_loop_run(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else None
    config_path = resolve_config(args.config, cwd=cwd)
    load_agent_dotenv(cwd=cwd, config_path=config_path)
    url = _gateway_url(args)
    prompt = _read_prompt(args)
    payload: dict[str, object] = {
        "prompt": prompt,
        "interval": args.interval,
        "session_id": args.session_id,
        "skip_if_busy": not args.queue_if_busy,
    }
    if args.loop_id:
        payload["loop_id"] = args.loop_id
    if args.max_ticks is not None:
        payload["max_ticks"] = args.max_ticks
    if args.max_runtime:
        payload["max_runtime"] = args.max_runtime
    if args.unbounded:
        payload["unbounded"] = True
    if not args.unbounded and args.max_ticks is None and not args.max_runtime:
        raise SystemExit(
            "Provide --max-ticks, --max-runtime, or --unbounded to set loop stop guards"
        )
    data = _post_json(url, "/scheduler/loops", payload)
    if args.json:
        print(json.dumps(data, indent=2))
    else:
        loop = data.get("loop", {})
        print(f"loop started: {loop.get('loop_id')} (session={loop.get('session_id')})")
    return 0


def run_loop_status(args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else None
    config_path = resolve_config(args.config, cwd=cwd)
    load_agent_dotenv(cwd=cwd, config_path=config_path)
    url = _gateway_url(args)
    if args.loop_id:
        data = _get_json(url, f"/scheduler/loops/{args.loop_id}")
    else:
        data = _get_json(url, "/scheduler/loops")
    print(json.dumps(data, indent=2) if args.json else json.dumps(data, indent=2))
    return 0


def _loop_action(action: str, args: argparse.Namespace) -> int:
    cwd = Path(args.cwd).expanduser().resolve() if args.cwd else None
    config_path = resolve_config(args.config, cwd=cwd)
    load_agent_dotenv(cwd=cwd, config_path=config_path)
    if not args.loop_id:
        raise SystemExit(f"loop {action} requires --id")
    url = _gateway_url(args)
    data = _post_json(url, f"/scheduler/loops/{args.loop_id}/{action}", {})
    print(json.dumps(data, indent=2) if args.json else json.dumps(data, indent=2))
    return 0


def register(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    loop = subparsers.add_parser("loop", help="Manage prompt-first scheduled agent loops")
    loop.add_argument("--cwd", help="Agent project root")
    loop.add_argument("--config", help="Path to monkeybot.yaml")
    loop.add_argument("--url", help="Gateway base URL")
    loop.add_argument("--port", type=int, help="Gateway port when --url omitted")
    loop.add_argument("--json", action="store_true", help="JSON output")

    sub = loop.add_subparsers(dest="loop_command", required=True)

    run = sub.add_parser("run", help="Register a scheduled loop from a prompt")
    run.add_argument("--interval", required=True, help="Tick interval, e.g. 20s, 5m, 1h")
    run.add_argument("--prompt", help="Loop plan / tick instructions")
    run.add_argument("--prompt-file", help="Read loop plan from a file")
    run.add_argument("--session-id", default="loop-main")
    run.add_argument("--loop-id", help="Optional stable loop id")
    run.add_argument("--max-ticks", type=int)
    run.add_argument("--max-runtime", help="Hard wall-clock limit, e.g. 2h")
    run.add_argument(
        "--unbounded",
        action="store_true",
        help="Run without max_ticks/max_runtime guards (explicit opt-in)",
    )
    run.add_argument(
        "--queue-if-busy",
        action="store_true",
        help="Do not skip ticks when the target session is busy",
    )
    run.set_defaults(func=run_loop_run)

    status = sub.add_parser("status", help="Show one loop or list all loops")
    status.add_argument("--id", dest="loop_id")
    status.set_defaults(func=run_loop_status)

    pause = sub.add_parser("pause", help="Pause a loop")
    pause.add_argument("--id", dest="loop_id", required=True)
    pause.set_defaults(func=lambda a: _loop_action("pause", a))

    resume = sub.add_parser("resume", help="Resume a paused loop")
    resume.add_argument("--id", dest="loop_id", required=True)
    resume.set_defaults(func=lambda a: _loop_action("resume", a))

    stop = sub.add_parser("stop", help="Stop a loop")
    stop.add_argument("--id", dest="loop_id", required=True)
    stop.set_defaults(func=lambda a: _loop_action("stop", a))
