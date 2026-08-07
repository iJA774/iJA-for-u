"""聊天模型统一协议适配器。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx

from config.settings import ModelSettings
from domain.errors import (
    InvalidModelResponseError,
    ProviderAuthError,
    ProviderError,
    ProviderQuotaExceededError,
    ProviderRateLimitError,
    ProviderRequestError,
)
from ports import (
    ModelMessage,
    ModelRequest,
    ModelResult,
    ModelStreamEvent,
    ModelToolCall,
    ModelUsage,
    ToolDefinition,
)


class OpenAICompatibleProvider:
    """适配 OpenAI Chat/Responses 与 Anthropic Messages 的公共能力。"""

    def __init__(self, settings: ModelSettings, *, max_attempts: int = 2) -> None:
        self.settings = settings
        if max_attempts < 1:
            raise ValueError("max_attempts 必须至少为 1")
        self.max_attempts = max_attempts
        headers = {"Content-Type": "application/json"}
        if settings.protocol == "anthropic_messages":
            headers.update(
                {
                    "x-api-key": settings.api_key,
                    "anthropic-version": "2023-06-01",
                }
            )
        else:
            headers["Authorization"] = f"Bearer {settings.api_key}"
        self.client = httpx.AsyncClient(
            base_url=settings.base_url.rstrip("/"),
            timeout=httpx.Timeout(settings.timeout_seconds),
            headers=headers,
        )

    @staticmethod
    def _message_payload(message: ModelMessage) -> dict[str, Any]:
        content: Any = message.content
        if message.images:
            content = []
            if message.content:
                content.append({"type": "text", "text": message.content})
            content.extend(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{item.mime_type};base64,{item.base64_data}",
                        "detail": item.detail,
                    },
                }
                for item in message.images
            )
        payload: dict[str, Any] = {"role": message.role, "content": content}
        if message.tool_calls:
            payload["tool_calls"] = [
                {
                    "id": item.id,
                    "type": "function",
                    "function": {"name": item.name, "arguments": item.arguments},
                }
                for item in message.tool_calls
            ]
        if message.tool_call_id:
            payload["tool_call_id"] = message.tool_call_id
        return payload

    @staticmethod
    def _tool_payload(tool: ToolDefinition) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.parameters,
            },
        }

    def _request_payload(self, request: ModelRequest) -> dict[str, Any]:
        """构造同步与 SSE 流式请求共用的最小兼容载荷。"""

        payload: dict[str, Any] = {
            "model": request.model or self.settings.name,
            "messages": [self._message_payload(message) for message in request.messages],
            "temperature": request.temperature,
        }
        token_key = "max_completion_tokens" if self.settings.use_max_completion_tokens else "max_tokens"
        payload[token_key] = request.max_tokens
        if request.json_mode and self.settings.supports_json_object:
            payload["response_format"] = {"type": "json_object"}
        if request.tools:
            if not self.settings.supports_tools:
                raise ProviderError("当前模型配置未启用原生工具调用")
            payload["tools"] = [self._tool_payload(tool) for tool in request.tools]
            payload["tool_choice"] = request.tool_choice or "auto"
        return payload

    def _responses_payload(self, request: ModelRequest) -> dict[str, Any]:
        """把统一请求映射到 OpenAI Responses 公共文本/工具子集。"""

        items: list[dict[str, Any]] = []
        for message in request.messages:
            if message.tool_call_id:
                items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.tool_call_id,
                        "output": message.content or "",
                    }
                )
                continue
            content: list[dict[str, Any]] = []
            if message.content:
                content.append({"type": "input_text", "text": message.content})
            content.extend(
                {
                    "type": "input_image",
                    "image_url": f"data:{image.mime_type};base64,{image.base64_data}",
                    "detail": image.detail,
                }
                for image in message.images
            )
            if content:
                items.append({"role": message.role, "content": content})
            for call in message.tool_calls:
                items.append(
                    {
                        "type": "function_call",
                        "call_id": call.id,
                        "name": call.name,
                        "arguments": call.arguments,
                    }
                )
        payload: dict[str, Any] = {
            "model": request.model or self.settings.name,
            "input": items,
            "temperature": request.temperature,
            "max_output_tokens": request.max_tokens,
        }
        if request.tools:
            if not self.settings.supports_tools:
                raise ProviderError("当前模型配置未启用原生工具调用")
            payload["tools"] = [
                {
                    "type": "function",
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                }
                for tool in request.tools
            ]
            if request.tool_choice == "required":
                payload["tool_choice"] = "required"
        if request.json_mode and self.settings.supports_json_object:
            payload["text"] = {"format": {"type": "json_object"}}
        return payload

    def _anthropic_payload(self, request: ModelRequest) -> dict[str, Any]:
        """把统一请求映射到 Anthropic Messages 文本/视觉/工具子集。"""

        system_parts: list[str] = []
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role == "system":
                if message.content:
                    system_parts.append(message.content)
                continue
            if message.role == "tool":
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": message.tool_call_id,
                                "content": message.content or "",
                            }
                        ],
                    }
                )
                continue
            blocks: list[dict[str, Any]] = []
            if message.content:
                blocks.append({"type": "text", "text": message.content})
            blocks.extend(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": image.mime_type,
                        "data": image.base64_data,
                    },
                }
                for image in message.images
            )
            blocks.extend(
                {
                    "type": "tool_use",
                    "id": call.id,
                    "name": call.name,
                    "input": json.loads(call.arguments),
                }
                for call in message.tool_calls
            )
            messages.append({"role": message.role, "content": blocks})
        payload: dict[str, Any] = {
            "model": request.model or self.settings.name,
            "messages": messages,
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if request.tools:
            if not self.settings.supports_tools:
                raise ProviderError("当前模型配置未启用原生工具调用")
            payload["tools"] = [
                {
                    "name": tool.name,
                    "description": tool.description,
                    "input_schema": tool.parameters,
                }
                for tool in request.tools
            ]
            if request.tool_choice == "required":
                payload["tool_choice"] = {"type": "any"}
        return payload

    def _endpoint_payload(self, request: ModelRequest) -> tuple[str, dict[str, Any]]:
        if self.settings.protocol == "openai_responses":
            return "/responses", self._responses_payload(request)
        if self.settings.protocol == "anthropic_messages":
            return "/messages", self._anthropic_payload(request)
        return "/chat/completions", self._request_payload(request)

    def _parse_usage(self, body: dict[str, Any]) -> ModelUsage | None:
        """只接受上游明确返回的非负整数，不从文本长度推断 token。"""

        raw = body.get("usage")
        if not isinstance(raw, dict):
            return None

        def token(field: str) -> int | None:
            value = raw.get(field)
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

        if self.settings.protocol in {"openai_responses", "anthropic_messages"}:
            input_tokens = token("input_tokens")
            output_tokens = token("output_tokens")
        else:
            input_tokens = token("prompt_tokens")
            output_tokens = token("completion_tokens")
        total_tokens = token("total_tokens")
        if total_tokens is None and input_tokens is not None and output_tokens is not None:
            total_tokens = input_tokens + output_tokens
        if input_tokens is None and output_tokens is None and total_tokens is None:
            return None
        return ModelUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens,
        )

    def _parse_result(self, body: dict[str, Any]) -> ModelResult:
        if self.settings.protocol == "openai_responses":
            content_parts: list[str] = []
            tool_calls: list[ModelToolCall] = []
            for item in body.get("output") or []:
                if item.get("type") == "message":
                    content_parts.extend(
                        block.get("text", "")
                        for block in item.get("content") or []
                        if block.get("type") == "output_text"
                    )
                elif item.get("type") == "function_call":
                    tool_calls.append(
                        ModelToolCall(
                            id=str(item.get("call_id") or item["id"]),
                            name=item["name"],
                            arguments=item["arguments"],
                        )
                    )
            content = "".join(content_parts).strip() or None
            if not content and not tool_calls:
                raise InvalidModelResponseError("Responses 响应没有文本或工具调用")
            return ModelResult(
                content=content,
                tool_calls=tool_calls,
                finish_reason=body.get("status"),
                usage=self._parse_usage(body),
            )
        if self.settings.protocol == "anthropic_messages":
            content_parts = []
            tool_calls = []
            for item in body.get("content") or []:
                if item.get("type") == "text":
                    content_parts.append(str(item.get("text") or ""))
                elif item.get("type") == "tool_use":
                    tool_calls.append(
                        ModelToolCall(
                            id=item["id"],
                            name=item["name"],
                            arguments=json.dumps(
                                item.get("input") or {},
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        )
                    )
            content = "".join(content_parts).strip() or None
            if not content and not tool_calls:
                raise InvalidModelResponseError("Anthropic 响应没有文本或工具调用")
            return ModelResult(
                content=content,
                tool_calls=tool_calls,
                finish_reason=body.get("stop_reason"),
                usage=self._parse_usage(body),
            )
        choice = body["choices"][0]
        message = choice["message"]
        content_value = message.get("content")
        content = content_value.strip() if isinstance(content_value, str) else None
        tool_calls_raw = message.get("tool_calls") or []
        if not isinstance(tool_calls_raw, list):
            raise InvalidModelResponseError("模型响应中的 tool_calls 不是数组")
        tool_calls = []
        for item in tool_calls_raw:
            function = item["function"]
            tool_calls.append(
                ModelToolCall(
                    id=item["id"],
                    name=function["name"],
                    arguments=function["arguments"],
                )
            )
        if not content and not tool_calls:
            raise InvalidModelResponseError("模型响应既没有文本也没有工具调用")
        return ModelResult(
            content=content,
            tool_calls=tool_calls,
            finish_reason=choice.get("finish_reason"),
            usage=self._parse_usage(body),
        )

    @staticmethod
    def _check_response_status(response: httpx.Response) -> None:
        if response.status_code in {401, 403}:
            raise ProviderAuthError("模型服务鉴权失败")
        if response.status_code == 429:
            details: dict[str, object] = {}
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict):
                error = body.get("error")
                if isinstance(error, dict):
                    provider_error_code = error.get("code")
                    if isinstance(provider_error_code, str) and provider_error_code:
                        details["provider_error_code"] = provider_error_code[:100]
                    if provider_error_code == "AccountQuotaExceeded":
                        raise ProviderQuotaExceededError(
                            "模型服务套餐额度已耗尽",
                            details=details or None,
                        )
            retry_after = response.headers.get("retry-after")
            if retry_after:
                try:
                    details["retry_after_seconds"] = max(0.0, float(retry_after))
                except ValueError:
                    pass
            raise ProviderRateLimitError(
                "模型服务触发限流",
                details=details or None,
            )
        if response.status_code >= 500:
            raise ProviderError(f"模型服务暂时不可用，HTTP {response.status_code}")
        if response.status_code >= 400:
            details: dict[str, object] = {"http_status": response.status_code}
            provider_message: str | None = None
            try:
                body = response.json()
            except ValueError:
                body = None
            if isinstance(body, dict):
                error = body.get("error")
                if isinstance(error, dict):
                    code = error.get("code")
                    message = error.get("message")
                    if isinstance(code, str) and code:
                        details["provider_error_code"] = code[:100]
                    if isinstance(message, str) and message:
                        provider_message = message[:500]
                        details["provider_message"] = provider_message
                elif isinstance(body.get("message"), str) and body["message"]:
                    provider_message = body["message"][:500]
                    details["provider_message"] = provider_message
                request_id = body.get("request_id")
                if isinstance(request_id, str) and request_id:
                    details["provider_request_id"] = request_id[:200]
            header_request_id = response.headers.get("x-request-id")
            if header_request_id and "provider_request_id" not in details:
                details["provider_request_id"] = header_request_id[:200]
            hint = "请检查 Base URL、协议、模型 API ID 与请求参数"
            message = f"模型服务拒绝请求，HTTP {response.status_code}；{hint}"
            if provider_message:
                message = f"{message}：{provider_message}"
            raise ProviderRequestError(message, details=details)

    async def complete(self, request: ModelRequest) -> ModelResult:
        endpoint, payload = self._endpoint_payload(request)

        last_error: Exception | None = None
        for attempt in range(self.max_attempts):
            try:
                response = await self.client.post(endpoint, json=payload)
                self._check_response_status(response)
                body = response.json()
                if not isinstance(body, dict):
                    raise InvalidModelResponseError("模型响应不是 JSON 对象")
                return self._parse_result(body)
            except (
                ProviderAuthError,
                ProviderQuotaExceededError,
                ProviderRequestError,
                InvalidModelResponseError,
            ):
                raise
            except ProviderRateLimitError as exc:
                last_error = exc
            except (httpx.TimeoutException, httpx.NetworkError, ProviderError) as exc:
                last_error = exc
            except (httpx.HTTPStatusError, KeyError, IndexError, TypeError, ValueError) as exc:
                raise InvalidModelResponseError(f"模型响应不符合 Chat Completions 契约: {exc}") from exc
            if attempt + 1 < self.max_attempts:
                await asyncio.sleep(0.5)
        if isinstance(last_error, ProviderError):
            raise last_error
        raise ProviderError(f"模型请求失败: {last_error}") from last_error

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        """按 SSE 转发可见文本增量；工具调用继续由完整工具循环处理。"""

        if self.settings.protocol != "openai_chat":
            raise ProviderError("当前协议适配器尚不支持流式输出")
        if request.tools:
            raise ProviderError("原生工具调用 Turn 不支持流式直出")
        payload = self._request_payload(request)
        payload["stream"] = True
        payload["stream_options"] = {"include_usage": True}
        seen_content = False
        try:
            async with self.client.stream("POST", "/chat/completions", json=payload) as response:
                self._check_response_status(response)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line.removeprefix("data:").strip()
                    if raw == "[DONE]":
                        break
                    try:
                        body = json.loads(raw)
                        usage = self._parse_usage(body)
                        choices = body.get("choices")
                        if not choices:
                            if usage is not None:
                                yield ModelStreamEvent(usage=usage)
                            continue
                        choice = body["choices"][0]
                        delta = choice.get("delta", {}).get("content")
                        finish_reason = choice.get("finish_reason")
                    except (IndexError, KeyError, TypeError, ValueError) as exc:
                        raise InvalidModelResponseError(
                            f"模型流式响应不符合 Chat Completions 契约: {exc}"
                        ) from exc
                    if isinstance(delta, str) and delta:
                        seen_content = True
                        yield ModelStreamEvent(delta=delta)
                    if finish_reason is not None:
                        yield ModelStreamEvent(
                            finish_reason=str(finish_reason),
                            usage=usage,
                        )
            if not seen_content:
                raise InvalidModelResponseError("模型流式响应没有返回文本")
        except (ProviderAuthError, ProviderRateLimitError, ProviderError, InvalidModelResponseError):
            raise
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise ProviderError(f"模型流式请求失败: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise InvalidModelResponseError(f"模型流式响应状态异常: {exc}") from exc

    async def probe(self) -> dict[str, Any]:
        """分别探测基础文本、JSON、工具和流式能力，返回配置偏差。"""

        response = await self.complete(
            ModelRequest(
                messages=[ModelMessage(role="user", content="只回复 OK")],
                model=self.settings.name,
                temperature=0,
                max_tokens=16,
            )
        )
        capabilities: dict[str, dict[str, Any]] = {
            "text": {"configured": True, "detected": bool(response.content)},
            "json_object": {
                "configured": self.settings.supports_json_object,
                "detected": False,
            },
            "tools": {
                "configured": self.settings.supports_tools,
                "detected": False,
            },
            "streaming": {
                "configured": self.settings.supports_streaming,
                "detected": False,
            },
            "vision": {
                "configured": self.settings.supports_vision,
                "detected": None,
                "note": "视觉探测需要真实图片，不在无副作用探测中执行",
            },
        }
        if self.settings.supports_json_object:
            try:
                json_result = await self.complete(
                    ModelRequest(
                        messages=[
                            ModelMessage(
                                role="user",
                                content='只返回 JSON 对象 {"ok":true}',
                            )
                        ],
                        model=self.settings.name,
                        temperature=0,
                        max_tokens=32,
                        json_mode=True,
                    )
                )
                parsed = json.loads(json_result.content or "")
                capabilities["json_object"]["detected"] = isinstance(parsed, dict)
            except (ProviderError, InvalidModelResponseError, ValueError) as exc:
                capabilities["json_object"]["error"] = str(exc)[:300]
        if self.settings.supports_tools:
            try:
                tool_response = await self.complete(
                    ModelRequest(
                        messages=[ModelMessage(role="user", content="调用探测工具")],
                        model=self.settings.name,
                        temperature=0,
                        max_tokens=32,
                        tools=[
                            ToolDefinition(
                                name="probe_tool_support",
                                description="用于验证模型是否支持函数工具调用。",
                                parameters={
                                    "type": "object",
                                    "properties": {},
                                    "additionalProperties": False,
                                },
                            )
                        ],
                        tool_choice="required",
                    )
                )
                capabilities["tools"]["detected"] = bool(
                    tool_response.tool_calls
                    and tool_response.tool_calls[0].name == "probe_tool_support"
                )
            except (ProviderError, InvalidModelResponseError, ValueError) as exc:
                capabilities["tools"]["error"] = str(exc)[:300]
        if self.settings.supports_streaming:
            try:
                events = [
                    item
                    async for item in self.stream(
                        ModelRequest(
                            messages=[ModelMessage(role="user", content="只回复 OK")],
                            model=self.settings.name,
                            temperature=0,
                            max_tokens=16,
                        )
                    )
                ]
                capabilities["streaming"]["detected"] = any(
                    item.delta for item in events
                )
            except (ProviderError, InvalidModelResponseError, ValueError) as exc:
                capabilities["streaming"]["error"] = str(exc)[:300]
        compatible = all(
            not detail.get("configured") or detail.get("detected") is not False
            for detail in capabilities.values()
        )
        return {
            "ok": True,
            "compatible": compatible,
            "provider": self.settings.protocol,
            "endpoint": self.settings.base_url,
            "model": self.settings.name,
            "capabilities": capabilities,
            "message": (response.content or "")[:80],
        }

    async def close(self) -> None:
        await self.client.aclose()
