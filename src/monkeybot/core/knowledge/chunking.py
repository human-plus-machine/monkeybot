"""Content-aware chunking for FTS / embedding indexing.

Per-suffix strategies:
- markdown/rst — heading sections
- code — tree-sitter top-level defs (optional), else indent/brace heuristic
- json/yaml/toml — top-level key / table groups
- everything else — line-aligned ~token window (prose fallback)

Bump ``CHUNKER_VERSION`` when boundary logic changes so indexers re-scan.
"""

from __future__ import annotations

import json
import logging
import re
import tomllib
from dataclasses import dataclass
from pathlib import PurePosixPath

from monkeybot.core.knowledge.types import SourceType, TextChunk

logger = logging.getLogger(__name__)

# Bump when chunk boundaries change so content-hash skips re-index.
CHUNKER_VERSION = 4

# Rough heuristic: ~4 characters per token for mixed code/prose.
_CHARS_PER_TOKEN = 4

# How far past ``target_chars`` a unit may grow before it is window-split.
# Atomic units (code defs, structured keys) are worth keeping whole well past
# the target: a function or config block cut mid-body retrieves badly, and the
# embedding APIs accept inputs far larger than our default 700-token target.
# Prose sections have no such structure to preserve, so they get only enough
# slack to avoid splitting off a tiny trailing chunk.
_ATOMIC_OVERSIZE_FACTOR = 8.0
_PROSE_OVERSIZE_FACTOR = 1.25
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_SETEXT_RE = re.compile(r"^(=+|-+)\s*$")
_YAML_TOP_KEY_RE = re.compile(r"^([^\s#-][^:#]*?)\s*:")
_JSON_KEY_RE = re.compile(r'^(\s*)"((?:\\.|[^"\\])*)"\s*:')
_TOML_TABLE_RE = re.compile(r"^\[([^\]]+)\]\s*$")
_TOML_KEY_RE = re.compile(r"^([A-Za-z0-9_.-]+)\s*=")

_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown", ".rst"})
_STRUCTURED_SUFFIXES = frozenset({".json", ".yaml", ".yml", ".toml"})
_CODE_SUFFIXES = frozenset(
    {
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".mjs",
        ".cjs",
        ".rs",
        ".go",
        ".java",
        ".kt",
        ".swift",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cs",
        ".rb",
        ".php",
        ".vue",
        ".svelte",
        ".sql",
        ".graphql",
        ".sh",
        ".bash",
        ".zsh",
    }
)

_SUFFIX_TO_LANG: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".swift": "swift",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "c_sharp",
    ".rb": "ruby",
    ".php": "php",
    ".vue": "vue",
    ".svelte": "svelte",
    ".sql": "sql",
    ".graphql": "graphql",
    ".sh": "bash",
    ".bash": "bash",
    ".zsh": "bash",
}

# Node types treated as top-level semantic units across grammars.
_DEF_NODE_TYPES = frozenset(
    {
        "function_definition",
        "function_declaration",
        "function_item",
        "method_definition",
        "method_declaration",
        "class_definition",
        "class_declaration",
        "class_specifier",
        "interface_declaration",
        "type_alias_declaration",
        "enum_declaration",
        "enum_item",
        "struct_item",
        "impl_item",
        "trait_item",
        "mod_item",
        "type_declaration",
        "lexical_declaration",
        "variable_declaration",
        "export_statement",
        "decorated_definition",
        "namespace_definition",
        "module",
        "package_clause",
    }
)


@dataclass(frozen=True)
class _Unit:
    """One semantic span before packing into ~token-sized chunks."""

    start_line: int  # 1-based inclusive
    end_line: int  # 1-based inclusive
    label: str | None
    body: str
    # When True, prefer keeping the unit whole (code defs / structured keys).
    atomic: bool = False


