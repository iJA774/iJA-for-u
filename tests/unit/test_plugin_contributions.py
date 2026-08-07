import asyncio
import logging
import os
import shutil
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from config.settings import PlatformPluginSettings
from domain.errors import InputValidationError, NotFoundError
from plugins._host import (
    ChannelPluginManager,
    ChannelRouter,
    ManagedServiceClient,
    ManagedServiceError,
    ManagedServiceManager,
    PluginContributionCatalog,
)
from plugins._host.managed_services import MAX_MESSAGE_BYTES
from ports import ModelMessage
from ports.egress import EgressEnvelope
from skill_runtime import SkillCatalog


def _write_portable_plugin(project_root: Path) -> Path:
    plugin = project_root / "plugins" / "portable"
    skill = plugin / "skills" / "portable-guide"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: portable-guide\n"
        "description: 测试插件贡献的普通 Skill\n"
        "---\n"
        "先读取要求，再执行。",
        encoding="utf-8",
    )
    (plugin / "worker.py").write_text(
        "import json\n"
        "import os\n"
        "import sys\n"
        "import time\n"
        "print(json.dumps({'type': 'ready', 'api_version': 1, 'service': 'worker'}), flush=True)\n"
        "for line in sys.stdin:\n"
        "    message = json.loads(line)\n"
        "    if message.get('type') == 'shutdown':\n"
        "        break\n"
        "    if message['method'] == 'fail':\n"
        "        print(json.dumps({\n"
        "            'type': 'response',\n"
        "            'request_id': message['request_id'],\n"
        "            'error': {'type': 'Denied', 'message': 'explicit failure'},\n"
        "        }), flush=True)\n"
        "        continue\n"
        "    if message['method'] == 'slow':\n"
        "        time.sleep(0.15)\n"
        "    if message['method'] == 'very-slow':\n"
        "        time.sleep(0.3)\n"
        "    if message['method'] == 'large-ok':\n"
        "        print(json.dumps({\n"
        "            'type': 'response',\n"
        "            'request_id': message['request_id'],\n"
        "            'result': {'blob': 'x' * 70000},\n"
        "        }), flush=True)\n"
        "        continue\n"
        "    if message['method'] == 'large-too':\n"
        "        print(json.dumps({\n"
        "            'type': 'response',\n"
        "            'request_id': message['request_id'],\n"
        "            'result': {'blob': 'x' * 1048700},\n"
        "        }), flush=True)\n"
        "        continue\n"
        "    result = {\n"
        "        'params': message['params'],\n"
        "        'secret_visible': 'IJA_TEST_SECRET' in os.environ,\n"
        "        'project_root': sys.argv[1],\n"
        "    }\n"
        "    print(json.dumps({\n"
        "        'type': 'response',\n"
        "        'request_id': message['request_id'],\n"
        "        'result': result,\n"
        "    }), flush=True)\n",
        encoding="utf-8",
    )
    (plugin / "plugin.toml").write_text(
        'id = "portable"\n'
        'name = "可移植测试插件"\n'
        'kind = "extension"\n'
        'version = "0.1.0"\n'
        'entrypoint = "runtime:create_plugin"\n'
        "\n"
        "[contributes]\n"
        'skill_roots = ["skills"]\n'
        "\n"
        "[[managed_services]]\n"
        'name = "worker"\n'
        'command = ["{python}", "worker.py", "{project_root}"]\n'
        'working_directory = "."\n'
        'capabilities = ["portable-worker"]\n'
        "startup_timeout_seconds = 5\n"
        "request_timeout_seconds = 5\n"
        "shutdown_timeout_seconds = 2\n",
        encoding="utf-8",
    )
    return plugin


def _write_hot_egress(project_root: Path, suffix: str, *, broken: bool = False) -> None:
    plugin = project_root / "plugins" / "hot_filter"
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "plugin.toml").write_text(
        'id = "hot_filter"\n'
        'kind = "egress"\n'
        'entrypoint = "runtime:create_plugin"\n'
        'activation = "automatic"\n',
        encoding="utf-8",
    )
    source = (
        "这不是合法 Python !!!"
        if broken
        else (
            "class Plugin:\n"
            "    def __init__(self, context): self.plugin_id = context.plugin_id\n"
            "    async def start(self): pass\n"
            "    async def stop(self): pass\n"
            "    async def filter(self, envelope, call_next):\n"
            f"        return (await call_next(envelope)) + {suffix!r}\n"
            "def create_plugin(context): return Plugin(context)\n"
        )
    )
    (plugin / "runtime.py").write_text(source, encoding="utf-8")


