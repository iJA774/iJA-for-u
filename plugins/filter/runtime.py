"""模型输出两步过滤器：简单模式匹配 + 模型确认。"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import replace

from observability import model_observation_scope
from plugins._host.contracts import PluginUnavailableError
from ports import ModelMessage, ModelRequest
from ports.egress import (
    EgressEnvelope,
    EgressHandler,
    EgressPluginContext,
)

logger = logging.getLogger(__name__)

_TEMPLATE = "对不起，我有口难言(ಥ﹏ಥ)"
_MAX_REGENERATIONS = 3
_DETECTION_MAX_TOKENS = 100


def _normalize(text: str) -> str:
    """剔除标点、换行等特殊符号并统一小写，只保留文字与数字。"""

    return "".join(c for c in text.lower() if c.isalnum())


class FilterPlugin:
    """对模型输出进行两步过滤的 egress 插件。

    第一步同时对原文和标准化文本做子串匹配：
    - 原文命中 -> 追加纠正指令重新生成，最多 3 次；仍命中则使用模板。
    - 原文未命中但标准化文本命中 -> 进入模型确认流程。
    模型确认把原文和触发词交给检测模型判断；通过则放行，否则使用模板。
    检测模型未配置时，标准化命中直接拦截。
    """

    plugin_id = "filter"

    def __init__(self, context: EgressPluginContext) -> None:
        self.context = context
        self._vocabulary: list[str] = []
        self._normalized_vocabulary: list[tuple[str, str]] = []
        self._detection_prompt_template: str = ""

    def _load_resources(self) -> None:
        vocab_path = self.context.plugin_root / "vocabulary.txt"
        words = [
            line.strip()
            for line in vocab_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self._vocabulary = words
        self._normalized_vocabulary = [(word, _normalize(word)) for word in words]
        prompt_path = self.context.plugin_root / "detection_prompt.txt"
        self._detection_prompt_template = prompt_path.read_text(encoding="utf-8")

    async def start(self) -> None:
        self._load_resources()

    async def stop(self) -> None:
        """无持有资源；词汇表仅在内存中。"""

    async def filter(self, envelope: EgressEnvelope, call_next: EgressHandler) -> str:
        text = envelope.text

        raw_word = self._find_raw_match(text)
        if raw_word:
            text = await self._regenerate_loop(envelope, text, raw_word)
            if self._find_raw_match(text):
                logger.warning(
                    "模型输出修改 %d 次仍命中敏感词，已拦截",
                    _MAX_REGENERATIONS,
                    extra={
                        "session_id": envelope.session_id,
                        "turn_id": envelope.turn_id,
                        "plugin_id": self.plugin_id,
                    },
                )
                return _TEMPLATE

        normalized_word = self._find_normalized_match(text)
        if normalized_word:
            detection_model = self.context.detection_model_resolver()
            if detection_model is None:
                logger.warning(
                    "标准化文本命中但检测模型未配置，已拦截",
                    extra={
                        "session_id": envelope.session_id,
                        "turn_id": envelope.turn_id,
                        "plugin_id": self.plugin_id,
                    },
                )
                return _TEMPLATE
            approved = await self._detect(
                detection_model,
                text,
                normalized_word,
                envelope,
            )
            if not approved:
                logger.info(
                    "检测模型未通过确认，已拦截",
                    extra={
                        "session_id": envelope.session_id,
                        "turn_id": envelope.turn_id,
                        "plugin_id": self.plugin_id,
                        "word": normalized_word,
                    },
                )
                return _TEMPLATE

        if text == envelope.text:
            return await call_next(envelope)
        return await call_next(replace(envelope, text=text))

    def _find_raw_match(self, text: str) -> str | None:
        """在原文中查找第一个命中的敏感词。"""

        for word in self._vocabulary:
            if word in text:
                return word
        return None

    def _find_normalized_match(self, text: str) -> str | None:
        """在标准化文本中查找第一个命中的敏感词，返回原始词汇。"""

        normalized = _normalize(text)
        for word, normalized_word in self._normalized_vocabulary:
            if normalized_word and normalized_word in normalized:
                return word
        return None

    async def _regenerate_loop(
        self, envelope: EgressEnvelope, text: str, word: str
    ) -> str:
        """最多重新生成 3 次；每次告知模型命中的敏感词。"""

        current = text
        current_word = word
        for _ in range(_MAX_REGENERATIONS):
            current = await self._regenerate(envelope, current, current_word)
            next_word = self._find_raw_match(current)
            if next_word is None:
                return current
            current_word = next_word
        return current

    async def _regenerate(
        self, envelope: EgressEnvelope, text: str, word: str
    ) -> str:
        """追加 assistant 输出和纠正指令后重新调用模型。"""

        messages = list(envelope.prompt_messages)
        messages.append(ModelMessage(role="assistant", content=text))
        messages.append(
            ModelMessage(
                role="user",
                content=f"你发送的内容包含敏感词汇{word}，请重新输出",
            )
        )
        try:
            with model_observation_scope(
                task="chat.filter_regenerate",
                session_id=envelope.session_id,
                turn_id=envelope.turn_id,
                run_id=envelope.turn_id,
            ):
                result = await envelope.model.complete(
                    ModelRequest(
                        messages=messages,
                        model=envelope.model_name,
                        temperature=envelope.temperature,
                        max_tokens=envelope.max_tokens,
                    )
                )
        except Exception:
            logger.exception(
                "重新生成失败，保留原文继续过滤",
                extra={
                    "session_id": envelope.session_id,
                    "turn_id": envelope.turn_id,
                    "plugin_id": self.plugin_id,
                },
            )
            return text
        return result.content or ""

    async def _detect(
        self,
        detection_model,
        text: str,
        word: str,
        envelope: EgressEnvelope,
    ) -> bool:
        """把原文和触发词交给检测模型判断是否可安全发送。"""

        prompt = self._detection_prompt_template.replace("{text}", text).replace("{word}", word)
        try:
            with model_observation_scope(
                task="detection.check",
                profile="detection",
                session_id=envelope.session_id,
                turn_id=envelope.turn_id,
                run_id=envelope.turn_id,
            ):
                result = await detection_model.complete(
                    ModelRequest(
                        messages=[ModelMessage(role="user", content=prompt)],
                        model="",
                        temperature=0,
                        max_tokens=_DETECTION_MAX_TOKENS,
                    )
                )
        except Exception:
            logger.exception(
                "检测模型调用失败，保守拦截",
                extra={"plugin_id": self.plugin_id},
            )
            return False
        return self._parse_detection_result(result.content or "")

    @staticmethod
    def _parse_detection_result(content: str) -> bool:
        """从检测模型响应中提取 approved 布尔值；解析失败视为未通过。"""

        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            match = re.search(r'"approved"\s*:\s*(true|false)', content, re.IGNORECASE)
            if match:
                return match.group(1).lower() == "true"
            logger.warning("检测模型响应无法解析: %s", content[:200])
            return False
        approved = payload.get("approved") if isinstance(payload, dict) else None
        if isinstance(approved, bool):
            return approved
        # 合法 JSON 但不符合契约时不能做真值转换，例如字符串 "false" 为真值。
        logger.warning("检测模型响应字段无效: %s", content[:200])
        return False


def create_plugin(context: EgressPluginContext) -> FilterPlugin:
    """由宿主同步创建输出过滤器插件。"""

    plugin = FilterPlugin(context)
    # 在 start() 之前预加载资源，保证无 egress 流量时也不依赖后续文件状态。
    try:
        plugin._load_resources()
    except (OSError, UnicodeError) as exc:
        raise PluginUnavailableError("输出过滤器资源缺失或损坏") from exc
    return plugin
