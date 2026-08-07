"""Windows-first 的本机凭据密封存储。"""

from __future__ import annotations

import base64
import ctypes
import json
import os
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from domain.errors import InputValidationError

_SCHEMA_VERSION = 1
_MASTER_KEY_ENV = "IJA_SECRET_MASTER_KEY"
_ENTROPY = b"iJA-for-u/local-secret-store/v1"
_LOCK_FILENAME = ".credentials.local.lock"
_PROCESS_LOCKS: dict[Path, threading.RLock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


class SecretStoreError(InputValidationError):
    """安全存储不可用、损坏或无法解密。"""

    code = "secret_store_error"


class LocalSecretStore:
    """把凭据密文保存在 data 目录，密钥由 DPAPI 或显式主密钥拥有。"""

    def __init__(self, data_dir: Path) -> None:
        self.path = data_dir.resolve() / "secrets" / "credentials.local.json"

    def get(self, reference: str) -> str:
        """读取并解密一个精确引用；缺失时响亮失败。"""

        entries = self._read_entries()
        normalized = _validate_reference(reference)
        item = entries.get(normalized)
        if not isinstance(item, dict):
            raise SecretStoreError(f"本机凭据引用不存在：{reference}")
        return self._decrypt_entry(normalized, item)

    def _decrypt_entry(
        self,
        reference: str,
        item: dict[str, str],
    ) -> str:
        """解密已经通过仓库 schema 校验的单个条目。"""

        backend = item.get("backend")
        ciphertext = item.get("ciphertext")
        if not isinstance(ciphertext, str):
            raise SecretStoreError(f"本机凭据密文损坏：{reference}")
        try:
            protected = base64.b64decode(ciphertext, validate=True)
            if backend == "dpapi-v1":
                clear = _dpapi_unprotect(protected)
            elif backend == "aesgcm-v1":
                clear = _aesgcm_unprotect(reference, protected)
            else:
                raise SecretStoreError(f"不支持的凭据密封后端：{backend!r}")
            value = clear.decode("utf-8")
        except (OSError, UnicodeDecodeError, ValueError, InvalidTag) as exc:
            raise SecretStoreError(f"本机凭据无法解密：{reference}") from exc
        if not value:
            raise SecretStoreError(f"本机凭据为空：{reference}")
        return value

    def get_optional(self, reference: str) -> str | None:
        """缺少引用时返回 None；仓库损坏或解密失败仍然响亮失败。"""

        entries = self._read_entries()
        normalized = _validate_reference(reference)
        if normalized not in entries:
            return None
        return self.get(normalized)

    def put(self, reference: str, value: str) -> None:
        """原子写入一个凭据；任何配置或日志都不接触密文前的摘要。"""

        normalized = _validate_reference(reference)
        if not value:
            raise SecretStoreError("不能保存空凭据")
        with self._exclusive_lock():
            entries = self._read_entries()
            entries[normalized] = _protect_entry(normalized, value)
            self._write_entries(entries)

    def get_or_create(
        self,
        reference: str,
        factory: Callable[[], str],
    ) -> tuple[str, bool]:
        """跨线程和进程串行化首次生成，避免两个启动进程拿到不同 Token。"""

        normalized = _validate_reference(reference)
        with self._exclusive_lock():
            entries = self._read_entries()
            existing = entries.get(normalized)
            if existing is not None:
                return self._decrypt_entry(normalized, existing), False
            value = factory()
            if not value:
                raise SecretStoreError("不能保存空凭据")
            entries[normalized] = _protect_entry(normalized, value)
            self._write_entries(entries)
            return value, True

    def delete(self, reference: str) -> None:
        """删除一个引用；不存在时保持幂等。"""

        normalized = _validate_reference(reference)
        with self._exclusive_lock():
            entries = self._read_entries()
            if normalized not in entries:
                return
            entries.pop(normalized)
            self._write_entries(entries)

    @contextmanager
    def _exclusive_lock(self) -> Iterator[None]:
        """持有同一凭据文件的进程内与操作系统排他锁。"""

        lock_path = self.path.parent / _LOCK_FILENAME
        canonical = lock_path.resolve()
        with _PROCESS_LOCKS_GUARD:
            process_lock = _PROCESS_LOCKS.setdefault(canonical, threading.RLock())
        with process_lock:
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with lock_path.open("a+b", buffering=0) as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b" ")
                    handle.flush()
                    os.fsync(handle.fileno())
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read_entries(self) -> dict[str, dict[str, str]]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SecretStoreError("本机凭据仓库无法读取") from exc
        if (
            not isinstance(raw, dict)
            or raw.get("schema_version") != _SCHEMA_VERSION
            or not isinstance(raw.get("entries"), dict)
        ):
            raise SecretStoreError("本机凭据仓库 schema 无效")
        entries: dict[str, dict[str, str]] = {}
        for reference, item in raw["entries"].items():
            if (
                not isinstance(reference, str)
                or not isinstance(item, dict)
                or set(item) != {"backend", "ciphertext"}
                or not all(isinstance(value, str) for value in item.values())
            ):
                raise SecretStoreError("本机凭据仓库条目无效")
            entries[reference] = dict(item)
        return entries

    def _write_entries(self, entries: dict[str, dict[str, str]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        payload = json.dumps(
            {
                "schema_version": _SCHEMA_VERSION,
                "entries": dict(sorted(entries.items())),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(self.path)


def resolve_secret_references(
    values: dict[str, Any],
    *,
    data_dir: Path,
) -> None:
    """在 Pydantic 校验前把受支持的引用解析到仅驻内存的 api_key 字段。"""

    store = LocalSecretStore(data_dir)
    for section_name, key_name, reference_name in _bindings():
        section = values.get(section_name)
        if not isinstance(section, dict):
            continue
        reference = section.get(reference_name)
        if not reference:
            continue
        if not isinstance(reference, str):
            raise SecretStoreError(f"{section_name}.{reference_name} 必须是字符串")
        # 显式环境变量或调用进程注入的密钥优先，不让本地引用覆盖它。
        if not str(section.get(key_name) or "").strip():
            section[key_name] = store.get(reference)


def persist_secret_settings(
    *,
    path: Path,
    section_name: str,
    values: dict[str, Any],
    data_dir: Path,
    key_bindings: tuple[tuple[str, str, str], ...],
) -> None:
    """先安全写凭据、再原子写引用；配置失败时恢复原凭据。"""

    store = LocalSecretStore(data_dir)
    output = {
        key: value
        for key, value in values.items()
        if value is not None
    }
    previous: dict[str, str | None] = {}
    applied: list[str] = []
    try:
        for key_name, reference_name, reference in key_bindings:
            secret = output.pop(key_name, "")
            if not isinstance(secret, str):
                raise SecretStoreError(f"{section_name}.{key_name} 必须是字符串")
            output[reference_name] = reference if secret else ""
            try:
                previous[reference] = store.get(reference)
            except SecretStoreError as exc:
                if "引用不存在" not in str(exc):
                    raise
                previous[reference] = None
            if secret:
                store.put(reference, secret)
            else:
                store.delete(reference)
            applied.append(reference)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".toml.tmp")
        from tomli_w import dumps

        temporary.write_text(
            dumps({section_name: output}),
            encoding="utf-8",
        )
        temporary.replace(path)
    except BaseException:
        for reference in reversed(applied):
            old = previous[reference]
            if old is None:
                store.delete(reference)
            else:
                store.put(reference, old)
        raise


def _bindings() -> tuple[tuple[str, str, str], ...]:
    return (
        ("model", "api_key", "api_key_ref"),
        ("model", "profile_api_key", "profile_api_key_ref"),
        ("image_model", "api_key", "api_key_ref"),
        ("vision_model", "api_key", "api_key_ref"),
        ("detection_model", "api_key", "api_key_ref"),
        ("embedding", "api_key", "api_key_ref"),
    )


def _validate_reference(reference: str) -> str:
    if (
        not reference
        or len(reference) > 200
        or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789._-" for character in reference)
    ):
        raise SecretStoreError("本机凭据引用格式无效")
    return reference


def _protect_entry(reference: str, value: str) -> dict[str, str]:
    """把单个明文转换为带后端标记的密封条目。"""

    clear = value.encode("utf-8")
    if _master_key() is not None:
        backend = "aesgcm-v1"
        protected = _aesgcm_protect(reference, clear)
    elif os.name == "nt":
        backend = "dpapi-v1"
        protected = _dpapi_protect(clear)
    else:
        raise SecretStoreError(
            "当前平台没有可用系统钥匙串；请通过环境变量提供 API Key，"
            f"或设置 {_MASTER_KEY_ENV} 为 32 字节随机密钥的 Base64"
        )
    return {
        "backend": backend,
        "ciphertext": base64.b64encode(protected).decode("ascii"),
    }


def _master_key() -> bytes | None:
    encoded = os.environ.get(_MASTER_KEY_ENV)
    if not encoded:
        return None
    try:
        key = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise SecretStoreError(f"{_MASTER_KEY_ENV} 不是合法 Base64") from exc
    if len(key) != 32:
        raise SecretStoreError(f"{_MASTER_KEY_ENV} 解码后必须恰好 32 字节")
    return key


def _aesgcm_protect(reference: str, clear: bytes) -> bytes:
    key = _master_key()
    if key is None:
        raise SecretStoreError("AES-GCM 主密钥未配置")
    nonce = os.urandom(12)
    encrypted = AESGCM(key).encrypt(nonce, clear, reference.encode("utf-8"))
    return nonce + encrypted


def _aesgcm_unprotect(reference: str, protected: bytes) -> bytes:
    key = _master_key()
    if key is None:
        raise SecretStoreError(
            f"凭据 {reference} 使用外部主密钥密封，但 {_MASTER_KEY_ENV} 未配置"
        )
    if len(protected) < 29:
        raise SecretStoreError("AES-GCM 密文长度无效")
    return AESGCM(key).decrypt(
        protected[:12],
        protected[12:],
        reference.encode("utf-8"),
    )


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _blob(data: bytes) -> tuple[_DataBlob, Any]:
    buffer = ctypes.create_string_buffer(data)
    result = _DataBlob(
        len(data),
        ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)),
    )
    return result, buffer


def _dpapi_protect(clear: bytes) -> bytes:
    if os.name != "nt":
        raise SecretStoreError("DPAPI 只在 Windows 可用")
    input_blob, input_buffer = _blob(clear)
    entropy_blob, entropy_buffer = _blob(_ENTROPY)
    output_blob = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "iJA local credential",
        ctypes.byref(entropy_blob),
        None,
        None,
        0x1,
        ctypes.byref(output_blob),
    ):
        raise OSError(ctypes.get_last_error(), "Windows DPAPI 加密失败")
    del input_buffer, entropy_buffer
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _dpapi_unprotect(protected: bytes) -> bytes:
    if os.name != "nt":
        raise SecretStoreError("DPAPI 密文只能由原 Windows 用户解密")
    input_blob, input_buffer = _blob(protected)
    entropy_blob, entropy_buffer = _blob(_ENTROPY)
    output_blob = _DataBlob()
    description = wintypes.LPWSTR()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        ctypes.byref(description),
        ctypes.byref(entropy_blob),
        None,
        None,
        0x1,
        ctypes.byref(output_blob),
    ):
        raise OSError(ctypes.get_last_error(), "Windows DPAPI 解密失败")
    del input_buffer, entropy_buffer
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        if description:
            kernel32.LocalFree(description)
        kernel32.LocalFree(output_blob.pbData)
