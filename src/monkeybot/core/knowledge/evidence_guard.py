"""POST-turn evidence-path verification for ``Evidence:`` citations.

Scans assistant text for ``Evidence: <path>`` lines and injects a correction
notice on the next model step (``PRE_TURN`` / ``PRE_TOOL``) when:

* a cited path does not exist under the workspace root (fabrication), or
* a cited path exists but was never opened with ``read_file`` (or located via
  ``glob``) this session — i.e. the answer came from a ``search`` snippet
  alone, which is the dominant residual miss mode (F22).
"""

from __future__ import annotations

import json
import logging
import re
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

from monkeybot.core.hooks import HookEvent, HookManager, HookPayload

logger = logging.getLogger(__name__)

# Lines like: Evidence: path  |  **Evidence:** `path`  |  - Evidence: a; b
_EVIDENCE_LINE_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?(?:\*\*)?Evidence(?:\*\*)?\s*:\s*(.+?)\s*$"
)
# Path tokens: backtick-wrapped or bare (stop at ; , or whitespace-run before next)
_PATH_TOKEN_RE = re.compile(r"`([^`]+)`|([^\s;`,]+)")
# Strip optional :L12 / :12-34 / :lines suffixes from path citations.
_LINE_SUFFIX_RE = re.compile(r":(?:L?\d+(?:\s*[-–—]\s*L?\d+)?|lines)\s*$", re.IGNORECASE)
# Bare tokens that are line refs, not paths (e.g. `L12–14` after a backticked path).
_LINE_ONLY_RE = re.compile(r"^L?\d+(?:\s*[-–—]\s*L?\d+)?$", re.IGNORECASE)
# Trailing sentence punctuation that is not part of a path.
_TRAILING_PUNCT_RE = re.compile(r"[.,;:!?)\]}\"']+$")

_CORRECTION_HEADING = "## Evidence path correction"
_VERIFICATION_HEADING = "## Evidence verification required"
_THREAD_STATE_CAP = 256


def extract_evidence_paths(text: str) -> list[str]:
    """Return unique workspace-relative paths cited on ``Evidence:`` lines."""
    found: list[str] = []
    seen: set[str] = set()
    for match in _EVIDENCE_LINE_RE.finditer(text):
        raw = match.group(1).strip()
        if not raw or raw.lower() in {"unknown", "(unknown)", "n/a", "none"}:
            continue
        for token_match in _PATH_TOKEN_RE.finditer(raw):
            token = (token_match.group(1) or token_match.group(2) or "").strip()
            if not token:
                continue
            path = _LINE_SUFFIX_RE.sub("", token).strip().strip("`\"'")
            path = _TRAILING_PUNCT_RE.sub("", path).strip()
            if not path or path.lower() in {"unknown", "or", "and", "similar"}:
                continue
            # Skip prose fragments / line-only refs that are not path-like
            if " " in path or path.startswith("(") or _LINE_ONLY_RE.match(path):
                continue
            if path not in seen:
                seen.add(path)
                found.append(path)
    return found


def missing_evidence_paths(text: str, workspace_root: Path) -> list[str]:
    """Paths from ``Evidence:`` lines that do not exist under ``workspace_root``."""
    root = workspace_root.resolve()
    missing: list[str] = []
    for rel in extract_evidence_paths(text):
        try:
            candidate = (root / rel).resolve()
        except (OSError, ValueError):
            missing.append(rel)
            continue
        if not candidate.is_relative_to(root):
            missing.append(rel)
            continue
        if not candidate.exists():
            missing.append(rel)
    return missing


def format_evidence_correction(missing: list[str]) -> str:
    """System-injection text telling the model to re-verify cited paths."""
    bullets = "\n".join(f"- `{p}`" for p in missing)
    return (
        f"{_CORRECTION_HEADING}\n"
        "The previous assistant message cited Evidence paths that do **not** "
        "exist in the workspace:\n"
        f"{bullets}\n"
        "Do not reuse those paths. Locate the real files with `glob` / `search` / "
        "`read_file`, then restate answers with verified Evidence lines "
        "(or `Evidence: unknown`)."
    )


def format_verification_notice(unread: list[str]) -> str:
    """System-injection text telling the model to read snippet-only citations."""
    bullets = "\n".join(f"- `{p}`" for p in unread)
    return (
        f"{_VERIFICATION_HEADING}\n"
        "The previous assistant message cited Evidence paths that were never "
        "opened with `read_file` this session:\n"
        f"{bullets}\n"
        "A `search` snippet is **not** verification — snippets truncate and can "
        "mislead. `read_file` each file above, check the answer against the "
        "actual source lines, and restate any answer that changes."
    )


def _norm_path(path: str) -> str:
    p = path.strip().strip("`\"'")
    while p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")


@dataclass
class _ThreadEvidenceState:
    pending: str | None = None
    confirmed_paths: set[str] = field(default_factory=set)
    verify_notified: set[str] = field(default_factory=set)


