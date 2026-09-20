"""Session provider/model binding precedence for verifier jobs."""

from __future__ import annotations

from types import SimpleNamespace

from monkeybot.core.verifier.binding import (
    bind_verifier_session,
    current_verifier_binding,
    reset_verifier_session,
    resolve_live_model,
    resolve_live_provider,
    resolve_session_verifier_model,
)


def test_unbound_session_is_empty() -> None:
    binding = current_verifier_binding()
    assert binding.provider is None
    assert binding.model == ""


def test_bind_pins_provider_and_model_until_reset() -> None:
    provider = SimpleNamespace(name="session")
    token = bind_verifier_session(provider, " session-model ")
    try:
        binding = current_verifier_binding()
        assert binding.provider is provider
        assert binding.model == "session-model"
    finally:
        reset_verifier_session(token)
    assert current_verifier_binding().provider is None
    assert current_verifier_binding().model == ""


def test_resolve_prefers_yaml_then_session_then_pinned() -> None:
    pinned = SimpleNamespace(model=SimpleNamespace(name="glm-5.3-flash"))
    assert resolve_session_verifier_model(pinned, None) == "glm-5.3-flash"
    token = bind_verifier_session(None, "session-model")
    try:
        assert resolve_session_verifier_model(pinned, None) == "session-model"
        assert resolve_session_verifier_model(pinned, "explicit-judge") == "explicit-judge"
        assert (
            resolve_session_verifier_model(pinned, None, session_model="from-evidence")
            == "from-evidence"
        )
        assert resolve_session_verifier_model(pinned, None, session_model="") == "glm-5.3-flash"
    finally:
        reset_verifier_session(token)
    assert resolve_session_verifier_model(pinned, None) == "glm-5.3-flash"


def test_resolve_live_model_static_beats_session_snapshot() -> None:
    assert resolve_live_model("static", "session-model") == "static"
    assert resolve_live_model(lambda: "from-callable", "session-model") == "from-callable"
    assert resolve_live_model(lambda session="": session or "pinned", "session-model") == (
        "session-model"
    )
    assert resolve_live_model(lambda session="": session or "pinned", "") == "pinned"
    assert resolve_live_provider(None) is None
    provider = SimpleNamespace(name="p")
    assert resolve_live_provider(lambda: provider) is provider