def estimate_tokens(text: str) -> int:
    return max(1, (len(text) + _CHARS_PER_TOKEN - 1) // _CHARS_PER_TOKEN)


def index_content_digest(text: str) -> str:
    """SHA-256 used for indexer skip/upsert; includes chunker version."""
    from monkeybot.core.knowledge.extractors import content_hash

    return content_hash(f"chunker:{CHUNKER_VERSION}\n{text}")


def chunk_text(
    text: str,
    *,
    path: str,
    source_type: SourceType,
    chunk_tokens: int = 700,
    overlap_ratio: float = 0.12,
    use_ast: bool | None = None,
) -> list[TextChunk]:
    """Split ``text`` into content-aware overlapping chunks with a path/label prefix.

    ``use_ast``: ``True`` force tree-sitter, ``False`` force heuristic, ``None`` auto.
    """
    if not text or not text.strip():
        return []

    lines = text.splitlines(keepends=True)
    if not lines:
        return []

    target_chars = max(200, int(chunk_tokens * _CHARS_PER_TOKEN))
    overlap_chars = max(0, int(target_chars * max(0.0, min(0.5, overlap_ratio))))
    suffix = PurePosixPath(path).suffix.lower()

    units = _units_for_suffix(
        text, lines, suffix=suffix, use_ast=use_ast, target_chars=target_chars
    )
    if not units:
        return _window_chunk_lines(
            lines,
            path=path,
            source_type=source_type,
            target_chars=target_chars,
            overlap_chars=overlap_chars,
            default_label=None,
        )

    return _pack_units(
        units,
        lines=lines,
        path=path,
        source_type=source_type,
        target_chars=target_chars,
        overlap_chars=overlap_chars,
    )


def _units_for_suffix(
    text: str,
    lines: list[str],
    *,
    suffix: str,
    use_ast: bool | None,
    target_chars: int,
) -> list[_Unit] | None:
    if suffix in _MARKDOWN_SUFFIXES:
        return _markdown_units(lines)
    if suffix in _STRUCTURED_SUFFIXES:
        return _structured_units(text, lines, suffix=suffix)
    if suffix in _CODE_SUFFIXES:
        return _code_units(text, lines, suffix=suffix, use_ast=use_ast)
    # Prose / unknown — caller uses window fallback.
    _ = target_chars
    return None


# ---------------------------------------------------------------------------
# Packing + prose window
# ---------------------------------------------------------------------------


def _pack_units(
    units: list[_Unit],
    *,
    lines: list[str],
    path: str,
    source_type: SourceType,
    target_chars: int,
    overlap_chars: int,
) -> list[TextChunk]:
    chunks: list[TextChunk] = []
    i = 0
    while i < len(units):
        unit = units[i]
        char_count = len(unit.body)

        # Oversized non-atomic unit → sub-split with window inside it.
        # Atomic units (code defs / structured keys) stay whole unless huge.
        oversize_limit = target_chars * (
            _ATOMIC_OVERSIZE_FACTOR if unit.atomic else _PROSE_OVERSIZE_FACTOR
        )
        if char_count > oversize_limit and unit.body.strip():
            unit_lines = unit.body.splitlines(keepends=True)
            if not unit_lines:
                i += 1
                continue
            sub = _window_chunk_lines(
                unit_lines,
                path=path,
                source_type=source_type,
                target_chars=target_chars,
                overlap_chars=overlap_chars,
                default_label=unit.label,
                line_offset=unit.start_line - 1,
            )
            chunks.extend(sub)
            i += 1
            continue

        group: list[_Unit] = [unit]
        j = i + 1
        while j < len(units) and char_count + len(units[j].body) <= target_chars:
            # Don't pack an atomic unit into a group that would force later
            # mid-unit cuts; still allow merging small atomics under budget.
            group.append(units[j])
            char_count += len(units[j].body)
            j += 1

        start_line = group[0].start_line
        end_line = group[-1].end_line
        body = "".join(u.body for u in group)
        label = group[0].label
        chunks.append(_make_chunk(path, source_type, start_line, end_line, body, label))

        if j >= len(units):
            break

        # Walk back by ``overlap_chars`` worth of trailing units (mirrors
        # _window_chunk_lines). A unit that alone exceeds the overlap budget is
        # not re-included, so overlap_ratio bounds the duplication.
        next_i = j
        back_chars = 0
        while (
            next_i > i + 1
            and back_chars + len(units[next_i - 1].body) <= overlap_chars
        ):
            next_i -= 1
            back_chars += len(units[next_i].body)
        i = max(i + 1, next_i)

    return chunks if chunks else _window_chunk_lines(
        lines,
        path=path,
        source_type=source_type,
        target_chars=target_chars,
        overlap_chars=overlap_chars,
        default_label=None,
    )


def _window_chunk_lines(
    lines: list[str],
    *,
    path: str,
    source_type: SourceType,
    target_chars: int,
    overlap_chars: int,
    default_label: str | None,
    line_offset: int = 0,
) -> list[TextChunk]:
    # Expand any single line that exceeds the target into hard character slices
    # so oversized one-line sections still produce multiple chunks.
    expanded: list[tuple[str, int]] = []  # (text_with_newline, abs_line_1based)
    for idx, line in enumerate(lines):
        abs_line = line_offset + idx + 1
        if len(line) <= target_chars * 2:
            expanded.append((line, abs_line))
            continue
        # Preserve trailing newline on the last slice only.
        newline = "\n" if line.endswith("\n") else ""
        core = line[:-1] if newline else line
        pos = 0
        while pos < len(core):
            piece = core[pos : pos + target_chars]
            pos += target_chars
            suffix = newline if pos >= len(core) else ""
            expanded.append((piece + suffix, abs_line))

    chunks: list[TextChunk] = []
    start_idx = 0
    n = len(expanded)

    while start_idx < n:
        char_count = 0
        end_idx = start_idx
        while end_idx < n and (char_count < target_chars or end_idx == start_idx):
            char_count += len(expanded[end_idx][0])
            end_idx += 1

        body = "".join(t for t, _ in expanded[start_idx:end_idx])
        start_line = expanded[start_idx][1]
        end_line = expanded[end_idx - 1][1]
        label = default_label
        if label is None:
            # Map back to original lines for heading search when possible.
            orig_idx = max(0, start_line - line_offset - 1)
            if orig_idx < len(lines):
                label = _nearest_heading(lines, min(orig_idx, len(lines) - 1))
        chunks.append(_make_chunk(path, source_type, start_line, end_line, body, label))

        if end_idx >= n:
            break

        if overlap_chars <= 0:
            start_idx = end_idx
            continue
        back_chars = 0
        new_start = end_idx
        while new_start > start_idx and back_chars < overlap_chars:
            new_start -= 1
            back_chars += len(expanded[new_start][0])
        start_idx = max(start_idx + 1, new_start)

    return chunks


def _make_chunk(
    path: str,
    source_type: SourceType,
    start_line: int,
    end_line: int,
    body: str,
    label: str | None,
) -> TextChunk:
    prefix_parts = [path]
    if label:
        prefix_parts.append(label)
    prefixed = f"{' · '.join(prefix_parts)}\n{body}"
    return TextChunk(
        path=path,
        source_type=source_type,
        start_line=start_line,
        end_line=end_line,
        text=prefixed,
    )


def _nearest_heading(lines: list[str], start_idx: int) -> str | None:
    for i in range(start_idx, -1, -1):
        m = _HEADING_RE.match(lines[i].rstrip("\n"))
        if m:
            return m.group(2).strip()
    return None


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _markdown_units(lines: list[str]) -> list[_Unit]:
    """Split on ATX and setext headings into section units."""
    boundaries: list[tuple[int, str | None]] = []  # (0-based line, label)
    i = 0
    while i < len(lines):
        raw = lines[i].rstrip("\n")
        atx = _HEADING_RE.match(raw)
        if atx:
            boundaries.append((i, atx.group(2).strip()))
            i += 1
            continue
        if (
            i + 1 < len(lines)
            and raw.strip()
            and _SETEXT_RE.match(lines[i + 1].rstrip("\n"))
            and not raw.startswith("#")
        ):
            boundaries.append((i, raw.strip()))
            i += 2
            continue
        i += 1

    if not boundaries:
        # Single prose unit — packer / window will handle size.
        body = "".join(lines)
        return [_Unit(1, len(lines), None, body)]

    # Preamble before first heading.
    units: list[_Unit] = []
    first = boundaries[0][0]
    if first > 0:
        body = "".join(lines[0:first])
        if body.strip():
            units.append(_Unit(1, first, None, body))

    for idx, (start, label) in enumerate(boundaries):
        end = boundaries[idx + 1][0] if idx + 1 < len(boundaries) else len(lines)
        body = "".join(lines[start:end])
        units.append(_Unit(start + 1, end, label, body))

    return units


# ---------------------------------------------------------------------------
# Structured (JSON / YAML / TOML)
# ---------------------------------------------------------------------------


def _structured_units(text: str, lines: list[str], *, suffix: str) -> list[_Unit]:
    if suffix == ".json":
        return _json_units(text, lines)
    if suffix in {".yaml", ".yml"}:
        return _yaml_units(lines)
    if suffix == ".toml":
        return _toml_units(text, lines)
    return [_Unit(1, len(lines), None, "".join(lines))]


def _json_units(text: str, lines: list[str]) -> list[_Unit]:
    """Split pretty-printed JSON objects on top-level keys (YAML-style lines)."""
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError, ValueError):
        return _indent_brace_units(lines)

    if not isinstance(data, dict) or not data:
        return [_Unit(1, len(lines), None, "".join(lines))]

    starts: list[tuple[int, str | None]] = []
    depth = 0
    in_string = False
    escape = False
    for i, line in enumerate(lines):
        raw = line.rstrip("\n")
        if not in_string and depth == 1:
            m = _JSON_KEY_RE.match(raw)
            if m:
                starts.append((i, _json_unescape_key(m.group(2))))

        for ch in line:
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch in "{[":
                depth += 1
            elif ch in "}]":
                depth = max(0, depth - 1)

    if not starts:
        # Compact / single-line object — keep as one unit.
        return [_Unit(1, len(lines), None, "".join(lines))]

    units: list[_Unit] = []
    if starts[0][0] > 0:
        body = "".join(lines[0 : starts[0][0]])
        if body.strip():
            units.append(_Unit(1, starts[0][0], None, body))

    for idx, (start, label) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
        units.append(
            _Unit(start + 1, end, label, "".join(lines[start:end]), atomic=True)
        )
    return units