def _write_phased_channel(project_root: Path) -> None:
    plugin = project_root / "plugins" / "phased_channel"
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "plugin.toml").write_text(
        'id = "phased_channel"\n'
        'kind = "channel"\n'
        'entrypoint = "runtime:create_plugin"\n'
        'activation = "automatic"\n'
        '[capabilities]\n'
        'platform = "phase"\n'
        'display_name = "阶段测试"\n'
        'limitations = ["仅用于生命周期测试。"]\n'
        '[capabilities.ingress]\n'
        'text = "supported"\nmention = "unsupported"\nquote = "unsupported"\n'
        'image = "unsupported"\naudio = "unsupported"\nfile = "unsupported"\n'
        'forward = "unsupported"\n'
        '[capabilities.processing]\n'
        'image_description = "unsupported"\nexpression_asset = "unsupported"\n'
        'audio_transcript = "unsupported"\nocr_text = "unsupported"\n'
        'forward_expansion = "unsupported"\n'
        '[capabilities.prompt_projection]\n'
        'text = "supported"\nmention = "unsupported"\nquote = "unsupported"\n'
        'image_description = "unsupported"\naudio_transcript = "unsupported"\n'
        'file_metadata = "unsupported"\nforward_expansion = "unsupported"\n'
        '[capabilities.egress]\n'
        'text = "supported"\nimage = "unsupported"\naudio = "unsupported"\n'
        'file = "unsupported"\n',
        encoding="utf-8",
    )
    (plugin / "runtime.py").write_text(
        "class Plugin:\n"
        "    def __init__(self, context):\n"
        "        self.plugin_id = context.plugin_id\n"
        "        self.platform = 'phase'\n"
        "        self.account_id = 'candidate'\n"
        "        self.context = context\n"
        "        self.calls = []\n"
        "    async def prepare(self): self.calls.append('prepare')\n"
        "    async def activate(self): self.calls.append('activate')\n"
        "    async def ready(self): self.calls.append('ready')\n"
        "    async def deactivate(self): self.calls.append('deactivate')\n"
        "def create_plugin(context): return Plugin(context)\n",
        encoding="utf-8",
    )


def _write_partial_start_egress(project_root: Path) -> None:
    plugin = project_root / "plugins" / "partial_start"
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "plugin.toml").write_text(
        'id = "partial_start"\n'
        'kind = "egress"\n'
        'entrypoint = "runtime:create_plugin"\n'
        'activation = "automatic"\n',
        encoding="utf-8",
    )
    (plugin / "runtime.py").write_text(
        "class Plugin:\n"
        "    def __init__(self, context):\n"
        "        self.plugin_id = context.plugin_id\n"
        "        self.start_calls = 0\n"
        "        self.stop_calls = 0\n"
        "        self.resource_open = False\n"
        "    async def start(self):\n"
        "        self.start_calls += 1\n"
        "        self.resource_open = True\n"
        "        raise RuntimeError('部分启动失败')\n"
        "    async def stop(self):\n"
        "        self.stop_calls += 1\n"
        "        self.resource_open = False\n"
        "    async def filter(self, envelope, call_next):\n"
        "        return await call_next(envelope)\n"
        "def create_plugin(context): return Plugin(context)\n",
        encoding="utf-8",
    )


def _write_phased_egress(project_root: Path, suffix: str) -> None:
    plugin = project_root / "plugins" / "phased_filter"
    plugin.mkdir(parents=True, exist_ok=True)
    (plugin / "plugin.toml").write_text(
        'id = "phased_filter"\n'
        'kind = "egress"\n'
        'entrypoint = "runtime:create_plugin"\n'
        'activation = "automatic"\n',
        encoding="utf-8",
    )
    (plugin / "runtime.py").write_text(
        "class Plugin:\n"
        "    def __init__(self, context):\n"
        "        self.plugin_id = context.plugin_id\n"
        "        self.prepare_calls = 0\n"
        "        self.activate_calls = 0\n"
        "        self.ready_calls = 0\n"
        "        self.deactivate_calls = 0\n"
        "    async def prepare(self): self.prepare_calls += 1\n"
        "    async def activate(self): self.activate_calls += 1\n"
        "    async def ready(self): self.ready_calls += 1\n"
        "    async def deactivate(self): self.deactivate_calls += 1\n"
        "    async def filter(self, envelope, call_next):\n"
        f"        return (await call_next(envelope)) + {suffix!r}\n"
        "def create_plugin(context): return Plugin(context)\n",
        encoding="utf-8",
    )


