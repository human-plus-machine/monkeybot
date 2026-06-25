"""Structured JSON output for validate/doctor commands."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Severity = Literal["error", "warning"]
Status = Literal["pass", "fail", "skip"]


@dataclass
class CheckResult:
    id: str
    category: str
    severity: Severity
    status: Status
    message: str = ""
    field: str | None = None
    value: Any = None
    expected: list[str] | None = None
    remediation: str | None = None
    docs: str | None = None


@dataclass
class CommandReport:
    command: str
    ok: bool
    config_path: str | None
    checks: list[CheckResult] = field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        passed = warnings = failed = skipped = 0
        for c in self.checks:
            if c.status == "pass":
                passed += 1
            elif c.status == "fail":
                if c.severity == "error":
                    failed += 1
                else:
                    warnings += 1
            elif c.status == "skip":
                skipped += 1
        return {"passed": passed, "warnings": warnings, "failed": failed, "skipped": skipped}

    def compute_ok(self) -> None:
        self.ok = not any(c.severity == "error" and c.status == "fail" for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        self.compute_ok()
        return {
            "command": self.command,
            "ok": self.ok,
            "config_path": self.config_path,
            "checks": [asdict(c) for c in self.checks],
            "summary": self.summary,
        }

    def print_human(self) -> None:
        self.compute_ok()
        status = "OK" if self.ok else "FAILED"
        print(f"{self.command}: {status}")
        if self.config_path:
            print(f"  config: {self.config_path}")
        for c in self.checks:
            if c.status == "skip":
                continue
            icon = "✓" if c.status == "pass" else "!"
            print(f"  [{icon}] {c.id}: {c.message or c.status}")
            if c.remediation and c.status == "fail":
                print(f"      → {c.remediation}")
        s = self.summary
        print(f"  summary: {s['passed']} passed, {s['warnings']} warnings, {s['failed']} failed, {s['skipped']} skipped")

    def emit(self, *, as_json: bool) -> int:
        self.compute_ok()
        if as_json:
            print(json.dumps(self.to_dict(), indent=2))
        else:
            self.print_human()
        if self.ok:
            return 0
        return 1


def check(
    report: CommandReport,
    *,
    id: str,
    category: str,
    severity: Severity,
    passed: bool,
    message: str = "",
    skip: bool = False,
    **extra: Any,
) -> None:
    status: Status = "skip" if skip else ("pass" if passed else "fail")
    report.checks.append(
        CheckResult(
            id=id,
            category=category,
            severity=severity,
            status=status,
            message=message,
            **{k: v for k, v in extra.items() if k in CheckResult.__dataclass_fields__},
        )
    )
