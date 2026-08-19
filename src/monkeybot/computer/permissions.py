"""Assembles the effective permission ruleset for ``computer_*`` tools.

Before any of that: ``safety.precheck_policy()`` runs first and unconditionally.
A call that would fail a hard limit (a credential path, an exec-surface `open`,
a denied app, trashing a top-level folder, ...) is denied right there — it
never reaches the ask/allow evaluation below, so it never shows an approval
card the user would just have to decline. This mirrors what the tool itself
would do once it actually runs; precheck exists only so the refusal happens
*before* asking, not to replace the tool's own check (which still runs,
unconditionally, when the call executes — defense in depth).

Once precheck passes, three rule sources are layered, last-match-wins, in this
fixed order:

1. **Built-in baseline** — ``tool: "computer_*", pattern: "*", effect: ask``, in
   code, unconditional. This is deliberately *not* shipped as a line in the
   scaffold ``permissions.yaml``: that file is optional and fail-open
   (``try_load_permission_inspector`` returns ``None`` on a missing or broken
   file — see its docstring), so a baseline that lived there could be silently
   defeated by deleting or breaking one YAML file. Baking it into code makes
   "every computer action asks by default" a property of the tool, not of a
   config file the user can edit.
2. **Durable approvals overlay** (``monkeybot_config/approvals.json``) — machine-
   written "Always allow" rules (see ``computer/approvals.py``). Evaluated after
   the baseline, so an approved resource beats the ask default.
3. **The user's ``permissions.yaml``** — evaluated last, so a hand-written rule
   (e.g. an explicit ``deny`` for ``computer_trash``) always wins over a
   remembered approval or the baseline.

:class:`ComputerAwarePermissionInspector` replaces the plain ``PermissionInspector``
in the gateway's inspector chain only when computer tools are enabled for this
gateway process (see ``should_enable_computer_tools``); every other deployment
is byte-for-byte unaffected.
"""

from __future__ import annotations

import datetime
import logging
from collections.abc import Callable
from pathlib import Path
from typing import cast

from monkeybot.computer import ALWAYS_SCOPE
from monkeybot.computer.approvals import add_approval, load_approvals, to_permission_rules
from monkeybot.computer.safety import precheck_policy
from monkeybot.core.context import TurnContext
from monkeybot.core.tools.inspector import Decision, InspectorToolCall
from monkeybot.core.tools.permission import (
    Effect,
    PermissionConfigError,
    PermissionRule,
    PermissionRuleset,
    SessionApprovals,
    evaluate,
    load_permissions,
    resource_for_call,
)

logger = logging.getLogger(__name__)

COMPUTER_BASELINE_RULES: tuple[PermissionRule, ...] = (
    PermissionRule(
        tool="computer_*",
        pattern="*",
        effect="ask",
        message="Allow this action on your computer?",
    ),
)


def _load_ruleset_fail_open(path: Path) -> PermissionRuleset:
    """Same fail-open behavior as ``try_load_permission_inspector``, but returns
    the raw (possibly empty) ruleset instead of ``None`` — this module always
    needs a ruleset object to layer the computer-specific rules on top of."""
    try:
        return load_permissions(path)
    except FileNotFoundError:
        logger.info("permissions.yaml missing (%s); user ruleset empty", path)
    except PermissionConfigError:
        logger.exception("permission ruleset load failed (%s)", path)
    except Exception:
        logger.exception("permission ruleset load failed (%s)", path)
    return PermissionRuleset(rules=(), default="allow")


