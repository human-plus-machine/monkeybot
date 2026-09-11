"""Ask-to-read for an out-of-workspace path — the "grant a folder" front door.

Companion to the workspace jail (`core_tool_executor.py::CoreToolExecutor._jail_roots`)
and the command-grant ask in `CommandTierInspector`: closing the ability to
smuggle a file out of the workspace only helps if there is a legitimate way
to reach it instead. Before this inspector, `read_file`/`load_file`/`glob`/
`grep` simply rejected any absolute or `~` path with a hint telling the model
to rewrite it as workspace-relative — which is exactly the nudge that made
"move it into the workspace, read it, move it back" look like the sanctioned
recovery. Now an out-of-workspace path asks instead, once, and — unlike
`computer_move` — the file is never relocated: `WorkspaceFileService`'s
`extra_read_roots` (see `workspace_service.py`) lets these four tools read a
granted folder in place.

Applies only to `read_file`, `load_file`, `glob`, `grep` (see
`grant_store.PATH_GRANT_TOOLS`) and only when the path/root argument is
absolute or `~`-prefixed — a workspace-relative path never reaches this
inspector at all, since it can only ever resolve inside the workspace.
"""

from __future__ import annotations

from pathlib import Path

from monkeybot.computer.safety import is_credential_path, is_within
from monkeybot.core.context import TurnContext
from monkeybot.core.tools.grant_store import PATH_GRANT_TOOLS, GrantStoreCache
from monkeybot.core.tools.inspector import Decision, InspectorToolCall


def _candidate_path(call: InspectorToolCall) -> str | None:
    """The path/root argument, whichever this tool shape uses."""
    for key in ("path", "root"):
        value = call.args.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _looks_absolute_or_home(raw: str) -> bool:
    s = raw.replace("\\", "/")
    return s.startswith("/") or s.startswith("~")


class PathGrantInspector:
    """``ToolInspector`` that turns an out-of-workspace read into an ask.

    Reads `grants.json` through the same mtime-cached accessor
    `CommandTierInspector` uses for command grants, so a folder approved in
    Settings (or by an earlier "Always allow" click) takes effect on the
    next call without a gateway restart.
    """

    def __init__(self, *, workspace_root: Path, grants_path: Path | None = None) -> None:
        self._workspace_root = workspace_root.resolve()
        self._grants_cache = GrantStoreCache(grants_path) if grants_path is not None else None

    def _granted_folders(self, ctx: TurnContext) -> set[Path]:
        folders = {Path(p).resolve() for p in ctx.turn_path_grants}
        if self._grants_cache is not None:
            folders |= {Path(p.path).resolve() for p in self._grants_cache.get().paths}
        return folders

    async def check(self, call: InspectorToolCall, ctx: TurnContext) -> Decision:
        if call.name not in PATH_GRANT_TOOLS:
            return Decision(kind="allow")
        raw = _candidate_path(call)
        if raw is None or not _looks_absolute_or_home(raw):
            return Decision(kind="allow")

        resolved = Path(raw).expanduser().resolve()

        if resolved == self._workspace_root or is_within(resolved, self._workspace_root):
            # Redundant with today's workspace-relative-only requirement (an
            # absolute in-workspace path is still rejected downstream with a
            # "use a relative path" hint) — but never this inspector's call to
            # deny or ask for something already fully in reach.
            return Decision(kind="allow")

        try:
            home = Path.home().resolve()
        except RuntimeError:
            home = None
        if home is None or not (resolved == home or is_within(resolved, home)):
            return Decision(
                kind="deny",
                message="This path is outside the user's home directory and is always denied.",
            )

        # The same credential/keychain/browser-profile denylist
        # `computer_list_dir`/`computer_find` filter their results through
        # (see `is_credential_path`), so grant-time and per-result
        # enforcement (`WorkspaceFileService._is_denied_extra_root_result`)
        # can never disagree about what's protected.
        if is_credential_path(resolved):
            return Decision(
                kind="deny",
                message=(
                    "This path is inside a protected directory (credentials, "
                    "keychains, browser profiles, or app-internal state) and "
                    "is always denied, regardless of approval."
                ),
            )

        folder = resolved if resolved.is_dir() else resolved.parent
        granted = self._granted_folders(ctx)
        if any(folder == g or is_within(folder, g) for g in granted):
            return Decision(kind="allow")

        return Decision(
            kind="confirm",
            message=f"Allow reading files in {folder}?",
            grant_key=str(folder),
            grant_kind="path",
        )