def _json_unescape_key(raw: str) -> str:
    try:
        decoded = json.loads(f'"{raw}"')
    except (json.JSONDecodeError, TypeError, ValueError):
        return raw
    return decoded if isinstance(decoded, str) else raw


def _yaml_units(lines: list[str]) -> list[_Unit]:
    starts: list[tuple[int, str | None]] = []
    for i, line in enumerate(lines):
        raw = line.rstrip("\n")
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        # Document start
        if raw.strip() in {"---", "..."}:
            continue
        m = _YAML_TOP_KEY_RE.match(raw)
        if m and not raw.startswith(" ") and not raw.startswith("\t"):
            starts.append((i, m.group(1).strip()))

    if not starts:
        return [_Unit(1, len(lines), None, "".join(lines))]

    units: list[_Unit] = []
    if starts[0][0] > 0:
        body = "".join(lines[0 : starts[0][0]])
        if body.strip():
            units.append(_Unit(1, starts[0][0], None, body))

    for idx, (start, label) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
        units.append(
            _Unit(start + 1, end, label, "".join(lines[start:end]), atomic=True)
        )
    return units


def _toml_units(text: str, lines: list[str]) -> list[_Unit]:
    try:
        tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return [_Unit(1, len(lines), None, "".join(lines))]

    starts: list[tuple[int, str | None]] = []
    for i, line in enumerate(lines):
        raw = line.rstrip("\n")
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        table = _TOML_TABLE_RE.match(raw)
        if table:
            starts.append((i, table.group(1).strip()))
            continue
        key = _TOML_KEY_RE.match(raw)
        if key and not starts:
            # Top-level keys before any table — each is a unit start when at
            # column 0. Group consecutive root keys until a table.
            starts.append((i, key.group(1).strip()))
        elif (
            key
            and starts
            and not _TOML_TABLE_RE.match(lines[starts[-1][0]].rstrip("\n"))
            and all(
                not _TOML_TABLE_RE.match(lines[s].rstrip("\n")) for s, _ in starts
            )
        ):
            # Another root key before first table.
            starts.append((i, key.group(1).strip()))

    # Prefer table-based units when tables exist.
    table_starts = [
        (i, lab)
        for i, lab in starts
        if _TOML_TABLE_RE.match(lines[i].rstrip("\n"))
    ]
    if table_starts:
        # Preamble (root keys) + each table section.
        first_table = table_starts[0][0]
        units: list[_Unit] = []
        if first_table > 0:
            body = "".join(lines[0:first_table])
            if body.strip():
                units.append(_Unit(1, first_table, None, body))
        for idx, (start, label) in enumerate(table_starts):
            end = (
                table_starts[idx + 1][0]
                if idx + 1 < len(table_starts)
                else len(lines)
            )
            units.append(
                _Unit(start + 1, end, label, "".join(lines[start:end]), atomic=True)
            )
        return units

    if not starts:
        return [_Unit(1, len(lines), None, "".join(lines))]

    units = []
    for idx, (start, label) in enumerate(starts):
        end = starts[idx + 1][0] if idx + 1 < len(starts) else len(lines)
        units.append(
            _Unit(start + 1, end, label, "".join(lines[start:end]), atomic=True)
        )
    return units


