import argparse
from pathlib import Path

import pytest

from benchmarks.benchmark_fts_search import run_benchmark, validate_args


def _args(**overrides: object) -> argparse.Namespace:
    values = {
        "messages": 20,
        "memories": 20,
        "sessions": 4,
        "iterations": 3,
        "message_p95_ms": 0,
        "memory_p95_ms": 0,
        "database": None,
        "overwrite": False,
        "output": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_fts_benchmark_rejects_invalid_workload_and_output(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="messages"):
        validate_args(_args(messages=0))
    with pytest.raises(ValueError, match="message-p95-ms"):
        validate_args(_args(message_p95_ms=-1))

    output = tmp_path / "report.json"
    output.write_text("keep", encoding="utf-8")
    with pytest.raises(FileExistsError, match="output"):
        validate_args(_args(output=output))

    shared = tmp_path / "shared.sqlite3"
    with pytest.raises(ValueError, match="不同路径"):
        validate_args(_args(database=shared, output=shared))


def test_fts_benchmark_reports_correctness_scope_and_percentiles(tmp_path: Path) -> None:
    report = run_benchmark(tmp_path / "benchmark.sqlite3", _args())

    assert report["status"] == "passed"
    assert report["message_fts"]["result_count"] == 1
    assert report["memory_fts"]["result_count"] == 1
    assert report["message_fts"]["p95_ms"] >= report["message_fts"]["p50_ms"]
    assert report["memory_fts"]["p95_ms"] >= report["memory_fts"]["p50_ms"]
    assert all(report["checks"].values())


def test_fts_benchmark_fails_closed_when_latency_budget_is_impossible(
    tmp_path: Path,
) -> None:
    report = run_benchmark(
        tmp_path / "benchmark.sqlite3",
        _args(message_p95_ms=1e-12),
    )

    assert report["status"] == "failed"
    assert report["checks"]["消息 FTS P95 满足门槛"] is False
