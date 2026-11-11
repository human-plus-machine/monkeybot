"""Outbound-to-provider secret/canary scanning (credential broker phase 5.3).

The threat this closes: an agent that captured a secret via `browser_login`
(or dumped its own environment, catching the phase 5.2 canary) must not be
able to send it to the LLM provider — a network egress the sealed-window
scrub and CDP deny lists (phase 1) cannot see, since it happens entirely
inside monkeybot's own process.

Only *new* assistant text (including `Thinking` blocks) and *new* tool
results are scanned, and only those — never user-authored text. A message
wrapping a real user Text block is left untouched; only `ToolResponse`
blocks (tool results, which arrive wrapped in a role="user" message on the
wire) and assistant `Text` / `Thinking` / `ToolRequest` blocks are ever
scanned or redacted. This matches the acceptance criterion: pasting a
saved password into a chat message manually does not trip the scanner.

Scanning happens against Spaces' `/json/scan` bridge endpoint (phase 5.1) —
the hot set of secrets and canaries never leaves the Electron main process,
so this module holds no secret material itself, only booleans back.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

from monkeybot.core.llm.provider import Message
from monkeybot.core.types.content_blocks import (
    ContentBlock,
    RedactedThinking,
    Text,
    Thinking,
    ToolRequest,
    ToolResponse,
)

logger = logging.getLogger(__name__)

WITHHELD_TEXT = "[withheld: credential detected]"

# Same file-path contract as browser_mcp.in_app_cdp, duplicated rather than
# imported: monkeybot's core process does not depend on the browser-mcp
# integration package (it is a separate, independently-installed MCP server).
_BRIDGE_URL_FILE = Path.home() / ".monkeybot" / "runtime" / "in-app-cdp-url"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_SCAN_TIMEOUT_S = 5.0
_SCAN_MAX_BATCH_BYTES = 2 * 1024 * 1024
# A digest confirmed clean stops being trusted after this long: the process
# holds this cache for the life of the gateway, and a password saved (or a
# canary generated) mid-session must eventually be caught even though its
# exact text was scanned clean earlier in that same session.
_CLEAN_CACHE_TTL_S = 30.0
# After a timeout or a connection error, skip the bridge for this long. Same
# window as the clean-text cache: that is already how stale a scan may be.
# An HTTP response does not open this window — the bridge answered.
_BRIDGE_DOWN_COOLDOWN_S = 30.0

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
    unchanged history is not re-sent every turn. Each cache entry expires
    after `_CLEAN_CACHE_TTL_S`: this process holds the cache for the whole
    gateway lifetime, and the bridge's hot set can grow after a digest was
    cached (a password saved, a canary registered) — an unbounded cache would
    never catch that text again even though it now matches. If Spaces is not
    running (no bridge file, or the request fails), scanning is a no-op —
    logged once at info — rather than blocking the agent: the egress gate is
    a backstop, not a hard dependency the app must always satisfy.

    A timeout or a connection error opens a breaker for that bridge URL and
    token for ``_BRIDGE_DOWN_COOLDOWN_S``. Later scans in the window are not
    sent and are not remembered as clean. The next scan after the window
    tries again. A new URL or token is a different bridge, so a restarted
    Spaces app is scanned at once.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clean_digests: dict[str, float] = {}
        self._warned_no_app = False
        self._clock = clock
        # Scans run on worker threads. The breaker is the only state those
        # threads both read and write around the decision to skip the bridge.
        self._lock = threading.Lock()
        self._down_key: tuple[str, str] | None = None
        self._down_until = 0.0

    def scan(self, texts: Sequence[str]) -> list[Hit]:
        if not texts:
            return []
        now = self._clock()
        to_check: list[tuple[int, str]] = []
        for i, text in enumerate(texts):
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            cached_at = self._clean_digests.get(digest)
            if cached_at is not None and now - cached_at < _CLEAN_CACHE_TTL_S:
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

        # Cap is on the JSON request body, not raw UTF-8: quotes/backslashes
        # (and ensure_ascii `\uXXXX` escapes) inflate the payload past the
        # bridge's 2 MB 413 threshold even when the raw string fits. A text
        # whose own `{"texts":[t]}` encoding already exceeds the cap can never
        # be sent — fail closed without a round trip rather than letting the
        # 413 path treat it as "unresolved" and ride out unredacted.
        oversized = [
            (i, t) for i, t in to_check if _scan_request_body_bytes([t]) > _SCAN_MAX_BATCH_BYTES
        ]
        scannable = [
            (i, t) for i, t in to_check if _scan_request_body_bytes([t]) <= _SCAN_MAX_BATCH_BYTES
        ]
        if oversized:
            logger.warning(
                "secret scan: %d text(s) exceed the %d-byte batch cap; redacting without a "
                "scan round trip",
                len(oversized),
                _SCAN_MAX_BATCH_BYTES,
            )
        hits: list[Hit] = [Hit(index=i, kind="secret") for i, _ in oversized]

        # Both sets below are indices into the original `texts` argument
        # (never batch-local): each batch's tuples already carry that
        # original index, so results from every batch merge into one space.
        hit_indices: set[int] = {i for i, _ in oversized}
        scanned_indices: set[int] = set()
        # An oversized text is still withheld: that decision does not ask the
        # bridge. The breaker only skips the round trips.
        bridge_key = (http, token)
        if not self._breaker_is_open(bridge_key):
            saw_success = False
            for batch in _chunk_by_bytes(scannable, _SCAN_MAX_BATCH_BYTES):
                batch_hits, batch_hit_indices, batch_scanned_indices, bridge_down = (
                    self._scan_batch(batch, http, token)
                )
                hits.extend(batch_hits)
                hit_indices.update(batch_hit_indices)
                scanned_indices.update(batch_scanned_indices)
                if bridge_down:
                    self._trip_breaker(bridge_key)
                    break
                if batch_scanned_indices:
                    saw_success = True
            else:
                if saw_success:
                    self._note_bridge_back(bridge_key)

        # Cache only texts a batch actually confirmed clean. A batch whose
        # request failed (or returned an unparseable body) contributes no
        # scanned indices, so its texts are neither cached as clean nor
        # reported as a hit — left unresolved to be retried on the next scan
        # rather than assumed safe. An oversized text is never in
        # `scanned_indices` either, so it is likewise never cached as clean —
        # every future scan of the same text redacts it again.
        for original_index, text in to_check:
            if original_index in scanned_indices and original_index not in hit_indices:
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                self._clean_digests[digest] = now
        return hits

    def _breaker_is_open(self, key: tuple[str, str]) -> bool:
        with self._lock:
            return self._down_key == key and self._clock() < self._down_until

    def _trip_breaker(self, key: tuple[str, str]) -> None:
        """Open the breaker. Logs once per opening, not once per skipped scan."""
        with self._lock:
            now = self._clock()
            already_open = self._down_key == key and now < self._down_until
            self._down_key = key
            self._down_until = now + _BRIDGE_DOWN_COOLDOWN_S
        if not already_open:
            logger.warning(
                "secret scan bridge unreachable; skipping scans for %.0f s",
                _BRIDGE_DOWN_COOLDOWN_S,
            )

    def _note_bridge_back(self, key: tuple[str, str]) -> None:
        """Clear a breaker this scan just found answering. Logs the recovery once."""
        with self._lock:
            recovered = self._down_key == key
            if recovered:
                self._down_key = None
                self._down_until = 0.0
        if recovered:
            logger.info("secret scan bridge reachable again")

    def _scan_batch(
        self, batch: list[tuple[int, str]], http: str, token: str
    ) -> tuple[list[Hit], set[int], set[int], bool]:
        """Returns (hits, hit_indices, scanned_indices, bridge_down).

        Indices are into the original `texts` argument passed to `scan`, via
        `batch`'s own `(original_index, text)` tuples. `bridge_down` is true
        only when the request timed out or the connection failed. An HTTP
        status is an answer, so it leaves the breaker alone.
        """
        payload = _scan_request_body([t for _, t in batch])
        req = urllib.request.Request(
            f"{http}/json/scan",
            data=payload,
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
            method="POST",
        )
        try:
            with _LOOPBACK_OPENER.open(req, timeout=_SCAN_TIMEOUT_S) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Drain the error body so the socket is not left hanging.
            try:
                exc.read()
            except Exception:
                pass
            if getattr(exc, "code", None) == 413:
                logger.warning(
                    "secret scan: batch rejected as too large (413); redacting without a scan"
                )
                # Chunking is supposed to keep us under the cap; a 413 means it
                # didn't. Fail closed — same as the oversized-single-item path —
                # rather than sending the batch onward unredacted. Not cached
                # as clean (scanned_indices stays empty).
                return (
                    [Hit(index=original_index, kind="secret") for original_index, _ in batch],
                    {original_index for original_index, _ in batch},
                    set(),
                    False,
                )
            logger.warning("secret scan request failed", exc_info=True)
            return [], set(), set(), False
        except (TimeoutError, urllib.error.URLError, OSError):
            # HTTPError is a URLError, and URLError is an OSError, so it has
            # to be handled above. A hung bridge raises TimeoutError from
            # getresponse(); a refused connection is a URLError. Either way
            # the bridge did not answer.
            logger.warning("secret scan request failed", exc_info=True)
            return [], set(), set(), True
        except Exception:
            logger.warning("secret scan request failed", exc_info=True)
            # Nothing in this batch was actually scanned — the caller must
            # not cache any of it as clean, nor report it as a hit. A parse
            # failure is not a dead bridge: the process answered. The
            # sealed-window/CDP-level controls remain the primary defense
            # regardless of this backstop's availability.
            return [], set(), set(), False
        raw_hits = body.get("hits") if isinstance(body, dict) else None
        if not isinstance(raw_hits, list):
            logger.warning("secret scan response malformed: missing/invalid 'hits'")
            return [], set(), set(), False
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
        return hits, hit_indices, scanned_indices, False


def _scan_request_body(texts: Sequence[str]) -> bytes:
    """JSON body posted to `/json/scan`. Encoding must match `_scan_batch`."""
    return json.dumps({"texts": list(texts)}).encode("utf-8")


def _scan_request_body_bytes(texts: Sequence[str]) -> int:
    return len(_scan_request_body(texts))


def _chunk_by_bytes(items: list[tuple[int, str]], max_bytes: int) -> list[list[tuple[int, str]]]:
    """Splits `items` into batches whose JSON request body stays under `max_bytes`.

    Callers must already have filtered out any single item whose own body
    exceeds `max_bytes`. Size is the encoded `{"texts":[...]}` payload, not
    raw string bytes, so escaping cannot push a batch over the bridge cap.
    """
    batches: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    for item in items:
        candidate = current + [item]
        if current and _scan_request_body_bytes([t for _, t in candidate]) > max_bytes:
            batches.append(current)
            current = [item]
        else:
            current = candidate
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
    """Assistant `Text`/`Thinking`/`ToolRequest` blocks, and `ToolResponse`-nested
    `Text` blocks only.

    A `ToolRequest`'s `args` are scanned too: a call blocked by the phase 5.3
    tool-argument gate (core_tool_executor.py) still leaves the model's
    `ToolRequest` sitting in `messages` with the secret in its `args`, which
    would otherwise ride out on the *next* provider call untouched by this
    scan (only the tool's own dispatch was blocked, not the transcript).

    `Thinking` is scanned for the same reason: Gemini/Anthropic/Bedrock
    round-trip reasoning on the next provider call. A hit is redacted to
    `RedactedThinking` so Anthropic's signature check is not required.

    Deliberately skips any `Text` block that is not inside a `ToolResponse` or
    an assistant message — that is real user-authored text, which must never
    be scanned or redacted.
    """
    units: list[ScanUnit] = []
    for m_idx, message in enumerate(messages):
        if message.role == "assistant":
            for b_idx, block in enumerate(message.content):
                if isinstance(block, Text) and block.text:
                    units.append(ScanUnit(m_idx, (b_idx,), block.text))
                elif isinstance(block, Thinking) and block.thinking:
                    units.append(ScanUnit(m_idx, (b_idx,), block.thinking))
                elif isinstance(block, ToolRequest) and block.args:
                    units.append(
                        ScanUnit(m_idx, (b_idx,), json.dumps(block.args, ensure_ascii=False))
                    )
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
                block = new_content[path[0]]
                if isinstance(block, ToolRequest):
                    # Keep `id`/`name` intact — a later `ToolResponse` in this
                    # same delta correlates back to this call's `id`, and the
                    # provider rejects a tool_use/tool_result pair that
                    # doesn't match up.
                    new_content[path[0]] = replace(block, args={"redacted": WITHHELD_TEXT})
                elif isinstance(block, Thinking):
                    # Drop the signed thinking payload. Replacing `thinking`
                    # in place would break Anthropic's signature check;
                    # RedactedThinking is the wire type that path already
                    # knows how to send (Gemini/Bedrock omit it).
                    new_content[path[0]] = RedactedThinking(data=WITHHELD_TEXT)
                else:
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


def scan_and_redact(
    messages: Sequence[Message], scanner: SecretScanner | None = None
) -> ScanOutcome:
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
    try:
        hits = scanner.scan([u.text for u in units])
    except Exception:
        # A scanner that throws did not clear the text. Withhold every unit in
        # this call so the secret cannot ride out on the next provider request.
        logger.warning(
            "secret scan raised; withholding %d unscanned text(s)",
            len(units),
            exc_info=True,
        )
        hits = [Hit(index=index, kind="secret") for index in range(len(units))]
    if not hits:
        return ScanOutcome(list(messages), [], frozenset())
    hit_message_indices = frozenset(units[h.index].message_index for h in hits)
    redacted = redact_units(messages, units, set(hit_message_indices))
    return ScanOutcome(redacted, hits, hit_message_indices)