# ---------------------------------------------------------------------------
# Code: tree-sitter + indent/brace heuristic
# ---------------------------------------------------------------------------


def _code_units(
    text: str,
    lines: list[str],
    *,
    suffix: str,
    use_ast: bool | None,
) -> list[_Unit]:
    want_ast = True if use_ast is None else use_ast
    if want_ast:
        ast_units = _tree_sitter_units(text, lines, suffix=suffix)
        if ast_units is not None:
            return ast_units
    return _indent_brace_units(lines)


def _tree_sitter_units(
    text: str, lines: list[str], *, suffix: str
) -> list[_Unit] | None:
    lang = _SUFFIX_TO_LANG.get(suffix)
    if not lang:
        return None
    try:
        from tree_sitter_language_pack import get_parser
    except ImportError:
        logger.debug("tree-sitter-language-pack not installed; using brace heuristic")
        return None

    try:
        parser = get_parser(lang)
        tree = parser.parse(text.encode("utf-8"))
    except Exception as exc:
        logger.debug("tree-sitter parse failed for %s: %r", lang, exc)
        return None

    root = tree.root_node
    if root is None:
        return None

    def_nodes = []
    for child in root.children:
        node = child
        # Unwrap export wrappers.
        if node.type == "export_statement" and node.named_child_count:
            inner = node.named_children[0]
            if inner.type in _DEF_NODE_TYPES or inner.type.endswith("_declaration"):
                node = inner
        if (
            node.type in _DEF_NODE_TYPES
            or node.type.endswith("_definition")
            or node.type.endswith("_declaration")
            or node.type.endswith("_item")
        ):
            def_nodes.append(child)  # keep outer for span (includes export)

    if not def_nodes:
        return None

    # Expand each def to include immediately preceding comment / decorator lines.
    units: list[_Unit] = []
    prev_end_line = 0  # 0-based exclusive end of previous unit

    for node in def_nodes:
        start_row = node.start_point[0]
        end_row = node.end_point[0]
        # Walk back for decorators / comments attached to this def.
        attach = start_row
        while attach > prev_end_line:
            prev = lines[attach - 1].rstrip("\n")
            stripped = prev.strip()
            if not stripped:
                attach -= 1
                continue
            if stripped.startswith("@") or stripped.startswith("#") or stripped.startswith("//") or stripped.startswith("/*") or stripped.startswith("*"):
                attach -= 1
                continue
            break

        # Gap between previous unit and this one → preamble unit.
        if attach > prev_end_line:
            gap = "".join(lines[prev_end_line:attach])
            if gap.strip():
                units.append(
                    _Unit(prev_end_line + 1, attach, None, gap)
                )

        body = "".join(lines[attach : end_row + 1])
        label = _node_label(node, text)
        units.append(_Unit(attach + 1, end_row + 1, label, body, atomic=True))
        prev_end_line = end_row + 1

    if prev_end_line < len(lines):
        tail = "".join(lines[prev_end_line:])
        if tail.strip():
            units.append(_Unit(prev_end_line + 1, len(lines), None, tail))

    return units or None


