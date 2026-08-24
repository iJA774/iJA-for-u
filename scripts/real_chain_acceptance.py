"""使用真实模型配置运行隔离的私聊、群聊与表情链路验收。

本脚本只复用当前项目的模型配置；会话、日志、数据库和媒体都写入临时或
显式指定的隔离目录，且不会启用任何平台插件。报告仅包含合成数据。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import mimetypes
import re
import sys
import tempfile
import time
import uuid
from collections.abc import Mapping, Sequence
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from bootstrap import Runtime, build_runtime  # noqa: E402
from config import AppSettings, load_settings  # noqa: E402
from domain.models import (  # noqa: E402
    ChatType,
    ComponentType,
    InboundMessage,
    MessageComponent,
    MessageRole,
    Participant,
    ParticipantRole,
    SessionView,
    StoredMessage,
)
from ports import ModelProvider  # noqa: E402

_SENSITIVE_KEY = re.compile(
    r"(?:api[_-]?key|authorization|base[_-]?url|control[_-]?token|"
    r"access[_-]?token|refresh[_-]?token|secret)",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_BEARER = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]+")
_COMMON_KEY = re.compile(r"\bsk-[a-z0-9_-]{8,}\b", re.IGNORECASE)
_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|authorization|token|secret)(\s*[:=]\s*)([^\s,;]+)"
)
_EMOJI_MARKERS = (
    "😂",
    "🤣",
    "🥳",
    "👍",
    "😎",
    "✨",
    "🎉",
    "🔥",
    "✅",
    "😏",
    "🤝",
)


def sanitize_text(value: str) -> str:
    """移除自由文本中的连接端点和常见凭据形态。"""

    value = _URL.sub("[URL已脱敏]", value)
    value = _BEARER.sub("Bearer [凭据已脱敏]", value)
    value = _COMMON_KEY.sub("[凭据已脱敏]", value)
    return _ASSIGNMENT.sub(r"\1\2[凭据已脱敏]", value)


def sanitize_for_report(value: Any) -> Any:
    """递归生成可写入验收报告的安全副本。"""

    if isinstance(value, Mapping):
        return {
            str(key): (
                "[已脱敏]"
                if _SENSITIVE_KEY.search(str(key))
                else sanitize_for_report(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [sanitize_for_report(item) for item in value]
    if isinstance(value, Path):
        return value.name
    if isinstance(value, str):
        return sanitize_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize_text(str(value))


def validate_isolated_data_dir(
    candidate: Path,
    *,
    authoritative_data_dir: Path,
) -> Path:
    """拒绝把验收数据写入当前权威数据目录或其子目录。"""

    resolved = candidate.resolve()
    authoritative = authoritative_data_dir.resolve()
    if resolved == authoritative or authoritative in resolved.parents:
        raise ValueError("验收 data_dir 不得等于或位于当前权威数据目录内")
    if resolved.exists() and (not resolved.is_dir() or any(resolved.iterdir())):
        raise ValueError("验收 data_dir 必须不存在或为空目录")
    return resolved


def build_isolated_settings(
    settings: AppSettings,
    *,
    data_dir: Path,
) -> AppSettings:
    """保留真实聊天/画像模型配置，同时切断平台和无关外部模型。"""

    isolated = settings.model_copy(deep=True)
    isolated.storage = isolated.storage.model_copy(update={"data_dir": data_dir})
    isolated.platform_plugins = isolated.platform_plugins.model_copy(
        update={"enabled": [], "disabled": [], "options": {}}
    )
    # 表情验收只复用本地上传素材；独立图片、检测和视觉端点不属于本链路。
    isolated.image_model = isolated.image_model.model_copy(update={"enabled": False})
    isolated.detection_model = isolated.detection_model.model_copy(
        update={"enabled": False}
    )
    isolated.vision_model = isolated.vision_model.model_copy(
        update={"mode": "main"}
    )
    isolated.chat = isolated.chat.model_copy(
        update={"private_debounce_ms": 0, "group_debounce_ms": 0}
    )
    isolated.social_learning = isolated.social_learning.model_copy(
        update={
            "enabled": True,
            "minimum_new_user_messages": 4,
            "jargon_reuse_min_occurrences": 4,
        }
    )
    return isolated


class _RedactingLogFilter(logging.Filter):
    """保证验收进程的控制台和隔离日志不打印端点或凭据。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = sanitize_text(record.getMessage())
        record.args = ()
        # 失败详情会进入脱敏报告；这里不让第三方 traceback 回显请求 URL。
        record.exc_info = None
        record.exc_text = None
        return True


