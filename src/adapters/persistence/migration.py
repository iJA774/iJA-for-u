"""程序启动时执行受控数据库迁移。"""

from pathlib import Path

from alembic import command
from alembic.config import Config


def upgrade_database(project_root: Path, database_path: Path) -> None:
    """将数据库迁移到当前代码声明的最新 revision。"""

    database_path.parent.mkdir(parents=True, exist_ok=True)
    config = Config(str(project_root / "alembic.ini"))
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database_path.as_posix()}")
    command.upgrade(config, "head")
