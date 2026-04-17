"""Redactor — regex-based scrubber used by ingress/egress middleware, event bus,
and the RunPackage writer."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence


@dataclass
class Redactor:
    patterns: Sequence[str]

    def __post_init__(self) -> None:
        self._compiled: list[re.Pattern[str]] = [re.compile(p) for p in self.patterns]

    def redact(self, text: str) -> tuple[str, bool]:
        if not isinstance(text, str) or not text:
            return text, False
        redacted = False
        out = text
        for rx in self._compiled:
            new_out, n = rx.subn(lambda m: "<redacted>", out)
            if n > 0:
                redacted = True
                out = new_out
        return out, redacted

    def redact_dict(self, data: dict) -> tuple[dict, bool]:
        out: dict = {}
        any_redacted = False
        for k, v in data.items():
            if isinstance(v, str):
                new_v, r = self.redact(v)
                out[k] = new_v
                any_redacted = any_redacted or r
            elif isinstance(v, dict):
                new_v, r = self.redact_dict(v)
                out[k] = new_v
                any_redacted = any_redacted or r
            elif isinstance(v, list):
                new_v, r = self._redact_list(v)
                out[k] = new_v
                any_redacted = any_redacted or r
            else:
                out[k] = v
        return out, any_redacted

    def _redact_list(self, data: list) -> tuple[list, bool]:
        out: list = []
        any_redacted = False
        for v in data:
            if isinstance(v, str):
                new_v, r = self.redact(v)
                out.append(new_v)
                any_redacted = any_redacted or r
            elif isinstance(v, dict):
                new_v, r = self.redact_dict(v)
                out.append(new_v)
                any_redacted = any_redacted or r
            elif isinstance(v, list):
                new_v, r = self._redact_list(v)
                out.append(new_v)
                any_redacted = any_redacted or r
            else:
                out.append(v)
        return out, any_redacted
