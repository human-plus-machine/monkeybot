"""Minimal subagent for testing: reads envelope, emits TurnComplete, exits 0."""
from __future__ import annotations

import sys

sys.path.insert(0, "src")

from monkeybot.core.events import TurnComplete
from monkeybot.core.subagent_proto import emit_event, read_envelope_from_stdin

envelope = read_envelope_from_stdin()
emit_event(TurnComplete(run_id=envelope.run_id))
