"""Per-model sampling capability table for Anthropic-family providers.

Newer Claude models (Sonnet 5, Opus 4.7+) reject non-default ``temperature``
and manual extended-thinking ``budget_tokens`` with HTTP 400. This table is
the fast path: known model prefixes are gated here so no failed request is
needed. For models not yet listed (a new release ahead of this table), the
matching retry-and-strip fallback in :func:`iter_anthropic_sdk_stream
<monkeybot.providers._utils.iter_anthropic_sdk_stream>` is the safety net —
this table does not need to be exhaustive to stay correct, only to stay fast.

Param names match the request kwargs the providers build (``temperature``,
``thinking``) so the table and the fallback speak one vocabulary.
"""

from __future__ import annotations

import re

# Sampling/thinking kwargs each model prefix is known to reject with HTTP 400.
# Default for an unlisted prefix: supports everything (matches every Claude
# model before Sonnet 5 / Opus 4.7).
UNSUPPORTED_SAMPLING_PARAMS: dict[str, frozenset[str]] = {
    "claude-sonnet-5": frozenset({"temperature", "thinking"}),
    "claude-opus-4-7": frozenset({"temperature", "thinking"}),
    "claude-opus-4-8": frozenset({"temperature", "thinking"}),
}

# Bedrock ids namespace the model: ``anthropic.claude-...`` and cross-region
# inference profiles like ``us.anthropic.claude-...``. Vertex ids are already
# bare (``claude-sonnet-5@20260101``).
_BEDROCK_NAMESPACE = re.compile(r"^(?:[a-z]{2,4}\.)?anthropic\.")


def normalize_model(model: str) -> str:
    """Strip provider-specific namespacing so prefix keys match everywhere."""
    return _BEDROCK_NAMESPACE.sub("", model)


def supports_param(model: str, param: str) -> bool:
    """True unless ``model`` is a known prefix match that rejects ``param``.

    Exact-prefix, longest-match wins — same lookup shape as
    :func:`monkeybot.providers.pricing.pricing_for`.
    """
    name = normalize_model(model)
    best_key: str | None = None
    for key in UNSUPPORTED_SAMPLING_PARAMS:
        if name.startswith(key) and (best_key is None or len(key) > len(best_key)):
            best_key = key
    if best_key is None:
        return True
    return param not in UNSUPPORTED_SAMPLING_PARAMS[best_key]