def install_log_redaction() -> None:
    """给 Runtime 已安装的所有 handler 增加验收专用脱敏过滤器。"""

    for handler in logging.getLogger().handlers:
        handler.addFilter(_RedactingLogFilter())


def _component_summary(component: MessageComponent) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "type": component.type.value,
        "is_expression": component.is_expression,
    }
    for name in (
        "text",
        "target_id",
        "target_name",
        "attachment_id",
        "filename",
        "mime_type",
        "description",
    ):
        value = getattr(component, name)
        if value is not None:
            summary[name] = value
    return sanitize_for_report(summary)


def _message_summary(message: StoredMessage) -> dict[str, Any]:
    return {
        "role": message.role.value,
        "sender_name": sanitize_text(message.sender_name),
        "components": [_component_summary(item) for item in message.components],
        "created_at": message.created_at.isoformat(),
        "origin": message.origin.value,
        "turn_id": message.origin_run_id,
    }


def _assistant_text(messages: Sequence[StoredMessage]) -> str:
    return "\n".join(
        component.text or ""
        for message in messages
        if message.role == MessageRole.ASSISTANT
        for component in message.components
        if component.type == ComponentType.TEXT
    ).strip()


def _percentile(values: Sequence[float], fraction: float) -> float:
    """使用最近秩生成稳定的验收延迟分位数。"""

    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return round(ordered[index], 2)