def _node_label(node: object, text: str) -> str | None:
    """Best-effort symbol name from a tree-sitter node."""
    name_fields = ("name", "identifier")
    for field in name_fields:
        getter = getattr(node, "child_by_field_name", None)
        if getter is None:
            break
        child = getter(field)
        if child is not None:
            try:
                start, end = child.start_byte, child.end_byte
                raw = text.encode("utf-8")[start:end].decode("utf-8", errors="replace")
                if raw.strip():
                    return raw.strip()
            except (AttributeError, TypeError, ValueError) as exc:
                logger.debug("tree-sitter label field %s failed: %r", field, exc)

    # Heuristic: first identifier-like named child.
    named = getattr(node, "named_children", None)
    if named:
        for child in named:
            if getattr(child, "type", "") in {
                "identifier",
                "type_identifier",
                "property_identifier",
                "name",
            }:
                try:
                    start, end = child.start_byte, child.end_byte
                    raw = text.encode("utf-8")[start:end].decode(
                        "utf-8", errors="replace"
                    )
                    if raw.strip():
                        return raw.strip()
                except (AttributeError, TypeError, ValueError) as exc:
                    logger.debug("tree-sitter label child failed: %r", exc)
                    continue
    return getattr(node, "type", None)


def _indent_brace_units(lines: list[str]) -> list[_Unit]:
    """Split at indent-0 / brace-depth-0 statement starts; never mid-block."""
    depth = 0
    # Open string delimiter (1 or 3 chars) when inside a multi-line string.
    in_string: str | None = None
    unit_starts = [0]

    for i, line in enumerate(lines):
        stripped = line.strip()
        if (
            in_string is None
            and i > 0
            and depth == 0
            and stripped
            and len(line) - len(line.lstrip(" \t")) == 0
            and not stripped.startswith(
                (")", "]", "}", "else", "elif", "except", "finally", "catch")
            )
            and unit_starts[-1] != i
        ):
            unit_starts.append(i)

        depth, in_string = _scan_line(line, depth=depth, in_string=in_string)

    units: list[_Unit] = []
    for idx, start in enumerate(unit_starts):
        end = unit_starts[idx + 1] if idx + 1 < len(unit_starts) else len(lines)
        body = "".join(lines[start:end])
        if not body.strip():
            continue
        label = _heuristic_label(lines[start])
        units.append(_Unit(start + 1, end, label, body, atomic=True))

    return units or [_Unit(1, len(lines), None, "".join(lines))]


