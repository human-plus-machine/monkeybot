"""Parse ``docs/testing.md`` §8. Stdlib only: the ledger imports this from cli/ and browser-mcp."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from testkit.failures.store import repo_root

_CASE_ID = re.compile(
    r"^T(?P<tier>\d+)-(?P<area>[A-Z]+)-"
    r"(?P<body>G(?P<gateway>\d+)|(?P<start>\d+)(?:\.\.(?P<end>\d+))?)$"
)
_SEPARATOR = re.compile(r"^:?-+:?$")
_AREA_ORDER = (
    "LOOP",
    "CTX",
    "HOOK",
    "FILE",
    "SHELL",
    "POL",
    "CODE",
    "ATT",
    "COMP",
    "SUB",
    "VER",
    "SCHED",
    "SKILL",
    "MCP",
    "WEB",
    "MEM",
    "STORE",
    "CFG",
    "TODO",
    "PROV",
    "GW",
    "RT",
    "OPS",
)
_cache: dict[tuple[str, int], Catalog] = {}


class CatalogError(ValueError):
    """``docs/testing.md`` §8 is missing or a row cannot be parsed."""


@dataclass(frozen=True)
class ParsedId:
    raw: str
    tier: str
    area: str
    kind: str
    number: int | None


@dataclass(frozen=True)
class CatalogRow:
    raw_id: str
    case: str
    expect: str
    tier: str
    pri: str
    origin: str
    tests: tuple[str, ...]
    area: str
    kind: str
    numbers: tuple[int, ...]

    def covers(self, case_id: str) -> bool:
        parsed = parse_case_id(case_id)
        if parsed is None:
            return False
        if parsed.raw == self.raw_id:
            return True
        if self.kind == "gateway" or parsed.kind == "gateway" or parsed.number is None:
            return False
        return (
            parsed.tier == self.tier and parsed.area == self.area and parsed.number in self.numbers
        )

    def identity_keys(self) -> tuple[tuple[str, str, str], ...]:
        if self.kind == "gateway":
            return ((self.tier, self.area, self.raw_id),)
        return tuple((self.tier, self.area, str(number)) for number in self.numbers)


@dataclass(frozen=True)
class Catalog:
    rows: tuple[CatalogRow, ...]
    path: Path

    def covering(self, case_id: str) -> CatalogRow | None:
        found = [row for row in self.rows if row.covers(case_id)]
        if not found:
            return None
        return found[0]

    def uncovered_lines(self) -> tuple[str, ...]:
        counts: dict[tuple[str, str], list[int]] = {}
        for row in self.rows:
            bucket = counts.setdefault((row.area, row.tier), [0, 0])
            bucket[1] += 1
            if not row.tests:
                bucket[0] += 1
        order = {area: index for index, area in enumerate(_AREA_ORDER)}
        lines = [
            f"uncovered {area} {tier}: {uncovered} of {total}"
            for (area, tier), (uncovered, total) in sorted(
                counts.items(),
                key=lambda item: (order.get(item[0][0], len(order)), item[0][1]),
            )
        ]
        uncovered_total = sum(1 for row in self.rows if not row.tests)
        lines.append(f"uncovered total: {uncovered_total} of {len(self.rows)}")
        return tuple(lines)


def default_catalog_path() -> Path:
    return repo_root() / "docs" / "testing.md"


def parse_case_id(case_id: str) -> ParsedId | None:
    match = _CASE_ID.fullmatch(case_id.strip())
    if match is None:
        return None
    tier = f"T{match.group('tier')}"
    area = match.group("area")
    if match.group("gateway") is not None:
        return ParsedId(case_id.strip(), tier, area, "gateway", None)
    start = int(match.group("start"))
    end = match.group("end")
    if end is None:
        return ParsedId(case_id.strip(), tier, area, "number", start)
    return ParsedId(case_id.strip(), tier, area, "range", None)


def load_catalog(path: Path | None = None) -> Catalog:
    catalog_path = (path or default_catalog_path()).resolve()
    if not catalog_path.is_file():
        raise CatalogError(f"catalog not found: {catalog_path}")
    key = (str(catalog_path), catalog_path.stat().st_mtime_ns)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    catalog = _parse(catalog_path.read_text(encoding="utf-8"), catalog_path)
    _cache.clear()
    _cache[key] = catalog
    return catalog


def catalog_expect(case_id: str | None, path: Path | None = None) -> str | None:
    """Expect text for a case id, including ids that fall inside a ``..`` range."""
    if not case_id:
        return None
    catalog_path = path or default_catalog_path()
    if not catalog_path.is_file():
        return None
    try:
        row = load_catalog(catalog_path).covering(case_id)
    except CatalogError:
        return None
    if row is None:
        return None
    text = row.expect.strip()
    return text or None


def folder_tier(path: str) -> str | None:
    """Goal 6: the tier is the folder. ``regression/selftest`` is T1."""
    norm = path.replace("\\", "/").lstrip("./")
    if norm == "regression/selftest" or norm.startswith("regression/selftest/"):
        return "T1"
    match = re.match(r"regression/t([2-5])(?:/|$)", norm)
    if match is not None:
        return f"T{match.group(1)}"
    if norm == "tests" or norm.startswith("tests/"):
        return "T1"
    return None


def _parse(text: str, path: Path) -> Catalog:
    section = _section_8(text)
    rows: list[CatalogRow] = []
    for line_number, line in enumerate(section.splitlines(), 1):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = _cells(stripped)
        if not cells:
            continue
        if cells[0] == "ID":
            continue
        if all(_SEPARATOR.fullmatch(cell) for cell in cells):
            continue
        if len(cells) != 7:
            raise CatalogError(f"{path}:{line_number} expected 7 catalog cells, got {len(cells)}")
        raw_id, case, expect, tier, pri, origin, tests = cells
        parsed = parse_case_id(raw_id)
        if parsed is None:
            raise CatalogError(f"{path}:{line_number} is not a single ID or a range: {raw_id!r}")
        if tier != parsed.tier:
            raise CatalogError(
                f"{path}:{line_number} {raw_id} tier cell is {tier}, not {parsed.tier}"
            )
        kind, numbers = _span(parsed, raw_id)
        rows.append(
            CatalogRow(
                raw_id=raw_id,
                case=case,
                expect=expect,
                tier=tier,
                pri=pri,
                origin=origin,
                tests=_test_paths(tests),
                area=parsed.area,
                kind=kind,
                numbers=numbers,
            )
        )
    if not rows:
        raise CatalogError(f"{path} §8 has no catalog rows")
    return Catalog(tuple(rows), path)


def _span(parsed: ParsedId, raw_id: str) -> tuple[str, tuple[int, ...]]:
    if parsed.kind == "gateway":
        return "gateway", ()
    match = _CASE_ID.fullmatch(raw_id)
    if match is None or match.group("start") is None:
        raise CatalogError(f"not a numeric case id: {raw_id}")
    start = int(match.group("start"))
    end_text = match.group("end")
    if end_text is None:
        return "number", (start,)
    end = int(end_text)
    if end < start:
        raise CatalogError(f"range ends before it starts: {raw_id}")
    return "range", tuple(range(start, end + 1))


def _test_paths(cell: str) -> tuple[str, ...]:
    if not cell.strip():
        return ()
    return tuple(part.strip() for part in cell.split(",") if part.strip())


def _section_8(text: str) -> str:
    lines = text.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if start is None and line.startswith("## 8."):
            start = index + 1
            continue
        if start is not None and line.startswith("## 9."):
            return "\n".join(lines[start:index])
    raise CatalogError("docs/testing.md needs the ## 8. and ## 9. headings")


def _cells(line: str) -> list[str]:
    body = line.strip()
    if not (body.startswith("|") and body.endswith("|")):
        raise CatalogError(f"catalog row is not a table line: {line!r}")
    inner = body[1:-1]
    parts: list[str] = []
    buf: list[str] = []
    index = 0
    while index < len(inner):
        if inner[index] == "\\" and index + 1 < len(inner) and inner[index + 1] == "|":
            buf.append("|")
            index += 2
            continue
        if inner[index] == "|":
            parts.append("".join(buf).strip())
            buf = []
            index += 1
            continue
        buf.append(inner[index])
        index += 1
    parts.append("".join(buf).strip())
    return parts
