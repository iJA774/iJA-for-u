"""在权威数据库副本上暂停模型边界，交互式模拟一次真实群聊重试。

脚本不会修改权威数据库，也不会连接真实聊天平台。每次模型请求会完整写入
``requests/``，随后从标准输入读取一个符合 ``ModelResult`` 的 JSON object，
以便人工或外部 Agent 代替上游模型后继续执行原应用链路。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from adapters.web_simulator import AttachmentStore, WebSimulatorChannel  # noqa: E402
from bootstrap import Runtime, build_runtime  # noqa: E402
from config import AppSettings, ModelTaskProfile, load_settings  # noqa: E402
from domain.errors import InputValidationError, NotFoundError  # noqa: E402
from ports import ModelRequest, ModelResult  # noqa: E402


async def _passthrough_egress(envelope: Any) -> str:
    """隔离运行未启动插件 generation 时，保持“无 egress 插件”的直通语义。"""

    return envelope.text


class InteractiveModelProvider:
    """把真实模型请求落盘，并从标准输入读取模拟的上游响应。"""

    def __init__(self, request_dir: Path) -> None:
        self.request_dir = request_dir
        self.request_dir.mkdir(parents=True, exist_ok=True)
        self.request_count = 0

    async def complete(self, request: ModelRequest) -> ModelResult:
        """暂停在 ModelProvider 边界，直到标准输入收到合法响应。"""

        self.request_count += 1
        request_id = f"request_{self.request_count:04d}"
        request_path = self.request_dir / f"{request_id}.json"
        response_path = self.request_dir / f"{request_id}.response.json"
        request_path.write_text(
            json.dumps(_serialize_request(request), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "event": "llm.request",
                    "request_id": request_id,
                    "path": str(request_path.resolve()),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        raw_response = await asyncio.to_thread(sys.stdin.readline)
        if not raw_response:
            raise RuntimeError(f"{request_id} 等待模拟模型响应时标准输入已关闭")
        try:
            payload = json.loads(raw_response)
        except json.JSONDecodeError as exc:
            raise InputValidationError(
                f"{request_id} 的模拟模型响应不是合法 JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise InputValidationError(f"{request_id} 的模拟模型响应必须是 JSON object")
        result = ModelResult.model_validate(payload)
        response_path.write_text(
            json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "event": "llm.response.accepted",
                    "request_id": request_id,
                    "path": str(response_path.resolve()),
                    "tool_call_count": len(result.tool_calls),
                    "has_content": bool(result.content),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return result

    async def probe(self) -> dict[str, Any]:
        """返回离线桥的就绪状态，不发起网络请求。"""

        return {"ok": True, "provider": "interactive-offline-bridge"}

    async def close(self) -> None:
        """交互式 Provider 不持有外部资源。"""

        return None


def _serialize_request(request: ModelRequest) -> dict[str, Any]:
    """保留上游可见的完整请求，不写入 Provider 凭据或连接端点。"""

    return {
        "model": request.model,
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
        "json_mode": request.json_mode,
        "tool_choice": request.tool_choice,
        "messages": [item.model_dump(mode="json") for item in request.messages],
        "tools": (
            [item.model_dump(mode="json") for item in request.tools]
            if request.tools is not None
            else None
        ),
    }


def _prepare_run_directory(run_dir: Path, source_data_dir: Path) -> Path:
    """创建隔离数据目录，并用 SQLite 在线备份复制权威数据库快照。"""

    resolved_run_dir = run_dir.resolve()
    resolved_source_dir = source_data_dir.resolve()
    if resolved_run_dir == resolved_source_dir or resolved_source_dir in resolved_run_dir.parents:
        raise InputValidationError("调试目录不得等于或位于权威数据目录内")
    if resolved_run_dir.exists() and any(resolved_run_dir.iterdir()):
        raise InputValidationError("调试目录必须不存在或为空")
    data_dir = resolved_run_dir / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    source_db = resolved_source_dir / "ija.sqlite3"
    if not source_db.is_file():
        raise NotFoundError(f"权威数据库不存在: {source_db}")
    target_db = data_dir / "ija.sqlite3"
    source_uri = f"file:{source_db.as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True) as source, sqlite3.connect(target_db) as target:
        source.backup(target)
    active_persona = resolved_source_dir / "personas" / "active.toml"
    if active_persona.is_file():
        target_persona_dir = data_dir / "personas"
        target_persona_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(active_persona, target_persona_dir / "active.toml")
    return data_dir


def _isolated_settings(
    settings: AppSettings,
    data_dir: Path,
    *,
    bridge_timeout_seconds: float = 600,
) -> AppSettings:
    """保留真实聊天配置，同时切断平台进程和无关外部模型。

    人工或外部 Agent 的往返时间明显长于在线 Provider，因此只在隔离副本中
    覆盖 ``chat.reply`` 的 hard timeout，避免断点尚未注入响应就被路由器取消。
    """

    isolated = settings.model_copy(deep=True)
    isolated.storage = isolated.storage.model_copy(update={"data_dir": data_dir})
    isolated.platform_plugins = isolated.platform_plugins.model_copy(
        update={"enabled": [], "disabled": [], "options": {}}
    )
    isolated.image_model = isolated.image_model.model_copy(update={"enabled": False})
    isolated.detection_model = isolated.detection_model.model_copy(update={"enabled": False})
    # 当前目标会话没有长期记忆或社交学习派生项；关闭 embedding 可证明本次
    # 调试不会偷偷访问第二个模型端点，同时不改变实际聊天 Prompt。
    isolated.embedding = isolated.embedding.model_copy(update={"enabled": False})
    chat_reply_profile = isolated.model.task_profiles.get(
        "chat.reply",
        ModelTaskProfile(),
    ).model_copy(update={"hard_timeout_seconds": bridge_timeout_seconds})
    isolated.model = isolated.model.model_copy(
        update={
            "task_profiles": {
                **isolated.model.task_profiles,
                "chat.reply": chat_reply_profile,
            }
        }
    )
    return isolated


def _bridge_timeout_seconds(value: str) -> float:
    """校验交互式模型桥的等待时限。"""

    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("桥接等待时限必须是数字") from exc
    if not 0 < timeout <= 600:
        raise argparse.ArgumentTypeError("桥接等待时限必须大于 0 且不超过 600 秒")
    return timeout


async def _close_runtime_resources(runtime: Runtime) -> None:
    """关闭本脚本实际初始化过的资源；Runtime 本身没有进入 start 生命周期。"""

    await runtime.store.close()
    seen: set[int] = set()
    for resource in (
        runtime.model,
        runtime.profile_model,
        runtime.embedding,
        runtime.weather,
        runtime.rss,
    ):
        if resource is None:
            continue
        owner = getattr(resource, "wrapped_provider", resource)
        if id(owner) in seen:
            continue
        seen.add(id(owner))
        await resource.close()


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    """装配隔离 Runtime，重试指定 Turn，并收集可核对的最终状态。"""

    source_data_dir = args.source_data_dir.resolve()
    data_dir = _prepare_run_directory(args.run_dir, source_data_dir)
    settings = _isolated_settings(
        load_settings(PROJECT_ROOT),
        data_dir,
        bridge_timeout_seconds=args.bridge_timeout_seconds,
    )
    provider = InteractiveModelProvider(args.run_dir.resolve() / "requests")
    runtime = build_runtime(
        settings,
        model_override=provider,
        profile_model_override=provider,
    )
    runtime.chat.set_egress_filter(_passthrough_egress)
    runtime.personas.initialize()
    await runtime.store.initialize()
    # 数据库组件保存的是权威目录绝对路径；调试只读这些已校验历史图片，
    # 新消息、投递和状态仍全部写入隔离数据库。
    source_attachments = AttachmentStore(
        uploads_root=source_data_dir / "uploads",
        personas_root=source_data_dir / "personas",
        media_root=source_data_dir / "media",
        max_bytes=settings.chat.max_attachment_bytes,
    )
    runtime.chat.prompting._image_reader = source_attachments.read_image_ref
    try:
        session = await runtime.store.get_session(args.session_id)
        if session is None:
            raise NotFoundError("目标群聊 Session 不存在")
        if session.chat_type.value != "group":
            raise InputValidationError("目标 Session 不是群聊")
        decision = await runtime.store.get_decision(args.turn_id)
        if decision is None or decision.session_id != session.id:
            raise NotFoundError("目标 Turn 不存在或不属于该群聊")
        runtime.channel.register(
            session.platform,
            session.account_id,
            WebSimulatorChannel(),
        )
        source_messages = await runtime.store.list_turn_source_messages(
            session.id,
            decision.id,
        )
        before_messages = await runtime.store.list_messages(session.id)
        before_message_ids = {item.id for item in before_messages}
        await runtime.chat.retry_reactive_reply(session.id, decision.id)
        after_messages = await runtime.store.list_messages(session.id)
        # 仓储按 created_at 倒序返回，不能假设新消息追加在 list 尾部。
        new_messages = [
            item for item in after_messages if item.id not in before_message_ids
        ]
        outbound_batch = await runtime.store.list_reactive_outbound_batch(
            session.id,
            decision.id,
        )
        deliveries = []
        for outbound in outbound_batch:
            receipt = await runtime.store.get_delivery(outbound.id)
            deliveries.append(
                {
                    "outbound": outbound.model_dump(mode="json"),
                    "receipt": (
                        receipt.model_dump(mode="json") if receipt is not None else None
                    ),
                }
            )
        report = {
            "status": "completed",
            "source_database": str((source_data_dir / "ija.sqlite3").resolve()),
            "isolated_database": str(settings.storage.database_path.resolve()),
            "session": {
                "id": session.id,
                "display_name": session.display_name,
                "platform": session.platform,
                "account_id": session.account_id,
            },
            "decision": decision.model_dump(mode="json"),
            "source_messages": [item.model_dump(mode="json") for item in source_messages],
            "model_request_count": provider.request_count,
            "new_messages": [item.model_dump(mode="json") for item in new_messages],
            "deliveries": deliveries,
            "checks": {
                "assistant_message_committed": bool(new_messages),
                "reactive_batch_persisted": bool(outbound_batch),
                "all_deliveries_sent": bool(deliveries)
                and all(
                    item["receipt"] is not None
                    and item["receipt"]["status"] == "sent"
                    for item in deliveries
                ),
            },
        }
        report_path = args.run_dir.resolve() / "report.json"
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "event": "simulation.completed",
                    "report": str(report_path),
                    "checks": report["checks"],
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        return report
    finally:
        await _close_runtime_resources(runtime)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="在数据库副本上交互式模拟群聊 LLM 边界")
    parser.add_argument(
        "--source-data-dir",
        type=Path,
        default=PROJECT_ROOT / "data",
        help="权威数据目录（只读）",
    )
    parser.add_argument("--run-dir", type=Path, required=True, help="必须为空的隔离调试目录")
    parser.add_argument("--session-id", required=True, help="目标群聊 Session ID")
    parser.add_argument("--turn-id", required=True, help="目标 reply Turn ID")
    parser.add_argument(
        "--bridge-timeout-seconds",
        type=_bridge_timeout_seconds,
        default=600.0,
        help="等待人工或外部 Agent 响应的时限，默认 600 秒",
    )
    return parser


def main() -> int:
    """命令行入口。"""

    args = _parser().parse_args()
    asyncio.run(_run(args))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