_QUOTE_CHARS = ("'", '"', "`")


def _scan_line(
    line: str, *, depth: int, in_string: str | None
) -> tuple[int, str | None]:
    """Update brace depth and open-string state after consuming ``line``.

    Tracks triple-quoted strings (``\"\"\"`` / ``'''``) so a brace or apostrophe
    inside a docstring cannot desync ``depth`` and split a function mid-body.
    Single-char string state is dropped at end of line (unless the line ends in
    a backslash continuation), which bounds the damage from apostrophes in
    prose, Rust lifetimes, and other one-line quote lookalikes.
    """
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if in_string is not None:
            if ch == "\\":
                i += 2
                continue
            if line.startswith(in_string, i):
                i += len(in_string)
                in_string = None
                continue
            i += 1
            continue
        if ch in _QUOTE_CHARS:
            triple = ch * 3
            if line.startswith(triple, i):
                in_string = triple
                i += 3
            else:
                in_string = ch
                i += 1
            continue
        if ch in "{[(":
            depth += 1
        elif ch in "}])":
            depth = max(0, depth - 1)
        i += 1

    if in_string is not None and len(in_string) == 1:
        # A single-quoted string never spans lines without a continuation.
        if not line.rstrip("\n").endswith("\\"):
            in_string = None
    return depth, in_string


def _heuristic_label(first_line: str) -> str | None:
    raw = first_line.strip()
    patterns = [
        re.compile(r"^(?:export\s+)?(?:async\s+)?(?:function\s+|class\s+|def\s+|fn\s+)(\w+)"),
        re.compile(r"^(?:pub\s+)?(?:async\s+)?(?:fn|struct|enum|impl|trait|mod)\s+(\w+)"),
        re.compile(r"^(?:public|private|protected)?\s*(?:static\s+)?(?:class|interface|enum)\s+(\w+)"),
        re.compile(r"^(?:const|let|var)\s+(\w+)"),
    ]
    for pat in patterns:
        m = pat.match(raw)
        if m:
            return m.group(1)
    return None


__all__ = [
    "CHUNKER_VERSION",
    "chunk_text",
    "estimate_tokens",
    "index_content_digest",
]
