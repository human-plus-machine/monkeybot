"""Legacy embeddinggemma-300m configs are renamed to MiniLM without a re-embed."""

from __future__ import annotations

from pathlib import Path

import pytest

from monkeybot.core.memory.migrate_embedding import (
    migrate_agent_embedding,
    migrate_all_agent_embeddings,
    rewrite_legacy_embedding_model,
)
from monkeybot.core.memory.palace import EMBEDDER_IDENTITY_FILE


def test_rewrite_replaces_legacy_name_only() -> None:
    text = (
        "memory:\n"
        "  embedding_model: embeddinggemma-300m\n"
        "  backend: chroma\n"
        "note: embeddinggemma-300m stays in this comment line? no, this is not a key\n"
    )
    rewritten = rewrite_legacy_embedding_model(text)
    assert "embedding_model: minilm\n" in rewritten
    assert "embeddinggemma-300m" in rewritten.split("note:", 1)[1]
    assert rewrite_legacy_embedding_model(rewritten) == rewritten


def test_rewrite_leaves_other_models() -> None:
    text = "memory:\n  embedding_model: embeddinggemma\n"
    assert rewrite_legacy_embedding_model(text) == text


def _write_agent(root: Path, model: str, *, identity: str | None) -> None:
    config = root / "monkeybot_config"
    config.mkdir(parents=True)
    (config / "monkeybot.yaml").write_text(
        "paths:\n"
        "  memory_storage_uri: local://./memory/mempalace\n"
        "memory:\n"
        f"  embedding_model: {model}\n",
        encoding="utf-8",
    )
    (config / "monkeybot.example.yaml").write_text(
        f"memory:\n  embedding_model: {model}\n",
        encoding="utf-8",
    )
    palace = root / "memory" / "mempalace"
    palace.mkdir(parents=True)
    (palace / "chroma.sqlite3").write_bytes(b"index")
    if identity is not None:
        (palace / EMBEDDER_IDENTITY_FILE).write_text(identity + "\n", encoding="utf-8")


def test_migrate_renames_config_and_identity_without_touching_the_index(tmp_path: Path) -> None:
    agent = tmp_path / "default"
    _write_agent(agent, "embeddinggemma-300m", identity="embeddinggemma-300m")
    index = agent / "memory" / "mempalace" / "chroma.sqlite3"
    before = index.read_bytes()

    result = migrate_agent_embedding(agent)

    assert result["action"] == "migrated"
    assert result["yaml_updated"] is True
    assert result["identity_updated"] is True
    yaml_text = (agent / "monkeybot_config" / "monkeybot.yaml").read_text()
    assert "embedding_model: minilm" in yaml_text
    assert "embeddinggemma-300m" not in yaml_text
    example = (agent / "monkeybot_config" / "monkeybot.example.yaml").read_text()
    assert "embedding_model: minilm" in example
    identity = (agent / "memory" / "mempalace" / EMBEDDER_IDENTITY_FILE).read_text()
    assert identity.strip() == "minilm"
    assert index.read_bytes() == before


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    agent = tmp_path / "default"
    _write_agent(agent, "embeddinggemma-300m", identity="embeddinggemma-300m")
    assert migrate_agent_embedding(agent)["action"] == "migrated"
    assert migrate_agent_embedding(agent)["action"] == "noop"


def test_migrate_keeps_an_explicit_gemma_palace(tmp_path: Path) -> None:
    agent = tmp_path / "default"
    _write_agent(agent, "embeddinggemma", identity="embeddinggemma")
    result = migrate_agent_embedding(agent)
    assert result["action"] == "noop"
    assert (agent / "memory" / "mempalace" / EMBEDDER_IDENTITY_FILE).read_text().strip() == (
        "embeddinggemma"
    )


def test_migrate_all_local_agents(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    alpha = home / "agents" / "alpha"
    beta = home / "agents" / "beta"
    _write_agent(alpha, "embeddinggemma-300m", identity="embeddinggemma-300m")
    _write_agent(beta, "minilm", identity="minilm")
    monkeypatch.setenv("MONKEYBOT_HOME", str(home))

    results = migrate_all_agent_embeddings()

    by_name = {Path(item["agent_root"]).name: item for item in results}
    assert by_name["alpha"]["action"] == "migrated"
    assert by_name["beta"]["action"] == "noop"
