"""跨平台工作区单实例锁。"""

from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path
from threading import Lock
from typing import BinaryIO

_LOCK_FILENAME = ".ija-workspace.lock"
_OWNER_LINE_LIMIT = 4096
_OWNER_OFFSET = 1
_HELD_PATHS: set[Path] = set()
_HELD_PATHS_GUARD = Lock()


class WorkspaceLockError(RuntimeError):
    """工作区已由另一个 iJA 实例持有。"""

    code = "workspace_locked"


class WorkspaceLock:
    """用操作系统文件锁保证同一数据工作区只有一个运行时 owner。

    锁文件只保存 PID、主机名和启动时间等诊断元数据，不保存命令行、环境变量
    或凭据。锁文件会保留在数据目录中；真正的所有权由文件描述符上的 OS 锁决定，
    因此进程崩溃后内核会自动释放，不依赖删除可能产生竞态的哨兵文件。
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.path = self.workspace / _LOCK_FILENAME
        self._handle: BinaryIO | None = None

    @property
    def acquired(self) -> bool:
        """返回当前对象是否持有锁。"""

        return self._handle is not None

    def acquire(self) -> None:
        """非阻塞获取工作区锁；占用时立即给出持有者提示。"""

        if self._handle is not None:
            raise RuntimeError("当前 WorkspaceLock 已经持有工作区锁")
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        handle = self.path.open("r+b", buffering=0)
        if handle.seek(0, os.SEEK_END) == 0:
            # Windows 的 msvcrt.locking 需要锁定一个实际存在的字节。
            handle.write(b" ")
            handle.flush()
            os.fsync(handle.fileno())
        canonical_path = self.path.resolve()
        try:
            with _HELD_PATHS_GUARD:
                if canonical_path in _HELD_PATHS:
                    raise WorkspaceLockError(self._occupied_message(handle))
                try:
                    _acquire_os_lock(handle)
                except OSError as exc:
                    raise WorkspaceLockError(self._occupied_message(handle)) from exc
                _HELD_PATHS.add(canonical_path)
        except BaseException:
            handle.close()
            raise

        self._handle = handle
        try:
            self._write_owner()
        except BaseException:
            self.release()
            raise

    def release(self) -> None:
        """释放当前对象持有的锁；重复释放是安全的。"""

        handle = self._handle
        if handle is None:
            return
        self._handle = None
        canonical_path = self.path.resolve()
        try:
            _release_os_lock(handle)
        finally:
            handle.close()
            with _HELD_PATHS_GUARD:
                _HELD_PATHS.discard(canonical_path)

    def __enter__(self) -> WorkspaceLock:
        self.acquire()
        return self

    def __exit__(self, *_: object) -> None:
        self.release()

    def _write_owner(self) -> None:
        """在持锁后写入有界诊断信息，供第二实例给出恢复提示。"""

        handle = self._require_handle()
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "hostname": socket.gethostname(),
                "started_at": datetime.now(UTC).isoformat(),
                "workspace": str(self.workspace),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        if len(payload) + 1 > _OWNER_LINE_LIMIT:
            raise OSError("工作区锁诊断信息超过大小上限")
        # Windows 的字节区间锁会阻止其他进程读取首字节，诊断信息必须避开它。
        handle.seek(_OWNER_OFFSET)
        handle.write(payload + b"\n")
        handle.truncate()
        handle.flush()
        os.fsync(handle.fileno())

    def _occupied_message(self, handle: BinaryIO) -> str:
        owner = _read_owner(handle)
        if owner is None:
            holder = "持有者信息不可读"
        else:
            pid = owner.get("pid", "未知")
            hostname = owner.get("hostname", "未知主机")
            started_at = owner.get("started_at", "未知时间")
            holder = f"持有者 PID={pid}，主机={hostname}，启动时间={started_at}"
        return (
            f"工作区已被另一个 iJA 实例占用：{self.workspace}；{holder}。"
            "请先关闭该实例，或为另一个实例配置独立的 IJA_DATA_DIR。"
        )

    def _require_handle(self) -> BinaryIO:
        handle = self._handle
        if handle is None:
            raise RuntimeError("工作区锁尚未获取")
        return handle


def _read_owner(handle: BinaryIO) -> dict[str, object] | None:
    """尽力读取占用者元数据；损坏或旧文件不能伪装成可用锁。"""

    try:
        handle.seek(_OWNER_OFFSET)
        raw = handle.readline(_OWNER_LINE_LIMIT)
        parsed = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    return parsed


def _acquire_os_lock(handle: BinaryIO) -> None:
    """使用当前平台的非阻塞排他文件锁。"""

    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _release_os_lock(handle: BinaryIO) -> None:
    """释放当前平台文件锁。"""

    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
