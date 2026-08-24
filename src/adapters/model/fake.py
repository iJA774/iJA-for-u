"""供本地演示和自动化测试使用的确定性模型。"""

from __future__ import annotations

import json
import re
from datetime import datetime
from io import BytesIO
from typing import Any
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

from ports import (
    ImageGenerationRequest,
    ImageGenerationResult,
    ModelRequest,
    ModelResult,
    ModelStreamEvent,
    ModelToolCall,
)


class FakeImageModelProvider:
    """测试和本地演示使用的确定性 9:16 图片 Provider。"""

    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, request: ImageGenerationRequest) -> ImageGenerationResult:
        self.calls += 1
        image = Image.new("RGB", (576, 1024), "#f7d7e3")
        drawer = ImageDraw.Draw(image)
        drawer.ellipse((160, 300, 416, 556), fill="#ffffff", outline="#333333", width=6)
        drawer.arc((215, 370, 275, 430), 180, 360, fill="#333333", width=6)
        drawer.arc((301, 370, 361, 430), 180, 360, fill="#333333", width=6)
        drawer.arc((250, 425, 326, 490), 0, 180, fill="#c44767", width=7)
        output = BytesIO()
        image.save(output, format="PNG")
        del request
        return ImageGenerationResult(content=output.getvalue())

    async def close(self) -> None:
        return None