def _latency_summary(turns: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    values = [float(item["elapsed_ms"]) for item in turns]
    return {
        "sample_count": len(values),
        "p50_ms": _percentile(values, 0.50),
        "p95_ms": _percentile(values, 0.95),
        "max_ms": round(max(values, default=0), 2),
    }


def _tool_execution_summary(executions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    completed = [item for item in executions if item.get("status") == "completed"]
    latencies = [
        float(item["elapsed_ms"])
        for item in completed
        if item.get("elapsed_ms") is not None
    ]
    return {
        "total": len(executions),
        "completed": len(completed),
        "failed": sum(item.get("status") == "failed" for item in executions),
        "latency_p50_ms": _percentile(latencies, 0.50),
        "latency_p95_ms": _percentile(latencies, 0.95),
    }


async def _send_turn(
    runtime: Runtime,
    *,
    session: SessionView,
    sender_id: str,
    sender_name: str,
    text: str,
    mention_agent: bool,
    label: str,
    extra_components: Sequence[MessageComponent] = (),
) -> dict[str, Any]:
    before_ids = {
        item.id for item in await runtime.store.list_messages(session.id)
    }
    components: list[MessageComponent] = []
    if mention_agent:
        components.append(
            MessageComponent(
                type=ComponentType.MENTION,
                target_id="agent",
                target_name="小佳",
            )
        )
    components.append(MessageComponent.text_component(text))
    components.extend(
        component.model_copy(deep=True)
        for component in extra_components
    )
    started = time.perf_counter()
    ingress = await runtime.chat.ingest(
        InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id=f"acceptance-{uuid.uuid4().hex}",
            external_chat_id=session.external_chat_id,
            sender_id=sender_id,
            sender_name=sender_name,
            chat_type=session.chat_type,
            components=components,
        ),
        schedule_turn=False,
    )
    await runtime.chat.process_session(session.id)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
    messages = await runtime.store.list_messages(session.id)
    committed = [item for item in messages if item.id not in before_ids]
    return {
        "label": label,
        "accepted": ingress.accepted,
        "elapsed_ms": elapsed_ms,
        "messages": [_message_summary(item) for item in committed],
        "assistant_text": sanitize_text(_assistant_text(committed)),
    }


async def _learning_snapshot(runtime: Runtime, session: SessionView) -> dict[str, Any]:
    jargons = await runtime.store.list_jargons(session.id)
    expressions = await runtime.store.list_group_expressions(session.id)
    behaviors = await runtime.store.list_behavior_patterns(session.id)
    return sanitize_for_report(
        {
            "jargons": [
                {
                    "term": item.term,
                    "meaning": item.meaning,
                    "status": item.status.value,
                    "confidence": item.confidence,
                    "occurrence_count": item.occurrence_count,
                }
                for item in jargons
            ],
            "expressions": [
                {
                    "situation": item.situation,
                    "style": item.style,
                    "status": item.status.value,
                    "confidence": item.confidence,
                    "occurrence_count": item.occurrence_count,
                }
                for item in expressions
            ],
            "behaviors": [
                {
                    "scene_summary": item.scene_summary,
                    "action": item.action,
                    "expected_outcome": item.expected_outcome,
                    "status": item.status.value,
                    "confidence": item.confidence,
                    "occurrence_count": item.occurrence_count,
                }
                for item in behaviors
            ],
        }
    )


async def _tool_snapshot(runtime: Runtime, session_ids: set[str]) -> list[dict[str, Any]]:
    executions = [
        execution
        for session_id in sorted(session_ids)
        for execution in await runtime.store.list_tool_executions(
            session_id=session_id
        )
    ]
    executions.sort(key=lambda item: (item.started_at, item.id))
    return sanitize_for_report(
        [
            {
                "session_id": item.session_id,
                "turn_id": item.turn_id,
                "tool_name": item.tool_name,
                "status": item.status.value,
                "arguments": item.arguments,
                "result": item.result,
                "error_code": item.error_code,
                "elapsed_ms": (
                    round(
                        (item.completed_at - item.started_at).total_seconds()
                        * 1000,
                        2,
                    )
                    if item.completed_at is not None
                    else None
                ),
            }
            for item in executions
        ]
    )


def _reply_has_expression(messages: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        component.get("type") == ComponentType.IMAGE_REF.value
        and component.get("is_expression") is True
        for message in messages
        for component in message.get("components", [])
        if isinstance(component, Mapping)
    )


def evaluate_acceptance(report: Mapping[str, Any], *, sticker_expected: bool) -> dict[str, bool]:
    """按用户可见行为计算验收项，不因报告生成而伪造成功。"""

    sessions = report.get("sessions", {})
    model_attempts = report.get("model_attempts", {})
    private = sessions.get("private", {}) if isinstance(sessions, Mapping) else {}
    group = sessions.get("group", {}) if isinstance(sessions, Mapping) else {}
    private_learning = (
        private.get("learning", {}) if isinstance(private, Mapping) else {}
    )
    group_learning = (
        group.get("learning", {}) if isinstance(group, Mapping) else {}
    )
    private_reply = (
        private.get("acceptance_turn", {})
        if isinstance(private, Mapping)
        else {}
    )
    group_reply = (
        group.get("acceptance_turn", {})
        if isinstance(group, Mapping)
        else {}
    )
    private_text = str(private_reply.get("assistant_text", ""))
    group_text = str(group_reply.get("assistant_text", ""))
    tools = report.get("tool_executions", [])
    sticker_sent = (
        any(
            isinstance(item, Mapping)
            and item.get("tool_name") == "send_expression"
            and item.get("status") == "completed"
            for item in tools
        )
        and (
            _reply_has_expression(private_reply.get("messages", []))
            or _reply_has_expression(group_reply.get("messages", []))
        )
    )
    checks = {
        "模型调用 attempt 已安全落库": bool(
            isinstance(model_attempts, Mapping)
            and int(model_attempts.get("attempt_count", 0)) > 0
            and int(model_attempts.get("success_count", 0)) > 0
        ),
        "私聊产生回复": bool(private_text)
        or _reply_has_expression(private_reply.get("messages", [])),
        "群聊产生回复": bool(group_text)
        or _reply_has_expression(group_reply.get("messages", [])),
        "私聊三类学习库均有结果": all(
            private_learning.get(key)
            for key in ("jargons", "expressions", "behaviors")
        ),
        "群聊三类学习库均有结果": all(
            group_learning.get(key)
            for key in ("jargons", "expressions", "behaviors")
        ),
        "私聊回复复用已学表达": any(
            marker.casefold() in private_text.casefold()
            for marker in ("yyds", "我嘞个", "稳麻了")
        ),
        "群聊回复复用已学表达": any(
            marker in group_text for marker in ("稳麻了", "我嘞个", "接梗")
        ),
        "回复体现 emoji 或图片表情": (
            any(marker in f"{private_text}\n{group_text}" for marker in _EMOJI_MARKERS)
            or _reply_has_expression(private_reply.get("messages", []))
            or _reply_has_expression(group_reply.get("messages", []))
        ),
        "回复保持聊天短句节奏": all(
            not text or len(text) <= 200
            for text in (private_text, group_text)
        ),
    }
    if sticker_expected:
        sticker = report.get("sticker", {})
        checks["入站表情已收集到隔离图库"] = bool(
            isinstance(sticker, Mapping)
            and sticker.get("collected_assets")
        )
        checks["表情工具真实完成并产生图片组件"] = sticker_sent
    return checks


async def run_acceptance(
    settings: AppSettings,
    *,
    sticker_path: Path | None,
    run_id: str,
    model_override: ModelProvider | None = None,
    profile_model_override: ModelProvider | None = None,
) -> dict[str, Any]:
    """运行完整隔离验收并返回尚未写盘的安全报告。"""

    runtime = build_runtime(
        settings,
        model_override=model_override,
        profile_model_override=profile_model_override,
    )
    install_log_redaction()
    started_at = datetime.now(UTC)
    started = time.perf_counter()
    report: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": started_at.isoformat(),
        "status": "running",
        "runtime": {
            "model_mode": settings.model.mode,
            "model_protocol": settings.model.protocol,
            "model_name": settings.model.name,
            "supports_tools": settings.model.supports_tools,
            "embedding_enabled": settings.embedding.available,
            "platform_plugins": [],
            "isolated_storage": True,
        },
        "synthetic_data_only": True,
        "sessions": {},
        "tool_executions": [],
        "tool_execution_summary": {},
        "latency": {},
        "model_attempts": {},
        "checks": {},
    }
    runtime_started = False
    sticker_component: MessageComponent | None = None
    try:
        await runtime.start()
        runtime_started = True
        if sticker_path is not None:
            content = await asyncio.to_thread(sticker_path.read_bytes)
            mime_type = mimetypes.guess_type(sticker_path.name)[0] or ""
            if not mime_type.startswith("image/"):
                raise ValueError("--sticker 必须是可识别 MIME 的图片文件")
            sticker_component = runtime.attachments.save_attachment(
                sticker_path.name,
                mime_type,
                content,
            )
            sticker_component.is_expression = True
            sticker_component.description = "QQ表情包：真实链路验收贴纸，开心、赞同、庆祝"
            report["sticker"] = {
                "synthetic_inbound": True,
                "name": "真实链路验收贴纸",
                "mime_type": sticker_component.mime_type,
            }

        private = await runtime.chat.create_session(
            chat_type=ChatType.PRIVATE,
            display_name="合成私聊验收",
            external_chat_id=f"synthetic-private-{run_id}",
            participants=[
                Participant(
                    external_user_id="synthetic-private-user",
                    display_name="合成用户甲",
                )
            ],
        )
        group = await runtime.chat.create_session(
            chat_type=ChatType.GROUP,
            display_name="合成群聊验收",
            external_chat_id=f"synthetic-group-{run_id}",
            participants=[
                Participant(
                    external_user_id="synthetic-group-owner",
                    display_name="合成群主",
                    role=ParticipantRole.OWNER,
                ),
                Participant(
                    external_user_id="synthetic-group-member",
                    display_name="合成群友",
                ),
            ],
        )

        private_training = (
            "我嘞个，这次改版真是 yyds，我们说 yyds 就是永远的神。",
            "今天接口一把过，yyds，稳麻了 😂",
            "这波体验还是 yyds，我嘞个，确实稳麻了。",
            "朋友也说 yyds；遇到惊喜我们习惯先来一句“我嘞个”，再补个 😂。",
        )
        group_training = (
            ("synthetic-group-owner", "合成群主", "咱群里说“稳麻了”就是特别稳，这次发布稳麻了 😂"),
            ("synthetic-group-member", "合成群友", "看到意外好结果我们先说“我嘞个”，再补“稳麻了”"),
            ("synthetic-group-owner", "合成群主", "接口一次全绿，稳麻了；回复别太正式，短句接梗就行 😂"),
            ("synthetic-group-member", "合成群友", "最后验收还是稳麻了，我嘞个，这波可以"),
        )

        private_turns = [
            await _send_turn(
                runtime,
                session=private,
                sender_id="synthetic-private-user",
                sender_name="合成用户甲",
                text=text,
                mention_agent=False,
                label=f"私聊学习 {index}",
                extra_components=(
                    [sticker_component]
                    if index == 2 and sticker_component is not None
                    else []
                ),
            )
            for index, text in enumerate(private_training, start=1)
        ]
        group_turns = [
            await _send_turn(
                runtime,
                session=group,
                sender_id=sender_id,
                sender_name=sender_name,
                text=text,
                mention_agent=True,
                label=f"群聊学习 {index}",
                extra_components=(
                    [sticker_component]
                    if index == 3 and sticker_component is not None
                    else []
                ),
            )
            for index, (sender_id, sender_name, text) in enumerate(
                group_training, start=1
            )
        ]

        # 第四轮会创建后台学习批次；验收提问前必须等三条学习车道稳定落库。
        await runtime.social_learning.stop()
        if sticker_component is not None:
            assets = await runtime.expressions.list_current()
            report["sticker"]["collected_assets"] = [
                {
                    "name": asset.name,
                    "emotion": asset.emotion,
                    "source": asset.source_kind.value,
                }
                for asset in assets
                # 收集链会再次规范化 PNG，输出字节摘要可以变化；隔离图库在
                # 本次运行前为空，因此权威来源类型比比较编码后 SHA 更准确。
                if asset.source_kind.value == "collected"
            ]
        private_acceptance = await _send_turn(
            runtime,
            session=private,
            sender_id="synthetic-private-user",
            sender_name="合成用户甲",
            text=(
                "用我们刚才的说法评价这次发布，短一点，合适的话带个 emoji；"
                "如果表情库里有验收贴纸，也请直接发一个。"
            ),
            mention_agent=False,
            label="私聊最终验收",
        )
        group_acceptance = await _send_turn(
            runtime,
            session=group,
            sender_id="synthetic-group-owner",
            sender_name="合成群主",
            text=(
                "用群里的黑话和节奏接一句，别写成公告；"
                "如果表情库里有验收贴纸，也请直接发一个。"
            ),
            mention_agent=True,
            label="群聊最终验收",
        )
        await runtime.social_learning.stop()

        report["sessions"] = {
            "private": {
                "chat_type": private.chat_type.value,
                "training_turns": private_turns,
                "acceptance_turn": private_acceptance,
                "learning": await _learning_snapshot(runtime, private),
                "messages": [
                    _message_summary(item)
                    for item in await runtime.store.list_messages(private.id)
                ],
            },
            "group": {
                "chat_type": group.chat_type.value,
                "training_turns": group_turns,
                "acceptance_turn": group_acceptance,
                "learning": await _learning_snapshot(runtime, group),
                "messages": [
                    _message_summary(item)
                    for item in await runtime.store.list_messages(group.id)
                ],
            },
        }
        report["tool_executions"] = await _tool_snapshot(
            runtime, {private.id, group.id}
        )
        all_turns = [
            *private_turns,
            *group_turns,
            private_acceptance,
            group_acceptance,
        ]
        report["latency"] = _latency_summary(all_turns)
        report["tool_execution_summary"] = _tool_execution_summary(
            report["tool_executions"]
        )
        # 只报告 SQL 聚合后的安全事实；attempt 本身不存 Prompt、输出、端点或凭据。
        report["model_attempts"] = (
            await runtime.store.summarize_model_attempts()
        )
        report["checks"] = evaluate_acceptance(
            report, sticker_expected=sticker_path is not None
        )
        report["status"] = (
            "passed" if all(report["checks"].values()) else "failed"
        )
    except Exception as exc:
        report["status"] = "error"
        report["error"] = {
            "type": type(exc).__name__,
            "message": sanitize_text(str(exc)),
        }
    finally:
        if runtime_started:
            try:
                await runtime.stop()
            except Exception as exc:
                report["status"] = "error"
                report["shutdown_error"] = {
                    "type": type(exc).__name__,
                    "message": sanitize_text(str(exc)),
                }
        report["finished_at"] = datetime.now(UTC).isoformat()
        report["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return sanitize_for_report(report)


def render_markdown(report: Mapping[str, Any]) -> str:
    """把结构化结果投影为便于人工复核的中文报告。"""

    lines = [
        "# iJA 真实链路验收报告",
        "",
        f"- 运行 ID：`{report.get('run_id', '-')}`",
        f"- 状态：`{report.get('status', 'unknown')}`",
        f"- 总耗时：`{report.get('elapsed_ms', 0)} ms`",
        "- 数据边界：仅合成用户、隔离数据库、平台插件全部禁用",
        "- 配置披露：报告不包含 API Key、Token 或 Base URL",
        f"- 端到端延迟：`{json.dumps(report.get('latency', {}), ensure_ascii=False)}`",
        "",
        "## 验收检查",
        "",
    ]
    checks = report.get("checks", {})
    if isinstance(checks, Mapping) and checks:
        lines.extend(
            f"- {'通过' if passed else '失败'}：{name}"
            for name, passed in checks.items()
        )
    else:
        lines.append("- 未执行完成")

    sessions = report.get("sessions", {})
    if isinstance(sessions, Mapping):
        for key, title in (("private", "私聊"), ("group", "群聊")):
            session = sessions.get(key)
            if not isinstance(session, Mapping):
                continue
            acceptance = session.get("acceptance_turn", {})
            reply = (
                acceptance.get("assistant_text", "")
                if isinstance(acceptance, Mapping)
                else ""
            )
            learning = session.get("learning", {})
            jargon_count = (
                len(learning.get("jargons", []))
                if isinstance(learning, Mapping)
                else 0
            )
            expression_count = (
                len(learning.get("expressions", []))
                if isinstance(learning, Mapping)
                else 0
            )
            behavior_count = (
                len(learning.get("behaviors", []))
                if isinstance(learning, Mapping)
                else 0
            )
            lines.extend(
                [
                    "",
                    f"## {title}",
                    "",
                    f"- 最终回复：{reply or '（仅图片组件或没有回复）'}",
                    f"- 黑话条目：{jargon_count}",
                    f"- 表达条目：{expression_count}",
                    f"- 行为条目：{behavior_count}",
                    "",
                    "### 学习快照",
                    "",
                    "```json",
                    json.dumps(learning, ensure_ascii=False, indent=2).replace(
                        "```", "\\`\\`\\`"
                    ),
                    "```",
                ]
            )

    tools = report.get("tool_executions", [])
    lines.extend(
        [
            "",
            "## 模型调用观测",
            "",
            "```json",
            json.dumps(
                report.get("model_attempts", {}),
                ensure_ascii=False,
                indent=2,
            ).replace("```", "\\`\\`\\`"),
            "```",
            "",
            "## 工具执行",
            "",
            f"汇总：`{json.dumps(report.get('tool_execution_summary', {}), ensure_ascii=False)}`",
            "",
            "```json",
            json.dumps(tools, ensure_ascii=False, indent=2).replace(
                "```", "\\`\\`\\`"
            ),
            "```",
        ]
    )
    error = report.get("error")
    if error:
        lines.extend(
            [
                "",
                "## 运行错误",
                "",
                f"`{json.dumps(error, ensure_ascii=False)}`",
            ]
        )
    return sanitize_text("\n".join(lines) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="用真实模型配置在隔离数据目录运行私聊、群聊与表情验收"
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=PROJECT_ROOT,
        help="项目根目录，默认使用脚本所在项目",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="持久化隔离验收数据；省略时使用并在结束后删除临时目录",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="报告目录；默认 reports/real-chain/<运行 ID>",
    )
    parser.add_argument(
        "--sticker",
        type=Path,
        default=PROJECT_ROOT / "scripts" / "assets" / "acceptance-sticker.png",
        help="本地图片；默认使用内置合成贴纸并强制验收 send_expression",
    )
    parser.add_argument(
        "--no-sticker",
        action="store_true",
        help="显式跳过表情收集与 send_expression Tool Execution 验收",
    )
    return parser


def _display_path(path: Path, project_root: Path) -> str:
    try:
        return str(path.relative_to(project_root))
    except ValueError:
        return path.name


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    project_root = args.project_root.resolve()
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else project_root / "reports" / "real-chain" / run_id
    )
    try:
        source_settings = load_settings(project_root)
        if source_settings.model.mode != "openai":
            raise ValueError("当前项目没有启用真实模型配置（model.mode 不是 openai）")
        sticker = (
            None
            if args.no_sticker
            else args.sticker.resolve() if args.sticker is not None else None
        )
        if sticker is not None and not sticker.is_file():
            raise ValueError("--sticker 指向的图片不存在或不是文件")

        temporary = (
            tempfile.TemporaryDirectory(prefix="ija-real-chain-")
            if args.data_dir is None
            else nullcontext(str(args.data_dir))
        )
        with temporary as raw_data_dir:
            data_dir = validate_isolated_data_dir(
                Path(raw_data_dir),
                authoritative_data_dir=source_settings.storage.data_dir,
            )
            data_dir.mkdir(parents=True, exist_ok=True)
            settings = build_isolated_settings(
                source_settings,
                data_dir=data_dir,
            )
            report = asyncio.run(
                run_acceptance(
                    settings,
                    sticker_path=sticker,
                    run_id=run_id,
                )
            )
        output_dir.mkdir(parents=True, exist_ok=False)
        json_path = output_dir / "report.json"
        markdown_path = output_dir / "report.md"
        json_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        markdown_path.write_text(render_markdown(report), encoding="utf-8")
        print(f"验收状态: {report.get('status', 'unknown')}")
        print(f"JSON 报告: {_display_path(json_path, project_root)}")
        print(f"Markdown 报告: {_display_path(markdown_path, project_root)}")
        return 0 if report.get("status") == "passed" else 2
    except Exception as exc:
        print(f"验收未启动: {sanitize_text(str(exc))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
