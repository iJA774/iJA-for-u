"""可复现的 SQLite FTS 规模基准；不由 pytest 默认收集。

默认数据形状为 10 万消息、1 万记忆、1000 个 Session，其中一半数据集中在
一个 hot Session，用来同时验证严格 scope 与大 Session 的候选预筛延迟。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
import tempfile
import time
import tracemalloc
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from adapters.persistence.migration import upgrade_database  # noqa: E402

_CREATED_AT = "2026-01-01 00:00:00.000000"
_MESSAGE_NEEDLE = "赛博火锅暗号"
_MEMORY_NEEDLE = "下周末去杭州参加独立音乐节"
_MEMORY_MATCH = (
    '"下周末" OR "周末去" OR "末去杭" OR "去杭州" OR "杭州参" '
    'OR "州参加" OR "参加独" OR "加独立" OR "独立音" OR "立音乐" '
    'OR "音乐节"'
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="运行 iJA SQLite FTS 规模基准")
    parser.add_argument("--messages", type=int, default=100_000)
    parser.add_argument("--memories", type=int, default=10_000)
    parser.add_argument("--sessions", type=int, default=1_000)
    parser.add_argument("--iterations", type=int, default=100)
    parser.add_argument(
        "--message-p95-ms",
        type=float,
        default=0,
        help="消息检索 P95 门槛；0 表示只报告、不设门槛",
    )
    parser.add_argument(
        "--memory-p95-ms",
        type=float,
        default=0,
        help="记忆候选检索 P95 门槛；0 表示只报告、不设门槛",
    )
    parser.add_argument(
        "--database",
        type=Path,
        help="保留基准数据库到指定路径；默认使用并清理临时目录",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="允许覆盖 --database 指定的既有文件",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="可选 JSON 报告路径；既有文件不会被覆盖",
    )
    return parser


def validate_args(args: argparse.Namespace) -> None:
    """校验 workload、延迟门槛与显式覆盖边界。"""

    for name in ("messages", "memories", "sessions", "iterations"):
        if getattr(args, name) < 1:
            raise ValueError(f"--{name} 必须大于 0")
    for name in ("message_p95_ms", "memory_p95_ms"):
        if getattr(args, name) < 0:
            raise ValueError(f"--{name.replace('_', '-')} 不能为负数")
    if args.database is not None and args.database.exists() and not args.overwrite:
        raise FileExistsError("基准数据库已存在；如确认覆盖，请显式传入 --overwrite")
    if args.output is not None and args.output.exists():
        raise FileExistsError("--output 已存在，拒绝覆盖")
    if (
        args.database is not None
        and args.output is not None
        and args.database.resolve() == args.output.resolve()
    ):
        raise ValueError("--database 与 --output 必须使用不同路径")


def _session_id(index: int) -> str:
    return f"session-{index:06d}"


def _record_session(index: int, count: int, session_count: int) -> str:
    """把一半数据放入 hot Session，其余稳定分散到所有 Session。"""

    hot_count = max(1, count // 2)
    if index < hot_count:
        return _session_id(min(777, session_count - 1))
    return _session_id(index % session_count)


def _message_rows(count: int, session_count: int) -> Iterator[tuple[Any, ...]]:
    target_index = max(0, count // 4)
    for index in range(count):
        session_id = _record_session(index, count, session_count)
        content = (
            f"今晚{_MESSAGE_NEEDLE}启动，只有这一条是检索目标"
            if index == target_index
            else f"普通聊天记录 {index:06d}：今天继续讨论项目进度"
        )
        yield (
            f"message-{index:09d}",
            session_id,
            "benchmark",
            "ija-benchmark",
            f"external-{index:09d}",
            "user",
            f"user-{index % 100:03d}",
            "基准用户",
            json.dumps([{"type": "text", "text": content}], ensure_ascii=False),
            content.casefold(),
            None,
            None,
            None,
            None,
            _CREATED_AT,
        )


def _memory_rows(count: int, session_count: int) -> Iterator[tuple[Any, ...]]:
    target_index = max(0, count // 4)
    for index in range(count):
        session_id = _record_session(index, count, session_count)
        content = (
            f"用户计划{_MEMORY_NEEDLE}，需要提前订票"
            if index == target_index
            else f"普通长期记忆 {index:06d}：用户持续关注项目迭代节奏"
        )
        yield (
            f"memory-{index:09d}",
            session_id,
            f"private:{session_id}",
            f"user-{index % 100:03d}",
            "event",
            content,
            hashlib.sha256(content.casefold().encode()).hexdigest(),
            0.9,
            0.7,
            "active",
            "manual",
            None,
            "[]",
            "[]",
            None,
            None,
            1,
            0,
            None,
            _CREATED_AT,
            _CREATED_AT,
        )


def _seed(
    connection: sqlite3.Connection,
    *,
    message_count: int,
    memory_count: int,
    session_count: int,
) -> float:
    started = time.perf_counter()
    connection.executemany(
        """
        INSERT INTO sessions(
            id, platform, account_id, external_chat_id, chat_type, display_name,
            revision, data_epoch, memory_cleared_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            (
                _session_id(index),
                "benchmark",
                "ija-benchmark",
                f"chat-{index:06d}",
                "private",
                f"基准会话-{index:06d}",
                1,
                1,
                None,
                _CREATED_AT,
                _CREATED_AT,
            )
            for index in range(session_count)
        ),
    )
    connection.executemany(
        """
        INSERT INTO messages(
            id, session_id, platform, account_id, external_message_id, role,
            sender_id, sender_name, components_json, search_text,
            processed_turn_id, origin, origin_run_id, source_refs_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        _message_rows(message_count, session_count),
    )
    connection.executemany(
        """
        INSERT INTO memory_records(
            id, session_id, scope_key, subject_id, kind, content, content_hash,
            confidence, importance, status, source_chain, source_run_id,
            source_message_ids_json, source_refs_json, supersedes_id, happened_at,
            reinforcement, recall_count, last_recalled_at, created_at, updated_at
        ) VALUES (
            ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
        )
        """,
        _memory_rows(memory_count, session_count),
    )
    connection.commit()
    return time.perf_counter() - started


def _percentile(values: list[float], percentile: float) -> float:
    ordered = sorted(values)
    index = round((len(ordered) - 1) * percentile)
    return ordered[index]


def _measure(
    operation: Callable[[], list[tuple[Any, ...]]],
    *,
    iterations: int,
) -> dict[str, float | int]:
    for _ in range(min(10, iterations)):
        operation()
    durations: list[float] = []
    result_count = 0
    for _ in range(iterations):
        started = time.perf_counter()
        result_count = len(operation())
        durations.append((time.perf_counter() - started) * 1000)
    return {
        "result_count": result_count,
        "p50_ms": round(_percentile(durations, 0.50), 3),
        "p95_ms": round(_percentile(durations, 0.95), 3),
        "max_ms": round(max(durations), 3),
    }


def run_benchmark(database_path: Path, args: argparse.Namespace) -> dict[str, Any]:
    """运行 FTS 规模基准，并同时验证命中正确性与 Session 隔离。"""

    upgrade_database(PROJECT_ROOT, database_path)
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=OFF")
    fts_tables = {
        row[0]
        for row in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE name IN ('message_search_fts', 'memory_search_fts')
            """
        )
    }
    if fts_tables != {"message_search_fts", "memory_search_fts"}:
        raise RuntimeError("当前 SQLite 缺少 FTS5 trigram，无法运行本基准")

    seed_seconds = _seed(
        connection,
        message_count=args.messages,
        memory_count=args.memories,
        session_count=args.sessions,
    )
    hot_session_id = _session_id(min(777, args.sessions - 1))
    hot_scope_key = f"private:{hot_session_id}"
    message_target_id = f"message-{max(0, args.messages // 4):09d}"
    memory_target_id = f"memory-{max(0, args.memories // 4):09d}"

    def message_query() -> list[tuple[Any, ...]]:
        return connection.execute(
            """
            SELECT messages.id
            FROM message_search_fts
            JOIN messages ON messages.id = message_search_fts.message_id
            WHERE message_search_fts MATCH ?
              AND message_search_fts.session_id = ?
            ORDER BY messages.created_at DESC, messages.id DESC
            LIMIT 20
            """,
            (f'"{_MESSAGE_NEEDLE}"', hot_session_id),
        ).fetchall()

    def memory_query() -> list[tuple[Any, ...]]:
        return connection.execute(
            """
            SELECT memory_id
            FROM memory_search_fts
            WHERE memory_search_fts MATCH ?
              AND scope_key = ?
              AND status = 'active'
            ORDER BY bm25(memory_search_fts), memory_id
            LIMIT 1000
            """,
            (_MEMORY_MATCH, hot_scope_key),
        ).fetchall()

    tracemalloc.start()
    message_result = _measure(message_query, iterations=args.iterations)
    memory_result = _measure(memory_query, iterations=args.iterations)
    message_ids = [row[0] for row in message_query()]
    memory_ids = [row[0] for row in memory_query()]
    if args.sessions > 1:
        other_session_id = _session_id(0 if hot_session_id != _session_id(0) else 1)
        other_message_count = connection.execute(
            """
            SELECT count(*)
            FROM message_search_fts
            WHERE message_search_fts MATCH ? AND session_id = ?
            """,
            (f'"{_MESSAGE_NEEDLE}"', other_session_id),
        ).fetchone()[0]
        other_memory_count = connection.execute(
            """
            SELECT count(*)
            FROM memory_search_fts
            WHERE memory_search_fts MATCH ? AND scope_key = ? AND status = 'active'
            """,
            (_MEMORY_MATCH, f"private:{other_session_id}"),
        ).fetchone()[0]
    else:
        other_message_count = 0
        other_memory_count = 0
    _current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()

    checks = {
        "消息 FTS 精确命中目标": message_ids == [message_target_id],
        "记忆 FTS 精确命中目标": memory_ids == [memory_target_id],
        "消息 FTS 没有跨 Session 泄漏": other_message_count == 0,
        "记忆 FTS 没有跨 Scope 泄漏": other_memory_count == 0,
        "消息 FTS P95 满足门槛": (
            args.message_p95_ms == 0
            or message_result["p95_ms"] <= args.message_p95_ms
        ),
        "记忆 FTS P95 满足门槛": (
            args.memory_p95_ms == 0
            or memory_result["p95_ms"] <= args.memory_p95_ms
        ),
    }
    return {
        "schema_version": 1,
        "status": "passed" if all(checks.values()) else "failed",
        "shape": {
            "messages": args.messages,
            "memories": args.memories,
            "sessions": args.sessions,
            "hot_session_fraction": 0.5,
            "iterations": args.iterations,
        },
        "sqlite_version": sqlite3.sqlite_version,
        "seed_seconds": round(seed_seconds, 3),
        "database_mib": round(database_path.stat().st_size / 1024 / 1024, 2),
        "query_python_peak_mib": round(peak_bytes / 1024 / 1024, 3),
        "message_fts": message_result,
        "memory_fts": memory_result,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    validate_args(args)
    if args.database is not None:
        database_path = args.database.resolve()
        database_path.parent.mkdir(parents=True, exist_ok=True)
        if database_path.exists():
            database_path.unlink()
        report = run_benchmark(database_path, args)
    else:
        with tempfile.TemporaryDirectory(prefix="ija-fts-benchmark-") as temp_dir:
            database_path = Path(temp_dir) / "benchmark.sqlite3"
            report = run_benchmark(database_path, args)
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
