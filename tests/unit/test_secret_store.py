import base64
import os
import secrets as stdlib_secrets
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path

import pytest

from config import (
    ModelSettings,
    control_login_url,
    ensure_control_token,
    load_persisted_model_settings,
    load_settings,
    migrate_legacy_secret_files,
    save_model_settings,
)
from config.secrets import LocalSecretStore, SecretStoreError


def _create_control_token_in_child(data_dir: str) -> tuple[str, bool]:
    """由独立进程竞争同一引用，真实覆盖操作系统文件锁。"""

    return LocalSecretStore(Path(data_dir)).get_or_create(
        "server.control_token",
        lambda: stdlib_secrets.token_urlsafe(32),
    )


def test_aesgcm_secret_store_round_trip_and_ciphertext_has_no_plaintext(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setenv(
        "IJA_SECRET_MASTER_KEY",
        base64.b64encode(b"k" * 32).decode("ascii"),
    )
    store = LocalSecretStore(tmp_path)
    store.put("model.api_key", "sensitive-value-for-test")

    assert store.get("model.api_key") == "sensitive-value-for-test"
    raw = store.path.read_text(encoding="utf-8")
    assert "sensitive-value-for-test" not in raw
    assert '"backend": "aesgcm-v1"' in raw

    store.delete("model.api_key")
    with pytest.raises(SecretStoreError, match="引用不存在"):
        store.get("model.api_key")


def test_secret_store_rejects_wrong_master_key(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "IJA_SECRET_MASTER_KEY",
        base64.b64encode(b"a" * 32).decode("ascii"),
    )
    store = LocalSecretStore(tmp_path)
    store.put("embedding.api_key", "embedding-secret")
    monkeypatch.setenv(
        "IJA_SECRET_MASTER_KEY",
        base64.b64encode(b"b" * 32).decode("ascii"),
    )

    with pytest.raises(SecretStoreError, match="无法解密"):
        store.get("embedding.api_key")


def test_control_token_is_generated_once_and_only_persisted_as_ciphertext(
    settings,
) -> None:
    assert ensure_control_token(settings) is True
    token = settings.server.control_token.get_secret_value()
    assert len(token) >= 32

    secret_file = settings.storage.data_dir / "secrets" / "credentials.local.json"
    assert token not in secret_file.read_text(encoding="utf-8")
    reloaded = load_settings(settings.project_root)
    assert reloaded.server.control_token.get_secret_value() == token
    assert ensure_control_token(reloaded) is False

    login_url = control_login_url(reloaded)
    assert login_url.startswith("http://127.0.0.1:")
    assert login_url.endswith(f"/#control_token={token}")


def test_concurrent_first_startup_reuses_one_control_token(settings) -> None:
    first = settings.model_copy(deep=True)
    second = settings.model_copy(deep=True)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(ensure_control_token, candidate)
            for candidate in (first, second)
        ]
        generated = [future.result(timeout=5) for future in futures]

    assert sorted(generated) == [False, True]
    assert (
        first.server.control_token.get_secret_value()
        == second.server.control_token.get_secret_value()
    )


def test_concurrent_processes_share_one_control_token(settings) -> None:
    with ProcessPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                _create_control_token_in_child,
                str(settings.storage.data_dir),
            )
            for _ in range(2)
        ]
        outcomes = [future.result(timeout=20) for future in futures]

    assert len({token for token, _ in outcomes}) == 1
    assert sorted(created for _, created in outcomes) == [False, True]


def test_model_config_persists_only_references_and_resolves_after_restart(
    settings,
) -> None:
    save_model_settings(
        settings.project_root,
        ModelSettings(
            mode="openai",
            base_url="https://model.example/v1",
            api_key="chat-secret",
            name="chat-model",
            profile_api_key="profile-secret",
        ),
        data_dir=settings.storage.data_dir,
    )

    local = (
        settings.project_root / "config" / "model.local.toml"
    ).read_text(encoding="utf-8")
    assert "chat-secret" not in local
    assert "profile-secret" not in local
    reloaded = load_persisted_model_settings(
        settings.project_root,
        data_dir=settings.storage.data_dir,
    )
    assert reloaded.api_key == "chat-secret"
    assert reloaded.profile_api_key == "profile-secret"


def test_legacy_plaintext_migration_is_explicit_and_removes_plaintext(
    settings,
) -> None:
    path = settings.project_root / "config" / "model.local.toml"
    path.write_text(
        """
[model]
mode = "openai"
base_url = "https://model.example/v1"
api_key = "legacy-chat-secret"
name = "chat-model"
profile_api_key = "legacy-profile-secret"
""".strip(),
        encoding="utf-8",
    )

    migrated = migrate_legacy_secret_files(
        settings.project_root,
        data_dir=settings.storage.data_dir,
    )

    assert migrated == ["model.local.toml"]
    rewritten = path.read_text(encoding="utf-8")
    assert "legacy-chat-secret" not in rewritten
    assert "legacy-profile-secret" not in rewritten
    assert load_persisted_model_settings(
        settings.project_root,
        data_dir=settings.storage.data_dir,
    ).api_key == "legacy-chat-secret"


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI 专属契约")
def test_windows_dpapi_secret_store_round_trip_without_external_master_key(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("IJA_SECRET_MASTER_KEY", raising=False)
    store = LocalSecretStore(tmp_path)
    store.put("vision_model.api_key", "dpapi-secret")

    assert store.get("vision_model.api_key") == "dpapi-secret"
    assert "dpapi-secret" not in store.path.read_text(encoding="utf-8")
