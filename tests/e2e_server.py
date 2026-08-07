"""为 Playwright 启动使用隔离数据目录的本地服务。"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from pathlib import Path
from uuid import uuid4


def main() -> None:
    """在工作区忽略目录中创建本次 E2E 独享数据库。"""

    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8000)
    arguments = parser.parse_args()
    project_root = Path(__file__).parents[1]
    data_dir = project_root / ".e2e-data" / uuid4().hex
    data_dir.mkdir(parents=True, exist_ok=False)
    runtime_root = data_dir / "workspace"
    for relative in (
        Path("config/personas"),
        Path("prompts/common"),
        Path("prompts/scenes"),
        Path("prompts/persona"),
        Path("prompts/tasks"),
        Path("prompts/channels"),
        Path("skills"),
        Path("migrations"),
        Path("frontend/dist"),
    ):
        shutil.copytree(project_root / relative, runtime_root / relative)
    shutil.copy2(project_root / "config/default.toml", runtime_root / "config/default.toml")
    shutil.copy2(project_root / "alembic.ini", runtime_root / "alembic.ini")
    os.environ["IJA_DATA_DIR"] = str(runtime_root / "data")
    # E2E 不得读取开发者本机的真实模型配置或向外发送聊天内容。
    os.environ["IJA_MODEL_MODE"] = "fake"
    os.chdir(runtime_root)

    import uvicorn

    from adapters.model import FakeImageModelProvider, FakeModelProvider
    from api import create_app
    from config import load_settings
    from ports import ModelRequest, ModelResult, ModelToolCall
    from proactive.rss import FeedFetchResult, FeedItem

    class E2EModelProvider(FakeModelProvider):
        """仅供端到端场景驱动可选表情 Skill，不污染通用 Fake Provider。"""

        async def complete(self, request: ModelRequest) -> ModelResult:
            system_text = "\n".join(
                message.content or ""
                for message in request.messages
                if message.role == "system"
            )
            if "# 主动候选判断任务" in system_text:
                payload = self._last_untrusted_payload(request)
                candidates = payload.get("candidates", [])
                if candidates and all(
                    str(item.get("title") or "").startswith("E2E Drift 原料")
                    for item in candidates
                ):
                    self.requests.append(request)
                    return ModelResult(
                        content=json.dumps(
                            {
                                "action": "skip",
                                "candidate_id": None,
                                "score": 0.2,
                                "reason_code": "needs_aggregation",
                                "reason": "单条价值不足，等待 Drift 聚合",
                            },
                            ensure_ascii=False,
                        )
                    )
            last_message = request.messages[-1] if request.messages else None
            if last_message and last_message.role == "tool":
                tool_result = json.loads(last_message.content or "{}")
                if tool_result.get("skill") == "send-expression":
                    self.requests.append(request)
                    runtime = tool_result.get("runtime") or {}
                    names = runtime.get("expression_names") or []
                    arguments = (
                        {
                            "action": "reuse",
                            "name": names[0],
                            "emotion": "开心",
                            "caption": "一起开心！",
                        }
                        if names
                        else {
                            "action": "generate",
                            "name": "开心挥手",
                            "emotion": "开心",
                            "image_prompt": "眼睛弯弯地笑着挥手",
                        }
                    )
                    return ModelResult(
                        tool_calls=[
                            ModelToolCall(
                                id="e2e_send_expression",
                                name="send_expression",
                                arguments=json.dumps(arguments, ensure_ascii=False),
                            )
                        ],
                        finish_reason="tool_calls",
                    )
            user_text = next(
                (
                    item.content or ""
                    for item in reversed(request.messages)
                    if item.role == "user"
                ),
                "",
            )
            available = {tool.name for tool in request.tools or []}
            expression_request = bool(
                re.search(
                    r"(?:发|来)(?:个|一个|一张)?[^，。！？\s]{0,8}表情",
                    user_text,
                )
            ) or "庆祝一下" in user_text
            # 插件终态工具只会在 load_skill 成功后的下一轮动态出现；
            # 首轮只能以 load_skill 是否可见判断 Skill 可用性。
            if "load_skill" in available and expression_request:
                self.requests.append(request)
                return ModelResult(
                    tool_calls=[
                        ModelToolCall(
                            id="e2e_load_expression_skill",
                            name="load_skill",
                            arguments='{"skill":"send-expression"}',
                        )
                    ],
                    finish_reason="tool_calls",
                )
            return await super().complete(request)

    class E2EFeedSource:
        """返回固定真实形态候选，避免 E2E 访问外网。"""

        async def fetch(self, source) -> FeedFetchResult:
            if "drift" in source.url:
                return FeedFetchResult(
                    title=source.title or "E2E Drift Feed",
                    items=[
                        FeedItem(
                            source_key=f"e2e-drift-{index}",
                            title=f"E2E Drift 原料 {index}",
                            summary="单条价值较低，聚合后才适合主动分享。",
                            url=f"https://example.com/e2e-drift-{index}",
                            published_at=None,
                        )
                        for index in (1, 2)
                    ],
                    etag='"e2e-drift"',
                    last_modified=None,
                )
            return FeedFetchResult(
                title=source.title or "E2E Feed",
                items=[
                    FeedItem(
                        source_key="e2e-entry-1",
                        title="E2E 主动候选",
                        summary="用于验证主动触达完整链路。",
                        url="https://example.com/e2e-entry-1",
                        published_at=None,
                    )
                ],
                etag='"e2e"',
                last_modified=None,
            )

        async def close(self) -> None:
            return None

    settings = load_settings(runtime_root)
    uvicorn.run(
        create_app(
            settings=settings,
            model_override=E2EModelProvider(),
            image_model_override=FakeImageModelProvider(),
            rss_override=E2EFeedSource(),
        ),
        host=settings.server.host,
        port=arguments.port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
