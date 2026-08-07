import base64
import shutil
import tomllib
from pathlib import Path

import pytest
import tomli_w

from config import AppSettings


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AppSettings:
    # 测试只在隔离目录使用确定性主密钥，绝不触碰真实系统凭据或用户配置。
    monkeypatch.setenv(
        "IJA_SECRET_MASTER_KEY",
        base64.b64encode(b"0" * 32).decode("ascii"),
    )
    source_root = Path(__file__).parents[1]
    shutil.copytree(source_root / "prompts" / "common", tmp_path / "prompts" / "common")
    shutil.copytree(source_root / "prompts" / "scenes", tmp_path / "prompts" / "scenes")
    shutil.copytree(source_root / "prompts" / "persona", tmp_path / "prompts" / "persona")
    shutil.copytree(source_root / "prompts" / "tasks", tmp_path / "prompts" / "tasks")
    shutil.copytree(source_root / "prompts" / "channels", tmp_path / "prompts" / "channels")
    shutil.copytree(source_root / "skills", tmp_path / "skills")
    shutil.copytree(source_root / "config" / "personas", tmp_path / "config" / "personas")
    persona_config_path = tmp_path / "config" / "personas" / "default.toml"
    persona_config = tomllib.loads(persona_config_path.read_text(encoding="utf-8"))
    # 测试不得读取真实用户形象；各表情测试按需在临时目录创建独立图片。
    persona_config.pop("portrait", None)
    persona_config_path.write_text(tomli_w.dumps(persona_config), encoding="utf-8")
    shutil.copy2(source_root / "config" / "default.toml", tmp_path / "config" / "default.toml")
    shutil.copytree(source_root / "migrations", tmp_path / "migrations")
    shutil.copy2(source_root / "alembic.ini", tmp_path / "alembic.ini")
    return AppSettings.model_validate(
        {
            "project_root": tmp_path,
            "server": {"trusted_hosts": ["127.0.0.1", "localhost", "::1", "testserver"]},
            "storage": {"data_dir": tmp_path / "data", "database_name": "test.sqlite3"},
            "model": {"mode": "fake"},
            "chat": {
                "private_debounce_ms": 10000,
                "group_debounce_ms": 10000,
                "group_reply_threshold": 80,
                "group_trigger_count": 3,
                "group_frequency_factor": 0.9,
            },
        }
    )
