"""工作区可恢复备份、校验与恢复边界。"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from uuid import uuid4

from bootstrap import upgrade_database
from workspace_lock import WorkspaceLock

_MANIFEST_NAME = "manifest.json"
_PAYLOAD_DIR = "workspace"
_MANIFEST_SCHEMA = 1
_EXCLUDED_TOP_LEVEL = {"logs"}
_EXCLUDED_NAMES = {".credentials.local.lock", ".ija-workspace.lock"}
_EXCLUDED_SUFFIXES = {".tmp", ".partial"}


class BackupValidationError(RuntimeError):
    """备份内容、哈希或数据库完整性不满足恢复契约。"""

    code = "backup_validation_failed"


@dataclass(frozen=True, slots=True)
class BackupFile:
    """manifest 中一项可恢复文件。"""

    path: str
    size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class BackupManifest:
    """不含凭据和正文摘要的备份清单。"""

    schema_version: int
    created_at: str
    application_version: str
    database_name: str
    database_revision: str
    files: tuple[BackupFile, ...]
    project_fingerprints: dict[str, str]

    def to_json(self) -> str:
        """稳定序列化，便于签名、审计和重复校验。"""

        return json.dumps(
            {
                "schema_version": self.schema_version,
                "created_at": self.created_at,
                "application_version": self.application_version,
                "database_name": self.database_name,
                "database_revision": self.database_revision,
                "files": [
                    {
                        "path": item.path,
                        "size": item.size,
                        "sha256": item.sha256,
                    }
                    for item in self.files
                ],
                "project_fingerprints": self.project_fingerprints,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )


def create_backup(
    *,
    data_dir: Path,
    database_name: str,
    project_root: Path,
    destination: Path,
) -> BackupManifest:
    """持有工作区单实例锁，创建经 SQLite 校验后原子发布的目录备份。"""

    source_root = data_dir.resolve()
    database_path = (source_root / database_name).resolve()
    target = destination.resolve()
    _require_outside(target, source_root, "备份目标不能位于源数据目录内")
    if target.exists():
        raise FileExistsError(f"备份目标已经存在：{target}")
    if not database_path.is_file():
        raise FileNotFoundError(f"SQLite 数据库不存在：{database_path}")

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.parent / f".{target.name}.tmp-{uuid4().hex}"
    payload = temporary / _PAYLOAD_DIR
    temporary.mkdir(parents=False, exist_ok=False)
    payload.mkdir()
    try:
        with WorkspaceLock(source_root):
            _backup_sqlite(database_path, payload / database_name)
            _copy_workspace_payload(
                source_root=source_root,
                destination_root=payload,
                database_name=database_name,
            )
            _assert_sqlite_integrity(payload / database_name)
            files = tuple(_inventory_files(payload))
            manifest = BackupManifest(
                schema_version=_MANIFEST_SCHEMA,
                created_at=datetime.now(UTC).isoformat(),
                application_version=_application_version(),
                database_name=database_name,
                database_revision=_database_revision(payload / database_name),
                files=files,
                project_fingerprints=_project_fingerprints(project_root.resolve()),
            )
            (temporary / _MANIFEST_NAME).write_text(
                manifest.to_json(),
                encoding="utf-8",
            )
            verify_backup(temporary)
        temporary.replace(target)
        return manifest
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def verify_backup(backup_root: Path) -> BackupManifest:
    """严格校验 manifest、文件集合、SHA-256 与 SQLite integrity_check。"""

    root = backup_root.resolve()
    manifest_path = root / _MANIFEST_NAME
    payload = root / _PAYLOAD_DIR
    if not manifest_path.is_file() or not payload.is_dir():
        raise BackupValidationError("备份缺少 manifest.json 或 workspace 目录")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest = _parse_manifest(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise BackupValidationError(f"备份 manifest 无效：{exc}") from exc

    expected = {item.path: item for item in manifest.files}
    actual_files = {
        path.relative_to(payload).as_posix(): path
        for path in _safe_files(payload)
    }
    if set(actual_files) != set(expected):
        missing = sorted(set(expected) - set(actual_files))
        extra = sorted(set(actual_files) - set(expected))
        raise BackupValidationError(
            f"备份文件集合不一致：missing={missing[:5]} extra={extra[:5]}"
        )
    for relative, path in actual_files.items():
        item = expected[relative]
        if path.stat().st_size != item.size:
            raise BackupValidationError(f"备份文件大小不匹配：{relative}")
        if _sha256(path) != item.sha256:
            raise BackupValidationError(f"备份文件哈希不匹配：{relative}")

    database_path = _safe_relative(payload, manifest.database_name)
    _assert_sqlite_integrity(database_path)
    if _database_revision(database_path) != manifest.database_revision:
        raise BackupValidationError("备份数据库 revision 与 manifest 不一致")
    return manifest


def restore_backup(
    *,
    backup_root: Path,
    target_data_dir: Path,
    allow_replace: bool = False,
) -> BackupManifest:
    """校验后恢复到目标数据目录；失败时恢复原文件，不触碰日志与锁文件。"""

    root = backup_root.resolve()
    target = target_data_dir.resolve()
    _validate_restore_target(target)
    _require_outside(root, target, "备份源不能位于待恢复的数据目录内")
    manifest = verify_backup(root)
    target.mkdir(parents=True, exist_ok=True)
    staging = target.parent / f".{target.name}.restore-{uuid4().hex}"
    rollback = target.parent / f".{target.name}.rollback-{uuid4().hex}"
    _require_outside(staging, target, "恢复 staging 不能位于目标数据目录内")
    shutil.copytree(root / _PAYLOAD_DIR, staging)
    _assert_sqlite_integrity(staging / manifest.database_name)

    with WorkspaceLock(target):
        current_items = [
            item
            for item in target.iterdir()
            if item.name not in _EXCLUDED_NAMES
            and item.name not in _EXCLUDED_TOP_LEVEL
        ]
        if current_items and not allow_replace:
            shutil.rmtree(staging, ignore_errors=True)
            raise FileExistsError("目标数据目录非空；恢复覆盖需要显式 allow_replace=True")
        rollback.mkdir(parents=False, exist_ok=False)
        installed: list[Path] = []
        moved_old: list[tuple[Path, Path]] = []
        try:
            for item in current_items:
                old = rollback / item.name
                item.replace(old)
                moved_old.append((old, item))
            for item in list(staging.iterdir()):
                destination = target / item.name
                item.replace(destination)
                installed.append(destination)
            _assert_sqlite_integrity(target / manifest.database_name)
        except BaseException:
            for item in reversed(installed):
                _remove_path(item)
            for old, original in reversed(moved_old):
                if old.exists():
                    old.replace(original)
            raise
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(rollback)
    return manifest


def restore_smoke(
    *,
    backup_root: Path,
    project_root: Path,
) -> BackupManifest:
    """在隔离临时目录真实恢复、迁移到 head，再次执行完整性检查。"""

    manifest = verify_backup(backup_root)
    with tempfile.TemporaryDirectory(prefix="ija-restore-smoke-") as temporary:
        data_dir = Path(temporary) / "data"
        restore_backup(
            backup_root=backup_root,
            target_data_dir=data_dir,
        )
        database_path = data_dir / manifest.database_name
        upgrade_database(project_root.resolve(), database_path)
        _assert_sqlite_integrity(database_path)
        if not _database_revision(database_path):
            raise BackupValidationError("恢复演练后的数据库缺少 Alembic revision")
    return manifest


def _copy_workspace_payload(
    *,
    source_root: Path,
    destination_root: Path,
    database_name: str,
) -> None:
    """复制数据库之外的权威数据；运行时文件和 SQLite sidecar 不进入备份。"""

    sqlite_files = {
        name.casefold()
        for name in {database_name, *_sqlite_sidecar_names(database_name)}
    }
    for source in _safe_files(source_root):
        relative = source.relative_to(source_root)
        if relative.as_posix().casefold() in sqlite_files:
            continue
        if relative.parts[0] in _EXCLUDED_TOP_LEVEL:
            continue
        if source.name in _EXCLUDED_NAMES or source.suffix in _EXCLUDED_SUFFIXES:
            continue
        destination = destination_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)


def _safe_files(root: Path):
    for current_root, directories, filenames in os.walk(root):
        current = Path(current_root)
        for name in list(directories):
            path = current / name
            if path.is_symlink():
                raise BackupValidationError(f"备份不接受符号链接目录：{path}")
        for name in filenames:
            path = current / name
            if path.is_symlink() or not path.is_file():
                raise BackupValidationError(f"备份不接受链接或特殊文件：{path}")
            yield path


def _inventory_files(payload: Path):
    for path in sorted(_safe_files(payload), key=lambda item: item.as_posix()):
        yield BackupFile(
            path=path.relative_to(payload).as_posix(),
            size=path.stat().st_size,
            sha256=_sha256(path),
        )


def _backup_sqlite(source_path: Path, destination_path: Path) -> None:
    source = sqlite3.connect(f"{source_path.as_uri()}?mode=ro", uri=True)
    destination = sqlite3.connect(destination_path)
    try:
        source.backup(destination)
        # backup API 已把源 WAL 中的已提交页合并进目标主库。恢复包必须是独立
        # SQLite 文件，不能依赖或误配源工作区的 WAL/SHM。
        journal_mode = destination.execute("PRAGMA journal_mode=DELETE").fetchone()
        if journal_mode is None or str(journal_mode[0]).casefold() != "delete":
            raise BackupValidationError("SQLite 备份无法切换为独立 DELETE journal")
    finally:
        destination.close()
        source.close()
    leftovers = [
        destination_path.with_name(name)
        for name in _sqlite_sidecar_names(destination_path.name)
        if destination_path.with_name(name).exists()
    ]
    if leftovers:
        raise BackupValidationError(
            f"SQLite 备份残留 sidecar：{[path.name for path in leftovers]}"
        )


def _assert_sqlite_integrity(path: Path) -> None:
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            rows = connection.execute("PRAGMA integrity_check").fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise BackupValidationError(f"SQLite 无法打开或校验：{path.name}") from exc
    if rows != [("ok",)]:
        raise BackupValidationError(f"SQLite integrity_check 失败：{rows[:5]}")


def _database_revision(path: Path) -> str:
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        try:
            row = connection.execute(
                "SELECT version_num FROM alembic_version"
            ).fetchone()
        except sqlite3.Error as exc:
            raise BackupValidationError("数据库缺少可读取的 Alembic revision") from exc
    finally:
        connection.close()
    if row is None or not isinstance(row[0], str) or not row[0]:
        raise BackupValidationError("数据库 Alembic revision 为空")
    return row[0]


def _parse_manifest(raw: object) -> BackupManifest:
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version",
        "created_at",
        "application_version",
        "database_name",
        "database_revision",
        "files",
        "project_fingerprints",
    }:
        raise ValueError("字段集合不符合 schema")
    if raw["schema_version"] != _MANIFEST_SCHEMA:
        raise ValueError(f"不支持的 schema_version={raw['schema_version']!r}")
    database_name = _validate_relative_name(raw["database_name"])
    forbidden_sqlite_sidecars = {
        name.casefold() for name in _sqlite_sidecar_names(database_name)
    }
    if not isinstance(raw["files"], list) or not raw["files"]:
        raise ValueError("files 必须是非空数组")
    files: list[BackupFile] = []
    known: set[str] = set()
    for item in raw["files"]:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise ValueError("files 项字段无效")
        path = _validate_relative_path(item["path"])
        if path.casefold() in forbidden_sqlite_sidecars:
            raise ValueError(f"files 不得包含 SQLite sidecar：{path}")
        size = item["size"]
        digest = item["sha256"]
        if (
            path in known
            or not isinstance(size, int)
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise ValueError("files 项值无效")
        known.add(path)
        files.append(BackupFile(path=path, size=size, sha256=digest))
    fingerprints = raw["project_fingerprints"]
    if not isinstance(fingerprints, dict) or not all(
        isinstance(key, str)
        and isinstance(value, str)
        and len(value) == 64
        for key, value in fingerprints.items()
    ):
        raise ValueError("project_fingerprints 无效")
    for field in ("created_at", "application_version", "database_revision"):
        if not isinstance(raw[field], str) or not raw[field]:
            raise ValueError(f"{field} 无效")
    return BackupManifest(
        schema_version=_MANIFEST_SCHEMA,
        created_at=raw["created_at"],
        application_version=raw["application_version"],
        database_name=database_name,
        database_revision=raw["database_revision"],
        files=tuple(files),
        project_fingerprints=dict(sorted(fingerprints.items())),
    )


def _project_fingerprints(project_root: Path) -> dict[str, str]:
    """只记录仓库内非敏感声明的摘要，不复制 local 配置或 API Key。"""

    candidates = [project_root / "config" / "default.toml"]
    for pattern in ("plugins/*/plugin.toml", "skills/*/SKILL.md", "skills/*/runtime.toml"):
        candidates.extend(project_root.glob(pattern))
    result: dict[str, str] = {}
    for path in sorted(set(candidates)):
        if path.is_file() and not path.is_symlink():
            result[path.relative_to(project_root).as_posix()] = _sha256(path)
    return result


def _safe_relative(root: Path, relative: str) -> Path:
    path = (root / _validate_relative_path(relative)).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError as exc:
        raise BackupValidationError("manifest 路径越出备份目录") from exc
    return path


def _validate_relative_name(value: object) -> str:
    result = _validate_relative_path(value)
    if "/" in result:
        raise ValueError("database_name 必须是单个文件名")
    return result


def _validate_relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("相对路径无效")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or "." in path.parts:
        raise ValueError("相对路径包含越界片段")
    normalized = path.as_posix()
    if normalized != value:
        raise ValueError("相对路径未规范化")
    return normalized


def _sqlite_sidecar_names(database_name: str) -> set[str]:
    """返回不能随主库备份或恢复的 SQLite 运行时 sidecar 文件名。"""

    return {
        f"{database_name}-wal",
        f"{database_name}-shm",
        f"{database_name}-journal",
    }


def _require_outside(candidate: Path, protected: Path, message: str) -> None:
    try:
        candidate.relative_to(protected)
    except ValueError:
        return
    raise ValueError(message)


def _validate_restore_target(target: Path) -> None:
    """拒绝把根目录或用户主目录当成可覆盖的数据工作区。"""

    if target.parent == target or target == Path.home().resolve():
        raise ValueError("恢复目标不能是文件系统根目录或用户主目录")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def _application_version() -> str:
    try:
        return version("ija-for-u")
    except PackageNotFoundError:
        return "0.1.0+workspace"
