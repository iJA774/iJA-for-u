"""表情 Skill 的可移植名称、参数和提示词规则。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{index}" for index in range(1, 10)),
    *(f"lpt{index}" for index in range(1, 10)),
}


@dataclass(frozen=True, slots=True)
class ExpressionName:
    """逻辑名称及其稳定存储形式。"""

    visible: str
    normalized: str
    filename: str


def normalize_expression_name(value: str) -> ExpressionName:
    """按 Unicode 与 Windows 文件名规则规范化表情名称。"""

    visible = unicodedata.normalize("NFKC", value).strip()
    visible = re.sub(r"\s+", " ", visible)
    if not visible or len(visible) > 40:
        raise ValueError("表情名称长度必须为 1 到 40 个字符")
    if INVALID_FILENAME_CHARS.search(visible) or visible.endswith((".", " ")):
        raise ValueError("表情名称包含 Windows 文件名不允许的字符")
    if visible.casefold() in WINDOWS_RESERVED:
        raise ValueError("表情名称是 Windows 保留名称")
    return ExpressionName(
        visible=visible,
        normalized=visible.casefold(),
        filename=f"{visible}.png",
    )


def build_generation_prompt(emotion: str, image_prompt: str) -> str:
    """构造只包含角色外观、情绪和姿态的最小化编辑提示词。"""

    clean_emotion = emotion.strip()
    clean_prompt = image_prompt.strip()
    if not clean_emotion:
        raise ValueError("emotion 不能为空")
    if not clean_prompt:
        raise ValueError("生成新表情时 image_prompt 不能为空")
    return (
        "以输入的 base_image 作为唯一人物身份参考，保持同一角色的脸、发型、服装和主要配色。"
        "生成一张单人角色表情图；宽高比按动作构图自然决定，不添加其他人物、文字、字幕、标志或水印。"
        f"核心情绪：{clean_emotion}。表情与姿态：{clean_prompt}。"
        "背景简洁，主体清晰，适合聊天表情使用。"
    )
