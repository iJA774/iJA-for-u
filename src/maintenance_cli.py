"""iJA 数据维护命令行。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from config import control_login_url, load_settings, migrate_legacy_secret_files
from data_governance import (
    create_backup,
    restore_backup,
    restore_smoke,
    verify_backup,
)


def main() -> None:
    """执行显式备份、校验或恢复操作。"""

    parser = argparse.ArgumentParser(description="iJA 本地数据备份与恢复")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("backup", help="创建经校验的工作区备份")
    backup.add_argument("destination", type=Path)

    verify = subparsers.add_parser("verify", help="校验备份 manifest、哈希和数据库")
    verify.add_argument("backup", type=Path)

    smoke = subparsers.add_parser("restore-smoke", help="在临时目录执行真实恢复与迁移演练")
    smoke.add_argument("backup", type=Path)

    restore = subparsers.add_parser("restore", help="恢复到当前或指定数据目录")
    restore.add_argument("backup", type=Path)
    restore.add_argument("--data-dir", type=Path)
    restore.add_argument(
        "--confirm",
        required=True,
        help="覆盖已有数据时必须明确填写 RESTORE",
    )
    subparsers.add_parser(
        "migrate-secrets",
        help="显式把现有本机 TOML 明文凭据迁移到安全存储",
    )
    subparsers.add_parser(
        "control-login-url",
        help="显式输出含一次性前端交接 fragment 的本机登录链接",
    )

    arguments = parser.parse_args()
    settings = load_settings()
    if arguments.command == "backup":
        manifest = create_backup(
            data_dir=settings.storage.data_dir,
            database_name=settings.storage.database_name,
            project_root=settings.project_root,
            destination=arguments.destination,
        )
    elif arguments.command == "verify":
        manifest = verify_backup(arguments.backup)
    elif arguments.command == "restore-smoke":
        manifest = restore_smoke(
            backup_root=arguments.backup,
            project_root=settings.project_root,
        )
    elif arguments.command == "restore":
        if arguments.confirm != "RESTORE":
            parser.error("恢复覆盖必须使用 --confirm RESTORE")
        manifest = restore_backup(
            backup_root=arguments.backup,
            target_data_dir=arguments.data_dir or settings.storage.data_dir,
            allow_replace=True,
        )
    elif arguments.command == "migrate-secrets":
        migrated = migrate_legacy_secret_files(
            settings.project_root,
            data_dir=settings.storage.data_dir,
        )
        print(
            json.dumps(
                {"ok": True, "migrated_files": migrated},
                ensure_ascii=False,
            )
        )
        return
    else:
        print(control_login_url(settings))
        return
    print(
        json.dumps(
            {
                "ok": True,
                "database_revision": manifest.database_revision,
                "file_count": len(manifest.files),
                "created_at": manifest.created_at,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
