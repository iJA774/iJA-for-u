"""表情资产与图像分析缓存的 SQLite 仓储实现。"""

from __future__ import annotations

import json

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError

from domain.errors import ConflictError, NotFoundError
from domain.models import (
    DeliveryStatus,
    ExpressionAsset,
    ExpressionSourceKind,
    ImageAnalysis,
    new_id,
    utc_now,
)

from ..schema import ExpressionAssetRow, ImageAnalysisRow, OutboundAttemptRow
from ._base import RepositoryMixinSupport


class AssetRepositoryMixin(RepositoryMixinSupport):
    """实现表达资产、识图缓存及 SENT 使用计数。"""

    async def list_expressions(
        self, character_id: str, *, source_portrait_sha256: str | None = None
    ) -> list[ExpressionAsset]:
        """按使用频率和名称列出角色的可复用表情。"""

        async with self.session_factory() as db:
            query = select(ExpressionAssetRow).where(
                ExpressionAssetRow.character_id == character_id
            )
            if source_portrait_sha256 is not None:
                query = query.where(
                    or_(
                        ExpressionAssetRow.source_portrait_sha256
                        == source_portrait_sha256,
                        ExpressionAssetRow.source_portrait_sha256.is_(None),
                    )
                )
            query = query.order_by(
                ExpressionAssetRow.use_count.desc(),
                ExpressionAssetRow.name.asc(),
            )
            rows = (await db.execute(query)).scalars().all()
            return [self._expression_from_row(row) for row in rows]

    async def get_expression(
        self, expression_id: str
    ) -> ExpressionAsset | None:
        """按内部 ID 读取表情。"""

        async with self.session_factory() as db:
            row = await db.get(ExpressionAssetRow, expression_id)
            return self._expression_from_row(row) if row is not None else None

    async def get_expression_by_name(
        self, character_id: str, normalized_name: str
    ) -> ExpressionAsset | None:
        """按角色与规范化名称精确读取表情。"""

        async with self.session_factory() as db:
            row = (
                await db.execute(
                    select(ExpressionAssetRow).where(
                        ExpressionAssetRow.character_id == character_id,
                        ExpressionAssetRow.normalized_name == normalized_name,
                    )
                )
            ).scalar_one_or_none()
            return self._expression_from_row(row) if row is not None else None

    async def get_expression_by_generation_key(
        self, generation_key: str
    ) -> ExpressionAsset | None:
        """按周期运行幂等键恢复已经完成落库的付费生成结果。"""

        async with self.session_factory() as db:
            row = (
                await db.execute(
                    select(ExpressionAssetRow).where(
                        ExpressionAssetRow.generation_key == generation_key
                    )
                )
            ).scalar_one_or_none()
            return self._expression_from_row(row) if row is not None else None

    async def create_expression(
        self, asset: ExpressionAsset
    ) -> ExpressionAsset:
        """保存新表情；同一角色的规范化名称不允许重复。"""

        async with self.session_factory() as db:
            db.add(
                ExpressionAssetRow(
                    id=asset.id,
                    character_id=asset.character_id,
                    name=asset.name,
                    normalized_name=asset.normalized_name,
                    emotion=asset.emotion,
                    description=asset.description,
                    source_kind=asset.source_kind.value,
                    generation_key=asset.generation_key,
                    source_portrait_sha256=asset.source_portrait_sha256,
                    storage_path=asset.storage_path,
                    mime_type=asset.mime_type,
                    size=asset.size,
                    sha256=asset.sha256,
                    width=asset.width,
                    height=asset.height,
                    use_count=asset.use_count,
                    created_at=asset.created_at,
                    last_used_at=asset.last_used_at,
                )
            )
            try:
                await db.commit()
            except IntegrityError as exc:
                await db.rollback()
                raise ConflictError(f"表情名称已存在: {asset.name}") from exc
            return asset

    async def update_expression_rename(
        self,
        expression_id: str,
        *,
        name: str,
        normalized_name: str,
        storage_path: str,
    ) -> ExpressionAsset:
        """更新表情名称、规范化名称与源文件路径；名称冲突时回滚。"""

        async with self.session_factory() as db:
            row = await db.get(ExpressionAssetRow, expression_id)
            if row is None:
                raise NotFoundError("表情不存在")
            row.name = name
            row.normalized_name = normalized_name
            row.storage_path = storage_path
            try:
                await db.commit()
            except IntegrityError as exc:
                await db.rollback()
                raise ConflictError(f"表情名称已存在: {name}") from exc
            return self._expression_from_row(row)

    async def delete_expression(self, expression_id: str) -> ExpressionAsset:
        """删除单个图库记录并返回待清理源文件。"""

        async with self.session_factory() as db:
            row = await db.get(ExpressionAssetRow, expression_id)
            if row is None:
                raise NotFoundError("表情不存在")
            asset = self._expression_from_row(row)
            await db.delete(row)
            await db.commit()
            return asset

    async def delete_expressions_for_character(
        self, character_id: str
    ) -> list[ExpressionAsset]:
        """停用并删除一个角色的整套可复用表情记录。"""

        async with self.session_factory() as db:
            rows = (
                await db.execute(
                    select(ExpressionAssetRow).where(
                        ExpressionAssetRow.character_id == character_id
                    )
                )
            ).scalars().all()
            assets = [self._expression_from_row(row) for row in rows]
            for row in rows:
                await db.delete(row)
            await db.commit()
            return assets

    async def delete_portrait_bound_expressions(
        self, character_id: str
    ) -> list[ExpressionAsset]:
        """删除随基础形象失效的生成素材，保留上传和聊天收集表情。"""

        async with self.session_factory() as db:
            rows = (
                await db.execute(
                    select(ExpressionAssetRow).where(
                        ExpressionAssetRow.character_id == character_id,
                        ExpressionAssetRow.source_kind
                        == ExpressionSourceKind.GENERATED.value,
                        ExpressionAssetRow.source_portrait_sha256.is_not(None),
                    )
                )
            ).scalars().all()
            assets = [self._expression_from_row(row) for row in rows]
            for row in rows:
                await db.delete(row)
            await db.commit()
            return assets

    async def get_image_analysis(
        self, sha256: str, provider_key: str, prompt_version: str
    ) -> ImageAnalysis | None:
        """读取完全匹配图片、Provider 与 Prompt 版本的派生识图缓存。"""

        async with self.session_factory() as db:
            row = (
                await db.execute(
                    select(ImageAnalysisRow).where(
                        ImageAnalysisRow.sha256 == sha256,
                        ImageAnalysisRow.provider_key == provider_key,
                        ImageAnalysisRow.prompt_version == prompt_version,
                    )
                )
            ).scalar_one_or_none()
            return self._image_analysis_from_row(row) if row is not None else None

    async def save_image_analysis(
        self, analysis: ImageAnalysis
    ) -> ImageAnalysis:
        """幂等写入视觉理解派生缓存；并发命中时读取权威已提交结果。"""

        async with self.session_factory() as db:
            db.add(
                ImageAnalysisRow(
                    id=new_id("image_analysis"),
                    sha256=analysis.sha256,
                    mime_type=analysis.mime_type,
                    provider_key=analysis.provider_key,
                    prompt_version=analysis.prompt_version,
                    description=analysis.description,
                    emotions_json=json.dumps(
                        analysis.emotions,
                        ensure_ascii=False,
                    ),
                    expression_name=analysis.expression_name,
                    is_expression=analysis.is_expression,
                    created_at=analysis.created_at,
                    updated_at=analysis.updated_at,
                )
            )
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                existing = (
                    await db.execute(
                        select(ImageAnalysisRow).where(
                            ImageAnalysisRow.sha256 == analysis.sha256,
                            ImageAnalysisRow.provider_key == analysis.provider_key,
                            ImageAnalysisRow.prompt_version
                            == analysis.prompt_version,
                        )
                    )
                ).scalar_one()
                return self._image_analysis_from_row(existing)
            return analysis

    async def record_expression_usage(
        self, outbound_id: str, expression_ids: set[str]
    ) -> None:
        """对同一 sent 出站消息至多累计一次表情使用次数。"""

        if not expression_ids:
            return
        async with self.session_factory() as db:
            attempt = (
                await db.execute(
                    select(OutboundAttemptRow).where(
                        OutboundAttemptRow.outbound_id == outbound_id
                    )
                )
            ).scalar_one_or_none()
            if attempt is None or attempt.status != DeliveryStatus.SENT.value:
                raise ConflictError("只有收到 sent 回执后才能累计表情使用次数")
            if attempt.expression_usage_recorded:
                return
            now = utc_now()
            rows = (
                await db.execute(
                    select(ExpressionAssetRow).where(
                        ExpressionAssetRow.id.in_(expression_ids)
                    )
                )
            ).scalars().all()
            for row in rows:
                row.use_count += 1
                row.last_used_at = now
            attempt.expression_usage_recorded = True
            await db.commit()