class EvidencePathGuard:
    """Scan assistant Evidence citations; inject corrections on the next step.

    Tracks ``read_file`` / ``glob`` confirmations so citations sourced only
    from ``search`` snippets trigger a one-shot read-and-verify notice (F22).

    State is scoped per ``thread_id`` because one gateway process shares a
    single guard instance across concurrent SSE sessions.
    """

    def __init__(self, *, thread_cap: int = _THREAD_STATE_CAP) -> None:
        self._thread_cap = max(1, thread_cap)
        self._by_thread: OrderedDict[str, _ThreadEvidenceState] = OrderedDict()

    def register(self, manager: HookManager) -> None:
        manager.register(HookEvent.AFTER_PROVIDER_RESPONSE, self.on_after_provider)
        manager.register(HookEvent.POST_TOOL, self.on_post_tool)
        manager.register(HookEvent.PRE_TURN, self.on_pre_turn)
        manager.register(HookEvent.PRE_TOOL, self.on_pre_tool)

    def _state(self, thread_id: str) -> _ThreadEvidenceState:
        existing = self._by_thread.get(thread_id)
        if existing is not None:
            self._by_thread.move_to_end(thread_id)
            return existing
        state = _ThreadEvidenceState()
        self._by_thread[thread_id] = state
        while len(self._by_thread) > self._thread_cap:
            self._by_thread.popitem(last=False)
        return state

    async def on_post_tool(self, payload: HookPayload) -> None:
        if payload.tool_error:
            return
        state = self._state(payload.thread_id)
        args = payload.tool_args or {}
        if payload.tool_name == "read_file":
            path = args.get("path")
            if isinstance(path, str) and path.strip():
                state.confirmed_paths.add(_norm_path(path))
        elif payload.tool_name == "glob":
            # glob proves existence — the right verification for binary assets
            # (images / PDFs) that read_file cannot open.
            for path in _paths_from_glob_result(payload.tool_result):
                state.confirmed_paths.add(_norm_path(path))
        elif payload.tool_name in ("write_file", "replace_in_file"):
            # Writing a file confirms its own existence/content …
            path = args.get("path")
            if isinstance(path, str) and path.strip():
                state.confirmed_paths.add(_norm_path(path))
            # … but Evidence citations *inside* the written content (e.g. an
            # answers.md deliverable) need the same scan as chat text —
            # otherwise externalized answers bypass the guard entirely.
            content = args.get("content") or args.get("new_string")
            root = payload.ctx.workspace_root
            if isinstance(content, str) and content.strip() and root is not None:
                self._scan_citations(state, content, Path(root))

    async def on_after_provider(self, payload: HookPayload) -> None:
        text = (payload.assistant_text or "").strip()
        root = payload.ctx.workspace_root
        if not text or root is None:
            return
        self._scan_citations(self._state(payload.thread_id), text, Path(root))

    def _scan_citations(
        self, state: _ThreadEvidenceState, text: str, root: Path
    ) -> None:
        """Queue missing / unread notices for ``Evidence:`` citations in ``text``."""
        cited = extract_evidence_paths(text)
        if not cited:
            return
        missing = missing_evidence_paths(text, root)
        missing_set = set(missing)
        unread = [
            p
            for p in cited
            if p not in missing_set
            and not self._is_confirmed(state, p)
            and p not in state.verify_notified
        ]
        notices: list[str] = []
        if missing:
            notices.append(format_evidence_correction(missing))
        if unread:
            state.verify_notified.update(unread)
            notices.append(format_verification_notice(unread))
        if not notices:
            return
        combined = "\n\n".join(notices)
        state.pending = f"{state.pending}\n\n{combined}" if state.pending else combined
        logger.info(
            "evidence_guard: queued correction (missing=%d unread=%d)",
            len(missing),
            len(unread),
        )

    def _is_confirmed(self, state: _ThreadEvidenceState, cited: str) -> bool:
        c = _norm_path(cited)
        if c in state.confirmed_paths:
            return True
        # Tolerate an extra leading directory on the citation (e.g. monorepo
        # root: cited ``auriga-web/src/x.ts`` after reading ``src/x.ts``).
        # Only allow citation-longer matches so a read of
        # ``packages/api/src/index.ts`` cannot confirm a bare ``src/index.ts``.
        return any("/" in r and c.endswith("/" + r) for r in state.confirmed_paths)

    async def on_pre_turn(self, payload: HookPayload) -> None:
        self._inject(payload)

    async def on_pre_tool(self, payload: HookPayload) -> None:
        self._inject(payload)

    def _inject(self, payload: HookPayload) -> None:
        state = self._state(payload.thread_id)
        if not state.pending:
            return
        notice = state.pending
        state.pending = None
        if payload.inject_text:
            payload.inject_text = f"{payload.inject_text.rstrip()}\n\n{notice}"
        else:
            payload.inject_text = notice


def _paths_from_glob_result(result: str | None) -> list[str]:
    """Extract the ``paths`` list from a glob tool result payload."""
    if not result:
        return []
    try:
        data = json.loads(result)
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, dict):
        return []
    paths = data.get("paths")
    if not isinstance(paths, list):
        return []
    return [p for p in paths if isinstance(p, str) and p.strip()]


__all__ = [
    "EvidencePathGuard",
    "extract_evidence_paths",
    "format_evidence_correction",
    "format_verification_notice",
    "missing_evidence_paths",
]
