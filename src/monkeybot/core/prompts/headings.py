"""Volatile-tail section headings, in one import-free leaf module.

These live here rather than beside the code that composes each section because
two unrelated layers need them: the prompt composer (``core.prompts.prompt``,
``core.context.epoch``) and the Anthropic cache-block splitter
(``providers._utils.split_system_prompt_for_cache``), which locates the
stable/volatile boundary in an already-flattened prompt string.

Importing the composing modules from ``providers`` would cycle
(``core.context`` -> ``core.config.settings`` -> ``providers.claude``), which is
why the literals used to be hand-duplicated in ``providers._utils``. This module
imports nothing, so both sides can share one definition and the duplication —
along with the whole class of silent drift it invited — is gone.

A heading may carry a prose body after its ``## Title`` line. Callers matching
against a composed prompt want the title line only; use
``heading_marker`` rather than slicing these strings by hand.
"""

from __future__ import annotations

CURRENT_DATE_HEADING = "\n\n## Current date\n"
MEMORY_INDEX_HEADING = (
    "\n\n## Memory index\n"
    "Stored memories from past sessions; entries are titles — "
    "`search_memory` retrieves the full note.\n"
)
MEMORY_NUDGE_HEADING = "\n\n## Memory\n"
SKILLS_HEADING = "\n\n## Skills\n"
TODO_LIST_HEADING = "\n\n## Todo list\n"
CURRENT_REQUEST_HEADING = "\n\n## Current request\n"
RUNTIME_NOTES_HEADING = "\n\n## Runtime notes\n"
SYSTEM_CONTEXT_UPDATE_HEADING = "\n\n## System context update\n"

#: Every heading that starts the volatile (non-cacheable) tail of a prompt.
VOLATILE_SECTION_HEADINGS = (
    CURRENT_DATE_HEADING,
    MEMORY_INDEX_HEADING,
    MEMORY_NUDGE_HEADING,
    SKILLS_HEADING,
    TODO_LIST_HEADING,
    CURRENT_REQUEST_HEADING,
    RUNTIME_NOTES_HEADING,
    SYSTEM_CONTEXT_UPDATE_HEADING,
)


def heading_marker(heading: str) -> str:
    """The ``\\n\\n## Title\\n`` prefix of a heading, dropping any prose body.

    Matching on the title line alone keeps prompt-wording edits from breaking
    boundary detection.
    """
    return heading[: heading.index("\n", 2) + 1]


#: Title-line-only markers for locating the volatile tail in a composed prompt.
VOLATILE_SECTION_MARKERS = tuple(heading_marker(h) for h in VOLATILE_SECTION_HEADINGS)
