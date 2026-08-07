"""控制面配置的信任边界测试。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from config import ServerSettings, load_settings


@pytest.mark.parametrize(
    "token",
    [
        "short",
        "contains whitespace token",
        "包含非 ASCII 字符且长度足够",
    ],
)
def test_server_settings_rejects_weak_or_header_unsafe_token(token: str) -> None:
    with pytest.raises(ValidationError, match="control_token"):
        ServerSettings.model_validate({"control_token": token})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trusted_hosts", ["*"]),
        ("trusted_hosts", ["localhost:8000"]),
        ("trusted_origins", ["https://trusted.example/path"]),
        ("trusted_origins", ["https://user:secret@trusted.example"]),
    ],
)
def test_server_settings_rejects_ambiguous_trust_rules(
    field: str,
    value: list[str],
) -> None:
    with pytest.raises(ValidationError):
        ServerSettings.model_validate({field: value})


def test_load_settings_reads_control_boundary_from_environment(
    settings,
    monkeypatch,
) -> None:
    token = "environment-control-token-value"
    monkeypatch.setenv("IJA_CONTROL_TOKEN", token)
    monkeypatch.setenv("IJA_TRUSTED_HOSTS", "localhost,127.0.0.1")
    monkeypatch.setenv(
        "IJA_TRUSTED_ORIGINS",
        "http://localhost:5173,https://console.example",
    )

    loaded = load_settings(settings.project_root)

    assert loaded.server.control_authentication_enabled is True
    assert loaded.server.control_token.get_secret_value() == token
    assert loaded.server.trusted_hosts == ["localhost", "127.0.0.1"]
    assert loaded.server.trusted_origins == [
        "http://localhost:5173",
        "https://console.example",
    ]
    assert token not in repr(loaded.server)
