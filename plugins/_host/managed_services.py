"""以受限环境和 JSON Lines 协议管理插件外部服务。"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import uuid
from collections.abc import Mapping
from typing import Any

from domain.errors import InputValidationError

from .contributions import ManagedServiceSpec

logger = logging.getLogger(__name__)

MAX_MESSAGE_BYTES = 1024 * 1024

_ENVIRONMENT_ALLOWLIST = {
    "LANG",
    "LC_ALL",
    "PATH",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "WINDIR",
}


class ManagedServiceError(RuntimeError):
    """受管服务启动、协议或业务调用失败。"""


class ManagedServiceClient:
    """一个受管子进程及其并发安全的 RPC 通道。"""

    API_VERSION = 1
    MAX_PENDING_REQUESTS = 1024
    MAX_ABANDONED_REQUESTS = 1024

    def __init__(self, spec: ManagedServiceSpec) -> None:
        self.spec = spec
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._abandoned: set[str] = set()
        self._write_lock = asyncio.Lock()
        self._stopping = False

    @property
    def running(self) -> bool:
        """返回服务进程是否仍在运行。"""

        return (
            self._process is not None
            and self._process.returncode is None
            and self._reader_task is not None
            and not self._reader_task.done()
        )

    async def start(self) -> None:
        """启动服务并等待严格的 ready 握手。"""

        if self._process is not None:
            raise ManagedServiceError(f"受管服务不能重复启动: {self.spec.service_id}")
        self._stopping = False
        environment = {
            key: value for key, value in os.environ.items() if key.upper() in _ENVIRONMENT_ALLOWLIST
        }
        environment.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"})
        process = await asyncio.create_subprocess_exec(
            *self.spec.resolved_command(),
            cwd=self.spec.working_directory,
            env=environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            # StreamReader 的默认上限约为 64 KiB；协议允许 1 MiB JSON 行，
            # 额外一个字节留给换行符，真正边界仍由 _decode_message 校验。
            limit=MAX_MESSAGE_BYTES + 1,
        )
        self._process = process
        self._stderr_task = asyncio.create_task(
            self._read_stderr(),
            name=f"managed-service-stderr:{self.spec.service_id}",
        )
        try:
            assert process.stdout is not None
            line = await asyncio.wait_for(
                self._readline(process.stdout, stage="启动握手"),
                timeout=self.spec.startup_timeout_seconds,
            )
            ready = self._decode_message(line, stage="启动握手")
            if ready != {
                "type": "ready",
                "api_version": self.API_VERSION,
                "service": self.spec.name,
            }:
                raise ManagedServiceError(f"受管服务 ready 握手无效: {self.spec.service_id}")
            self._reader_task = asyncio.create_task(
                self._read_responses(),
                name=f"managed-service-reader:{self.spec.service_id}",
            )
        except BaseException:
            await self.stop()
            raise

    async def request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """向服务发送一次有界 RPC 请求，并返回结构化结果。"""

        if not self.running:
            raise ManagedServiceError(f"受管服务未运行: {self.spec.service_id}")
        if not isinstance(method, str) or not method or len(method) > 128:
            raise InputValidationError("受管服务 method 无效")
        if not isinstance(params, Mapping):
            raise InputValidationError("受管服务 params 必须是映射")
        request_id = uuid.uuid4().hex
        message = {
            "type": "request",
            "request_id": request_id,
            "method": method,
            "params": dict(params),
        }
        try:
            serialized = json.dumps(message, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise InputValidationError("受管服务请求无法序列化为 JSON") from exc
        if len(serialized) > MAX_MESSAGE_BYTES:
            raise InputValidationError("受管服务请求超过 1 MiB")
        payload = serialized + b"\n"
        if len(self._pending) >= self.MAX_PENDING_REQUESTS:
            raise ManagedServiceError(
                f"受管服务在途请求达到上限: {self.spec.service_id}"
            )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[request_id] = future
        try:
            async with self._write_lock:
                process = self._process
                if process is None or process.stdin is None:
                    raise ManagedServiceError(f"受管服务输入通道已关闭: {self.spec.service_id}")
                process.stdin.write(payload)
                await process.stdin.drain()
            response = await asyncio.wait_for(
                future,
                timeout=self.spec.request_timeout_seconds,
            )
        except TimeoutError as exc:
            await self._remember_abandoned(request_id)
            raise ManagedServiceError(f"受管服务请求超时: {self.spec.service_id}:{method}") from exc
        except asyncio.CancelledError:
            # 调用方取消不等于服务违反协议；服务仍可能合法返回已接收的请求。
            await self._remember_abandoned(request_id)
            raise
        finally:
            self._pending.pop(request_id, None)
        if "error" in response:
            error = response["error"]
            if not isinstance(error, dict):
                raise ManagedServiceError(f"受管服务错误响应无效: {self.spec.service_id}")
            code = error.get("code", error.get("type", "unknown"))
            message_text = error.get("message", "未提供错误说明")
            raise ManagedServiceError(
                f"受管服务调用失败: {self.spec.service_id}:{method}: {code}: {message_text}"
            )
        result = response.get("result")
        if not isinstance(result, dict):
            raise ManagedServiceError(f"受管服务 result 必须是对象: {self.spec.service_id}")
        return result

    async def stop(self) -> None:
        """请求服务正常退出；超时后终止，确保子进程不泄漏。"""

        process = self._process
        if process is None:
            return
        self._stopping = True
        if process.returncode is None and process.stdin is not None:
            try:
                process.stdin.write(b'{"type":"shutdown"}\n')
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
        if process.returncode is None:
            try:
                await asyncio.wait_for(
                    process.wait(),
                    timeout=self.spec.shutdown_timeout_seconds,
                )
            except TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=1.0)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        await self._cancel_task(self._reader_task)
        await self._cancel_task(self._stderr_task)
        self._reader_task = None
        self._stderr_task = None
        self._process = None
        self._fail_pending(ManagedServiceError(f"受管服务已停止: {self.spec.service_id}"))
        self._abandoned.clear()

    async def _read_responses(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            while line := await self._readline(process.stdout, stage="响应"):
                message = self._decode_message(line, stage="响应")
                if set(message) not in (
                    {"type", "request_id", "result"},
                    {"type", "request_id", "error"},
                ) or message.get("type") != "response":
                    raise ManagedServiceError(f"受管服务响应协议无效: {self.spec.service_id}")
                request_id = message.get("request_id")
                if not isinstance(request_id, str):
                    raise ManagedServiceError(f"受管服务返回未知 request_id: {self.spec.service_id}")
                if request_id in self._abandoned:
                    self._abandoned.discard(request_id)
                    logger.debug(
                        "丢弃已超时或取消请求的迟到响应",
                        extra={
                            "session_id": "-",
                            "turn_id": "-",
                            "plugin_id": self.spec.plugin_id,
                            "service": self.spec.name,
                            "request_id": request_id,
                        },
                    )
                    continue
                if request_id not in self._pending:
                    raise ManagedServiceError(f"受管服务返回未知 request_id: {self.spec.service_id}")
                future = self._pending[request_id]
                if not future.done():
                    future.set_result(message)
            if self._stopping:
                return
            if process.returncode is None:
                await process.wait()
            raise ManagedServiceError(
                f"受管服务意外退出: {self.spec.service_id}: exit={process.returncode}"
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._fail_pending(exc)
            logger.error(
                "受管服务响应读取失败",
                exc_info=(type(exc), exc, exc.__traceback__),
                extra={
                    "session_id": "-",
                    "turn_id": "-",
                    "plugin_id": self.spec.plugin_id,
                    "service": self.spec.name,
                },
            )

    async def _read_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        while line := await process.stderr.readline():
            logger.warning(
                "受管服务 stderr: %s",
                line.decode("utf-8", errors="replace").rstrip()[:2000],
                extra={
                    "session_id": "-",
                    "turn_id": "-",
                    "plugin_id": self.spec.plugin_id,
                    "service": self.spec.name,
                },
            )

    @staticmethod
    def _decode_message(line: bytes, *, stage: str) -> dict[str, Any]:
        payload_size = len(line) - 1 if line.endswith(b"\n") else len(line)
        if not line or payload_size > MAX_MESSAGE_BYTES:
            raise ManagedServiceError(f"受管服务{stage}为空或超过 1 MiB")
        try:
            message = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ManagedServiceError(f"受管服务{stage}不是合法 JSON") from exc
        if not isinstance(message, dict):
            raise ManagedServiceError(f"受管服务{stage}必须是 JSON 对象")
        return message

    @staticmethod
    async def _readline(
        stream: asyncio.StreamReader,
        *,
        stage: str,
    ) -> bytes:
        """把 StreamReader 的边界异常转换成稳定且不包含响应正文的协议错误。"""

        try:
            return await stream.readline()
        except ValueError as exc:
            raise ManagedServiceError(f"受管服务{stage}超过 1 MiB") from exc

    def _fail_pending(self, error: BaseException) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(error)

    async def _remember_abandoned(self, request_id: str) -> None:
        """有界记录迟到响应；容量耗尽时关闭通道，绝不误判合法响应。"""

        if len(self._abandoned) >= self.MAX_ABANDONED_REQUESTS:
            error = ManagedServiceError(
                f"受管服务迟到响应记录达到上限，通道已关闭: {self.spec.service_id}"
            )
            self._fail_pending(error)
            await self.stop()
            raise error
        self._abandoned.add(request_id)

    @staticmethod
    async def _cancel_task(task: asyncio.Task[None] | None) -> None:
        if task is None or task.done():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


class ManagedServiceManager:
    """拥有全部插件外部服务生命周期和 capability 注入的唯一 owner。"""

    def __init__(self, specs: tuple[ManagedServiceSpec, ...]) -> None:
        self._clients: list[ManagedServiceClient] = []
        self.capabilities: dict[str, ManagedServiceClient] = {}
        for spec in specs:
            client = ManagedServiceClient(spec)
            for capability in spec.capabilities:
                if capability in self.capabilities:
                    raise InputValidationError(f"插件服务 capability 重复: {capability}")
                self.capabilities[capability] = client
            self._clients.append(client)
        self._started: list[ManagedServiceClient] = []

    @property
    def ready(self) -> bool:
        """所有声明服务都完成握手且子进程仍存活时才算 ready。"""

        return len(self._started) == len(self._clients) and all(
            client.running for client in self._started
        )

    async def start(self) -> None:
        """依声明顺序启动；失败时逆序回滚已经启动的服务。"""

        try:
            for client in self._clients:
                await client.start()
                self._started.append(client)
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        """逆序停止所有已启动服务，并记录但不掩盖其他清理。"""

        clients = list(reversed(self._started))
        self._started.clear()
        results = await asyncio.gather(*(client.stop() for client in clients), return_exceptions=True)
        for client, result in zip(clients, results, strict=True):
            if isinstance(result, BaseException):
                logger.error(
                    "停止受管服务失败",
                    exc_info=(type(result), result, result.__traceback__),
                    extra={
                        "session_id": "-",
                        "turn_id": "-",
                        "plugin_id": client.spec.plugin_id,
                        "service": client.spec.name,
                    },
                )
