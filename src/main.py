"""Windows 单机启动入口。"""

from __future__ import annotations

import uvicorn

from api import create_app
from config import ensure_control_token, load_settings


def run(port: int | None = None) -> None:
    """加载配置并仅在本机回环地址启动服务。"""

    settings = load_settings()
    ensure_control_token(settings)
    uvicorn.run(
        create_app(settings=settings),
        host=settings.server.host,
        port=port or settings.server.port,
        log_config=None,
    )


if __name__ == "__main__":
    run()
