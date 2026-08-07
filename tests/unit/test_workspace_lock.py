from __future__ import annotations

import json
import multiprocessing
import os
from pathlib import Path
from typing import Any

import pytest

from bootstrap import build_runtime
from config import AppSettings
from workspace_lock import WorkspaceLock, WorkspaceLockError


def _hold_workspace_lock(
    workspace: str,
    ready: Any,
    release: Any,
) -> None:
    """在独立进程持锁，模拟已经运行的另一个 iJA 实例。"""

    lock = WorkspaceLock(Path(workspace))
    try:
        lock.acquire()
        ready.put(("acquired", os.getpid()))
        release.wait(timeout=60)
    except BaseException as exc:
        ready.put(("error", repr(exc)))
        raise
    finally:
        lock.release()


def test_second_process_cannot_acquire_same_workspace(tmp_path: Path) -> None:
    """第二进程应快速失败，并在首实例退出后恢复可启动状态。"""

    context = multiprocessing.get_context("spawn")
    ready = context.Queue()
    release = context.Event()
    process = context.Process(
        target=_hold_workspace_lock,
        args=(str(tmp_path), ready, release),
    )
    process.start()
    try:
        status, holder_pid = ready.get(timeout=30)
        assert status == "acquired"

        contender = WorkspaceLock(tmp_path)
        with pytest.raises(WorkspaceLockError, match="工作区已被另一个 iJA 实例占用") as exc_info:
            contender.acquire()
        assert f"PID={holder_pid}" in str(exc_info.value)

        with contender.path.open("rb") as handle:
            handle.seek(1)
            owner = json.loads(handle.read().decode("utf-8"))
        assert set(owner) == {"hostname", "pid", "started_at", "workspace"}
        assert owner["pid"] == holder_pid
    finally:
        release.set()
        process.join(timeout=30)
        if process.is_alive():
            process.terminate()
            process.join(timeout=5)
        ready.close()
        ready.join_thread()

    assert process.exitcode == 0
    with WorkspaceLock(tmp_path) as recovered:
        assert recovered.acquired


@pytest.mark.asyncio
async def test_runtime_releases_workspace_lock_after_normal_stop(settings: AppSettings) -> None:
    """正常停机必须让被拒绝的第二 Runtime 随后可以完整启动。"""

    runtime = build_runtime(settings)
    contender = build_runtime(settings)
    await runtime.start()
    try:
        with pytest.raises(WorkspaceLockError):
            await contender.start()
    finally:
        await runtime.stop()

    await contender.start()
    try:
        assert contender.workspace_lock.acquired
    finally:
        await contender.stop()


@pytest.mark.asyncio
async def test_runtime_releases_workspace_lock_when_startup_fails(
    settings: AppSettings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """迁移和后台服务之前的启动失败也不能遗留进程内占用。"""

    runtime = build_runtime(settings)

    def fail_initialize() -> None:
        raise RuntimeError("模拟启动失败")

    monkeypatch.setattr(runtime.personas, "initialize", fail_initialize)
    with pytest.raises(RuntimeError, match="模拟启动失败"):
        await runtime.start()

    assert not runtime.workspace_lock.acquired
    with WorkspaceLock(settings.storage.data_dir) as recovered:
        assert recovered.acquired
