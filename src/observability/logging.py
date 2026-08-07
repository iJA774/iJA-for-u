"""默认不记录聊天正文和密钥的中文结构化日志。"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .log_hub import LogHub


def configure_logging(data_dir: Path, log_hub: LogHub | None = None) -> None:
    """配置控制台、本地轮转日志，并可选接入实时日志中心。"""

    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s session=%(session_id)s turn=%(turn_id)s"
    )

    class ContextFilter(logging.Filter):
        def filter(self, record: logging.LogRecord) -> bool:
            from .log_hub import redact_sensitive_log_text

            if not hasattr(record, "session_id"):
                record.session_id = "-"
            if not hasattr(record, "turn_id"):
                record.turn_id = "-"
            record.msg = redact_sensitive_log_text(record.getMessage())
            record.args = ()
            if record.exc_info:
                record.exc_text = redact_sensitive_log_text(
                    logging.Formatter().formatException(record.exc_info)
                )
                record.exc_info = None
            return True

    context_filter = ContextFilter()
    handlers: list[logging.Handler] = []
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(context_filter)
    handlers.append(console)
    file_handler = RotatingFileHandler(
        log_dir / "ija.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(context_filter)
    handlers.append(file_handler)
    if log_hub is not None:
        from .log_hub import LogHubHandler

        hub_handler = LogHubHandler(log_hub)
        hub_handler.addFilter(context_filter)
        handlers.append(hub_handler)
    # 显式重置 root logger，避免 basicConfig 在已有 handler 时的语义歧义，
    # 也保证第三方库（如 alembic）重置后能恢复实时日志订阅。
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
        existing.close()
    root.setLevel(logging.INFO)
    for handler in handlers:
        root.addHandler(handler)
