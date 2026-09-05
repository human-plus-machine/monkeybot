"""Unit tests for executable playbook flows (parse, params, run, recent actions)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from browser_mcp import actions, dom_indexing, playbooks, server, tabs, backend

_SIGNUP = """```playbook
name: signup
params: [nickname]
steps:
  - {do: goto, url: "https://a.test/form.html"}
  - {do: fill_form, fields: {Nickname: "{{nickname}}"}, submit: true}
expect:
  url_contains: "/form"
```
"""


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "playbooks"
    monkeypatch.setenv("BROWSER_MCP_PLAYBOOKS_DIR", str(root))
    monkeypatch.setenv("BROWSER_MCP_QUIET_MS", "1")
    monkeypatch.setenv("BROWSER_MCP_SETTLE_MS", "200")
    original = backend._bh
    original_bound = backend._bound_cdp
    backend._bh = None
    backend._bound_cdp = None
    dom_indexing.clear_registered_targets()
    tabs.reset_registry()
    yield root
    backend._bh = original
    backend._bound_cdp = original_bound
    dom_indexing.clear_registered_targets()
    tabs.reset_registry()


def _patch_harness(helpers: MagicMock):
    return patch.object(backend, "browser_harness", return_value=(helpers, MagicMock()))


def _helpers(*, url: str = "https://a.test/form.html") -> MagicMock:
    helpers = MagicMock()
    row = {"targetId": "aaa", "target_id": "aaa", "url": url, "title": "A"}
    helpers.list_tabs.return_value = [row]
    helpers.current_tab.return_value = dict(row)
    helpers.page_info.return_value = {"url": url, "title": "A", "w": 800, "h": 600}
    helpers.js.return_value = True
    helpers.switch_tab.return_value = "sid"
    return helpers


def test_parse_render_round_trip() -> None:
    flows = playbooks.parse_flows(_SIGNUP, host="a.test")
    assert len(flows) == 1
    flow = flows[0]
    assert flow.name == "signup"
    assert flow.params == ["nickname"]
    assert flow.steps[0]["do"] == "goto"
    assert flow.expect["url_contains"] == "/form"
    again = playbooks.parse_flows(playbooks.render_flow(flow), host="a.test")
    assert again[0].name == flow.name
    assert again[0].params == flow.params
    assert again[0].steps == flow.steps
    assert again[0].expect == flow.expect


def test_parse_rejects_unknown_do() -> None:
    md = """```playbook
name: bad
steps:
  - {do: js, expression: "1"}
```
"""
    with pytest.raises(playbooks.PlaybookError, match="unknown do"):
        playbooks.parse_flows(md)


@pytest.mark.parametrize("secret", ["password", "secret", "token", "PASSWORD"])
def test_parse_rejects_secret_param_names(secret: str) -> None:
    md = f"""```playbook
name: login
params: [{secret}]
steps:
  - {{do: settle}}
```
"""
    with pytest.raises(playbooks.PlaybookError, match="secret"):
        playbooks.parse_flows(md)


def test_substitute_refuses_unknown_missing_extra_params() -> None:
    flow = playbooks.parse_flows(_SIGNUP)[0]
    with pytest.raises(playbooks.PlaybookError, match="missing params"):
        playbooks.substitute_params(flow, {})
    with pytest.raises(playbooks.PlaybookError, match="unknown params"):
        playbooks.substitute_params(flow, {"nickname": "n", "extra": "x"})
    steps = playbooks.substitute_params(flow, {"nickname": "ada"})
    assert steps[1]["fields"]["Nickname"] == "ada"


def test_substitute_unknown_placeholder_in_string() -> None:
    md = """```playbook
name: x
params: [a]
steps:
  - {do: goto, url: "https://a.test/{{b}}"}
```
"""
    flow = playbooks.parse_flows(md)[0]
    with pytest.raises(playbooks.PlaybookError, match="unknown param"):
        playbooks.substitute_params(flow, {"a": "1"})


def test_write_playbook_rejects_broken_fence_and_keeps_file(_isolated: Path) -> None:
    playbooks.write_playbook("a.test", "# notes\n")
    broken = """```playbook
name: bad
steps:
  - {do: explode}
```
"""
    with pytest.raises(playbooks.PlaybookError, match="unknown do"):
        playbooks.write_playbook("a.test", broken)
    assert playbooks.read_playbook("a.test") == "# notes\n"


def test_write_playbook_notes_only_still_writes() -> None:
    result = playbooks.write_playbook("a.test", "just notes")
    assert result["ok"] is True
    assert playbooks.read_playbook("a.test") == "just notes"


def test_list_playbooks_includes_flows() -> None:
    playbooks.write_playbook("a.test", _SIGNUP)
    listed = json.loads(server.browser_list_playbooks("a.test"))
    assert listed["ok"] is True
    assert "a.test.md" in listed["playbooks"]
    assert listed["flows"] == [{"host": "a.test", "name": "signup", "params": ["nickname"]}]


def test_run_playbook_success_and_expect() -> None:
    playbooks.write_playbook("a.test", _SIGNUP)
    helpers = _helpers()
    tree = {
        "tree": "[0]<input />",
        "elementCount": 1,
        "url": "https://a.test/form.html",
        "title": "A",
        "truncated": False,
        "below_viewport": 0,
        "omitted": 0,
    }
    with (
        _patch_harness(helpers),
        patch.object(dom_indexing, "settle", return_value={"quiet": True, "navigated": False}),
        patch.object(dom_indexing, "get_elements", return_value=tree),
        patch.object(actions, "do_goto", return_value={"ok": True, "url": "https://a.test/form.html", "title": "A"}),
        patch.object(
            actions,
            "do_fill_form",
            return_value={"ok": True, "filled": [{"label": "Nickname", "index": 1, "how": "label_for"}], "unresolved": [], "submitted": True},
        ),
    ):
        json.loads(server.browser_tabs())
        result = json.loads(
            server.browser_run_playbook("a.test", "signup", {"nickname": "ada"}, observe="diff")
        )
    assert result["ok"] is True
    assert result["name"] == "signup"
    assert len(result["completed"]) == 2
    assert result["observation"]["mode"] in {"diff", "full"}


def test_run_playbook_stale_click_text_returns_failed_step_and_observation() -> None:
    md = """```playbook
name: stale
steps:
  - {do: click_text, text: "No Such Button"}
```
"""
    playbooks.write_playbook("a.test", md)
    helpers = _helpers()
    tree = {
        "tree": "[0]<button>Other />",
        "elementCount": 1,
        "url": "https://a.test/form.html",
        "title": "A",
        "truncated": False,
        "below_viewport": 0,
        "omitted": 0,
    }
    with (
        _patch_harness(helpers),
        patch.object(dom_indexing, "settle", return_value={"quiet": True, "navigated": False}),
        patch.object(dom_indexing, "get_elements", return_value=tree),
        patch.object(
            actions,
            "do_click_text",
            return_value={"ok": False, "error": "no matching element for 'No Such Button'", "did_you_mean": []},
        ),
    ):
        json.loads(server.browser_tabs())
        result = json.loads(server.browser_run_playbook("a.test", "stale", observe="full"))
    assert result["ok"] is False
    assert result["failed_step"] == 0
    assert result.get("observation")


def test_run_playbook_timeout_between_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    md = """```playbook
name: slow
steps:
  - {do: settle}
  - {do: settle}
```
"""
    playbooks.write_playbook("a.test", md)
    monkeypatch.setenv("BROWSER_MCP_PLAYBOOK_TIMEOUT_S", "0")
    helpers = _helpers()
    tree = {
        "tree": "[0]<div />",
        "elementCount": 1,
        "url": "https://a.test/form.html",
        "title": "A",
        "truncated": False,
        "below_viewport": 0,
        "omitted": 0,
    }
    with (
        _patch_harness(helpers),
        patch.object(dom_indexing, "settle", return_value={"quiet": True, "navigated": False}),
        patch.object(dom_indexing, "get_elements", return_value=tree),
    ):
        json.loads(server.browser_tabs())
        result = json.loads(server.browser_run_playbook("a.test", "slow", observe="none"))
    assert result["ok"] is False
    assert result["error"] == "playbook timeout"
    assert result["failed_step"] == 0


def test_recent_actions_records_labels_and_lengths_not_contents() -> None:
    helpers = _helpers()
    with (
        _patch_harness(helpers),
        patch.object(
            dom_indexing,
            "fill",
            return_value={"ok": True, "tagName": "input", "mode_used": "fast"},
        ),
    ):
        json.loads(server.browser_tabs())
        json.loads(server.browser_input_by_index(3, "super-secret", observe="none"))
    listed = json.loads(server.browser_recent_actions("a.test"))
    assert listed["ok"] is True
    assert listed["host"] == "a.test"
    assert listed["actions"]
    rec = listed["actions"][-1]
    assert rec["do"] == "input"
    assert rec["index"] == 3
    assert rec["text_len"] == len("super-secret")
    assert "super-secret" not in json.dumps(listed)
    assert "text" not in rec


def test_recent_actions_caps_at_50() -> None:
    helpers = _helpers()
    with (
        _patch_harness(helpers),
        patch.object(
            dom_indexing,
            "fill",
            return_value={"ok": True, "tagName": "input", "mode_used": "fast"},
        ),
    ):
        json.loads(server.browser_tabs())
        for i in range(55):
            json.loads(server.browser_input_by_index(i, "x", observe="none"))
    listed = json.loads(server.browser_recent_actions("https://a.test/"))
    assert len(listed["actions"]) == 50
    assert listed["actions"][0]["index"] == 5
    assert listed["actions"][-1]["index"] == 54
