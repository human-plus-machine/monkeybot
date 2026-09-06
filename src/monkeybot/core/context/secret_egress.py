"""Outbound-to-provider secret/canary scanning (credential broker phase 5.3).

The threat this closes: an agent that captured a secret via `browser_login`
(or dumped its own environment, catching the phase 5.2 canary) must not be
able to send it to the LLM provider — a network egress the sealed-window
scrub and CDP deny lists (phase 1) cannot see, since it happens entirely
inside monkeybot's own process.

Only *new* assistant text and *new* tool results are scanned, and only
those — never user-authored text. A message wrapping a real user Text block
is left untouched; only `ToolResponse` blocks (tool results, which arrive
wrapped in a role="user" message on the wire) and assistant `Text` blocks
are ever scanned or redacted. This matches the acceptance criterion: pasting
a saved password into a chat message manually does not trip the scanner.

Scanning happens against Spaces' `/json/scan` bridge endpoint (phase 5.1) —
the hot set of secrets and canaries never leaves the Electron main process,
so this module holds no secret material itself, only booleans back.
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from monkeybot.core.llm.provider import Message
from monkeybot.core.types.content_blocks import ContentBlock, Text, ToolResponse

logger = logging.getLogger(__name__)

WITHHELD_TEXT = "[withheld: credential detected]"

# Same file-path contract as browser_mcp.in_app_cdp, duplicated rather than
# imported: monkeybot's core process does not depend on the browser-mcp
# integration package (it is a separate, independently-installed MCP server).
_BRIDGE_URL_FILE = Path.home() / ".monkeybot" / "runtime" / "in-app-cdp-url"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_SCAN_TIMEOUT_S = 5.0
_SCAN_MAX_BATCH_BYTES = 2 * 1024 * 1024

# Passing ProxyHandler({}) makes urllib skip its default env-based proxy
# handler, so loopback requests never honor HTTP_PROXY (mirrors login.py).
_LOOPBACK_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _is_loopback_host(host: str | None) -> bool:
    if not host:
        return False
    return host.lower().strip("[]") in _LOOPBACK_HOSTS


def _format_loopback_host(hostname: str) -> str:
    if ":" in hostname and not hostname.startswith("["):
        return f"[{hostname}]"
    return hostname


def _bridge_http_origin(url_raw: str) -> str | None:
    parsed = urlparse(url_raw)
    if parsed.scheme not in {"ws", "wss", "http", "https"}:
        return None
    hostname = parsed.hostname
    if not _is_loopback_host(hostname) or not hostname:
        return None
    http_scheme = "https" if parsed.scheme in {"https", "wss"} else "http"
    default_port = 443 if http_scheme == "https" else 80
    port = parsed.port or default_port
    return f"{http_scheme}://{_format_loopback_host(hostname)}:{port}"


def _bridge_http_and_token() -> tuple[str | None, str | None]:
    """HTTP origin + bearer token for the published in-app CDP bridge only."""
    try:
        raw = _BRIDGE_URL_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None, None
    if not raw:
        return None, None
    http = _bridge_http_origin(raw)
    if not http:
        return None, None
    query_token: str | None = None
    for key, value in parse_qsl(urlparse(raw).query, keep_blank_values=True):
        if key == "token" and value:
            query_token = value
    if query_token:
        return http, query_token
    token_file = _BRIDGE_URL_FILE.parent / "in-app-cdp-token"
    try:
        file_token = token_file.read_text(encoding="utf-8").strip() or None
    except OSError:
        file_token = None
    return http, file_token


@dataclass(frozen=True)
class Hit:
    """One scanned text that matched a secret or canary. Never carries the value."""

    index: int
    kind: str  # "secret" | "canary"


class SecretScanner:
    """POSTs new text to Spaces' `/json/scan` bridge endpoint.

    A content-hash cache skips re-scanning text already confirmed clean, so
    unchanged history is not re-sent every turn. If Spaces is not running
    (no bridge file, or the request fails), scanning is a no-op — logged
    once at info — rather than blocking the agent: the egress gate is a
    backstop, not a hard dependency the app must always satisfy.
    """

    def __init__(self) -> None:
        self._clean_digests: set[str] = set()
        self._warned_no_app = False

    def scan(self, texts: Sequence[str]) -> list[Hit]:
        if not texts:
            return []
        to_check: list[tuple[int, str]] = []
        for i, text in enumerate(texts):
            if hashlib.sha256(text.encode("utf-8")).hexdigest() in self._clean_digests:
                continue
            to_check.append((i, text))
        if not to_check:
            return []

        http, token = _bridge_http_and_token()
        if not http or not token:
            if not self._warned_no_app:
                logger.info("secret scan skipped: in-app browser is not available")
                self._warned_no_app = True
            return []

        hits: list[Hit] = []
        # Both sets below are indices into the original `texts` argument
        # (never batch-local): each batch's tuples already carry that
        # original index, so results from every batch merge into one space.
        hit_indices: set[int] = set()
        scanned_indices: set[int] = set()
        for batch in _chunk_by_bytes(to_check, _SCAN_MAX_BATCH_BYTES):
            batch_hits, batch_hit_indices, batch_scanned_indices = self._scan_batch(
                batch, http, token
            )
            hits.extend(batch_hits)
            hit_indices.update(batch_hit_indices)
            scanned_indices.update(batch_scanned_indices)

        # Cache only texts a batch actually confirmed clean. A batch whose
        # request failed (or returned an unparseable body) contributes no
        # scanned indices, so its texts are neither cached as clean nor
        # reported as a hit — left unresolved to be retried on the next scan
        # rather than assumed safe.
        for original_index, text in to_check:
            if original_index in scanned_indices and original_index not in hit_indices:
                self._clean_digests.add(hashlib.sha256(text.encode("utf-8")).hexdigest())
        return hits

    def _scan_batch(
        self, batch: list[tuple[int, str]], http: str, token: str
    ) -> tuple[list[Hit], set[int], set[int]]:
        """Returns (hits, hit_indices, scanned_indices) — all indices are into
        the original `texts` argument passed to `scan`, via `batch`'s own
        `(original_index, text)` tuples."""
        payload = json.dumps({"texts": [t for _, t in batch]}).encode("utf-8")
        req = urllib.request.Request(
            f"{http}/json/scan",
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        try:
            with _LOOPBACK_OPENER.open(req, timeout=_SCAN_TIMEOUT_S) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except Exception:
            logger.warning("secret scan request failed", exc_info=True)
            # Nothing in this batch was actually scanned — the caller must
            # not cache any of it as clean, nor report it as a hit. The
            # sealed-window/CDP-level controls remain the primary defense
            # regardless of this backstop's availability.
            return [], set(), set()
        raw_hits = body.get("hits") if isinstance(body, dict) else None
        if not isinstance(raw_hits, list):
            logger.warning("secret scan response malformed: missing/invalid 'hits'")
            return [], set(), set()
        hits: list[Hit] = []
        hit_indices: set[int] = set()
        for raw in raw_hits:
            if not isinstance(raw, dict):
                continue
            batch_local_index = raw.get("index")
            kind = raw.get("kind")
            if not isinstance(batch_local_index, int) or not isinstance(kind, str):
                continue
            if batch_local_index < 0 or batch_local_index >= len(batch):
                continue
            original_index = batch[batch_local_index][0]
            hits.append(Hit(index=original_index, kind=kind))
            hit_indices.add(original_index)
        scanned_indices = {original_index for original_index, _ in batch}
        return hits, hit_indices, scanned_indices


def _chunk_by_bytes(
    items: list[tuple[int, str]], max_bytes: int
) -> list[list[tuple[int, str]]]:
    """Splits `items` into batches whose JSON-encoded text stays under `max_bytes`."""
    batches: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    current_bytes = 0
    for item in items:
        item_bytes = len(item[1].encode("utf-8"))
        if current and current_bytes + item_bytes > max_bytes:
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(item)
        current_bytes += item_bytes
    if current:
        batches.append(current)
    return batches


_scanner = SecretScanner()


def get_scanner() -> SecretScanner:
    """Process-wide scanner instance, so its clean-text cache persists across turns."""
    return _scanner


@dataclass(frozen=True)
class ScanUnit:
    """One scannable text block, addressable back to its exact position."""

    message_index: int
    block_path: tuple[int, ...]
    text: str


def extract_scan_units(messages: Sequence[Message]) -> list[ScanUnit]:
    """Assistant `Text` blocks, and `ToolResponse`-nested `Text` blocks only.

    Deliberately skips any `Text` block that is not inside a `ToolResponse` —
    that is real user-authored text, which must never be scanned or redacted.
    """
    units: list[ScanUnit] = []
    for m_idx, message in enumerate(messages):
        if message.role == "assistant":
            for b_idx, block in enumerate(message.content):
                if isinstance(block, Text) and block.text:
                    units.append(ScanUnit(m_idx, (b_idx,), block.text))
        else:
            for b_idx, block in enumerate(message.content):
                if isinstance(block, ToolResponse):
                    for r_idx, inner in enumerate(block.result):
                        if isinstance(inner, Text) and inner.text:
                            units.append(ScanUnit(m_idx, (b_idx, r_idx), inner.text))
    return units


def redact_units(
    messages: Sequence[Message],
    units: Sequence[ScanUnit],
    hit_message_indices: set[int],
) -> list[Message]:
    """Rebuilds `messages`, replacing every scanned block in a flagged message.

    Redacts every scannable block in a message that had *any* hit, not only
    the specific block that matched: a hit means this message is not safe to
    send as-is, and partial redaction risks leaving a second copy of the
    secret in an adjacent block the scan happened to miss.
    """
    if not hit_message_indices:
        return list(messages)
    out = list(messages)
    paths_by_message: dict[int, set[tuple[int, ...]]] = {}
    for unit in units:
        if unit.message_index in hit_message_indices:
            paths_by_message.setdefault(unit.message_index, set()).add(unit.block_path)
    for m_idx, paths in paths_by_message.items():
        message = out[m_idx]
        new_content: list[ContentBlock] = list(message.content)
        tool_response_paths: dict[int, set[int]] = {}
        for path in paths:
            if len(path) == 1:
                new_content[path[0]] = Text(text=WITHHELD_TEXT)
            else:
                tool_response_paths.setdefault(path[0], set()).add(path[1])
        for b_idx, r_indices in tool_response_paths.items():
            block = new_content[b_idx]
            if not isinstance(block, ToolResponse):
                continue
            new_result: list[ContentBlock] = list(block.result)
            for r_idx in r_indices:
                new_result[r_idx] = Text(text=WITHHELD_TEXT)
            new_content[b_idx] = replace(block, result=new_result)
        out[m_idx] = replace(message, content=new_content)
    return out


@dataclass(frozen=True)
class ScanOutcome:
    messages: list[Message]
    hits: list[Hit]
    hit_message_indices: frozenset[int]


def scan_and_redact(messages: Sequence[Message], scanner: SecretScanner | None = None) -> ScanOutcome:
    """Scans `messages` (the delta since the last provider call) and redacts hits.

    `messages` should be exactly the new messages since the last call to this
    function for a given turn stream — callers own tracking that offset
    (mirrors `_write_transcript_provider_request`'s own delta bookkeeping in
    turn_loop.py, kept as a separate counter so a scan always runs
    independent of whether transcript writing is enabled).
    """
    scanner = scanner or get_scanner()
    units = extract_scan_units(messages)
    if not units:
        return ScanOutcome(list(messages), [], frozenset())
    hits = scanner.scan([u.text for u in units])
    if not hits:
        return ScanOutcome(list(messages), [], frozenset())
    hit_message_indices = frozenset(units[h.index].message_index for h in hits)
    redacted = redact_units(messages, units, set(hit_message_indices))
    return ScanOutcome(redacted, hits, hit_message_indices)
