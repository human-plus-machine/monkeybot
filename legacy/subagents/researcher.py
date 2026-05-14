"""Research subagent — reads SubagentEnvelope from stdin, runs a minimal research loop."""
from __future__ import annotations

import sys
import traceback

sys.path.insert(0, "src")

from monkeybot.core.events import AssistantDelta, ErrorEvent, TurnComplete
from monkeybot.core.subagent_proto import emit_event, read_envelope_from_stdin


def main() -> None:
    """Entry point: read envelope, emit research delta, then signal turn complete."""
    envelope = read_envelope_from_stdin()
    run_id = envelope.run_id
    try:
        emit_event(AssistantDelta(text=f"Researching: {envelope.task}"))
        emit_event(TurnComplete(run_id=run_id))
    except Exception:
        traceback.print_exc(file=sys.stderr)
        emit_event(ErrorEvent(message="researcher error", recoverable=False))
        sys.exit(1)


main()
