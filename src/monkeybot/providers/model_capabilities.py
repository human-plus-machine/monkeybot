"""Per-model sampling capability table for Anthropic-family providers.

Newer Claude models (Sonnet 5, Opus 4.7+) reject non-default ``temperature``
and manual extended-thinking ``budget_tokens`` with HTTP 400. This table is
the fast path: known model prefixes are gated here so no failed request is
needed. For models not yet listed (a new release ahead of this table), the
matching retry-and-strip fallback in :func:`iter_anthropic_sdk_stream
<monkeybot.providers._utils.iter_anthropic_sdk_stream>` is the safety net —
this table does not need to be exhaustive to stay correct, only to stay fast.
"""

from __future__ import annotations

# Sampling/thinking params each model prefix is known to reject with HTTP 400.
# Default for an unlisted prefix: supports everything (matches every Claude
# model before Sonnet 5 / Opus 4.7).
UNSUPPORTED_SAMPLING_PARAMS: dict[str, frozenset[str]] = {
    "claude-sonnet-5": frozenset({"temperature", "thinking_budget_tokens"}),
    "claude-opus-4-7": frozenset({"temperature", "thinking_budget_tokens"}),
    "claude-opus-4-8": frozenset({"temperature", "thinking_budget_tokens"}),
}


def supports_param(model: str, param: str) -> bool:
    """True unless ``model`` is a known prefix match that rejects ``param``.

    Exact-prefix, longest-match wins — same lookup shape as
    :func:`monkeybot.providers.pricing.pricing_for`.
    """
    best_key: str | None = None
    for key in UNSUPPORTED_SAMPLING_PARAMS:
        if model.startswith(key) and (best_key is None or len(key) > len(best_key)):
            best_key = key
    if best_key is None:
        return True
    return param not in UNSUPPORTED_SAMPLING_PARAMS[best_key]
