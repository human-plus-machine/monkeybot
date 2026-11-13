"""Firestore flat vs nested collection path resolution."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from monkeybot.core.persistence.backends import _parse_firestore_config
from monkeybot.core.persistence.firestore_layout import (
    flat_collection_id,
    firestore_monkeybot_collection,
    parse_firestore_layout,
)


def test_parse_firestore_layout_defaults_to_flat() -> None:
    assert parse_firestore_layout("") == "flat"
    assert parse_firestore_layout("flat") == "flat"
    assert parse_firestore_layout("NESTED") == "nested"


def test_parse_firestore_layout_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="invalid Firestore layout"):
        parse_firestore_layout("diagonal")


def test_flat_collection_id_matches_legacy_names() -> None:
    assert flat_collection_id("", "conversation_history") == "conversation_history"
    assert flat_collection_id("mb", "conversation_history") == "mb_conversation_history"
    assert flat_collection_id("mb", "session_turn_locks") == "mb_session_turn_locks"
    assert flat_collection_id("", "session_turn_locks") == "session_turn_locks"


def test_parse_firestore_config_reads_layout_query() -> None:
    flat = _parse_firestore_config("firestore://proj/db?prefix=student-agent")
    assert flat is not None
    assert flat.layout == "flat"
    nested = _parse_firestore_config("firestore://proj/db?prefix=student-agent&layout=nested")
    assert nested is not None
    assert nested.layout == "nested"
    assert nested.prefix == "student-agent"


def test_nested_collection_path_under_monkeybot_doc() -> None:
    client = MagicMock()
    root = MagicMock(name="root_collection")
    doc = MagicMock(name="monkeybot_doc")
    leaf = MagicMock(name="leaf_collection")
    client.collection.return_value = root
    root.document.return_value = doc
    doc.collection.return_value = leaf

    ref = firestore_monkeybot_collection(
        client,
        prefix="student-agent",
        layout="nested",
        base="conversation_history",
    )

    client.collection.assert_called_once_with("student-agent")
    root.document.assert_called_once_with("monkeybot")
    doc.collection.assert_called_once_with("conversation_history")
    assert ref is leaf


def test_nested_collection_uses_default_prefix_when_empty() -> None:
    client = MagicMock()
    root = MagicMock()
    client.collection.return_value = root
    root.document.return_value = MagicMock(collection=MagicMock(return_value="leaf"))

    firestore_monkeybot_collection(client, prefix="", layout="nested", base="threads")
    client.collection.assert_called_once_with("_default")


def test_flat_collection_uses_prefixed_name() -> None:
    client = MagicMock()
    expected = MagicMock(name="flat_collection")
    client.collection.return_value = expected

    ref = firestore_monkeybot_collection(
        client, prefix="essay-coach", layout="flat", base="turn_usage"
    )
    client.collection.assert_called_once_with("essay-coach_turn_usage")
    assert ref is expected
