"""Alembic 迁移环境。

不在此处调用 ``logging.config.fileConfig``：应用已通过 ``configure_logging``
统一配置控制台、轮转文件与实时日志中心。Alembic 自身的迁移日志会经由 root
logger 传播到这些 handler，避免迁移重置日志配置、丢失实时日志订阅。
"""

import logging

from alembic import context
from sqlalchemy import engine_from_config, pool

from adapters.persistence.database import Base

config = context.config
# Alembic 默认 fileConfig 会用 alembic.ini 的 handlers 覆盖 root logger，
# 这里改为让 alembic 日志走应用统一配置；仅显式放开 alembic logger 级别。
logging.getLogger("alembic").setLevel(logging.INFO)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
