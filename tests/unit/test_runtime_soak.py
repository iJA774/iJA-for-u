import argparse
from pathlib import Path

import pytest

from benchmarks.benchmark_runtime_soak import (
    build_soak_settings,
    run_soak,
    validate_args,
)


def test_runtime_soak_rejects_empty_or_unsafe_workload(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="sessions"):
        validate_args(
            argparse.Namespace(
                sessions=0,
                rounds=1,
                concurrency=1,
                duration_seconds=0,
                data_dir=None,
                output=None,
            )
        )

    occupied = tmp_path / "occupied"
    occupied.mkdir()
    (occupied / "keep.txt").write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError, match="data-dir"):
        validate_args(
            argparse.Namespace(
                sessions=1,
                rounds=1,
                concurrency=1,
                duration_seconds=0,
                data_dir=occupied,
                output=None,
            )
        )


@pytest.mark.asyncio
async def test_runtime_soak_smoke_proves_counts_restart_and_foreign_keys(
    tmp_path: Path,
) -> None:
    report = await run_soak(
        build_soak_settings(tmp_path / "soak"),
        session_count=2,
        minimum_rounds=2,
        concurrency=2,
        duration_seconds=0,
    )

    assert report["status"] == "passed"
    assert report["workload"]["operations"] == 4
    assert report["observed"]["message_count"] == 8
    assert all(report["checks"].values())