def _write_failed_cleanup_egress(project_root: Path) -> None:
    """写入 ready 失败且需测试显式放行后才能清理的候选插件。"""

    plugin = project_root / "plugins" / "phased_filter"
    (plugin / "runtime.py").write_text(
        "class Plugin:\n"
        "    def __init__(self, context):\n"
        "        self.plugin_id = context.plugin_id\n"
        "        self.active = False\n"
        "        self.allow_cleanup = False\n"
        "        self.deactivate_calls = 0\n"
        "    async def prepare(self): pass\n"
        "    async def activate(self): self.active = True\n"
        "    async def ready(self): raise RuntimeError('候选 ready 失败')\n"
        "    async def deactivate(self):\n"
        "        self.deactivate_calls += 1\n"
        "        if not self.allow_cleanup:\n"
        "            raise RuntimeError('候选清理失败')\n"
        "        self.active = False\n"
        "    async def filter(self, envelope, call_next):\n"
        "        return await call_next(envelope)\n"
        "def create_plugin(context): return Plugin(context)\n",
        encoding="utf-8",
    )


def _egress_envelope(text: str) -> EgressEnvelope:
    return EgressEnvelope(
        session_id="session-hot",
        turn_id="turn-hot",
        text=text,
        prompt_messages=[ModelMessage(role="user", content="测试")],
        model=cast(Any, object()),
        model_name="fake",
        temperature=0,
        max_tokens=100,
    )


@pytest.mark.asyncio
async def test_plugin_directory_cold_install_contributes_skill_and_managed_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    (project_root / "skills").mkdir(parents=True)
    _write_portable_plugin(project_root)

    contributions = PluginContributionCatalog(project_root)
    catalog = SkillCatalog([project_root / "skills", *contributions.skill_roots])
    assert catalog.names == {"portable-guide"}
    assert len(contributions.managed_services) == 1

    monkeypatch.setenv("IJA_TEST_SECRET", "不得传给服务")
    services = ManagedServiceManager(contributions.managed_services)
    await services.start()
    try:
        result = await services.capabilities["portable-worker"].request(
            "echo", {"text": "你好"}
        )
        with pytest.raises(ManagedServiceError, match="Denied: explicit failure"):
            await services.capabilities["portable-worker"].request("fail", {})
    finally:
        await services.stop()
    assert result == {
        "params": {"text": "你好"},
        "secret_visible": False,
        "project_root": str(project_root.resolve()),
    }


@pytest.mark.asyncio
async def test_managed_service_invalid_handshake_fails_fast_and_cleans_process(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    (project_root / "skills").mkdir(parents=True)
    plugin_root = _write_portable_plugin(project_root)
    (plugin_root / "worker.py").write_text(
        "print('{}', flush=True)\n",
        encoding="utf-8",
    )
    services = ManagedServiceManager(
        PluginContributionCatalog(project_root).managed_services
    )
    client = services.capabilities["portable-worker"]

    with pytest.raises(ManagedServiceError, match="ready 握手无效"):
        await services.start()
    assert client.running is False


@pytest.mark.asyncio
async def test_python_service_uses_normalized_script_when_working_directory_is_subdir(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    (project_root / "skills").mkdir(parents=True)
    plugin_root = _write_portable_plugin(project_root)
    (plugin_root / "run").mkdir()
    manifest_path = plugin_root / "plugin.toml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            'working_directory = "."',
            'working_directory = "run"',
        ),
        encoding="utf-8",
    )
    contributions = PluginContributionCatalog(project_root)
    spec = contributions.managed_services[0]
    assert Path(spec.command[1]).is_absolute()

    services = ManagedServiceManager(contributions.managed_services)
    await services.start()
    try:
        result = await services.capabilities["portable-worker"].request("echo", {})
        assert result["project_root"] == str(project_root.resolve())
    finally:
        await services.stop()


@pytest.mark.asyncio
async def test_late_response_after_timeout_does_not_break_shared_channel(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    (project_root / "skills").mkdir(parents=True)
    plugin_root = _write_portable_plugin(project_root)
    manifest_path = plugin_root / "plugin.toml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "request_timeout_seconds = 5",
            "request_timeout_seconds = 0.1",
        ),
        encoding="utf-8",
    )
    services = ManagedServiceManager(
        PluginContributionCatalog(project_root).managed_services
    )
    client = services.capabilities["portable-worker"]
    await services.start()
    try:
        with pytest.raises(ManagedServiceError, match="请求超时"):
            await client.request("slow", {})
        await asyncio.sleep(0.1)
        assert client.running is True
        result = await client.request("echo", {"after": "timeout"})
        assert result["params"] == {"after": "timeout"}
    finally:
        await services.stop()


