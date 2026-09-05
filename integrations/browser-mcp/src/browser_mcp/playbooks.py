"""Playbook storage under a single directory; host slugs only (no path traversal).

Markdown notes stay free-form. Optional ```playbook YAML fences are executable
flows (Phase 6). Treat fence contents as untrusted: no shell, no file paths,
no JS steps.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from browser_mcp import actions

logger = logging.getLogger(__name__)

SECRET_PARAM_NAMES = frozenset({"password", "secret", "token"})
_FENCE_RE = re.compile(r"^```playbook[^\n]*\n(.*?)(?:^```[ \t]*$)", re.MULTILINE | re.DOTALL)
_FENCE_OPEN_RE = re.compile(r"^```playbook\b", re.MULTILINE)
_PLACEHOLDER_RE = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")
_PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PlaybookError(ValueError):
    """Invalid host, playbook path, or executable flow."""


@dataclass
class Flow:
    name: str
    params: list[str] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    expect: dict[str, Any] = field(default_factory=dict)
    host: str | None = None


def playbooks_dir() -> Path:
    """Resolve playbooks root from env or the agent's writable workspace."""
    raw = os.environ.get("BROWSER_MCP_PLAYBOOKS_DIR", "").strip()
    if raw:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        else:
            p = p.resolve()
        return p
    workspace = os.environ.get("MONKEYBOT_WORKSPACE_ROOT") or os.environ.get("WORKSPACE_ROOT")
    root = Path(workspace).expanduser() if workspace else Path.cwd() / "workspace"
    return (root / "browser" / "playbooks").resolve()


def host_slug(host_or_url: str) -> str:
    """Normalize a URL or hostname to a safe playbook filename stem."""
    s = (host_or_url or "").strip()
    if not s:
        raise PlaybookError("host is required")
    if "://" in s:
        host = urlparse(s).hostname or ""
    else:
        host = s.split("/")[0]
    host = host.removeprefix("www.").strip().lower()
    slug = re.sub(r"[^a-z0-9.-]", "_", host)
    slug = slug.strip("._")
    if not slug or slug in (".", "..") or ".." in slug:
        raise PlaybookError(f"invalid host: {host_or_url!r}")
    return slug


