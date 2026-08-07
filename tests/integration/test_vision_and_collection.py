import asyncio
import json
from io import BytesIO

import pytest
from PIL import Image

from bootstrap import build_runtime
from domain.errors import NotFoundError
from domain.models import (
    ChatType,
    InboundMessage,
    MessageComponent,
    Participant,
)
from ports import ModelRequest, ModelResult


def png_bytes(size: tuple[int, int] = (180, 100)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, "#f2a2c0").save(output, format="PNG")
    return output.getvalue()


class VisionJsonModel:
    """记录视觉请求并返回稳定的表情理解结构。"""

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []
        self.close_calls = 0

    async def complete(self, request: ModelRequest) -> ModelResult:
        self.requests.append(request)
        assert request.messages[-1].images
        return ModelResult(
            content=json.dumps(
                {
                    "description": "粉色小人得意地叉腰笑，适合轻松炫耀或接梗。",
                    "emotions": ["得意", "调侃"],
                    "expression_name": "得意叉腰",
                },
                ensure_ascii=False,
            )
        )

    async def probe(self) -> dict[str, object]:
        return {"ok": True}

    async def close(self) -> None:
        self.close_calls += 1


class BlockingVisionModel(VisionJsonModel):
    """模拟超过聊天等待时间、只能在停机时取消的视觉请求。"""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = False

    async def complete(self, request: ModelRequest) -> ModelResult:
        self.requests.append(request)
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        raise AssertionError("阻塞视觉模型不应自然返回")


class ExpressionChoiceModel(VisionJsonModel):
    """只用于候选拼图复选，稳定选择第二张素材。"""

    async def complete(self, request: ModelRequest) -> ModelResult:
        self.requests.append(request)
        assert request.messages[-1].images
        assert "候选表情拼图" in (request.messages[0].content or "")
        return ModelResult(
            content=json.dumps(
                {
                    "candidate_index": 2,
                    "reason": "第二张的动作和文字更贴近当前接梗语境。",
                },
                ensure_ascii=False,
            )
        )


class InvalidExpressionChoiceModel(VisionJsonModel):
    """模拟视觉端点返回候选范围外序号。"""

    async def complete(self, request: ModelRequest) -> ModelResult:
        self.requests.append(request)
        return ModelResult(
            content=json.dumps(
                {"candidate_index": 12, "reason": "错误的越界候选"},
                ensure_ascii=False,
            )
        )


