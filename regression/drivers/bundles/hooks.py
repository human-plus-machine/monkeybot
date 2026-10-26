"""Verification gate, evidence guard, secret scanner, and memory index lines."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from contextlib import contextmanager

import monkeybot.core.runtime.loop_hooks as loop_hooks
import monkeybot.core.tools.core_tool_executor as core_tool_executor
from monkeybot.core.coding.verification_gate import VerificationGate
from monkeybot.core.context.secret_egress import Hit
from monkeybot.core.hooks import HookEvent, HookManager, HookPayload
from monkeybot.core.hooks.evidence_guard import EvidencePathGuard
from regression.drivers.bundles.protocol import Bundle, LoopDriverError, RunCtx
from regression.drivers.support.paths import _SECRET_CANARY, _SECRET_SCANNER_ENV


def _index_hook(lines: list[str]) -> object:
    async def _inject(payload: HookPayload) -> None:
        payload.inject_memory_lines.extend(lines)

    return _inject


def register_memory_index(manager: HookManager, ctx: RunCtx) -> None:
    """PRE_TURN index lines. Registered before the memory hooks, ahead of git."""
    index = list(ctx.scenario.memory.index) if ctx.scenario.memory is not None else []
    if index:
        manager.register(HookEvent.PRE_TURN, _index_hook(index))


class _ScenarioSecretScanner:
    """Scenario scanner. ``raise`` mode throws; ``scan_and_redact`` withholds."""

    def __init__(self, *, raise_scan: bool) -> None:
        self._raise_scan = raise_scan

    def scan(self, texts: Sequence[str]) -> list[Hit]:
        if self._raise_scan:
            raise RuntimeError("secret scanner failed")
        return [
            Hit(index, kind="canary") for index, text in enumerate(texts) if _SECRET_CANARY in text
        ]


def _take_secret_scanner_mode(env: dict[str, str], hook_names: Sequence[str]) -> str | None:
    """Pop the scenario scanner mode. ``None`` means the real scanner stays."""
    raw = env.pop(_SECRET_SCANNER_ENV, None)
    if "secret_scanner" not in hook_names:
        if raw is not None:
            raise LoopDriverError(
                f"{_SECRET_SCANNER_ENV} is set but hooks does not include secret_scanner"
            )
        return None
    mode = "" if raw is None else raw.strip()
    if mode not in {"", "raise"}:
        raise LoopDriverError(
            f"{_SECRET_SCANNER_ENV} must be empty or 'raise' when set, not {mode!r}"
        )
    return mode


@contextmanager
def _installed_secret_scanner(mode: str | None) -> Iterator[None]:
    """Patch both ``get_scanner`` bindings for the run, then restore them."""
    if mode is None:
        yield
        return
    scanner = _ScenarioSecretScanner(raise_scan=mode == "raise")

    def _scanner() -> _ScenarioSecretScanner:
        return scanner

    previous_loop = loop_hooks.get_scanner
    previous_tools = core_tool_executor.get_scanner
    loop_hooks.get_scanner = _scanner
    core_tool_executor.get_scanner = _scanner
    try:
        yield
    finally:
        loop_hooks.get_scanner = previous_loop
        core_tool_executor.get_scanner = previous_tools


class _HookBundle(Bundle):
    order = 60
    hooks = ("verification_gate", "evidence_guard", "secret_scanner")

    async def prepare(self, ctx: RunCtx) -> None:
        ctx.scanner_mode = _take_secret_scanner_mode(ctx.env, ctx.hook_names)

    def register(self, manager: HookManager, ctx: RunCtx) -> None:
        if "verification_gate" in ctx.hook_names:
            VerificationGate(ctx.cfg.coding.verification_gate).register(manager)
        if "evidence_guard" in ctx.hook_names:
            EvidencePathGuard().register(manager)


BUNDLE = _HookBundle()