class ComputerAwarePermissionInspector:
    """``ToolInspector`` layering the computer baseline + approvals overlay + user rules.

    Reloads the approvals overlay on every ``check()`` (cheap: one ``stat()`` call)
    and rebuilds its rules only when the file's ``(mtime_ns, size, inode)`` changes.
    On a genuine change (not the first load), also clears
    ``bus.session_approvals`` — otherwise a revoke in Settings would keep being
    masked by the in-memory "already approved this session" cache until the
    gateway process restarts.
    """

    def __init__(
        self, *, base_ruleset: PermissionRuleset, approvals_path: Path, allow_ask: bool = True
    ) -> None:
        self._base_ruleset = base_ruleset
        self._approvals_path = approvals_path
        self._allow_ask = allow_ask
        self._overlay_rules: tuple[PermissionRule, ...] = ()
        self._overlay_stamp: tuple[int, int, int] | None = None

    @property
    def ruleset_default(self) -> Effect:
        return self._base_ruleset.default

    def _stat_stamp(self) -> tuple[int, int, int] | None:
        try:
            st = self._approvals_path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size, st.st_ino)

    def _reload_overlay_if_stale(self, bus: object | None) -> None:
        stamp = self._stat_stamp()
        if stamp == self._overlay_stamp:
            return
        records = load_approvals(self._approvals_path)
        triples = to_permission_rules(records)
        self._overlay_rules = tuple(
            PermissionRule(tool=t, pattern=p, effect=cast(Effect, e)) for t, p, e in triples
        )
        if self._overlay_stamp is not None:
            approvals = getattr(bus, "session_approvals", None)
            if isinstance(approvals, SessionApprovals):
                approvals.clear()
        self._overlay_stamp = stamp

    async def check(self, call: InspectorToolCall, ctx: TurnContext) -> Decision:
        # Hard denial always wins — checked first, before the session-approval
        # cache and before rule evaluation, so a credential path or exec-surface
        # is refused outright rather than showing an approval card that would
        # only fail after the user clicks Allow. Never overridable by any rule
        # or remembered approval (see safety.precheck_policy's docstring).
        precheck_error = precheck_policy(call.name, call.args)
        if precheck_error is not None:
            return Decision(kind="deny", message=precheck_error.message)

        resource = resource_for_call(call)
        bus = ctx.sse_bus
        self._reload_overlay_if_stale(bus)

        if bus is not None:
            approvals = getattr(bus, "session_approvals", None)
            if isinstance(approvals, SessionApprovals) and approvals.is_allowed(
                call.name, resource
            ):
                return Decision(kind="allow")

        combined = (*COMPUTER_BASELINE_RULES, *self._overlay_rules, *self._base_ruleset.rules)
        matched = evaluate(call.name, resource, combined)
        effect: Effect = self._base_ruleset.default if matched is None else matched.effect
        message = None if matched is None else matched.message

        if effect == "ask" and not self._allow_ask:
            return Decision(
                kind="deny", message=message or "ask effect requires an interactive session; denied"
            )
        if effect == "allow":
            return Decision(kind="allow")
        if effect == "deny":
            return Decision(kind="deny", message=message or "denied by permission ruleset")
        return Decision(kind="confirm", message=message or "Approval required for this tool call")


def build_computer_permission_inspector(
    permission_config_path: Path, approvals_path: Path, *, allow_ask: bool = True
) -> ComputerAwarePermissionInspector:
    base_ruleset = _load_ruleset_fail_open(permission_config_path)
    return ComputerAwarePermissionInspector(
        base_ruleset=base_ruleset, approvals_path=approvals_path, allow_ask=allow_ask
    )


def build_persist_hook(approvals_path: Path) -> Callable[[str, str], bool]:
    """Build the ``persist`` callback for ``permission.remember_always_approval``.

    Only tools listed in ``computer.ALWAYS_SCOPE`` are ever remembered — mutating
    tools like ``computer_move``/``computer_trash`` are excluded there because
    their resource string is derived from the *source* path only, so an
    "always" rule keyed on it would silently cover any destination. Returning
    ``False`` for those tools tells ``remember_always_approval`` to skip *both*
    the durable write and the in-memory ``SessionApprovals`` remember — refusing
    only the durable half would leave the same over-broad grant alive for the
    rest of the session, which is just as wrong. A client sending
    ``always: true`` for one of those tools anyway (the app UI never offers the
    button, but nothing stops a raw API call) is a no-op: the call was already
    approved and executed once, it just isn't remembered.
    """

    def _persist(tool: str, resource: str) -> bool:
        scope = ALWAYS_SCOPE.get(tool)
        if scope is None:
            return False
        add_approval(
            approvals_path,
            tool=tool,
            resource=resource,
            scope=scope,
            created_at=datetime.datetime.now(datetime.UTC).isoformat(),
        )
        return True

    return _persist