class FakeModelProvider:
    """不访问网络，但严格遵循 ModelProvider 契约。"""

    def __init__(self, response_text: str | None = None) -> None:
        self.response_text = response_text
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResult:
        self.requests.append(request)
        system_text = "\n".join(item.content or "" for item in request.messages if item.role == "system")
        if "# 画像事实提取任务" in system_text:
            user_text = next(
                (item.content or "" for item in reversed(request.messages) if item.role == "user"), ""
            )
            _, separator, encoded = user_text.partition("\n")
            if not separator:
                raise ValueError("画像提取输入缺少 JSON 数据")
            payload = json.loads(encoded)
            facts = []
            for message in payload.get("messages", []):
                match = re.search(r"我喜欢([^。！!？?\n]{1,80})", message.get("content", ""))
                if match:
                    facts.append(
                        {
                            "category": "偏好",
                            "content": f"喜欢{match.group(1).strip()}",
                            "confidence": 0.95,
                            "source_message_ids": [message["message_id"]],
                        }
                    )
            return ModelResult(content=json.dumps({"facts": facts}, ensure_ascii=False))
        if "# 长期记忆归档任务" in system_text:
            payload = self._last_untrusted_payload(request)
            memories: list[dict[str, Any]] = []
            allowed_ids = set(payload.get("source_message_ids", []))
            for message in payload.get("source_messages", []):
                message_id = str(message.get("message_id") or "")
                if message_id not in allowed_ids or message.get("role") != "user":
                    continue
                content = str(message.get("content") or "")
                preference = re.search(
                    r"我喜欢([^。！!？?\n]{1,80})", content
                )
                if preference:
                    memories.append(
                        {
                            "kind": "preference",
                            "subject_id": message.get("sender_id"),
                            "content": f"喜欢{preference.group(1).strip()}",
                            "confidence": 0.95,
                            "importance": 0.75,
                            "source_message_ids": [message_id],
                            "supersedes_memory_id": None,
                        }
                    )
            return ModelResult(
                content=json.dumps(
                    {"memories": memories, "retract_memory_ids": []},
                    ensure_ascii=False,
                )
            )
        if "# 黑话候选提取任务" in system_text:
            payload = self._last_untrusted_payload(request)
            jargon_candidates: dict[str, list[str]] = {}
            definition_re = re.compile(
                r"[“\"']?([A-Za-z][A-Za-z0-9_-]{1,23}|[\u4e00-\u9fff]{2,10})"
                r"[”\"']?(?:的意思)?(?:是|就是|指|表示|等于|=)"
            )
            token_sources: dict[str, list[str]] = {}
            for message in payload.get("messages", []):
                if message.get("role") != "user":
                    continue
                message_id = str(message.get("message_id") or "")
                content = str(message.get("content") or "")
                for term in definition_re.findall(content):
                    jargon_candidates.setdefault(term, []).append(message_id)
                for term in set(
                    re.findall(
                        r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_-]{1,15}(?![A-Za-z0-9_])",
                        content,
                    )
                ):
                    if term.casefold() not in {"http", "https", "www", "api"}:
                        token_sources.setdefault(term, []).append(message_id)
            for term, source_ids in token_sources.items():
                if len(set(source_ids)) >= 2:
                    jargon_candidates.setdefault(term, []).extend(source_ids)
            return ModelResult(
                content=json.dumps(
                    {
                        "candidates": [
                            {
                                "term": term,
                                "source_message_ids": list(dict.fromkeys(source_ids)),
                            }
                            for term, source_ids in jargon_candidates.items()
                        ]
                    },
                    ensure_ascii=False,
                )
            )
        if "# 黑话语义推断任务" in system_text:
            payload = self._last_untrusted_payload(request)
            messages = payload.get("messages", [])
            inferences = []
            known = {
                "yyds": "“永远的神”的拼音首字母缩写，用于强烈夸赞。",
                "xswl": "“笑死我了”的拼音首字母缩写，用于表示觉得很好笑。",
            }
            for candidate in payload.get("candidates", []):
                term = str(candidate.get("term") or "")
                meaning = ""
                pattern = re.compile(
                    re.escape(term)
                    + r"[”\"']?(?:的意思)?(?:是|就是|指|表示|等于|=)"
                    + r"\s*([^。！!？?\n]{1,100})",
                    re.IGNORECASE,
                )
                for message in messages:
                    if message.get("role") != "user":
                        continue
                    match = pattern.search(str(message.get("content") or ""))
                    if match:
                        meaning = match.group(1).strip(" ，,；;")
                        break
                meaning = meaning or known.get(term.casefold(), "")
                inferences.append(
                    {
                        "jargon_id": candidate.get("jargon_id"),
                        "status": "active" if meaning else "candidate",
                        "meaning": meaning,
                        "confidence": 0.92 if meaning else 0.35,
                    }
                )
            return ModelResult(
                content=json.dumps({"inferences": inferences}, ensure_ascii=False)
            )
        if (
            "# 群体表达方式学习任务" in system_text
            or "# 会话表达方式学习任务" in system_text
        ):
            payload = self._last_untrusted_payload(request)
            patterns = []
            surprise_ids = [
                str(message.get("message_id"))
                for message in payload.get("messages", [])
                if message.get("role") == "user"
                and any(
                    marker in str(message.get("content") or "")
                    for marker in ("我嘞个", "离谱", "卧槽", "我去")
                )
            ]
            if len(surprise_ids) >= 2:
                patterns.append(
                    {
                        "situation": "表示惊叹或意外",
                        "style": "使用短促的感叹开场",
                        "confidence": 0.82,
                        "source_message_ids": surprise_ids,
                    }
                )
            return ModelResult(
                content=json.dumps({"patterns": patterns}, ensure_ascii=False)
            )
        if "# 行为选择场景画像任务" in system_text:
            payload = self._last_untrusted_payload(request)
            text = "\n".join(
                str(item.get("content") or "")
                for item in payload.get("messages", [])
            )
            tag_clusters = []
            need = None
            traits = []
            if any(
                marker in text
                for marker in ("报错", "接口", "配置", "代码", "bug")
            ):
                tag_clusters.append(
                    {
                        "tag_name": "技术排障",
                        "tag_aliases": ["故障排查", "定位报错"],
                    }
                )
                need = {
                    "tag_name": "澄清关键信息",
                    "tag_aliases": ["补充必要上下文"],
                }
                traits.append(
                    {"tag_name": "困惑", "tag_aliases": ["不知所措"]}
                )
            elif any(marker in text for marker in ("哈哈", "离谱", "笑死")):
                tag_clusters.append(
                    {
                        "tag_name": "轻松玩笑",
                        "tag_aliases": ["接梗", "调侃"],
                    }
                )
            return ModelResult(
                content=json.dumps(
                    {
                        "summary": "当前对话场景",
                        "tag_clusters": tag_clusters,
                        "need": need,
                        "other_traits": traits,
                        "confidence": 0.8 if tag_clusters else 0.2,
                    },
                    ensure_ascii=False,
                )
            )
        if "# 场景与行为模式学习任务" in system_text:
            payload = self._last_untrusted_payload(request)
            messages = payload.get("messages", [])
            user_messages = [
                item for item in messages if item.get("role") == "user"
            ]
            assistant_messages = [
                item for item in messages if item.get("role") == "assistant"
            ]
            patterns = []
            if len(user_messages) >= 2 and assistant_messages:
                source_ids = [
                    str(item.get("message_id"))
                    for item in [user_messages[-2], assistant_messages[-1], user_messages[-1]]
                ]
                patterns.append(
                    {
                        "scene_summary": "对方带着问题继续补充信息",
                        "scene_tags": [
                            {
                                "tag_name": "技术排障",
                                "tag_aliases": ["故障排查", "定位报错"],
                            }
                        ],
                        "need_tags": [
                            {
                                "tag_name": "追问关键信息",
                                "tag_aliases": ["澄清必要上下文"],
                            }
                        ],
                        "other_traits": [
                            {
                                "tag_name": "愿意配合",
                                "tag_aliases": ["可协作"],
                            }
                        ],
                        "action": "先确认当前信息边界，再追问一个关键点",
                        "expected_outcome": "对方补充信息，使问题更容易继续处理",
                        "actor_type": "agent_self",
                        "learning_type": "self_reflection",
                        "confidence": 0.78,
                        "source_message_ids": source_ids,
                    }
                )
            return ModelResult(
                content=json.dumps({"patterns": patterns}, ensure_ascii=False)
            )
        if "# 行为选择反馈评价任务" in system_text:
            payload = self._last_untrusted_payload(request)
            timeline = payload.get("timeline", [])
            assistant_ids = [
                str(item.get("message_id"))
                for item in timeline
                if item.get("role") == "assistant"
            ]
            positive_users = [
                item
                for item in timeline
                if item.get("role") == "user"
                and any(
                    marker in str(item.get("content") or "")
                    for marker in ("谢谢", "明白", "好了", "可以", "补充", "确实")
                )
            ]
            feedback = []
            if assistant_ids and positive_users:
                for reference in payload.get("behavior_references", []):
                    feedback.append(
                        {
                            "selection_id": reference.get("selection_id"),
                            "adopted": True,
                            "status": "success",
                            "score_delta": 0.7,
                            "outcome": "用户给出积极回应并继续推进对话",
                            "reason": "助手采用了参考行为，用户随后给出明确积极反馈",
                            "source_message_ids": [
                                assistant_ids[-1],
                                str(positive_users[-1].get("message_id")),
                            ],
                        }
                    )
            return ModelResult(
                content=json.dumps({"feedback": feedback}, ensure_ascii=False)
            )
        if "# 主动候选判断任务" in system_text:
            payload = self._last_untrusted_payload(request)
            candidates = payload.get("candidates", [])
            if not candidates:
                return ModelResult(
                    content=json.dumps(
                        {
                            "action": "skip",
                            "candidate_id": None,
                            "score": 0.0,
                            "reason_code": "no_candidates",
                            "reason": "没有候选",
                        },
                        ensure_ascii=False,
                    )
                )
            return ModelResult(
                content=json.dumps(
                    {
                        "action": "send",
                        "candidate_id": candidates[0]["candidate_id"],
                        "score": 0.9,
                        "reason_code": "relevant",
                        "reason": "Fake Provider 选择首个候选",
                    },
                    ensure_ascii=False,
                )
            )
        if "# 主动消息生成任务" in system_text:
            payload = self._last_untrusted_payload(request)
            candidate = payload.get("candidate", {})
            title = str(candidate.get("title") or "有条新内容")
            url = str(candidate.get("url") or "")
            text = f"看到一条可能和你有关的内容：{title}"
            if url:
                text += f"\n{url}"
            return ModelResult(
                content=json.dumps({"text": text}, ensure_ascii=False)
            )
        if "# Drift 活动选择任务" in system_text:
            payload = self._last_untrusted_payload(request)
            activity = (
                "candidate_aggregation"
                if len(payload.get("rss_candidates", [])) >= 2
                else "conversation_topic_preparation"
                if payload.get("messages")
                or payload.get("facts")
                or payload.get("memories")
                else "idle"
            )
            return ModelResult(
                content=json.dumps(
                    {"activity": activity, "reason": "Fake Provider 确定性选择"},
                    ensure_ascii=False,
                )
            )
        if "# Drift 原子活动任务" in system_text:
            payload = self._last_untrusted_payload(request)
            activity = payload.get("activity")
            if activity == "candidate_aggregation":
                parents = [
                    item["candidate_id"]
                    for item in payload.get("rss_candidates", [])[:3]
                ]
                evidence = [
                    ref
                    for item in payload.get("rss_candidates", [])[:3]
                    for ref in item.get("source_refs", [])
                ]
                result = {
                    "title": "近期订阅内容小结",
                    "summary": "几条相关订阅内容可以放在一起关注。",
                    "parent_candidate_ids": parents,
                    "evidence_refs": evidence,
                }
            else:
                messages = payload.get("messages", [])
                facts = payload.get("facts", [])
                memories = payload.get("memories", [])
                evidence = []
                if messages:
                    evidence.append(messages[-1]["message_id"])
                elif facts:
                    evidence.append(facts[0]["fact_id"])
                elif memories:
                    evidence.append(memories[0]["memory_id"])
                result = {
                    "title": "可以自然延续的话题",
                    "summary": "基于最近聊天准备一个低打扰的延续话题。",
                    "parent_candidate_ids": [],
                    "evidence_refs": evidence,
                }
            return ModelResult(content=json.dumps(result, ensure_ascii=False))
        if self.response_text is not None:
            return ModelResult(content=self.response_text)
        last_message = request.messages[-1] if request.messages else None
        if last_message and last_message.role == "tool":
            try:
                tool_result = json.loads(last_message.content or "{}")
                summary = tool_result.get("summary") or tool_result.get("resolved_name") or "查询完成"
            except json.JSONDecodeError:
                summary = "查询完成"
            return ModelResult(content=f"工具结果：{summary}", finish_reason="stop")
        user_text = next(
            (item.content or "" for item in reversed(request.messages) if item.role == "user"), ""
        )
        available = {tool.name for tool in request.tools or []}
        if "schedule_create" in available and any(
            term in user_text for term in ("每天", "每周", "每隔", "周期", "定时", "提醒")
        ):
            interval_match = re.search(r"每隔\s*(\d+)\s*分钟", user_text)
            if interval_match:
                rrule = f"FREQ=MINUTELY;INTERVAL={interval_match.group(1)}"
            elif "每周" in user_text:
                rrule = "FREQ=WEEKLY"
            else:
                rrule = "FREQ=DAILY"
            now = datetime.now(ZoneInfo("Asia/Shanghai"))
            return ModelResult(
                tool_calls=[
                    ModelToolCall(
                        id="fake_call_schedule_create",
                        name="schedule_create",
                        arguments=json.dumps(
                            {
                                "title": user_text[:40],
                                "instruction": user_text,
                                "timezone": "Asia/Shanghai",
                                "dtstart": now.isoformat(),
                                "rrule": rrule,
                            },
                            ensure_ascii=False,
                        ),
                    )
                ],
                finish_reason="tool_calls",
            )
        if "get_current_time" in available and any(term in user_text for term in ("几点", "时间", "日期")):
            return ModelResult(
                tool_calls=[
                    ModelToolCall(
                        id="fake_call_time",
                        name="get_current_time",
                        arguments=json.dumps({"timezone": "Asia/Shanghai"}),
                    )
                ],
                finish_reason="tool_calls",
            )
        if "get_weather" in available and "天气" in user_text:
            location_match = re.search(r"(?:查|看|告诉我)?([^，。！？\s]{2,20})(?:的)?天气", user_text)
            location = location_match.group(1) if location_match else "北京"
            return ModelResult(
                tool_calls=[
                    ModelToolCall(
                        id="fake_call_weather",
                        name="get_weather",
                        arguments=json.dumps({"location": location, "days": 1}, ensure_ascii=False),
                    )
                ],
                finish_reason="tool_calls",
            )
        visible_user_text = self._visible_user_text(user_text)
        return ModelResult(
            content=(
                f"我听到了：{visible_user_text[-120:]}"
                if visible_user_text
                else "我在。"
            )
        )

    async def stream(self, request: ModelRequest):
        """以确定性单增量模拟流式协议，供本地 UI 与契约测试使用。"""

        result = await self.complete(request)
        if result.tool_calls:
            raise ValueError("Fake 流式路径不支持工具调用")
        if result.content:
            yield ModelStreamEvent(delta=result.content, finish_reason=result.finish_reason)

    async def probe(self) -> dict[str, Any]:
        return {"ok": True, "provider": "fake", "tools": True, "message": "Fake Provider 可用"}

    async def close(self) -> None:
        return None

    @staticmethod
    def _last_untrusted_payload(request: ModelRequest) -> dict[str, Any]:
        text = next(
            (
                item.content or ""
                for item in reversed(request.messages)
                if item.role == "user"
            ),
            "",
        )
        _, separator, encoded = text.partition("\n")
        if not separator:
            return {}
        loaded = json.loads(encoded)
        return loaded if isinstance(loaded, dict) else {}

    @staticmethod
    def _visible_user_text(text: str) -> str:
        """移除应用层稳定索引，只回显模型实际可见的用户正文。"""

        marker = "[应用层消息索引，固定首行]"
        if text.startswith(marker) and "\n" in text:
            return text.split("\n", maxsplit=1)[1]
        return text
