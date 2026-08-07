"""平台入站、派生处理、Prompt 投影与出站能力的严格清单。"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from domain.models import ComponentType


class CapabilityStatus(StrEnum):
    """模态能力的三态，不允许用模糊布尔值掩盖占位实现。"""

    SUPPORTED = "supported"
    PLACEHOLDER = "placeholder"
    UNSUPPORTED = "unsupported"


class _StrictCapabilityModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CanonicalIngressCapabilities(_StrictCapabilityModel):
    """平台事件能否形成对应的 canonical component。"""

    text: CapabilityStatus
    mention: CapabilityStatus
    quote: CapabilityStatus
    image: CapabilityStatus
    audio: CapabilityStatus
    file: CapabilityStatus
    forward: CapabilityStatus


class ProcessingCapabilities(_StrictCapabilityModel):
    """canonical component 后可形成的处理结果或派生事实。"""

    image_description: CapabilityStatus
    expression_asset: CapabilityStatus
    audio_transcript: CapabilityStatus
    ocr_text: CapabilityStatus
    forward_expansion: CapabilityStatus


class PromptProjectionCapabilities(_StrictCapabilityModel):
    """哪些规范化或派生内容会进入模型 Prompt。"""

    text: CapabilityStatus
    mention: CapabilityStatus
    quote: CapabilityStatus
    image_description: CapabilityStatus
    audio_transcript: CapabilityStatus
    file_metadata: CapabilityStatus
    forward_expansion: CapabilityStatus


class EgressCapabilities(_StrictCapabilityModel):
    """Channel 能真实投递的出站组件。"""

    text: CapabilityStatus
    image: CapabilityStatus
    audio: CapabilityStatus
    file: CapabilityStatus


class ChannelCapabilityMatrix(_StrictCapabilityModel):
    """单个平台从 ingress 到 egress 的端到端能力矩阵。"""

    platform: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    display_name: str = Field(min_length=1, max_length=80)
    ingress: CanonicalIngressCapabilities
    processing: ProcessingCapabilities
    prompt_projection: PromptProjectionCapabilities
    egress: EgressCapabilities
    limitations: list[str] = Field(min_length=1, max_length=30)

    def supports_ingress(self, component_type: ComponentType | str) -> bool:
        """仅允许 manifest 明确 supported 的 canonical 入站组件。"""

        value = (
            component_type.value
            if isinstance(component_type, ComponentType)
            else component_type
        )
        key_by_component = {
            ComponentType.TEXT.value: "text",
            ComponentType.MENTION.value: "mention",
            ComponentType.QUOTE.value: "quote",
            ComponentType.IMAGE_REF.value: "image",
            ComponentType.AUDIO_REF.value: "audio",
            ComponentType.FILE_REF.value: "file",
        }
        key = key_by_component.get(value)
        return bool(
            key is not None
            and getattr(self.ingress, key) == CapabilityStatus.SUPPORTED
        )

    def supports_processing(self, capability: str) -> bool:
        """查询宿主是否可执行一项已声明的派生处理。"""

        if capability not in ProcessingCapabilities.model_fields:
            raise ValueError(f"未知 processing capability: {capability}")
        return (
            getattr(self.processing, capability)
            == CapabilityStatus.SUPPORTED
        )

    @property
    def supported_prompt_projections(self) -> frozenset[str]:
        """返回允许进入模型 Prompt 的字段级投影白名单。"""

        return frozenset(
            name
            for name in PromptProjectionCapabilities.model_fields
            if getattr(self.prompt_projection, name)
            == CapabilityStatus.SUPPORTED
        )

    def supports_egress(self, component_type: ComponentType | str) -> bool:
        """仅对明确 supported 的真实投递能力返回 True。"""

        value = (
            component_type.value
            if isinstance(component_type, ComponentType)
            else component_type
        )
        key_by_component: dict[str, Literal["text", "image", "audio", "file"]] = {
            ComponentType.TEXT.value: "text",
            ComponentType.IMAGE_REF.value: "image",
            ComponentType.AUDIO_REF.value: "audio",
            ComponentType.FILE_REF.value: "file",
        }
        key = key_by_component.get(value)
        return bool(
            key is not None
            and getattr(self.egress, key) == CapabilityStatus.SUPPORTED
        )

    @property
    def supported_egress_components(self) -> frozenset[str]:
        """返回 ToolContext 使用的 canonical component 白名单。"""

        return frozenset(
            component.value
            for component in (
                ComponentType.TEXT,
                ComponentType.IMAGE_REF,
                ComponentType.AUDIO_REF,
                ComponentType.FILE_REF,
            )
            if self.supports_egress(component)
        )

    def prompt_summary(self) -> str:
        """生成不承诺 placeholder 能力的模型可见平台边界。"""

        supported_egress = [
            name
            for name in ("text", "image", "audio", "file")
            if getattr(self.egress, name) == CapabilityStatus.SUPPORTED
        ]
        supported_processing = [
            name
            for name in ProcessingCapabilities.model_fields
            if getattr(self.processing, name) == CapabilityStatus.SUPPORTED
        ]
        supported_projection = sorted(self.supported_prompt_projections)
        return (
            f"当前平台：{self.display_name}（`{self.platform}`）。"
            f"真实派生处理仅有：{', '.join(supported_processing) or '无'}；"
            f"Prompt 投影仅有：{', '.join(supported_projection) or '无'}；"
            f"真实出站组件仅有：{', '.join(supported_egress) or '无'}。"
            "标记为 placeholder/unsupported 的能力不得调用工具、输出占位符或声称已完成。"
        )


class ChannelCapabilityRegistry:
    """持有内置 Web 与插件 manifest 的单一权威能力清单。"""

    def __init__(
        self,
        entries: list[tuple[str | None, ChannelCapabilityMatrix]],
    ) -> None:
        self._entries: dict[str, tuple[str | None, ChannelCapabilityMatrix]] = {}
        for plugin_id, matrix in entries:
            if matrix.platform in self._entries:
                raise ValueError(f"平台 capability 重复声明: {matrix.platform}")
            self._entries[matrix.platform] = (plugin_id, matrix)

    def get(self, platform: str) -> ChannelCapabilityMatrix:
        """未知平台 fail-loud，禁止默认假定能发送富媒体。"""

        entry = self._entries.get(platform)
        if entry is None:
            raise ValueError(f"平台缺少 capability matrix: {platform}")
        return entry[1]

    def replace_from(self, other: ChannelCapabilityRegistry) -> None:
        """在 generation 发布临界区替换完整清单，不保留旧插件声明。"""

        self._entries = dict(other._entries)

    def supports_egress(
        self, platform: str, component_type: ComponentType | str
    ) -> bool:
        return self.get(platform).supports_egress(component_type)

    def supports_ingress(
        self, platform: str, component_type: ComponentType | str
    ) -> bool:
        return self.get(platform).supports_ingress(component_type)

    def supports_processing(self, platform: str, capability: str) -> bool:
        return self.get(platform).supports_processing(capability)

    def supported_prompt_projections(self, platform: str) -> frozenset[str]:
        return self.get(platform).supported_prompt_projections

    def supported_egress_components(self, platform: str) -> frozenset[str]:
        return self.get(platform).supported_egress_components

    def prompt_summary(self, platform: str) -> str:
        return self.get(platform).prompt_summary()

    def snapshot(self, *, enabled_plugin_ids: set[str]) -> list[dict[str, object]]:
        """返回 API 使用的只读清单，并区分声明与实际启用状态。"""

        return [
            {
                **matrix.model_dump(mode="json"),
                "source": "builtin" if plugin_id is None else "plugin_manifest",
                "plugin_id": plugin_id,
                "enabled": plugin_id is None or plugin_id in enabled_plugin_ids,
            }
            for _, (plugin_id, matrix) in sorted(self._entries.items())
        ]


WEB_SIMULATOR_CAPABILITIES = ChannelCapabilityMatrix.model_validate(
    {
        "platform": "web-simulator",
        "display_name": "Web 模拟器",
        "ingress": {
            "text": "supported",
            "mention": "supported",
            "quote": "supported",
            "image": "supported",
            "audio": "supported",
            "file": "supported",
            "forward": "unsupported",
        },
        "processing": {
            "image_description": "supported",
            "expression_asset": "supported",
            "audio_transcript": "unsupported",
            "ocr_text": "unsupported",
            "forward_expansion": "unsupported",
        },
        "prompt_projection": {
            "text": "supported",
            "mention": "supported",
            "quote": "supported",
            "image_description": "supported",
            "audio_transcript": "unsupported",
            "file_metadata": "supported",
            "forward_expansion": "unsupported",
        },
        "egress": {
            "text": "supported",
            "image": "supported",
            "audio": "supported",
            "file": "supported",
        },
        "limitations": [
            "语音仅作为受控附件保存，尚未提供 ASR。",
            "图片可进入视觉描述与表情收集；尚未提供 OCR。",
            "合并转发消息尚未展开。",
        ],
    }
)