@pytest.mark.asyncio
async def test_abandoned_request_capacity_fails_closed_without_reclassifying_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    (project_root / "skills").mkdir(parents=True)
    plugin_root = _write_portable_plugin(project_root)
    manifest_path = plugin_root / "plugin.toml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "request_timeout_seconds = 5",
            "request_timeout_seconds = 0.1",
        ),
        encoding="utf-8",
    )
    services = ManagedServiceManager(
        PluginContributionCatalog(project_root).managed_services
    )
    client = services.capabilities["portable-worker"]
    monkeypatch.setattr(ManagedServiceClient, "MAX_ABANDONED_REQUESTS", 1)
    await services.start()
    try:
        with pytest.raises(ManagedServiceError, match="请求超时"):
            await client.request("very-slow", {})
        with pytest.raises(ManagedServiceError, match="迟到响应记录达到上限"):
            await client.request("echo", {"queued": True})
        assert client.running is False
    finally:
        await services.stop()


@pytest.mark.asyncio
async def test_pending_request_limit_rejects_new_call_without_disrupting_inflight(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    (project_root / "skills").mkdir(parents=True)
    _write_portable_plugin(project_root)
    services = ManagedServiceManager(
        PluginContributionCatalog(project_root).managed_services
    )
    client = services.capabilities["portable-worker"]
    monkeypatch.setattr(ManagedServiceClient, "MAX_PENDING_REQUESTS", 1)
    await services.start()
    try:
        inflight = asyncio.create_task(client.request("very-slow", {}))
        for _ in range(20):
            if len(client._pending) == 1:
                break
            await asyncio.sleep(0)
        assert len(client._pending) == 1
        with pytest.raises(ManagedServiceError, match="在途请求达到上限"):
            await client.request("echo", {"must": "reject"})
        assert len(client._pending) == 1
        completed = await inflight
        assert completed["params"] == {}
        assert client._pending == {}
        assert client.running is True
    finally:
        await services.stop()


@pytest.mark.asyncio
async def test_managed_service_message_limit_accepts_large_line_and_fails_oversize_cleanly(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    project_root = tmp_path / "project"
    (project_root / "skills").mkdir(parents=True)
    _write_portable_plugin(project_root)
    services = ManagedServiceManager(
        PluginContributionCatalog(project_root).managed_services
    )
    client = services.capabilities["portable-worker"]
    await services.start()
    try:
        result = await client.request("large-ok", {})
        assert len(result["blob"]) == 70000

        with caplog.at_level(logging.ERROR, logger="plugins._host.managed_services"):
            with pytest.raises(ManagedServiceError, match="响应超过 1 MiB"):
                await client.request("large-too", {})
        await asyncio.sleep(0)
        assert client.running is False
        assert any(
            record.message == "受管服务响应读取失败"
            and getattr(record, "plugin_id", None) == "portable"
            and getattr(record, "service", None) == "worker"
            for record in caplog.records
        )
    finally:
        await services.stop()


@pytest.mark.asyncio
async def test_managed_service_request_limit_is_utf8_bounded_before_pending_registration(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    (project_root / "skills").mkdir(parents=True)
    _write_portable_plugin(project_root)
    services = ManagedServiceManager(
        PluginContributionCatalog(project_root).managed_services
    )
    client = services.capabilities["portable-worker"]
    await services.start()
    try:
        large_but_valid = "界" * 23000
        accepted = await client.request("echo", {"blob": large_but_valid})
        assert accepted["params"]["blob"] == large_but_valid

        with pytest.raises(InputValidationError, match="请求超过 1 MiB"):
            await client.request("echo", {"blob": "x" * MAX_MESSAGE_BYTES})
        assert client.running is True
        assert client._pending == {}
        recovered = await client.request("echo", {"after": "oversize"})
        assert recovered["params"] == {"after": "oversize"}
    finally:
        await services.stop()


def test_removing_plugin_directory_removes_contributions_without_stale_state(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    (project_root / "skills").mkdir(parents=True)
    plugin_root = _write_portable_plugin(project_root)
    assert PluginContributionCatalog(project_root).skill_roots

    for path in sorted(plugin_root.rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
        else:
            path.rmdir()
    plugin_root.rmdir()

    contributions = PluginContributionCatalog(project_root)
    assert contributions.skill_roots == ()
    assert contributions.managed_services == ()
    assert SkillCatalog(project_root / "skills").names == set()


def test_plugin_contribution_rejects_external_directory_symlink(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    plugins_root = project_root / "plugins"
    plugins_root.mkdir(parents=True)
    external = tmp_path / "external-plugin"
    external.mkdir()
    (external / "plugin.toml").write_text(
        'id = "linked"\n',
        encoding="utf-8",
    )
    link = plugins_root / "linked"
    try:
        os.symlink(external, link, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"当前环境不能创建目录符号链接: {exc}")

    with pytest.raises(InputValidationError, match="符号链接或 junction"):
        PluginContributionCatalog(project_root)


def test_plugin_contribution_rejects_external_plugins_root_symlink(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    external_plugins = tmp_path / "external-plugins"
    external_plugins.mkdir()
    try:
        os.symlink(
            external_plugins,
            project_root / "plugins",
            target_is_directory=True,
        )
    except OSError as exc:
        pytest.skip(f"当前环境不能创建目录符号链接: {exc}")

    with pytest.raises(
        InputValidationError,
        match="plugins 根目录禁止使用符号链接或 junction",
    ):
        PluginContributionCatalog(project_root)


def test_plugin_contribution_rejects_plugins_root_junction_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    (project_root / "plugins").mkdir(parents=True)
    original = getattr(Path, "is_junction", lambda _: False)
    monkeypatch.setattr(
        Path,
        "is_junction",
        lambda path: path.name == "plugins" or original(path),
        raising=False,
    )

    with pytest.raises(
        InputValidationError,
        match="plugins 根目录禁止使用符号链接或 junction",
    ):
        PluginContributionCatalog(project_root)


def test_plugin_contribution_rejects_windows_junction_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    plugin = project_root / "plugins" / "linked"
    plugin.mkdir(parents=True)
    (plugin / "plugin.toml").write_text('id = "linked"\n', encoding="utf-8")
    original = getattr(Path, "is_junction", lambda _: False)
    monkeypatch.setattr(
        Path,
        "is_junction",
        lambda path: path.name == "linked" or original(path),
        raising=False,
    )

    with pytest.raises(InputValidationError, match="符号链接或 junction"):
        PluginContributionCatalog(project_root)


def test_plugin_contribution_rejects_path_traversal_and_unknown_service_fields(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    plugin = project_root / "plugins" / "portable"
    plugin.mkdir(parents=True)
    (plugin / "plugin.toml").write_text(
        'id = "portable"\n'
        "[contributes]\n"
        'skill_roots = ["../skills"]\n',
        encoding="utf-8",
    )
    with pytest.raises(InputValidationError, match="路径越界"):
        PluginContributionCatalog(project_root)

    (plugin / "plugin.toml").write_text(
        'id = "portable"\n'
        "[[managed_services]]\n"
        'name = "worker"\n'
        'command = ["{python}", "worker.py"]\n'
        'capabilities = ["portable-worker"]\n'
        'unexpected = true\n',
        encoding="utf-8",
    )
    with pytest.raises(InputValidationError, match="字段无效"):
        PluginContributionCatalog(project_root)


def test_plugin_contribution_declares_scopes_for_all_contributed_skill_roots(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    (project_root / "skills").mkdir(parents=True)
    plugin_root = _write_portable_plugin(project_root)
    manifest_path = plugin_root / "plugin.toml"
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            'skill_roots = ["skills"]',
            'skill_roots = ["skills"]\n'
            'required_scopes = ["workspace:skills:write"]',
        ),
        encoding="utf-8",
    )

    contributions = PluginContributionCatalog(project_root)
    assert contributions.skill_required_scopes == {
        (plugin_root / "skills").resolve(): frozenset(
            {"workspace:skills:write"}
        )
    }

    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8").replace(
            "workspace:skills:write",
            "workspace:*",
        ),
        encoding="utf-8",
    )
    with pytest.raises(InputValidationError, match="required_scopes 无效"):
        PluginContributionCatalog(project_root)


def test_stale_enabled_plugin_does_not_block_host_startup(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    (project_root / "plugins").mkdir(parents=True)
    manager = ChannelPluginManager(
        project_root=project_root,
        enabled=["already_removed"],
        options={},
        router=ChannelRouter(),
        ingress=cast(Any, SimpleNamespace(accept=lambda envelope: None)),
        store=cast(Any, object()),
    )
    assert manager.plugins == {}
    assert manager.ingress_plugins == {}


def test_runtime_plugin_is_discovered_disabled_and_removed_without_host_changes(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    plugin = project_root / "plugins" / "portable"
    plugin.mkdir(parents=True)
    (plugin / "runtime.py").write_text(
        "class PortablePlugin:\n"
        "    def __init__(self, context): self.plugin_id = context.plugin_id\n"
        "    async def start(self): pass\n"
        "    async def stop(self): pass\n"
        "    async def ingest(self, envelope, call_next):\n"
        "        return await call_next(envelope)\n"
        "    async def typing(self, event, call_next):\n"
        "        await call_next(event)\n"
        "def create_plugin(context): return PortablePlugin(context)\n",
        encoding="utf-8",
    )
    (plugin / "plugin.toml").write_text(
        'id = "portable"\n'
        'kind = "ingress"\n'
        'entrypoint = "runtime:create_plugin"\n'
        'activation = "automatic"\n',
        encoding="utf-8",
    )

    def build_manager(*, disabled: list[str] | None = None) -> ChannelPluginManager:
        catalog = PluginContributionCatalog(project_root)
        return ChannelPluginManager(
            project_root=project_root,
            enabled=[],
            disabled=disabled,
            options={},
            router=ChannelRouter(),
            ingress=cast(Any, SimpleNamespace(accept=lambda envelope: None)),
            store=cast(Any, object()),
            catalog=catalog,
        )

    assert set(build_manager().ingress_plugins) == {"portable"}
    assert build_manager(disabled=["portable"]).ingress_plugins == {}

    shutil.rmtree(plugin)
    assert build_manager().ingress_plugins == {}


@pytest.mark.asyncio
async def test_plugin_generation_validates_atomically_leases_and_rolls_back(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    (project_root / "skills").mkdir(parents=True)
    _write_hot_egress(project_root, "-v1")

    async def accept(envelope):
        return envelope

    manager = ChannelPluginManager(
        project_root=project_root,
        enabled=[],
        options={},
        router=ChannelRouter(),
        ingress=cast(Any, SimpleNamespace(accept=accept)),
        store=cast(Any, object()),
        catalog=PluginContributionCatalog(project_root),
    )
    await manager.start()
    try:
        assert await manager.egress_filter(_egress_envelope("正文")) == "正文-v1"

        _write_hot_egress(project_root, "-broken", broken=True)
        with pytest.raises(SyntaxError):
            await manager.hot_reload()
        assert manager.generation_status()["current_generation"] == 1
        assert await manager.egress_filter(_egress_envelope("正文")) == "正文-v1"

        old = manager._snapshot
        lease = manager._lease_current()
        await lease.__aenter__()
        _write_hot_egress(project_root, "-v2")
        assert await manager.hot_reload() == 2
        await asyncio.sleep(0)
        assert old.running is True
        assert old.lease_count == 1
        assert await manager.egress_filter(_egress_envelope("正文")) == "正文-v2"

        await lease.__aexit__(None, None, None)
        for _ in range(20):
            if not old.running:
                break
            await asyncio.sleep(0)
        assert old.running is False

        assert await manager.rollback(1) == 1
        assert await manager.egress_filter(_egress_envelope("正文")) == "正文-v1"
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_candidate_prepare_has_no_activation_or_formal_ingress_side_effects(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    (project_root / "skills").mkdir(parents=True)
    _write_phased_channel(project_root)
    stored_images = 0

    async def accept(envelope):
        return envelope

    def store_image(filename: str, mime_type: str, content: bytes):
        nonlocal stored_images
        del filename, mime_type, content
        stored_images += 1
        return cast(Any, object())

    candidate = ChannelPluginManager(
        project_root=project_root,
        enabled=[],
        options={},
        router=ChannelRouter(),
        ingress=cast(Any, SimpleNamespace(accept=accept)),
        store=cast(Any, object()),
        image_store=store_image,
        catalog=PluginContributionCatalog(project_root),
        _generation=2,
        _candidate_mode=True,
    )
    plugin = cast(Any, candidate.plugins["phased_channel"])
    try:
        await candidate.prepare()

        assert plugin.calls == ["prepare"]
        assert candidate._snapshot.prepared is True
        assert candidate._snapshot.running is False
        assert candidate._snapshot.accepting is False
        async with plugin.context.admit_event() as admitted:
            assert admitted is False
        with pytest.raises(RuntimeError, match="尚未发布"):
            await plugin.context.ingest(cast(Any, object()))
        assert plugin.context.store_image is not None
        with pytest.raises(RuntimeError, match="未获事件准入"):
            plugin.context.store_image("candidate.png", "image/png", b"candidate")
        assert stored_images == 0
    finally:
        await candidate.stop()


@pytest.mark.asyncio
async def test_admitted_old_event_finishes_in_its_original_generation_after_publish(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    (project_root / "skills").mkdir(parents=True)
    _write_phased_channel(project_root)
    stored_images: list[bytes] = []
    accepted_by: list[str] = []

    async def accept(envelope):
        accepted_by.append("base")
        return envelope

    def store_image(filename: str, mime_type: str, content: bytes):
        del filename, mime_type
        stored_images.append(content)
        return cast(Any, object())

    manager = ChannelPluginManager(
        project_root=project_root,
        enabled=[],
        options={},
        router=ChannelRouter(),
        ingress=cast(Any, SimpleNamespace(accept=accept)),
        store=cast(Any, object()),
        image_store=store_image,
        catalog=PluginContributionCatalog(project_root),
    )
    await manager.start()
    old = manager._snapshot
    old_plugin = cast(Any, manager.plugins["phased_channel"])

    async def old_ingress(envelope):
        accepted_by.append("old")
        return envelope

    old.ingest_handler = old_ingress
    admission = old_plugin.context.admit_event()
    assert await admission.__aenter__() is True
    try:
        assert await manager.hot_reload() == 2
        assert old.accepting is False
        assert old.running is True
        assert old.lease_count == 1

        assert old_plugin.context.store_image is not None
        old_plugin.context.store_image("old.png", "image/png", b"old-generation")
        await old_plugin.context.ingest(cast(Any, object()))

        assert stored_images == [b"old-generation"]
        assert accepted_by == ["old"]
    finally:
        await admission.__aexit__(None, None, None)
        for _ in range(20):
            if not old.running:
                break
            await asyncio.sleep(0)
        await manager.stop()

    assert old.running is False


@pytest.mark.asyncio
async def test_legacy_partial_start_failure_runs_compensating_stop(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    (project_root / "skills").mkdir(parents=True)
    _write_partial_start_egress(project_root)
    manager = ChannelPluginManager(
        project_root=project_root,
        enabled=[],
        options={},
        router=ChannelRouter(),
        ingress=cast(Any, SimpleNamespace(accept=lambda envelope: envelope)),
        store=cast(Any, object()),
        catalog=PluginContributionCatalog(project_root),
    )
    plugin = cast(Any, manager.egress_plugins["partial_start"])

    with pytest.raises(RuntimeError, match="部分启动失败"):
        await manager.start()

    assert plugin.start_calls == 1
    assert plugin.stop_calls == 1
    assert plugin.resource_open is False
    assert manager._snapshot.started == []
    assert manager._snapshot.running is False
    assert manager._snapshot.ready is False


@pytest.mark.asyncio
async def test_publish_failure_cleans_candidate_and_keeps_old_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    (project_root / "skills").mkdir(parents=True)
    _write_phased_egress(project_root, "-v1")

    async def accept(envelope):
        return envelope

    manager = ChannelPluginManager(
        project_root=project_root,
        enabled=[],
        options={},
        router=ChannelRouter(),
        ingress=cast(Any, SimpleNamespace(accept=accept)),
        store=cast(Any, object()),
        catalog=PluginContributionCatalog(project_root),
    )
    await manager.start()
    old = manager._snapshot
    candidate_snapshots: list[Any] = []

    async def fail_publish(snapshot) -> None:
        assert snapshot.prepared is True
        assert snapshot.ready is True
        assert snapshot.running is True
        candidate_snapshots.append(snapshot)
        raise RuntimeError("模拟发布提交失败")

    monkeypatch.setattr(manager, "_publish_snapshot", fail_publish)
    _write_phased_egress(project_root, "-v2")
    try:
        with pytest.raises(RuntimeError, match="模拟发布提交失败"):
            await manager.hot_reload()

        assert manager._snapshot is old
        assert old.accepting is True
        assert old.running is True
        assert manager.generation_status()["current_generation"] == 1
        assert await manager.egress_filter(_egress_envelope("正文")) == "正文-v1"
        assert len(candidate_snapshots) == 1
        failed_candidate = candidate_snapshots[0]
        failed_plugin = cast(Any, failed_candidate.egress_plugins["phased_filter"])
        assert failed_plugin.deactivate_calls == 1
        assert failed_candidate.started == []
        assert failed_candidate.ready is False
        assert failed_candidate.running is False
    finally:
        await manager.stop()


@pytest.mark.asyncio
async def test_failed_candidate_cleanup_retains_owner_and_stop_retries(
    tmp_path: Path,
) -> None:
    """候选连续清理失败必须可见、可重试，不能随 hot_reload 局部引用丢失。"""

    project_root = tmp_path / "project"
    (project_root / "skills").mkdir(parents=True)
    _write_phased_egress(project_root, "-v1")

    async def accept(envelope):
        return envelope

    manager = ChannelPluginManager(
        project_root=project_root,
        enabled=[],
        options={},
        router=ChannelRouter(),
        ingress=cast(Any, SimpleNamespace(accept=accept)),
        store=cast(Any, object()),
        catalog=PluginContributionCatalog(project_root),
    )
    await manager.start()
    old = manager._snapshot
    _write_failed_cleanup_egress(project_root)

    with pytest.raises(RuntimeError, match="插件 generation 停止失败"):
        await manager.hot_reload()

    assert manager._snapshot is old
    assert old.accepting is True
    assert manager.generation_status()["current_generation"] == 1
    retained = manager._history[2]
    retained_plugin = cast(Any, retained.egress_plugins["phased_filter"])
    assert retained_plugin.active is True
    assert retained_plugin.deactivate_calls == 2
    assert retained.started == [retained_plugin]
    retained_status = next(
        item
        for item in cast(list[dict[str, Any]], manager.generation_status()["generations"])
        if item["generation"] == 2
    )
    assert retained_status["cleanup_pending"] is True
    with pytest.raises(NotFoundError, match="没有可回滚"):
        await manager.rollback(2)

    retained_plugin.allow_cleanup = True
    await manager.stop()
    assert retained_plugin.active is False
    assert retained_plugin.deactivate_calls == 3
    assert retained.started == []


def test_plugin_settings_reject_conflicting_or_invalid_plugin_ids() -> None:
    with pytest.raises(ValidationError, match="不能同时启用和禁用"):
        PlatformPluginSettings(
            enabled=["portable"],
            disabled=["portable"],
        )
    with pytest.raises(ValidationError, match="无效的插件 ID"):
        PlatformPluginSettings(options={"../portable": {}})


def test_invalid_enabled_plugin_config_isolated_from_host(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    project_root = tmp_path / "project"
    (project_root / "plugins").mkdir(parents=True)
    catalog = PluginContributionCatalog(project_root, include_bundled=True)

    with caplog.at_level(logging.WARNING, logger="plugins._host.manager"):
        manager = ChannelPluginManager(
            project_root=project_root,
            enabled=["wechat"],
            options={},
            router=ChannelRouter(),
            ingress=cast(Any, SimpleNamespace(accept=lambda envelope: None)),
            store=cast(Any, object()),
            catalog=catalog,
        )

    assert manager.plugins == {}
    assert "filter" in manager.egress_plugins
    assert any(
        record.getMessage().startswith(
            "插件配置或可选依赖不完整，跳过 plugin_id=wechat"
        )
        and getattr(record, "plugin_id", None) == "wechat"
        for record in caplog.records
    )


def test_channel_plugin_entrypoint_supports_package_relative_import(tmp_path: Path) -> None:
    plugin = tmp_path / "portable"
    plugin.mkdir()
    (plugin / "helper.py").write_text("VALUE = 'relative-import-ok'\n", encoding="utf-8")
    runtime = plugin / "runtime.py"
    runtime.write_text("from .helper import VALUE\n", encoding="utf-8")

    module = ChannelPluginManager._load_module("portable", runtime)
    assert module.VALUE == "relative-import-ok"


def test_channel_plugin_manifest_rejects_unknown_top_level_field(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    plugin = project_root / "plugins" / "portable"
    plugin.mkdir(parents=True)
    (plugin / "runtime.py").write_text("def create_plugin(context): return None\n", encoding="utf-8")
    (plugin / "plugin.toml").write_text(
        'id = "portable"\n'
        'name = "测试"\n'
        'kind = "channel"\n'
        'version = "0.1.0"\n'
        'entrypoint = "runtime:create_plugin"\n'
        'mystery = "value"\n',
        encoding="utf-8",
    )
    with pytest.raises(InputValidationError, match="未知字段"):
        ChannelPluginManager(
            project_root=project_root,
            enabled=["portable"],
            options={},
            router=ChannelRouter(),
                ingress=cast(Any, SimpleNamespace(accept=lambda envelope: None)),
            store=cast(Any, object()),
        )
