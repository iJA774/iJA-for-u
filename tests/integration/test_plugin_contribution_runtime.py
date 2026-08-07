from pathlib import Path

import pytest

from adapters.model import FakeModelProvider
from bootstrap import build_runtime


def _install_fixture_plugin(project_root: Path) -> None:
    """在隔离项目中放入无需宿主名称特判的插件测试包。"""

    plugin = project_root / "plugins" / "portable"
    skill = plugin / "skills" / "portable-guide"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: portable-guide\n"
        "description: 自动发现测试 Skill\n"
        "---\n"
        "这是由插件目录贡献的说明。",
        encoding="utf-8",
    )
    (plugin / "worker.py").write_text(
        "import json\n"
        "import sys\n"
        "print(json.dumps({'type': 'ready', 'api_version': 1, 'service': 'worker'}), flush=True)\n"
        "for line in sys.stdin:\n"
        "    message = json.loads(line)\n"
        "    if message.get('type') == 'shutdown':\n"
        "        break\n"
        "    print(json.dumps({\n"
        "        'type': 'response',\n"
        "        'request_id': message['request_id'],\n"
        "        'result': {'available': True},\n"
        "    }), flush=True)\n",
        encoding="utf-8",
    )
    (plugin / "plugin.toml").write_text(
        'id = "portable"\n'
        'name = "自动发现测试插件"\n'
        'version = "0.1.0"\n'
        "\n"
        "[contributes]\n"
        'skill_roots = ["skills"]\n'
        "\n"
        "[[managed_services]]\n"
        'name = "worker"\n'
        'command = ["{python}", "worker.py"]\n'
        'capabilities = ["portable-worker"]\n',
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_runtime_cold_installs_and_stops_plugin_contributions(settings) -> None:
    _install_fixture_plugin(settings.project_root)
    runtime = build_runtime(settings, model_override=FakeModelProvider())
    assert "portable-guide" in runtime.skills.names
    assert "portable-worker" in runtime.managed_services.capabilities

    await runtime.start()
    client = runtime.managed_services.capabilities["portable-worker"]
    try:
        assert client.running is True
        assert await client.request("ping", {}) == {"available": True}
    finally:
        await runtime.stop()
    assert client.running is False