def _assert_under_root(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    root_resolved = root.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise PlaybookError("playbook path escapes playbooks directory") from exc
    return resolved


def playbook_path(host_or_url: str) -> Path:
    """Return playbooks/<slug>.md, guaranteed under playbooks_dir()."""
    slug = host_slug(host_or_url)
    root = playbooks_dir()
    return _assert_under_root(root / f"{slug}.md", root)


def list_playbook_names(host_or_url: str | None = None) -> list[str]:
    """List playbook filenames (*.md), optionally filtered by host prefix."""
    root = playbooks_dir()
    if not root.is_dir():
        return []
    if host_or_url:
        prefix = host_slug(host_or_url)
        return sorted(p.name for p in root.glob(f"{prefix}*.md") if p.is_file())
    return sorted(p.name for p in root.glob("*.md") if p.is_file())


def read_playbook(host_or_url: str) -> str:
    path = playbook_path(host_or_url)
    if not path.is_file():
        raise PlaybookError(f"no playbook for {host_or_url!r}")
    return path.read_text(encoding="utf-8")


def write_playbook(host_or_url: str, content: str, *, append: bool = False) -> dict[str, str | bool]:
    root = playbooks_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = playbook_path(host_or_url)
    text = content if content is not None else ""
    if append and path.is_file():
        existing = path.read_text(encoding="utf-8").rstrip()
        text = f"{existing}\n\n---\n\n{text}" if existing else text
    parse_flows(text, host=host_slug(host_or_url))
    path.write_text(text, encoding="utf-8")
    return {"ok": True, "path": str(path), "host": host_slug(host_or_url)}


def _fence_bodies(markdown: str) -> list[str]:
    bodies = [m.group(1) for m in _FENCE_RE.finditer(markdown or "")]
    n_open = len(_FENCE_OPEN_RE.findall(markdown or ""))
    if n_open != len(bodies):
        raise PlaybookError("unclosed or malformed playbook fence")
    return bodies


def _secret_param(name: str) -> bool:
    return name.strip().lower() in SECRET_PARAM_NAMES


def _parse_flow_mapping(raw: Any, *, host: str | None, index: int) -> Flow:
    if raw is None:
        raise PlaybookError(f"playbook fence {index + 1} is empty")
    if not isinstance(raw, dict):
        raise PlaybookError(f"playbook fence {index + 1} must be a YAML mapping")
    name = raw.get("name")
    if not isinstance(name, str) or not name.strip():
        raise PlaybookError(f"playbook fence {index + 1} requires string name")
    name = name.strip()
    params_raw = raw.get("params") or []
    if not isinstance(params_raw, list) or not all(isinstance(p, str) for p in params_raw):
        raise PlaybookError(f"flow {name!r} params must be a list of strings")
    params: list[str] = []
    for param in params_raw:
        if not _PARAM_NAME_RE.fullmatch(param):
            raise PlaybookError(f"flow {name!r} has invalid param name {param!r}")
        if _secret_param(param):
            raise PlaybookError(
                f"flow {name!r} param {param!r} looks like a secret; "
                "use {{do: login, expected_origin: ...}} instead"
            )
        params.append(param)
    steps = raw.get("steps")
    if not isinstance(steps, list) or not steps:
        raise PlaybookError(f"flow {name!r} requires a non-empty steps list")
    normalized: list[dict[str, Any]] = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise PlaybookError(f"flow {name!r} step {i} must be an object")
        kind = step.get("do")
        if not isinstance(kind, str) or kind not in actions.STEP_KINDS:
            known = ", ".join(sorted(actions.STEP_KINDS))
            raise PlaybookError(
                f"flow {name!r} unknown do {kind!r}; expected {known}"
            )
        normalized.append(dict(step))
    validated = actions.validate_steps(normalized)
    if isinstance(validated, dict):
        raise PlaybookError(
            f"flow {name!r} {validated.get('error') or 'invalid steps'}"
        )
    expect = raw.get("expect") or {}
    if expect is None:
        expect = {}
    if not isinstance(expect, dict):
        raise PlaybookError(f"flow {name!r} expect must be an object")
    unknown_expect = set(expect) - {"url_contains", "selector", "text"}
    if unknown_expect:
        raise PlaybookError(
            f"flow {name!r} unknown expect keys: {', '.join(sorted(unknown_expect))}"
        )
    for key in ("url_contains", "selector", "text"):
        if key in expect and not isinstance(expect[key], str):
            raise PlaybookError(f"flow {name!r} expect.{key} must be a string")
    return Flow(name=name, params=params, steps=list(validated), expect=dict(expect), host=host)


def parse_flows(markdown: str, *, host: str | None = None) -> list[Flow]:
    """Parse ```playbook YAML fences. Notes-only markdown returns []."""
    bodies = _fence_bodies(markdown or "")
    flows: list[Flow] = []
    seen: set[str] = set()
    for i, body in enumerate(bodies):
        try:
            loaded = yaml.safe_load(body)
        except yaml.YAMLError as exc:
            raise PlaybookError(f"playbook fence {i + 1} is not valid YAML: {exc}") from exc
        flow = _parse_flow_mapping(loaded, host=host, index=i)
        if flow.name in seen:
            raise PlaybookError(f"duplicate flow name {flow.name!r}")
        seen.add(flow.name)
        flows.append(flow)
    return flows


def render_flow(flow: Flow) -> str:
    payload: dict[str, Any] = {
        "name": flow.name,
        "params": list(flow.params),
        "steps": list(flow.steps),
    }
    if flow.expect:
        payload["expect"] = dict(flow.expect)
    dumped = yaml.safe_dump(payload, sort_keys=False).rstrip()
    return f"```playbook\n{dumped}\n```\n"


def list_flows(host_or_url: str | None = None) -> list[dict[str, Any]]:
    """Return ``{host, name, params}`` for parseable flows. Skip broken files."""
    root = playbooks_dir()
    if not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for name in list_playbook_names(host_or_url):
        slug = name[:-3] if name.endswith(".md") else name
        path = root / name
        try:
            text = path.read_text(encoding="utf-8")
            for flow in parse_flows(text, host=slug):
                out.append({"host": slug, "name": flow.name, "params": list(flow.params)})
        except (PlaybookError, OSError) as exc:
            logger.warning("skipping unreadable playbook %s: %s", name, exc)
            continue
    return out


def load_flow(host_or_url: str, name: str) -> Flow:
    slug = host_slug(host_or_url)
    text = read_playbook(host_or_url)
    flows = parse_flows(text, host=slug)
    for flow in flows:
        if flow.name == name:
            return flow
    known = ", ".join(flow.name for flow in flows) or "(none)"
    raise PlaybookError(f"unknown flow {name!r}; known: {known}")


def _subst_value(value: Any, *, declared: set[str], params: dict[str, str]) -> Any:
    if isinstance(value, str):
        def _replace(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in declared:
                raise PlaybookError(f"unknown param {key!r}")
            return params[key]

        return _PLACEHOLDER_RE.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _subst_value(v, declared=declared, params=params) for k, v in value.items()}
    if isinstance(value, list):
        return [_subst_value(v, declared=declared, params=params) for v in value]
    return value


def substitute_params(flow: Flow, params: dict[str, Any] | None) -> list[dict[str, Any]]:
    incoming = params or {}
    if not isinstance(incoming, dict):
        raise PlaybookError("params must be an object")
    coerced: dict[str, str] = {}
    for key, value in incoming.items():
        name = str(key)
        if not isinstance(value, str):
            raise PlaybookError(f"param {name!r} must be a string")
        coerced[name] = value
    declared = set(flow.params)
    extra = sorted(set(coerced) - declared)
    if extra:
        raise PlaybookError(f"unknown params: {', '.join(extra)}")
    missing = sorted(declared - set(coerced))
    if missing:
        raise PlaybookError(f"missing params: {', '.join(missing)}")
    substituted = _subst_value(flow.steps, declared=declared, params=coerced)
    if not isinstance(substituted, list):
        raise PlaybookError("steps must be a list of objects")
    validated = actions.validate_steps(substituted)
    if isinstance(validated, dict):
        raise PlaybookError(str(validated.get("error") or "invalid steps after substitution"))
    return validated


def check_expect(handle: Any, expect: dict[str, Any] | None) -> str | None:
    """Return an error string when ``expect`` is not met, else None."""
    if not expect:
        return None
    if not isinstance(expect, dict):
        return "expect failed: expect must be an object"
    if "url_contains" in expect:
        needle = str(expect["url_contains"])
        info = handle.page_info() if callable(getattr(handle, "page_info", None)) else {}
        url = str((info or {}).get("url") or "")
        if needle not in url:
            return f"expect failed: url_contains {needle!r} (url is {url!r})"
    if "selector" in expect:
        selector = str(expect["selector"])
        expr = f"!!document.querySelector({json.dumps(selector)})"
        found = handle.evaluate(expr) if callable(getattr(handle, "evaluate", None)) else False
        if not found:
            return f"expect failed: selector {selector!r} not found"
    if "text" in expect:
        needle = str(expect["text"])
        text = ""
        if callable(getattr(handle, "readable_text", None)):
            text = str(handle.readable_text() or "")
        if needle not in text:
            return f"expect failed: text {needle!r} not found"
    return None
