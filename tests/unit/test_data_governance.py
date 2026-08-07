import json
import sqlite3

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from adapters.persistence import DatabaseStore
from bootstrap import upgrade_database
from config.secrets import LocalSecretStore
from data_governance import (
    BackupValidationError,
    create_backup,
    restore_backup,
    restore_smoke,
    verify_backup,
)
from domain.models import ChatType, InboundMessage, MessageComponent, Participant


def _migration_head(settings) -> str:
    config = Config(str(settings.project_root / "alembic.ini"))
    config.set_main_option(
        "script_location",
        str(settings.project_root / "migrations"),
    )
    head = ScriptDirectory.from_config(config).get_current_head()
    assert head is not None
    return head


async def _seed_workspace(settings) -> str:
    upgrade_database(settings.project_root, settings.storage.database_path)
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    session = await store.create_session(
        platform="web-simulator",
        account_id="ija-local",
        external_chat_id="backup-private",
        chat_type=ChatType.PRIVATE,
        display_name="备份私聊",
        participants=[Participant(external_user_id="u1", display_name="小明")],
    )
    await store.append_inbound(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id="backup-message",
            external_chat_id=session.external_chat_id,
            sender_id="u1",
            sender_name="小明",
            chat_type=session.chat_type,
            components=[MessageComponent.text_component("需要被恢复的正文")],
        )
    )
    await store.close()
    media = settings.storage.data_dir / "media" / "expressions"
    media.mkdir(parents=True)
    (media / "asset.bin").write_bytes(b"restorable-media")
    logs = settings.storage.data_dir / "logs"
    logs.mkdir()
    (logs / "private.log").write_text("do-not-back-up", encoding="utf-8")
    return session.id


@pytest.mark.asyncio
async def test_backup_restore_and_smoke_are_recoverable_without_logs_or_secrets(
    settings,
) -> None:
    session_id = await _seed_workspace(settings)
    local_config = settings.project_root / "config" / "model.local.toml"
    local_config.write_text(
        '[model]\napi_key = "secret-that-must-not-enter-backup"\n',
        encoding="utf-8",
    )
    LocalSecretStore(settings.storage.data_dir).put(
        "model.api_key",
        "encrypted-secret-in-backup",
    )
    destination = settings.project_root / "backups" / "snapshot"

    created = create_backup(
        data_dir=settings.storage.data_dir,
        database_name=settings.storage.database_name,
        project_root=settings.project_root,
        destination=destination,
    )
    verified = verify_backup(destination)

    assert verified == created
    assert created.database_revision == _migration_head(settings)
    relative_files = {item.path for item in created.files}
    assert settings.storage.database_name in relative_files
    assert "media/expressions/asset.bin" in relative_files
    assert "secrets/credentials.local.json" in relative_files
    assert not any(path.startswith("logs/") for path in relative_files)
    manifest_text = (destination / "manifest.json").read_text(encoding="utf-8")
    assert "secret-that-must-not-enter-backup" not in manifest_text
    assert "encrypted-secret-in-backup" not in manifest_text
    assert "model.local.toml" not in manifest_text
    assert "encrypted-secret-in-backup" not in (
        destination / "workspace" / "secrets" / "credentials.local.json"
    ).read_text(encoding="utf-8")

    restored_root = settings.project_root / "restored-data"
    restored = restore_backup(
        backup_root=destination,
        target_data_dir=restored_root,
    )
    assert restored == created
    assert (
        restored_root / "media" / "expressions" / "asset.bin"
    ).read_bytes() == b"restorable-media"
    restored_store = DatabaseStore(restored_root / settings.storage.database_name)
    await restored_store.initialize()
    session = await restored_store.get_session(session_id)
    assert session is not None
    messages = await restored_store.list_messages(session_id)
    assert [item.plain_text for item in messages] == ["需要被恢复的正文"]
    await restored_store.close()

    assert restore_smoke(
        backup_root=destination,
        project_root=settings.project_root,
    ) == created


