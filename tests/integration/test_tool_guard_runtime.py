"""执行守卫经过真实插件发现、生命周期和 ToolLoop 的集成测试。"""

import asyncio
import shutil
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from adapters.model import FakeModelProvider
from bootstrap import build_runtime
from domain.errors import ToolLoopAbortedError
from domain.models import ChatType, InboundMessage, MessageComponent, Participant
from plugins._host import ChannelPluginManager, ChannelRouter, PluginContributionCatalog
from ports import ModelRequest, ModelResult, ModelToolCall
from ports.tool_guard import ToolObservation


def install_plugin(project_root: Path) -> Path:
    """复制代码到测试工作区；后续配置和移除均只作用于该副本。"""

    source = Path(__file__).resolve().parents[2] / "plugins" / "circuit_breaker"
    target = project_root / "plugins" / "circuit_breaker"
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__", "tests"))
    return target


def manager(project_root: Path, *, disabled=None, options=None):
    """真实插件管理器搭配无副作用的宿主端口。"""

    return ChannelPluginManager(
        project_root=project_root, enabled=[], disabled=disabled,
        options=options or {}, router=ChannelRouter(), ingress=AsyncMock(),
        store=AsyncMock(), catalog=PluginContributionCatalog(project_root),
    )


def test_tool_guard_manifest_discovered(tmp_path: Path) -> None:
    install_plugin(tmp_path)
    catalog = PluginContributionCatalog(tmp_path)
    manifest = catalog.runtime_manifest("circuit_breaker")
    assert manifest is not None
    assert manifest.kind == "tool_guard"


async def test_guard_lifecycle_reload_and_rollback_isolate_inflight_state(tmp_path: Path):
    install_plugin(tmp_path)
    host = manager(tmp_path)
    await host.start()
    sample = ToolObservation("lookup", "{}", "{}", True)
    try:
        async with host.tool_guards() as old_guards:
            old_guard = old_guards[0][1]
            assert old_guard.observe(sample) is None
            host.replace_options({"circuit_breaker": {"repetitions": 2}})
            await host.hot_reload()
            async with host.tool_guards() as new_guards:
                new_guard = new_guards[0][1]
                assert new_guard.observe(sample) is None
                assert new_guard.observe(sample) is not None
            # 在途任务仍有原来的三次阈值，也不会被新一代的熔断污染。
            assert old_guard.observe(sample) is None
            assert old_guard.observe(sample) is not None
            generations = host.generation_status()["generations"]
            assert isinstance(generations, list)
            assert generations[0]["lease_count"] == 1
        await asyncio.gather(*host._retire_tasks)
        await host.rollback(1)
        async with host.tool_guards() as restored:
            guard = restored[0][1]
            assert guard.observe(sample) is None
            assert guard.observe(sample) is None
            assert guard.observe(sample) is not None
    finally:
        await host.stop()


async def test_disable_and_remove_plugin_leave_host_available(tmp_path: Path):
    plugin_root = install_plugin(tmp_path)
    host = manager(tmp_path, disabled=["circuit_breaker"])
    await host.start()
    try:
        async with host.tool_guards() as guards:
            assert guards == ()
    finally:
        await host.stop()
    # 以改名模拟移出 plugins，而不删除任何文件。
    plugin_root.rename(tmp_path / "removed_plugin")
    host = manager(tmp_path)
    await host.start()
    try:
        async with host.tool_guards() as guards:
            assert guards == ()
    finally:
        await host.stop()


async def test_invalid_reload_keeps_existing_protection(tmp_path: Path):
    install_plugin(tmp_path)
    host = manager(tmp_path)
    await host.start()
    try:
        host.replace_options({"circuit_breaker": {"repetitions": 0}})
        with pytest.raises(ValidationError):
            await host.hot_reload()
        async with host.tool_guards() as guards:
            assert guards[0][1].timeout_seconds == 120
    finally:
        await host.stop()


class InvalidArgumentsModel(FakeModelProvider):
    """规划使用离线模型，工具阶段持续输出非法参数且不触发外部操作。"""

    async def complete(self, request: ModelRequest) -> ModelResult:
        if request.tools:
            return ModelResult(tool_calls=[
                ModelToolCall(id="reused-id", name="get_current_time", arguments="{")
            ])
        return await super().complete(request)


async def test_real_bootstrap_wires_guard_and_preserves_failed_audit(settings):
    runtime = build_runtime(settings, model_override=InvalidArgumentsModel())
    assert runtime.chat.tool_loop.guard_lease == runtime.platform_plugins.tool_guards
    await runtime.start()
    try:
        async with runtime.platform_plugins.tool_guards() as guards:
            assert any(plugin_id == "circuit_breaker" for plugin_id, _ in guards)
        session = await runtime.chat.create_session(
            chat_type=ChatType.PRIVATE, display_name="熔断测试", external_chat_id="circuit-test",
            participants=[Participant(external_user_id="u1", display_name="测试用户")],
        )
        await runtime.chat.ingest(InboundMessage(
            platform=session.platform, account_id=session.account_id,
            external_message_id="time-1", external_chat_id=session.external_chat_id,
            sender_id="u1", sender_name="测试用户", chat_type=ChatType.PRIVATE,
            components=[MessageComponent.text_component("上海现在几点？")],
        ), schedule_turn=False)
        with pytest.raises(ToolLoopAbortedError):
            await runtime.chat.process_session(session.id)
        executions = await runtime.store.list_tool_executions(session_id=session.id)
        assert len(executions) == 3
        assert all(item.error_code == "invalid_tool_arguments" for item in executions)
        assert len(await runtime.store.list_messages(session.id)) == 1
    finally:
        await runtime.stop()
