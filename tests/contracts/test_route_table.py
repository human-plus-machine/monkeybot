"""T1-GW-05 route snapshot and T1-GW-06 APIError envelope.

The live app is the combined gateway (SSE routes plus realtime). Appendix D
lists those realtime routes, so the SSE-only app is not the snapshot source.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from starlette.requests import Request
from starlette.routing import WebSocketRoute

from monkeybot.gateway.realtime.app import create_realtime_app
from monkeybot.gateway.sse.models import APIError

_REPO = Path(__file__).resolve().parents[2]
_APPENDIX = _REPO / "regression" / "plan" / "reference" / "appendices.md"
_GOLDEN = Path(__file__).resolve().parent / "goldens" / "routes.txt"
_GATEWAY = _REPO / "src" / "monkeybot" / "gateway"
_ROUTE = re.compile(r"`([A-Z]+)\s+(\S+)`")
# APIError(..., exc.code) forwards these constructors' code strings.
_FORWARDED: dict[str, tuple[str, str, int]] = {
    "gateway/sse/routes.py": (
        "src/monkeybot/gateway/sse/reply_body.py",
        "ReplyBodyError",
        0,
    ),
    "gateway/sse/history_routes.py": (
        "src/monkeybot/core/runtime/history_rewrite.py",
        "HistoryRewriteError",
        1,
    ),
}


def _expand(method: str, path: str) -> set[str]:
    """Expand Appendix D's ``pause|resume|stop`` alternation into three paths."""
    head, sep, tail = path.rpartition("/")
    if "|" not in tail:
        if "|" in path:
            raise AssertionError(f"Appendix D '|' is not in the last segment: {path}")
        return {f"{method} {path}"}
    if not sep:
        raise AssertionError(f"Appendix D alternation has no slash: {path}")
    parts = [part for part in tail.split("|") if part]
    if len(parts) < 2:
        raise AssertionError(f"Appendix D alternation is empty: {path}")
    return {f"{method} {head}/{part}" for part in parts}


def _appendix_d() -> set[str]:
    text = _APPENDIX.read_text(encoding="utf-8")
    marker = "## Appendix D"
    start = text.find(marker)
    if start < 0:
        raise AssertionError("Appendix D is missing from appendices.md")
    rest = text[start:]
    nxt = rest.find("\n## ", len(marker))
    section = rest if nxt < 0 else rest[:nxt]
    found: set[str] = set()
    for method, path in _ROUTE.findall(section):
        found.update(_expand(method, path))
    if not found:
        raise AssertionError("Appendix D lists no routes")
    return found


def _live_routes() -> list[str]:
    """method+path lines from ``app.routes``, one per method."""
    found: set[str] = set()
    for route in create_realtime_app().routes:
        path = getattr(route, "path", None)
        if not isinstance(path, str):
            raise AssertionError(f"route has no path: {type(route).__name__}")
        methods = getattr(route, "methods", None)
        if methods:
            for method in methods:
                found.add(f"{method} {path}")
            continue
        if isinstance(route, WebSocketRoute):
            found.add(f"WS {path}")
            continue
        raise AssertionError(f"route has no methods: {type(route).__name__} {path}")
    return sorted(found)


def _commit(text: str, *, update: bool) -> None:
    if update:
        _GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        _GOLDEN.write_text(text, encoding="utf-8")
        return
    assert _GOLDEN.is_file(), "missing routes.txt; re-run with --update-golden"
    assert _GOLDEN.read_text(encoding="utf-8") == text


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _literal(node: ast.expr | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _code_arg(node: ast.Call) -> ast.expr | None:
    if len(node.args) >= 2:
        return node.args[1]
    for keyword in node.keywords:
        if keyword.arg == "code":
            return keyword.value
    return None


def _is_attr_code(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "code"


def _constructor_codes(path: Path, class_name: str, index: int) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    codes: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or _call_name(node) != class_name:
            continue
        expr = node.args[index] if len(node.args) > index else None
        if expr is None:
            for keyword in node.keywords:
                if keyword.arg == "code":
                    expr = keyword.value
        literal = _literal(expr)
        if literal is None:
            raise AssertionError(f"{path}:{node.lineno} {class_name} code is not a string")
        codes.add(literal)
    if not codes:
        raise AssertionError(f"no {class_name} codes in {path}")
    return codes


def _api_error_codes() -> set[str]:
    """Every code string an ``APIError`` in the gateway can carry."""
    codes: set[str] = set()
    for path in sorted(_GATEWAY.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        relative = path.relative_to(_REPO / "src" / "monkeybot").as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) != "APIError":
                continue
            expr = _code_arg(node)
            if expr is None:
                raise AssertionError(f"{path}:{node.lineno} APIError has no code")
            literal = _literal(expr)
            if literal is not None:
                codes.add(literal)
                continue
            if not _is_attr_code(expr):
                raise AssertionError(f"{path}:{node.lineno} APIError code is not a string")
            forwarded = _FORWARDED.get(relative)
            if forwarded is None:
                raise AssertionError(f"{path}:{node.lineno} forwards .code with no known source")
            source, class_name, index = forwarded
            codes.update(_constructor_codes(_REPO / source, class_name, index))
    if not codes:
        raise AssertionError("no APIError codes in gateway routes")
    return codes


def _http_request() -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"spec_version": "2.3", "version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 0),
            "server": ("test", 80),
        }
    )


@pytest.mark.case("T1-GW-05")
def test_route_table_matches_committed_list(request: pytest.FixtureRequest) -> None:
    """``app.routes`` method+path set equals the committed list and Appendix D."""
    lines = _live_routes()
    text = "\n".join(lines) + "\n"
    _commit(text, update=bool(request.config.getoption("update_golden")))
    committed = {
        line.strip() for line in _GOLDEN.read_text(encoding="utf-8").splitlines() if line.strip()
    }
    assert set(lines) == committed
    appendix = _appendix_d()
    missing = sorted(appendix - set(lines))
    extra = sorted(set(lines) - appendix)
    assert set(lines) == appendix, (
        "app.routes disagrees with Appendix D\n"
        f"missing ({len(missing)}): {missing}\n"
        f"extra ({len(extra)}): {extra}"
    )


@pytest.mark.case("T1-GW-06")
async def test_api_error_codes_use_error_envelope() -> None:
    """Every APIError code raised from gateway routes has the error envelope."""
    handler = create_realtime_app().exception_handlers[APIError]
    request = _http_request()
    codes = _api_error_codes()
    assert codes
    for code in sorted(codes):
        message = f"message for {code}"
        request_id = f"rid-{code}"
        response = await handler(request, APIError(400, code, message, request_id))
        body = json.loads(response.body)
        assert body == {"error": {"code": code, "message": message, "request_id": request_id}}, (
            f"{code} envelope is {body}"
        )