@pytest.mark.asyncio
async def test_wal_backup_merges_commits_and_never_restores_sqlite_sidecars(
    settings,
) -> None:
    """在线备份必须吸收 WAL 已提交页，但不得复制或发布源 sidecar。"""

    await _seed_workspace(settings)
    source = sqlite3.connect(settings.storage.database_path)
    try:
        assert source.execute("PRAGMA journal_mode=WAL").fetchone() == ("wal",)
        source.execute("PRAGMA wal_autocheckpoint=0")
        source.execute(
            "CREATE TABLE wal_restore_probe "
            "(id INTEGER PRIMARY KEY, value TEXT NOT NULL)"
        )
        source.execute(
            "INSERT INTO wal_restore_probe(value) VALUES (?)",
            ("已提交到 WAL 的恢复探针",),
        )
        source.commit()
        assert settings.storage.database_path.with_name(
            f"{settings.storage.database_name}-wal"
        ).is_file()
        assert settings.storage.database_path.with_name(
            f"{settings.storage.database_name}-shm"
        ).is_file()

        destination = settings.project_root / "backups" / "wal-snapshot"
        manifest = create_backup(
            data_dir=settings.storage.data_dir,
            database_name=settings.storage.database_name,
            project_root=settings.project_root,
            destination=destination,
        )
    finally:
        source.close()

    sidecars = {
        f"{settings.storage.database_name}-wal",
        f"{settings.storage.database_name}-shm",
        f"{settings.storage.database_name}-journal",
    }
    assert sidecars.isdisjoint(item.path for item in manifest.files)
    assert all(
        not (destination / "workspace" / name).exists()
        for name in sidecars
    )

    restored_root = settings.project_root / "wal-restored"
    restore_backup(
        backup_root=destination,
        target_data_dir=restored_root,
    )
    restored = sqlite3.connect(restored_root / settings.storage.database_name)
    try:
        assert restored.execute(
            "SELECT value FROM wal_restore_probe"
        ).fetchone() == ("已提交到 WAL 的恢复探针",)
        assert restored.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    finally:
        restored.close()
    assert all(not (restored_root / name).exists() for name in sidecars)


@pytest.mark.asyncio
async def test_manifest_cannot_authorize_sqlite_sidecar_restore(settings) -> None:
    """即使 sidecar 与 manifest 同时被篡改，恢复校验也必须显式拒绝。"""

    await _seed_workspace(settings)
    destination = settings.project_root / "backups" / "sidecar-manifest"
    create_backup(
        data_dir=settings.storage.data_dir,
        database_name=settings.storage.database_name,
        project_root=settings.project_root,
        destination=destination,
    )
    # Windows 路径大小写不敏感；大小写变体也不能绕过 sidecar 拒绝规则。
    sidecar_name = f"{settings.storage.database_name.upper()}-WAL"
    (destination / "workspace" / sidecar_name).write_bytes(b"")
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"].append(
        {
            "path": sidecar_name,
            "size": 0,
            "sha256": (
                "e3b0c44298fc1c149afbf4c8996fb924"
                "27ae41e4649b934ca495991b7852b855"
            ),
        }
    )
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(BackupValidationError, match="SQLite sidecar"):
        restore_backup(
            backup_root=destination,
            target_data_dir=settings.project_root / "should-not-restore",
        )


@pytest.mark.asyncio
async def test_backup_tampering_and_unconfirmed_overwrite_fail_loudly(
    settings,
) -> None:
    await _seed_workspace(settings)
    destination = settings.project_root / "backups" / "tamper"
    create_backup(
        data_dir=settings.storage.data_dir,
        database_name=settings.storage.database_name,
        project_root=settings.project_root,
        destination=destination,
    )
    target = settings.project_root / "existing-data"
    target.mkdir()
    (target / "stale.txt").write_text("keep-until-confirmed", encoding="utf-8")

    with pytest.raises(FileExistsError, match="显式 allow_replace"):
        restore_backup(
            backup_root=destination,
            target_data_dir=target,
        )
    assert (target / "stale.txt").read_text(encoding="utf-8") == "keep-until-confirmed"

    asset = destination / "workspace" / "media" / "expressions" / "asset.bin"
    asset.write_bytes(b"tampered")
    with pytest.raises(BackupValidationError, match="大小不匹配|哈希不匹配"):
        verify_backup(destination)


@pytest.mark.asyncio
async def test_restore_rejects_manifest_path_traversal(settings) -> None:
    await _seed_workspace(settings)
    destination = settings.project_root / "backups" / "traversal"
    create_backup(
        data_dir=settings.storage.data_dir,
        database_name=settings.storage.database_name,
        project_root=settings.project_root,
        destination=destination,
    )
    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../outside"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(BackupValidationError, match="相对路径"):
        verify_backup(destination)
