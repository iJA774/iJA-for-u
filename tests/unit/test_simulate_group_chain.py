import sqlite3
from pathlib import Path

import pytest

from config import ModelTaskProfile
from domain.errors import InputValidationError
from scripts.simulate_group_chain import _isolated_settings, _prepare_run_directory


def test_prepare_run_directory_copies_database_snapshot_and_persona(tmp_path: Path) -> None:
    source_data = tmp_path / "authoritative"
    source_data.mkdir()
    with sqlite3.connect(source_data / "ija.sqlite3") as database:
        database.execute("CREATE TABLE sentinel (value TEXT NOT NULL)")
        database.execute("INSERT INTO sentinel VALUES ('真实快照')")
    persona_dir = source_data / "personas"
    persona_dir.mkdir()
    (persona_dir / "active.toml").write_text(
        'group_character_id = "nailong"\n',
        encoding="utf-8",
    )

    data_dir = _prepare_run_directory(tmp_path / "simulation", source_data)

    with sqlite3.connect(data_dir / "ija.sqlite3") as database:
        assert database.execute("SELECT value FROM sentinel").fetchone() == (
            "真实快照",
        )
    assert (data_dir / "personas" / "active.toml").read_text(
        encoding="utf-8"
    ) == 'group_character_id = "nailong"\n'


def test_prepare_run_directory_rejects_authoritative_tree(tmp_path: Path) -> None:
    source_data = tmp_path / "authoritative"
    source_data.mkdir()

    with pytest.raises(InputValidationError, match="权威数据目录"):
        _prepare_run_directory(source_data / "simulation", source_data)


def test_isolated_settings_disconnects_external_side_effects_without_mutating_source(
    settings,
    tmp_path: Path,
) -> None:
    settings.platform_plugins.enabled = ["onebot"]
    settings.embedding.enabled = True

    isolated = _isolated_settings(settings, tmp_path / "simulation" / "data")

    assert isolated.storage.data_dir == tmp_path / "simulation" / "data"
    assert isolated.platform_plugins.enabled == []
    assert isolated.platform_plugins.options == {}
    assert isolated.image_model.enabled is False
    assert isolated.detection_model.enabled is False
    assert isolated.embedding.enabled is False
    assert isolated.model.task_profiles["chat.reply"].hard_timeout_seconds == 600
    assert settings.platform_plugins.enabled == ["onebot"]
    assert settings.embedding.enabled is True
    assert "chat.reply" not in settings.model.task_profiles


def test_isolated_settings_overrides_existing_chat_reply_timeout_only(
    settings,
    tmp_path: Path,
) -> None:
    settings.model.task_profiles["chat.reply"] = ModelTaskProfile(
        models=["primary-model"],
        hard_timeout_seconds=30,
    )

    isolated = _isolated_settings(
        settings,
        tmp_path / "simulation" / "data",
        bridge_timeout_seconds=120,
    )

    profile = isolated.model.task_profiles["chat.reply"]
    assert profile.models == ["primary-model"]
    assert profile.hard_timeout_seconds == 120
    assert settings.model.task_profiles["chat.reply"].hard_timeout_seconds == 30
