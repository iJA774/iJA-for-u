"""角色表情检索、生成、发布与图库生命周期。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from adapters.persistence import PersonaStore
from adapters.web_simulator import AttachmentStore
from config import ExpressionSelectionSettings
from domain.errors import ConflictError, IJAError, InputValidationError, NotFoundError
from domain.models import (
    ExpressionAsset,
    ExpressionSourceKind,
    ImageAnalysis,
    MessageComponent,
    Persona,
    ReplyDraft,
)
from observability import model_observation_scope
from ports import ImageGenerationRequest, ImageModelProvider, OperationsRepository

logger = logging.getLogger(__name__)

# 表情名称规范化规则需与 send-expression skill 包的 normalize_expression_name 保持一致，
# 使手动上传与模型生成的表情在同一规范化空间内去重。
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


class ExpressionVisualSelector(Protocol):
    """只允许视觉服务在本地候选集合内返回一个素材 ID。"""

    @property
    def can_analyze(self) -> bool: ...

    @property
    def can_select_expressions(self) -> bool: ...

    async def select_expression_candidate(
        self, query: str, candidates: list[ExpressionAsset]
    ) -> str | None: ...


@dataclass(frozen=True, slots=True)
class RankedExpression:
    """一项本地语义预筛结果。"""

    asset: ExpressionAsset
    score: float


class ExpressionService:
    """向 Skill 提供人格、图库、图片 Provider 和历史媒体宿主能力。"""

    MAX_EXPRESSIONS_PER_CHARACTER = 200

    def __init__(
        self,
        *,
        store: OperationsRepository,
        personas: PersonaStore,
        attachments: AttachmentStore,
        image_provider: ImageModelProvider | None,
        persona_lock: asyncio.Lock | None = None,
        selection_settings: ExpressionSelectionSettings | None = None,
    ) -> None:
        self.store = store
        self.personas = personas
        self.attachments = attachments
        self.image_provider = image_provider
        self.persona_lock = persona_lock or asyncio.Lock()
        self.selection_settings = selection_settings or ExpressionSelectionSettings()
        self.visual_selector: ExpressionVisualSelector | None = None
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    def set_image_provider(self, provider: ImageModelProvider | None) -> None:
        """原子切换后续生成使用的图片 Provider。"""

        self.image_provider = provider

    def set_visual_selector(self, selector: ExpressionVisualSelector | None) -> None:
        """设置低置信度候选的视觉复选器；None 表示仅使用本地排序。"""

        self.visual_selector = selector

    async def list_current(
        self,
        character_id: str | None = None,
    ) -> list[ExpressionAsset]:
        """列出当前角色可发送素材；独立上传和聊天收集不依赖基础形象。"""

        async with self.persona_lock:
            persona = self._persona(character_id=character_id)
            if persona.portrait is None or not persona.portrait.aspect_valid:
                return [
                    asset
                    for asset in await self.store.list_expressions(persona.character_id)
                    if asset.source_portrait_sha256 is None
                ]
            return await self.store.list_expressions(
                persona.character_id,
                source_portrait_sha256=persona.portrait.sha256,
            )

    async def availability(self, *, context: Any | None = None) -> dict[str, Any]:
        """返回 Skill 加载时需要的完整、无截断图库上下文。"""

        async with self.persona_lock:
            persona = self._persona(context=context)
            portrait_ready = bool(persona.portrait and persona.portrait.aspect_valid)
            if portrait_ready and persona.portrait is not None:
                assets = await self.store.list_expressions(
                    persona.character_id,
                    source_portrait_sha256=persona.portrait.sha256,
                )
            else:
                assets = [
                    asset
                    for asset in await self.store.list_expressions(persona.character_id)
                    if asset.source_portrait_sha256 is None
                ]
            all_assets = await self.store.list_expressions(persona.character_id)
            remaining_capacity = max(0, self.MAX_EXPRESSIONS_PER_CHARACTER - len(all_assets))
            return {
                "character_id": persona.character_id,
                "base_image_sha256": (
                    persona.portrait.sha256 if portrait_ready and persona.portrait else None
                ),
                "expression_names": [asset.name for asset in assets],
                "expression_catalog": [
                    {
                        "name": asset.name,
                        "emotion": asset.emotion,
                        "description": asset.description,
                        "source": asset.source_kind.value,
                    }
                    for asset in assets
                ],
                "can_generate": (
                    portrait_ready and self.image_provider is not None and remaining_capacity > 0
                ),
                "library_limit": self.MAX_EXPRESSIONS_PER_CHARACTER,
                "remaining_capacity": remaining_capacity,
                "image_model_status": ("enabled" if self.image_provider is not None else "disabled"),
                "portrait_status": ("ready" if portrait_ready else "missing_or_noncompliant"),
                "selection_mode": "local_top_k_with_optional_visual_rerank",
            }

    async def is_available(self, *, context: Any | None = None) -> bool:
        state = await self.availability(context=context)
        return bool(state["expression_names"] or state["can_generate"])

    async def get_by_generation_key(self, generation_key: str) -> ExpressionAsset | None:
        """为 Skill 的周期恢复钩子读取已落库生成素材。"""

        return await self.store.get_expression_by_generation_key(generation_key)

    async def select_for_reply(
        self,
        *,
        query: str,
        expected_character_id: str | None = None,
        expected_portrait_sha256: str | None = None,
    ) -> dict[str, Any]:
        """先本地筛选 Top-K，只有低置信度时才让视觉模型在候选内复选。"""

        selection_query = " ".join(query.split()).strip()
        if not selection_query:
            raise InputValidationError("selection_query 不能为空")
        if len(selection_query) > 500:
            raise InputValidationError("selection_query 不能超过 500 个字符")
        async with self.persona_lock:
            persona = self._persona(character_id=expected_character_id)
            if expected_character_id is not None and persona.character_id != expected_character_id:
                raise ConflictError("加载 Skill 后当前角色已变化，请重新加载")
            portrait = persona.portrait
            if expected_portrait_sha256 is not None and (
                portrait is None or portrait.sha256 != expected_portrait_sha256
            ):
                raise ConflictError("加载 Skill 后 base_image 已变化，请重新加载")
            if portrait is not None and portrait.aspect_valid:
                assets = await self.store.list_expressions(
                    persona.character_id,
                    source_portrait_sha256=portrait.sha256,
                )
            else:
                assets = [
                    asset
                    for asset in await self.store.list_expressions(persona.character_id)
                    if asset.source_portrait_sha256 is None
                ]
        if not assets:
            raise NotFoundError("当前角色没有可发送的表情")

        ranked = self.rank_candidates(selection_query, assets)
        settings = self.selection_settings
        candidates = ranked[: settings.candidate_count]
        top = candidates[0]
        runner_up_score = candidates[1].score if len(candidates) > 1 else 0.0
        margin = top.score - runner_up_score
        high_confidence = top.score >= settings.direct_score_threshold and (
            len(candidates) == 1
            or margin >= settings.direct_margin_threshold
            or self._contains_semantic_text(selection_query, top.asset.name)
        )
        if high_confidence:
            return self._selection_result(
                top,
                mode="semantic_direct",
                margin=margin,
                candidate_count=len(candidates),
                visual_used=False,
            )

        selector = self.visual_selector
        visual_attempted = False
        if (
            settings.visual_rerank_enabled
            and selector is not None
            and selector.can_select_expressions
            and len(candidates) > 1
        ):
            visual_attempted = True
            selected_id = await selector.select_expression_candidate(
                selection_query,
                [item.asset for item in candidates],
            )
            selected = next(
                (item for item in candidates if item.asset.id == selected_id),
                None,
            )
            if selected is not None:
                return self._selection_result(
                    selected,
                    mode="visual_rerank",
                    margin=margin,
                    candidate_count=len(candidates),
                    visual_used=True,
                    visual_attempted=True,
                    top_score=top.score,
                )

        if top.score < settings.minimum_fallback_score:
            raise NotFoundError("没有找到足够匹配的表情，请改用普通文字回复")
        return self._selection_result(
            top,
            mode="semantic_fallback",
            margin=margin,
            candidate_count=len(candidates),
            visual_used=False,
            visual_attempted=visual_attempted,
        )

    @classmethod
    def rank_candidates(cls, query: str, assets: list[ExpressionAsset]) -> list[RankedExpression]:
        """按名称、情绪和理解描述进行确定性的本地语义预筛。"""

        ranked = [RankedExpression(asset=asset, score=cls._semantic_score(query, asset)) for asset in assets]
        return sorted(
            ranked,
            key=lambda item: (
                -item.score,
                -item.asset.use_count,
                item.asset.name.casefold(),
                item.asset.id,
            ),
        )

    @classmethod
    def _semantic_score(cls, query: str, asset: ExpressionAsset) -> float:
        name_score = cls._field_match(query, asset.name)
        emotion_score = cls._field_match(query, asset.emotion)
        description_score = cls._field_match(query, asset.description)
        weighted = (name_score * 3 + emotion_score * 2 + description_score) / 6
        score = max(name_score, emotion_score) * 0.55 + description_score * 0.25 + weighted * 0.20
        return round(min(1.0, max(0.0, score)), 6)

    @classmethod
    def _field_match(cls, query: str, field: str) -> float:
        query_text, query_units = cls._semantic_units(query)
        field_text, field_units = cls._semantic_units(field)
        if not query_text or not field_text:
            return 0.0
        if query_text == field_text:
            return 1.0
        overlap = query_units & field_units
        if not overlap:
            return 0.0
        recall = len(overlap) / max(1, len(field_units))
        precision = len(overlap) / max(1, len(query_units))
        score = recall * 0.7 + precision * 0.3
        if field_text in query_text:
            score = max(score, 0.95)
        elif query_text in field_text:
            score = max(score, 0.85)
        return min(1.0, score)

    @staticmethod
    def _semantic_units(value: str) -> tuple[str, set[str]]:
        normalized = unicodedata.normalize("NFKC", value).casefold()
        segments = re.findall(r"[a-z0-9]+|[\u3400-\u9fff]+", normalized)
        units: set[str] = set()
        for segment in segments:
            if segment.isascii():
                units.add(segment)
                continue
            if len(segment) == 1:
                units.add(segment)
            else:
                units.update(segment[index : index + 2] for index in range(len(segment) - 1))
                if len(segment) <= 4:
                    units.add(segment)
        return "".join(segments), units

    @classmethod
    def _contains_semantic_text(cls, query: str, field: str) -> bool:
        query_text, _ = cls._semantic_units(query)
        field_text, _ = cls._semantic_units(field)
        return bool(field_text and field_text in query_text)

    @staticmethod
    def _selection_result(
        selected: RankedExpression,
        *,
        mode: str,
        margin: float,
        candidate_count: int,
        visual_used: bool,
        visual_attempted: bool = False,
        top_score: float | None = None,
    ) -> dict[str, Any]:
        asset = selected.asset
        return {
            "expression_id": asset.id,
            "name": asset.name,
            "normalized_name": asset.normalized_name,
            "filename": f"{asset.name}.png",
            "selection": {
                "mode": mode,
                "selected_score": round(selected.score, 4),
                "top_score": round(top_score if top_score is not None else selected.score, 4),
                "score_margin": round(max(0.0, margin), 4),
                "candidate_count": candidate_count,
                "visual_attempted": visual_attempted,
                "visual_used": visual_used,
            },
        }

    async def prepare_reply(
        self,
        *,
        action: str,
        name: str,
        normalized_name: str,
        filename: str,
        emotion: str,
        image_prompt: str | None,
        model_prompt: str | None,
        caption: str | None,
        expected_character_id: str | None = None,
        expected_portrait_sha256: str | None = None,
        expected_expression_id: str | None = None,
        generation_key: str | None = None,
    ) -> ReplyDraft:
        """按 Skill 已校验的请求执行受控副作用并发布消息组件。"""

        emotion = emotion.strip()
        if not emotion:
            raise InputValidationError("emotion 不能为空")
        async with self.persona_lock:
            persona = self._persona(character_id=expected_character_id)
            portrait = persona.portrait
            if expected_character_id is not None and persona.character_id != expected_character_id:
                raise ConflictError("加载 Skill 后当前角色已变化，请重新加载")
            if expected_portrait_sha256 is not None and (
                portrait is None or portrait.sha256 != expected_portrait_sha256
            ):
                raise ConflictError("加载 Skill 后 base_image 已变化，请重新加载")
            async with self._locks[persona.character_id]:
                existing = await self.store.get_expression_by_name(persona.character_id, normalized_name)
                if action == "reuse":
                    if existing is None or (
                        existing.source_portrait_sha256 is not None
                        and (portrait is None or existing.source_portrait_sha256 != portrait.sha256)
                    ):
                        raise NotFoundError(f"当前形象下不存在表情: {name}")
                    if (
                        expected_expression_id is not None
                        and existing.id != expected_expression_id
                    ):
                        raise ConflictError("选中的表情已变化，请重新选择")
                    if name != existing.name:
                        raise NotFoundError(f"复用表情时必须逐字使用现有名称: {existing.name}")
                    asset = existing
                elif action == "generate":
                    recovered = (
                        await self.store.get_expression_by_generation_key(generation_key)
                        if generation_key is not None
                        else None
                    )
                    if recovered is not None:
                        if (
                            recovered.character_id != persona.character_id
                            or portrait is None
                            or recovered.source_portrait_sha256 != portrait.sha256
                            or recovered.normalized_name != normalized_name
                        ):
                            raise ConflictError("生成幂等键已绑定到其他表情或形象")
                        asset = recovered
                    else:
                        if portrait is None or not portrait.aspect_valid:
                            raise InputValidationError("生成新表情需要当前角色具有合规的 9:16 base_image")
                        if existing is not None:
                            raise ConflictError(f"表情名称已存在，请改为复用: {existing.name}")
                        provider = self.image_provider
                        if provider is None:
                            raise InputValidationError("图片模型未启用，不能生成新表情")
                        assets = await self.store.list_expressions(persona.character_id)
                        if len(assets) >= self.MAX_EXPRESSIONS_PER_CHARACTER:
                            raise ConflictError("当前角色表情库已达到 200 个上限")
                        prompt = (image_prompt or "").strip()
                        final_model_prompt = (model_prompt or "").strip()
                        if not prompt or not final_model_prompt:
                            raise InputValidationError("生成新表情时 image_prompt 和 model_prompt 不能为空")
                        base_path = await asyncio.to_thread(Path(portrait.storage_path).resolve)
                        try:
                            base_content = await asyncio.to_thread(base_path.read_bytes)
                        except OSError as exc:
                            raise InputValidationError("base_image 无法读取") from exc
                        if (
                            len(base_content) != portrait.size
                            or hashlib.sha256(base_content).hexdigest() != portrait.sha256
                        ):
                            raise InputValidationError("base_image 与人格配置摘要不一致")
                        with model_observation_scope(
                            task="image.expression_generate",
                            profile="image",
                            run_id=generation_key,
                        ):
                            result = await provider.generate(
                                ImageGenerationRequest(
                                    base_image=base_content,
                                    prompt=final_model_prompt,
                                    filename="base_image.png",
                                )
                            )
                        metadata = self.attachments.save_expression_source(
                            persona.character_id, filename, result.content
                        )
                        asset = ExpressionAsset(
                            character_id=persona.character_id,
                            name=filename.removesuffix(".png"),
                            normalized_name=normalized_name,
                            emotion=emotion,
                            description=prompt,
                            source_kind=ExpressionSourceKind.GENERATED,
                            generation_key=generation_key,
                            source_portrait_sha256=portrait.sha256,
                            storage_path=metadata.storage_path,
                            mime_type=metadata.mime_type,
                            size=metadata.size,
                            sha256=metadata.sha256,
                            width=metadata.width,
                            height=metadata.height,
                        )
                        try:
                            await self.store.create_expression(asset)
                        except Exception:
                            self.attachments.remove_expression_source(asset)
                            raise
                else:
                    raise InputValidationError("action 只能是 reuse 或 generate")

                image_component = self.attachments.publish_expression(asset)
                components: list[MessageComponent] = []
                visible_caption = (caption or "").strip()
                if visible_caption:
                    components.append(MessageComponent.text_component(visible_caption))
                components.append(image_component)
                return ReplyDraft(components=components, expression_id=asset.id)

    async def delete(
        self,
        expression_id: str,
        *,
        character_id: str | None = None,
    ) -> ExpressionAsset:
        """删除指定角色的图库素材，但保留已发布历史媒体。"""

        async with self.persona_lock:
            current = self._persona(character_id=character_id)
            async with self._locks[current.character_id]:
                asset = await self.store.get_expression(expression_id)
                if asset is None or asset.character_id != current.character_id:
                    raise NotFoundError("表情不存在")
                deleted = await self.store.delete_expression(expression_id)
                self.attachments.remove_expression_source(deleted)
                return deleted

    async def clear_character(self, character_id: str) -> int:
        """删除角色全部表情；仅供显式删除角色数据的管理流程。"""

        async with self._locks[character_id]:
            assets = await self.store.delete_expressions_for_character(character_id)
            for asset in assets:
                try:
                    self.attachments.remove_expression_source(asset)
                except (IJAError, OSError):
                    logger.exception(
                        "已停用表情的源文件清理失败",
                        extra={"character_id": character_id, "expression_id": asset.id},
                    )
            return len(assets)

    async def clear_portrait_bound(self, character_id: str) -> int:
        """形象替换后仅清理旧形象生成素材，保留上传和聊天收集表情。"""

        async with self._locks[character_id]:
            assets = await self.store.delete_portrait_bound_expressions(character_id)
            for asset in assets:
                try:
                    self.attachments.remove_expression_source(asset)
                except (IJAError, OSError):
                    logger.exception(
                        "旧形象表情源文件清理失败",
                        extra={
                            "character_id": character_id,
                            "expression_id": asset.id,
                        },
                    )
            return len(assets)

    async def upload_expression(
        self,
        *,
        content: bytes,
        mime_type: str,
        name: str,
        emotion: str,
        character_id: str | None = None,
    ) -> ExpressionAsset:
        """手动上传图片作为当前角色的可复用表情，不依赖图片生成模型。

        与模型生成共用同一存储与命名空间；图片会去元数据并规范化为静态 PNG，
        但不限制宽高比，也不要求或绑定当前基础形象。
        """

        emotion = emotion.strip()
        if not emotion:
            raise InputValidationError("emotion 不能为空")
        visible, normalized, filename = self._normalize_name(name)
        async with self.persona_lock:
            persona = self._persona(character_id=character_id)
            async with self._locks[persona.character_id]:
                existing = await self.store.get_expression_by_name(persona.character_id, normalized)
                if existing is not None:
                    raise ConflictError(f"表情名称已存在: {existing.name}")
                all_assets = await self.store.list_expressions(persona.character_id)
                if len(all_assets) >= self.MAX_EXPRESSIONS_PER_CHARACTER:
                    raise ConflictError("当前角色表情库已达到 200 个上限")
                metadata = self.attachments.save_expression_source(persona.character_id, filename, content)
                asset = ExpressionAsset(
                    character_id=persona.character_id,
                    name=visible,
                    normalized_name=normalized,
                    emotion=emotion,
                    description=f"手动上传：{emotion}",
                    source_kind=ExpressionSourceKind.UPLOADED,
                    source_portrait_sha256=None,
                    storage_path=metadata.storage_path,
                    mime_type=metadata.mime_type,
                    size=metadata.size,
                    sha256=metadata.sha256,
                    width=metadata.width,
                    height=metadata.height,
                )
                try:
                    await self.store.create_expression(asset)
                except Exception:
                    self.attachments.remove_expression_source(asset)
                    raise
                return asset

    async def collect_expression(
        self,
        component: MessageComponent,
        analysis: ImageAnalysis,
        *,
        character_id: str,
    ) -> ExpressionAsset:
        """把平台确认的真实表情资源按内容去重后收进当前聊天人格图库。"""

        if not analysis.is_expression:
            raise InputValidationError("只有已标记为表情包的图片才能自动收集")
        if component.sha256 != analysis.sha256:
            raise InputValidationError("表情组件与视觉理解摘要不一致")
        content = self.attachments.read_image_ref(component)
        collection_key = (
            "collected:" + hashlib.sha256(f"{character_id}:{analysis.sha256}".encode()).hexdigest()
        )
        async with self._locks[character_id]:
            existing = await self.store.get_expression_by_generation_key(collection_key)
            if existing is not None:
                return existing
            all_assets = await self.store.list_expressions(character_id)
            if len(all_assets) >= self.MAX_EXPRESSIONS_PER_CHARACTER:
                raise ConflictError("当前角色表情库已达到 200 个上限")
            requested_name = (
                analysis.expression_name
                or (analysis.emotions[0] if analysis.emotions else "")
                or f"收集表情-{analysis.sha256[:8]}"
            )
            visible, normalized, filename = self._unique_collected_name(
                requested_name,
                analysis.sha256,
                {asset.normalized_name for asset in all_assets},
            )
            metadata = self.attachments.save_expression_source(character_id, filename, content)
            asset = ExpressionAsset(
                character_id=character_id,
                name=visible,
                normalized_name=normalized,
                emotion="、".join(analysis.emotions) or "聊天表情",
                description=analysis.description,
                source_kind=ExpressionSourceKind.COLLECTED,
                generation_key=collection_key,
                source_portrait_sha256=None,
                storage_path=metadata.storage_path,
                mime_type=metadata.mime_type,
                size=metadata.size,
                sha256=metadata.sha256,
                width=metadata.width,
                height=metadata.height,
            )
            try:
                await self.store.create_expression(asset)
            except Exception:
                self.attachments.remove_expression_source(asset)
                raise
            return asset

    async def rename_expression(
        self,
        expression_id: str,
        new_name: str,
        *,
        character_id: str | None = None,
    ) -> ExpressionAsset:
        """重命名表情并同步源文件名；已发布到历史媒体的副本不受影响。"""

        visible, normalized, filename = self._normalize_name(new_name)
        async with self.persona_lock:
            persona = self._persona(character_id=character_id)
            async with self._locks[persona.character_id]:
                asset = await self.store.get_expression(expression_id)
                if asset is None or asset.character_id != persona.character_id:
                    raise NotFoundError("表情不存在")
                if asset.normalized_name == normalized:
                    return asset
                existing = await self.store.get_expression_by_name(persona.character_id, normalized)
                if existing is not None:
                    raise ConflictError(f"表情名称已存在: {existing.name}")
                old_asset = asset.model_copy(deep=True)
                old_content = await asyncio.to_thread(Path(asset.storage_path).read_bytes)
                metadata = self.attachments.save_expression_source(
                    persona.character_id, filename, old_content
                )
                renamed = asset.model_copy(
                    update={
                        "name": visible,
                        "normalized_name": normalized,
                        "storage_path": metadata.storage_path,
                    }
                )
                try:
                    await self.store.update_expression_rename(
                        expression_id,
                        name=visible,
                        normalized_name=normalized,
                        storage_path=metadata.storage_path,
                    )
                except Exception:
                    self.attachments.remove_expression_source(renamed)
                    raise
                try:
                    self.attachments.remove_expression_source(old_asset)
                except (IJAError, OSError):
                    logger.warning(
                        "重命名后旧表情源文件清理失败",
                        extra={
                            "character_id": persona.character_id,
                            "expression_id": expression_id,
                            "old_path": old_asset.storage_path,
                        },
                    )
                return renamed

    def _persona(
        self,
        *,
        character_id: str | None = None,
        context: Any | None = None,
    ) -> Persona:
        """优先使用工具上下文冻结的人格，控制台旧调用才回退到编辑人格。"""

        contextual_id = str(getattr(context, "character_id", "") or "") if context is not None else ""
        selected_id = character_id or contextual_id
        return self.personas.get(selected_id) if selected_id else self.personas.get_active()

    @staticmethod
    def _normalize_name(value: str) -> tuple[str, str, str]:
        """规范化表情名称，返回 (visible, normalized, filename)。

        规则与 send-expression skill 包的 normalize_expression_name 保持一致，
        确保手动上传与模型生成共用同一去重空间。
        """

        visible = unicodedata.normalize("NFKC", value).strip()
        visible = re.sub(r"\s+", " ", visible)
        if not visible or len(visible) > 40:
            raise InputValidationError("表情名称长度必须为 1 到 40 个字符")
        if _INVALID_FILENAME_CHARS.search(visible) or visible.endswith((".", " ")):
            raise InputValidationError("表情名称包含 Windows 文件名不允许的字符")
        if visible.casefold() in _WINDOWS_RESERVED:
            raise InputValidationError("表情名称是 Windows 保留名称")
        return visible, visible.casefold(), f"{visible}.png"

    @classmethod
    def _unique_collected_name(
        cls, value: str, sha256: str, existing_names: set[str]
    ) -> tuple[str, str, str]:
        """为模型给出的短名称生成稳定且不冲突的本地文件名。"""

        visible, normalized, filename = cls._normalize_name(value[:40])
        if normalized not in existing_names:
            return visible, normalized, filename
        suffix = f"-{sha256[:6]}"
        base = visible[: 40 - len(suffix)].rstrip()
        return cls._normalize_name(f"{base}{suffix}")
