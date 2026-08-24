"""可复现的隔离 Runtime 并发/重启 soak；不由 pytest 默认长时间运行。

默认使用 Fake Provider、临时数据库和 Web 模拟 Channel，不读取本机模型凭据，
也不启动任何真实平台。可用 ``--duration-seconds`` 把同一检查扩展为小时级 soak。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import statistics
import sys
import tempfile
import time
import tracemalloc
import uuid
from collections.abc import Sequence
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from sqlalchemy import text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from adapters.model import FakeModelProvider  # noqa: E402
from bootstrap import Runtime, build_runtime  # noqa: E402
from config import AppSettings  # noqa: E402
from domain.models import (  # noqa: E402
    ChatType,
    ComponentType,
    InboundMessage,
    MessageComponent,
    Participant,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="运行隔离的 Runtime 并发、数据完整性与重启恢复 soak"
    )
    parser.add_argument("--sessions", type=int, default=16)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument(
        "--duration-seconds",
        type=float,
        default=0,
        help="至少运行的墙钟时长；0 表示只跑指定轮数",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        help="保留隔离数据；省略时使用并清理临时目录",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="可选 JSON 报告路径；既有文件不会被覆盖",
    )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    """拒绝无界并发、空 workload 和隐式覆盖。"""

    if args.sessions < 1:
        raise ValueError("--sessions 必须大于 0")
    if args.rounds < 1:
        raise ValueError("--rounds 必须大于 0")
    if args.concurrency < 1 or args.concurrency > args.sessions:
        raise ValueError("--concurrency 必须在 1 到 sessions 之间")
    if args.duration_seconds < 0:
        raise ValueError("--duration-seconds 不能为负数")
    if args.data_dir is not None and args.data_dir.exists():
        if not args.data_dir.is_dir() or any(args.data_dir.iterdir()):
            raise ValueError("--data-dir 必须不存在或为空目录")
    if args.output is not None and args.output.exists():
        raise FileExistsError("--output 已存在，拒绝覆盖")


def build_soak_settings(data_dir: Path) -> AppSettings:
    """构造不读取真实配置、不访问网络的最小运行时。"""

    return AppSettings.model_validate(
        {
            "project_root": PROJECT_ROOT,
            "storage": {"data_dir": data_dir},
            "chat": {"private_debounce_ms": 0, "group_debounce_ms": 0},
            "memory": {"enabled": False},
            "social_learning": {"enabled": False},
            "embedding": {"enabled": False},
            "platform_plugins": {
                "enabled": [],
                "disabled": [],
                "options": {},
            },
        }
    )


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return round(ordered[index], 3)


def _quiet_runtime_logging() -> None:
    """Soak 只输出聚合报告，避免逐 Turn 日志污染终端。"""

    logging.getLogger().setLevel(logging.WARNING)
    for value in logging.root.manager.loggerDict.values():
        if isinstance(value, logging.Logger):
            value.setLevel(logging.WARNING)


async def _database_checks(runtime: Runtime) -> dict[str, Any]:
    async with runtime.store.engine.connect() as connection:
        integrity = (
            await connection.execute(text("PRAGMA integrity_check"))
        ).scalar_one()
        foreign_key_rows = (
            await connection.execute(text("PRAGMA foreign_key_check"))
        ).all()
    return {
        "integrity_check": integrity,
        "foreign_key_violation_count": len(foreign_key_rows),
    }


async def _recovery_snapshot(
    runtime: Runtime,
    session_ids: set[str],
) -> dict[str, Any]:
    """生成可跨进程比较的权威会话状态摘要，避免只比较总行数。"""

    sessions = [
        item
        for item in await runtime.store.list_sessions()
        if item.id in session_ids
    ]
    messages = [
        item
        for session_id in sorted(session_ids)
        for item in await runtime.store.list_messages(session_id)
    ]
    decisions = [
        item
        for session_id in sorted(session_ids)
        for item in await runtime.store.list_decisions(session_id)
    ]
    payload = {
        "sessions": [
            item.model_dump(mode="json")
            for item in sorted(sessions, key=lambda value: value.id)
        ],
        "messages": [
            item.model_dump(mode="json")
            for item in sorted(messages, key=lambda value: value.id)
        ],
        "decisions": [
            item.model_dump(mode="json")
            for item in sorted(decisions, key=lambda value: value.id)
        ],
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return {
        "sha256": hashlib.sha256(canonical).hexdigest(),
        "session_count": len(sessions),
        "message_count": len(messages),
        "decision_count": len(decisions),
    }


async def run_soak(
    settings: AppSettings,
    *,
    session_count: int,
    minimum_rounds: int,
    concurrency: int,
    duration_seconds: float,
) -> dict[str, Any]:
    """并发推进独立 Session，验证精确计数、重启恢复与 SQLite 不变量。"""

    runtime = build_runtime(
        settings,
        model_override=FakeModelProvider(response_text="收到，继续。"),
    )
    # 基准报告提供聚合数据，逐 Turn INFO 仍写隔离日志但不淹没控制台。
    _quiet_runtime_logging()
    started = time.perf_counter()
    deadline = started + duration_seconds
    latencies_ms: list[float] = []
    operations = 0
    operation_lock = asyncio.Lock()
    sessions = []
    tracemalloc.start()
    await runtime.start()
    try:
        for index in range(session_count):
            group = index % 2 == 1
            sessions.append(
                await runtime.chat.create_session(
                    chat_type=ChatType.GROUP if group else ChatType.PRIVATE,
                    display_name=f"Soak {'群聊' if group else '私聊'} {index}",
                    external_chat_id=f"soak-chat-{index}",
                    participants=[
                        Participant(
                            external_user_id=f"soak-user-{index}",
                            display_name=f"Soak 用户 {index}",
                        )
                    ],
                )
            )

        semaphore = asyncio.Semaphore(concurrency)

        async def exercise(index: int) -> None:
            nonlocal operations
            session = sessions[index]
            round_index = 0
            async with semaphore:
                while (
                    round_index < minimum_rounds
                    or time.perf_counter() < deadline
                ):
                    components = [
                        MessageComponent.text_component(
                            f"Soak 第 {round_index} 轮：请确认收到。"
                        )
                    ]
                    if session.chat_type == ChatType.GROUP:
                        components.insert(
                            0,
                                MessageComponent(
                                    type=ComponentType.MENTION,
                                target_id="agent",
                                target_name="小佳",
                            ),
                        )
                    turn_started = time.perf_counter()
                    ingress = await runtime.chat.ingest(
                        InboundMessage(
                            platform=session.platform,
                            account_id=session.account_id,
                            external_message_id=(
                                f"soak-{index}-{round_index}-{uuid.uuid4().hex}"
                            ),
                            external_chat_id=session.external_chat_id,
                            sender_id=f"soak-user-{index}",
                            sender_name=f"Soak 用户 {index}",
                            chat_type=session.chat_type,
                            components=components,
                        ),
                        schedule_turn=False,
                    )
                    if not ingress.accepted:
                        raise RuntimeError("隔离 Soak 入站被意外拒绝")
                    await runtime.chat.process_session(session.id)
                    latencies_ms.append(
                        (time.perf_counter() - turn_started) * 1000
                    )
                    async with operation_lock:
                        operations += 1
                    round_index += 1

        await asyncio.gather(
            *(exercise(index) for index in range(session_count))
        )
        message_count = 0
        decision_count = 0
        pending_count = 0
        for session in sessions:
            message_count += len(await runtime.store.list_messages(session.id))
            decision_count += len(await runtime.store.list_decisions(session.id))
            pending_count += len(
                await runtime.store.list_pending_messages(session.id)
            )
        database = await _database_checks(runtime)
        session_ids = {session.id for session in sessions}
        before_restart = await _recovery_snapshot(runtime, session_ids)
        _, peak_bytes = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        await runtime.stop()

    restarted = build_runtime(
        settings,
        model_override=FakeModelProvider(response_text="收到，继续。"),
    )
    _quiet_runtime_logging()
    await restarted.start()
    try:
        restored_sessions = await restarted.store.list_sessions()
        restored_messages = 0
        for session in restored_sessions:
            restored_messages += len(
                await restarted.store.list_messages(session.id)
            )
        restart_database = await _database_checks(restarted)
        after_restart = await _recovery_snapshot(restarted, session_ids)
    finally:
        await restarted.stop()

    checks = {
        "每轮恰好提交一条用户消息和一条助手消息": message_count
        == operations * 2,
        "每轮恰好冻结一个 TurnDecision": decision_count == operations,
        "没有遗留待处理消息": pending_count == 0,
        "SQLite integrity_check 通过": database["integrity_check"] == "ok",
        "SQLite 外键检查无违规": database["foreign_key_violation_count"] == 0,
        "重启后 Session 数量一致": len(restored_sessions) == session_count,
        "重启后消息数量一致": restored_messages == message_count,
        "重启前后权威状态摘要一致": after_restart == before_restart,
        "重启后 SQLite 不变量仍成立": (
            restart_database["integrity_check"] == "ok"
            and restart_database["foreign_key_violation_count"] == 0
        ),
    }
    elapsed = time.perf_counter() - started
    return {
        "schema_version": 1,
        "status": "passed" if all(checks.values()) else "failed",
        "workload": {
            "sessions": session_count,
            "minimum_rounds": minimum_rounds,
            "concurrency": concurrency,
            "requested_duration_seconds": duration_seconds,
            "operations": operations,
        },
        "observed": {
            "elapsed_seconds": round(elapsed, 3),
            "operations_per_second": round(operations / elapsed, 3),
            "latency_ms_p50": _percentile(latencies_ms, 0.50),
            "latency_ms_p95": _percentile(latencies_ms, 0.95),
            "latency_ms_max": round(max(latencies_ms, default=0), 3),
            "latency_ms_mean": round(
                statistics.fmean(latencies_ms) if latencies_ms else 0,
                3,
            ),
            "python_peak_mib": round(peak_bytes / 1024 / 1024, 3),
            "message_count": message_count,
            "decision_count": decision_count,
        },
        "database": database,
        "restart_database": restart_database,
        "recovery": {
            "before_restart": before_restart,
            "after_restart": after_restart,
        },
        "checks": checks,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        validate_args(args)
        temporary = (
            tempfile.TemporaryDirectory(prefix="ija-runtime-soak-")
            if args.data_dir is None
            else nullcontext(str(args.data_dir.resolve()))
        )
        with temporary as raw_data_dir:
            data_dir = Path(raw_data_dir)
            data_dir.mkdir(parents=True, exist_ok=True)
            report = asyncio.run(
                run_soak(
                    build_soak_settings(data_dir),
                    session_count=args.sessions,
                    minimum_rounds=args.rounds,
                    concurrency=args.concurrency,
                    duration_seconds=args.duration_seconds,
                )
            )
        rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 0 if report["status"] == "passed" else 2
    except Exception as exc:
        print(f"Runtime soak 未完成: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