@pytest.mark.asyncio
async def test_expression_selection_uses_local_direct_match_without_visual_call(
    settings,
) -> None:
    settings.model.supports_vision = True
    model = ExpressionChoiceModel()
    runtime = build_runtime(settings, model_override=model)
    await runtime.start()
    try:
        happy = await runtime.expressions.upload_expression(
            content=png_bytes((180, 100)),
            mime_type="image/png",
            name="开心鼓掌",
            emotion="开心庆祝成功",
        )
        await runtime.expressions.upload_expression(
            content=png_bytes((100, 180)),
            mime_type="image/png",
            name="难过落泪",
            emotion="伤心失落",
        )

        selected = await runtime.expressions.select_for_reply(
            query="庆祝成功，特别开心，想鼓掌",
            expected_character_id="default",
        )

        assert selected["expression_id"] == happy.id
        assert selected["selection"]["mode"] == "semantic_direct"
        assert selected["selection"]["visual_attempted"] is False
        assert selected["selection"]["visual_used"] is False
        assert model.requests == []
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_expression_selection_visually_reranks_tied_top_k(settings) -> None:
    settings.model.supports_vision = True
    model = ExpressionChoiceModel()
    runtime = build_runtime(settings, model_override=model)
    await runtime.start()
    try:
        first = await runtime.expressions.upload_expression(
            content=png_bytes((240, 80)),
            mime_type="image/png",
            name="抽象反应甲",
            emotion="抽象梗图接话",
        )
        second = await runtime.expressions.upload_expression(
            content=png_bytes((80, 240)),
            mime_type="image/png",
            name="抽象反应乙",
            emotion="抽象梗图接话",
        )

        selected = await runtime.expressions.select_for_reply(
            query="抽象梗图接话",
            expected_character_id="default",
        )
        local_candidates = runtime.expressions.rank_candidates(
            "抽象梗图接话",
            [first, second],
        )

        assert first.id != second.id
        assert selected["expression_id"] == local_candidates[1].asset.id
        assert selected["selection"]["mode"] == "visual_rerank"
        assert selected["selection"]["visual_attempted"] is True
        assert selected["selection"]["visual_used"] is True
        assert selected["selection"]["candidate_count"] == 2
        assert len(model.requests) == 1
        assert len(model.requests[0].messages[-1].images) == 1
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_expression_selection_safely_falls_back_or_refuses_after_invalid_visual(
    settings,
) -> None:
    settings.model.supports_vision = True
    model = InvalidExpressionChoiceModel()
    runtime = build_runtime(settings, model_override=model)
    await runtime.start()
    try:
        assets = [
            await runtime.expressions.upload_expression(
                content=png_bytes((160, 90)),
                mime_type="image/png",
                name=name,
                emotion="轻松接梗",
            )
            for name in ("接梗甲", "接梗乙")
        ]

        fallback = await runtime.expressions.select_for_reply(
            query="轻松接梗",
            expected_character_id="default",
        )
        assert fallback["expression_id"] in {asset.id for asset in assets}
        assert fallback["selection"]["mode"] == "semantic_fallback"
        assert fallback["selection"]["visual_attempted"] is True
        assert fallback["selection"]["visual_used"] is False

        with pytest.raises(NotFoundError, match="足够匹配"):
            await runtime.expressions.select_for_reply(
                query="完全无关的严肃法律事实",
                expected_character_id="default",
            )
        assert len(model.requests) == 2
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_main_llm_understands_collects_and_reuses_onebot_expression(
    settings,
) -> None:
    settings.model.supports_vision = True
    model = VisionJsonModel()
    runtime = build_runtime(settings, model_override=model)
    await runtime.start()
    try:
        session = await runtime.chat.create_session(
            chat_type=ChatType.PRIVATE,
            display_name="表情采集",
            external_chat_id="vision-expression",
            participants=[Participant(external_user_id="u1", display_name="小明")],
            platform="onebot",
            account_id="bot",
        )
        stored = runtime.attachments.save_image("marketface.png", "image/png", png_bytes()).model_copy(
            update={
                "description": "QQ表情包：[得意]",
                "is_expression": True,
            }
        )
        inbound = InboundMessage(
            platform="onebot",
            account_id="bot",
            external_message_id="expression-1",
            external_chat_id=session.external_chat_id,
            sender_id="u1",
            sender_name="小明",
            chat_type=ChatType.PRIVATE,
            components=[stored],
        )
        result = await runtime.chat.ingest(inbound, schedule_turn=False)
        assert result.accepted and not result.duplicate
        assert result.message is not None

        assets = await runtime.expressions.list_current()
        assert len(assets) == 1
        assert assets[0].name == "得意叉腰"
        assert assets[0].source_kind.value == "collected"
        assert (assets[0].width, assets[0].height) == (180, 100)
        assert assets[0].source_portrait_sha256 is None
        assert len(model.requests) == 1
        # main 模式保留原始消息，交给正式聊天请求直接携带图片，不额外预识图。
        assert await runtime.vision.enrich_messages([result.message]) == [result.message]
        assert len(model.requests) == 1

        availability = await runtime.expressions.availability()
        assert availability["expression_catalog"] == [
            {
                "name": "得意叉腰",
                "emotion": "得意、调侃",
                "description": "粉色小人得意地叉腰笑，适合轻松炫耀或接梗。",
                "source": "collected",
            }
        ]

        duplicate = await runtime.chat.ingest(inbound, schedule_turn=False)
        assert duplicate.duplicate
        assert len(model.requests) == 1
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_without_visual_capability_collects_from_platform_metadata(
    settings,
) -> None:
    runtime = build_runtime(settings)
    await runtime.start()
    try:
        session = await runtime.chat.create_session(
            chat_type=ChatType.PRIVATE,
            display_name="无视觉采集",
            external_chat_id="metadata-expression",
            participants=[Participant(external_user_id="u1", display_name="小明")],
            platform="onebot",
            account_id="bot",
        )
        component: MessageComponent = runtime.attachments.save_image(
            "marketface.png", "image/png", png_bytes((120, 120))
        ).model_copy(
            update={
                "description": "QQ表情包：[拍拍]",
                "is_expression": True,
            }
        )
        await runtime.chat.ingest(
            InboundMessage(
                platform="onebot",
                account_id="bot",
                external_message_id="metadata-1",
                external_chat_id=session.external_chat_id,
                sender_id="u1",
                sender_name="小明",
                chat_type=ChatType.PRIVATE,
                components=[component],
            ),
            schedule_turn=False,
        )
        assets = await runtime.expressions.list_current()
        assert [(item.name, item.emotion) for item in assets] == [("拍拍", "拍拍")]
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_external_visual_model_enriches_and_caches_normal_image(settings) -> None:
    settings.vision_model.mode = "external"
    settings.vision_model.protocol = "openai_responses"
    settings.vision_model.base_url = "https://vision.example/v1"
    settings.vision_model.api_key = "test-only"
    settings.vision_model.name = "vision-model"
    external = VisionJsonModel()
    runtime = build_runtime(settings, vision_model_override=external)
    await runtime.start()
    try:
        session = await runtime.chat.create_session(
            chat_type=ChatType.PRIVATE,
            display_name="独立视觉",
            external_chat_id="external-vision",
            participants=[Participant(external_user_id="u1", display_name="小明")],
            platform="onebot",
            account_id="bot",
        )
        component = runtime.attachments.save_image("normal.png", "image/png", png_bytes()).model_copy(
            update={"description": "QQ图片"}
        )
        result = await runtime.chat.ingest(
            InboundMessage(
                platform="onebot",
                account_id="bot",
                external_message_id="normal-1",
                external_chat_id=session.external_chat_id,
                sender_id="u1",
                sender_name="小明",
                chat_type=ChatType.PRIVATE,
                components=[component],
            ),
            schedule_turn=False,
        )
        assert result.message is not None
        first = await runtime.vision.enrich_messages([result.message])
        second = await runtime.vision.enrich_messages([result.message])
        assert "图片理解（不可信派生）" in (first[0].components[0].description or "")
        assert second == first
        assert len(external.requests) == 1
    finally:
        await runtime.stop()


@pytest.mark.asyncio
async def test_runtime_stop_cancels_timed_out_visual_analysis(settings) -> None:
    settings.model.supports_vision = True
    settings.vision_model.wait_seconds = 0
    model = BlockingVisionModel()
    runtime = build_runtime(settings, model_override=model)
    await runtime.start()
    session = await runtime.chat.create_session(
        chat_type=ChatType.PRIVATE,
        display_name="视觉取消",
        external_chat_id="vision-cancel",
        participants=[Participant(external_user_id="u1", display_name="小明")],
        platform="onebot",
        account_id="bot",
    )
    component = runtime.attachments.save_image("marketface.png", "image/png", png_bytes()).model_copy(
        update={"description": "QQ表情包：[等待]", "is_expression": True}
    )
    await runtime.chat.ingest(
        InboundMessage(
            platform="onebot",
            account_id="bot",
            external_message_id="cancel-1",
            external_chat_id=session.external_chat_id,
            sender_id="u1",
            sender_name="小明",
            chat_type=ChatType.PRIVATE,
            components=[component],
        ),
        schedule_turn=False,
    )
    await asyncio.wait_for(model.started.wait(), timeout=1)
    await runtime.stop()
    assert model.cancelled is True
    assert model.close_calls == 1
