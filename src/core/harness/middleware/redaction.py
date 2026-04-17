"""RedactionMW — ingress/egress scrubbing of secrets & PII-like strings."""

from __future__ import annotations

from typing import Literal

from ..redaction import Redactor


class RedactionMW:
    """Applied twice in the pipeline: once as ingress (direction="in") immediately
    after RulesEnforcementMW, and once as egress (direction="out") after tool calls.
    """

    name_in = "RedactionMW(in)"
    name_out = "RedactionMW(out)"

    def __init__(self, redactor: Redactor, direction: Literal["in", "out"]) -> None:
        self.redactor = redactor
        self.direction = direction

    @property
    def name(self) -> str:
        return self.name_in if self.direction == "in" else self.name_out

    def redact_text(self, text: str) -> tuple[str, bool]:
        return self.redactor.redact(text)

    def redact_messages(self, messages: list[dict]) -> tuple[list[dict], bool]:
        out: list[dict] = []
        any_redacted = False
        for m in messages:
            if isinstance(m.get("content"), str):
                new_c, r = self.redactor.redact(m["content"])
                mm = dict(m)
                mm["content"] = new_c
                out.append(mm)
                any_redacted = any_redacted or r
            else:
                out.append(m)
        return out, any_redacted
