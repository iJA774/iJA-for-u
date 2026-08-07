from config import EmbeddingSettings


def test_embedding_defaults_enabled_without_claiming_remote_availability() -> None:
    """首启默认打开开关，但空配置不能创建会发送正文的 Provider。"""

    settings = EmbeddingSettings()

    assert settings.enabled is True
    assert settings.available is False


def test_embedding_becomes_available_only_when_model_and_key_are_complete() -> None:
    """统一配置必须同时具备启用意图、模型名和密钥。"""

    assert EmbeddingSettings(name="text-embedding", api_key="secret").available is True
    assert (
        EmbeddingSettings(enabled=False, name="text-embedding", api_key="secret").available
        is False
    )
    assert EmbeddingSettings(name="text-embedding").available is False
    assert EmbeddingSettings(api_key="secret").available is False
