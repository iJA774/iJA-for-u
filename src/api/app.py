"""FastAPI 本地控制台入口。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any
from uuid import uuid4

from fastapi import (
    APIRouter,
    FastAPI,
    File,
    Form,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from api.schemas import (
    BlacklistCreateRequest,
    CreateSessionRequest,
    DeleteAllLocalUserDataRequest,
    DetectionModelConfigUpdateRequest,
    EmbeddingConfigUpdateRequest,
    EngagementPolicyUpdateRequest,
    ExpressionRenameRequest,
    FeedCreateRequest,
    FeedUpdateRequest,
    GroupParticipationPolicyUpdateRequest,
    ImageModelConfigUpdateRequest,
    MemoryCorrectRequest,
    MemoryCreateRequest,
    MemoryForgetRequest,
    ModelConfigUpdateRequest,
    OneBotGroupAccessUpdateRequest,
    PersonaAssignmentsUpdateRequest,
    PersonaUpdateRequest,
    ProactiveCandidateCreateRequest,
    RevisionRequest,
    ScheduleUpdateRequest,
    SendMessageRequest,
    SessionMembersUpdateRequest,
    VisionModelConfigUpdateRequest,
)
from api.security import ControlAccessFailure, ControlPlaneSecurity
from bootstrap import build_runtime
from config import (
    AppSettings,
    load_persisted_detection_model_settings,
    load_persisted_embedding_settings,
    load_persisted_image_model_settings,
    load_persisted_model_settings,
    load_persisted_vision_model_settings,
    load_settings,
    save_detection_model_settings,
    save_embedding_settings,
    save_image_model_settings,
    save_model_settings,
    save_onebot_group_configs,
    save_vision_model_settings,
)
from domain.errors import ConflictError, IJAError, InputValidationError, NotFoundError
from domain.models import (
    BlacklistSource,
    InboundMessage,
    MemoryKind,
    MemorySourceChain,
    MemoryStatus,
    ProactiveCandidateStatus,
)
from observability import model_observation_scope
from plugins._host import HttpCallbackRequest
from ports import ImageModelProvider, ModelProvider
from proactive.rss import CandidateSource

logger = logging.getLogger(__name__)


def _error_payload(code: str, message: str, request_id: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message, "request_id": request_id}}


def _control_failure_response(
    failure: ControlAccessFailure,
    request_id: str,
) -> JSONResponse:
    """生成不回显 Host、Origin 或 Token 的统一控制面拒绝响应。"""

    response = JSONResponse(
        status_code=failure.status_code,
        content=_error_payload(failure.code, failure.message, request_id),
    )
    if failure.status_code == 401:
        response.headers["WWW-Authenticate"] = "Bearer"
    return response


def _is_public_plugin_callback(path: str) -> bool:
    """公开平台回调使用插件自身认证，不能套用浏览器控制面规则。"""

    parts = path.strip("/").split("/")
    return len(parts) == 3 and parts[0] == "platform-plugins" and bool(parts[1]) and parts[2] == "callback"


def _is_control_api(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def create_app(
    *,
    project_root: Path | None = None,
    settings: AppSettings | None = None,
    model_override: ModelProvider | None = None,
    image_model_override: ImageModelProvider | None = None,
    rss_override: CandidateSource | None = None,
) -> FastAPI:
    """创建可测试的应用实例；生产启动和测试共用同一装配路径。"""

    app_settings = settings or load_settings(project_root)
    runtime = build_runtime(
        app_settings,
        model_override=model_override,
        image_model_override=image_model_override,
        rss_override=rss_override,
    )
    control_security = ControlPlaneSecurity(app_settings.server)
    boot_id = uuid4().hex

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(title="iJA for u", version="0.1.0", lifespan=lifespan)
    app.state.runtime = runtime
    app.state.boot_id = boot_id
    if not control_security.authentication_enabled:
        logger.warning(
            "控制面 Token 未配置：当前仅依赖回环监听与 Host/Origin 校验；"
            "同一系统用户下的本地进程不受此模式隔离",
            extra={"session_id": "-", "turn_id": "-", "control_authentication": "disabled"},
        )

    @app.api_route(
        "/platform-plugins/{plugin_id}/callback",
        methods=["GET", "POST"],
        include_in_schema=False,
    )
    async def platform_plugin_callback(plugin_id: str, request: Request) -> Response:
        """把公开平台回调转成框架无关请求，插件自行完成来源认证。"""

        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError as exc:
                raise InputValidationError("Content-Length 无效") from exc
            if declared_length > app_settings.platform_plugins.callback_max_body_bytes:
                return Response(status_code=413, content="payload too large")
        body_parts: list[bytes] = []
        body_size = 0
        async for chunk in request.stream():
            body_size += len(chunk)
            if body_size > app_settings.platform_plugins.callback_max_body_bytes:
                return Response(status_code=413, content="payload too large")
            body_parts.append(chunk)
        body = b"".join(body_parts)
        result = await runtime.platform_plugins.handle_http(
            plugin_id,
            HttpCallbackRequest(
                method=request.method,
                query=dict(request.query_params),
                headers=dict(request.headers),
                body=body,
            ),
        )
        return Response(
            status_code=result.status_code,
            content=result.body,
            media_type=result.media_type,
            headers=dict(result.headers),
        )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = request.headers.get("X-Request-ID") or f"req_{uuid4().hex}"
        failure = None
        if not _is_public_plugin_callback(request.url.path):
            failure = control_security.validate_request_boundary(
                request.headers,
                scheme=request.url.scheme,
            )
        if _is_control_api(request.url.path) and failure is None:
            authentication_exempt = request.url.path in {
                "/api/health",
                "/api/readiness",
            }
            if not authentication_exempt:
                failure = control_security.validate_http_authentication(
                    request.headers,
                    request.cookies,
                )
                if (
                    failure is None
                    and control_security.authentication_enabled
                    and request.method not in {"GET", "HEAD", "OPTIONS"}
                    and request.headers.get("origin") is None
                    and not control_security.bearer_authenticated(request.headers)
                ):
                    failure = control_security.origin_required_failure()
        if failure is not None:
            response = _control_failure_response(
                failure,
                request.state.request_id,
            )
            response.headers["X-Request-ID"] = request.state.request_id
            return response
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(IJAError)
    async def handle_ija_error(request: Request, exc: IJAError):
        status = 400
        if isinstance(exc, NotFoundError):
            status = 404
        elif isinstance(exc, ConflictError):
            status = 409
        content = _error_payload(exc.code, str(exc), request.state.request_id)
        if exc.details is not None:
            content["details"] = exc.details
        return JSONResponse(
            status_code=status,
            content=content,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={
                **_error_payload("request_validation_error", "请求字段校验失败", request.state.request_id),
                "details": [
                    {key: error[key] for key in ("type", "loc", "msg") if key in error}
                    for error in exc.errors()
                ],
            },
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception):
        """捕获未处理的服务端异常（含数据库错误），记录后返回 500，不暴露内部细节。"""

        logger.exception(
            "未处理的服务端异常",
            extra={
                "session_id": "-",
                "turn_id": "-",
                "path": request.url.path,
                "method": request.method,
            },
        )
        return JSONResponse(
            status_code=500,
            content=_error_payload(
                "internal_error", "服务端内部错误，请查看控制台日志", request.state.request_id
            ),
        )

    router = APIRouter(prefix="/api")

    async def _readiness_payload() -> tuple[bool, dict[str, Any]]:
        """汇总已完成启动链和当前插件 generation 的真实准入状态。"""

        snapshot = await runtime.readiness_snapshot()
        ready = snapshot["ready"] is True
        return ready, {
            "ok": ready,
            "status": "ready" if ready else "not_ready",
            "boot_id": boot_id,
            "runtime_state": snapshot["state"],
            "checks": snapshot["checks"],
            "current_plugin_generation": snapshot["current_plugin_generation"],
        }

    def config_status_payload() -> dict[str, Any]:
        """构造可安全返回给浏览器的运行配置，不包含 API Key。"""

        persisted_model = load_persisted_model_settings(
            app_settings.project_root,
            data_dir=app_settings.storage.data_dir,
        )
        persisted_image_model = load_persisted_image_model_settings(
            app_settings.project_root,
            data_dir=app_settings.storage.data_dir,
        )
        persisted_vision_model = load_persisted_vision_model_settings(
            app_settings.project_root,
            data_dir=app_settings.storage.data_dir,
        )
        persisted_detection_model = load_persisted_detection_model_settings(
            app_settings.project_root,
            data_dir=app_settings.storage.data_dir,
        )
        persisted_embedding = load_persisted_embedding_settings(
            app_settings.project_root,
            data_dir=app_settings.storage.data_dir,
        )
        return {
            "server": {
                "host": app_settings.server.host,
                "port": app_settings.server.port,
                "control_authentication": (
                    "required" if control_security.authentication_enabled else "disabled_local_compatibility"
                ),
                "trusted_hosts": app_settings.server.trusted_hosts,
                "trusted_origins": app_settings.server.trusted_origins,
                "event_history_capacity": app_settings.server.event_history_capacity,
            },
            "model": {
                "mode": app_settings.model.mode,
                "protocol": app_settings.model.protocol,
                "base_url": app_settings.model.base_url,
                "name": app_settings.model.name,
                "profile_protocol": (app_settings.model.profile_protocol or app_settings.model.protocol),
                "profile_base_url": (app_settings.model.profile_base_url or app_settings.model.base_url),
                "profile_name": app_settings.model.profile_name,
                "api_key_configured": bool(app_settings.model.api_key),
                "api_key_saved_locally": bool(persisted_model.api_key),
                "profile_api_key_configured": bool(
                    app_settings.model.profile_api_key or app_settings.model.api_key
                ),
                "profile_api_key_saved_locally": bool(persisted_model.profile_api_key),
                "supports_json_object": app_settings.model.supports_json_object,
                "supports_tools": app_settings.model.supports_tools,
                "supports_vision": app_settings.model.supports_vision,
                "supports_streaming": app_settings.model.supports_streaming,
                "task_profiles": {
                    task: profile.model_dump()
                    for task, profile in app_settings.model.task_profiles.items()
                },
            },
            "image_model": {
                "enabled": app_settings.image_model.enabled,
                "base_url": app_settings.image_model.base_url,
                "name": app_settings.image_model.name,
                "timeout_seconds": app_settings.image_model.timeout_seconds,
                "api_key_configured": bool(app_settings.image_model.api_key),
                "api_key_saved_locally": bool(persisted_image_model.api_key),
            },
            "vision_model": {
                "mode": app_settings.vision_model.mode,
                "protocol": app_settings.vision_model.protocol,
                "base_url": app_settings.vision_model.base_url,
                "name": app_settings.vision_model.name,
                "timeout_seconds": app_settings.vision_model.timeout_seconds,
                "wait_seconds": app_settings.vision_model.wait_seconds,
                "api_key_configured": bool(app_settings.vision_model.api_key),
                "api_key_saved_locally": bool(persisted_vision_model.api_key),
            },
            "detection_model": {
                "enabled": app_settings.detection_model.enabled,
                "base_url": app_settings.detection_model.base_url,
                "name": app_settings.detection_model.name,
                "timeout_seconds": app_settings.detection_model.timeout_seconds,
                "api_key_configured": bool(app_settings.detection_model.api_key),
                "api_key_saved_locally": bool(persisted_detection_model.api_key),
            },
            "embedding": {
                "enabled": app_settings.embedding.enabled,
                "available": app_settings.embedding.available,
                "base_url": app_settings.embedding.base_url,
                "name": app_settings.embedding.name,
                "timeout_seconds": app_settings.embedding.timeout_seconds,
                "api_key_configured": bool(app_settings.embedding.api_key),
                "api_key_saved_locally": bool(persisted_embedding.api_key),
            },
            "chat": app_settings.chat.model_dump(),
            "memory": app_settings.memory.model_dump(),
            "social_learning": app_settings.social_learning.model_dump(),
        }

    @router.get("/health")
    async def health() -> dict[str, Any]:
        """仅报告进程存活；依赖是否完成初始化由 readiness 单独表达。"""

        return {
            "ok": True,
            "status": "alive",
            "version": "0.1.0",
            "boot_id": boot_id,
            "control_authentication": (
                "required" if control_security.authentication_enabled else "disabled_local_compatibility"
            ),
        }

    @router.get("/readiness")
    async def readiness() -> JSONResponse:
        """只有完整启动链和当前插件代都可受理时才返回 200。"""

        ready, payload = await _readiness_payload()
        return JSONResponse(status_code=200 if ready else 503, content=payload)

    @router.post("/control/session")
    async def create_control_session(request: Request) -> JSONResponse:
        """用 Bearer Token 换取同站 HttpOnly Cookie，供媒体与 WebSocket 复用。"""

        response = JSONResponse(
            {
                "ok": True,
                "authentication": (
                    "required" if control_security.authentication_enabled else "disabled_local_compatibility"
                ),
            }
        )
        if control_security.authentication_enabled:
            response.set_cookie(
                key=control_security.SESSION_COOKIE_NAME,
                value=control_security.session_credential,
                # 主流浏览器会限制持久 Cookie 的最大寿命；使用 400 天并在每次
                # bootstrapControlSession 成功时重新签发，可跨浏览器重启并持续续期。
                max_age=400 * 24 * 60 * 60,
                path="/api",
                secure=request.url.scheme == "https",
                httponly=True,
                samesite="strict",
            )
        return response

    @router.delete("/control/session")
    async def delete_control_session() -> JSONResponse:
        """清除当前浏览器控制会话；控制 Token 本身保持不变。"""

        response = JSONResponse({"ok": True})
        response.delete_cookie(
            key=control_security.SESSION_COOKIE_NAME,
            path="/api",
            httponly=True,
            samesite="strict",
        )
        return response

    @router.post("/simulations/sessions")
    async def create_session(payload: CreateSessionRequest):
        return await runtime.chat.create_session(
            chat_type=payload.chat_type,
            display_name=payload.display_name,
            external_chat_id=payload.external_chat_id,
            participants=payload.participants,
        )

    @router.get("/sessions")
    async def list_sessions():
        return await runtime.store.list_sessions()

    @router.delete("/sessions/{session_id}")
    async def delete_session(session_id: str):
        """物理删除整个会话及其全部聊天记录与记忆，会话从列表消失。"""

        return await runtime.chat.delete_session(session_id)

    @router.get("/sessions/{session_id}/messages")
    async def list_messages(session_id: str, limit: int = 100):
        if await runtime.store.get_session(session_id) is None:
            raise NotFoundError("会话不存在")
        return await runtime.store.list_messages(session_id, min(max(limit, 1), 500))

    @router.get("/sessions/{session_id}/messages/search")
    async def search_session_messages(
        session_id: str,
        query: Annotated[str, Query(min_length=1, max_length=500)],
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
    ):
        """按发送者名称或用户可见正文搜索当前会话消息。"""

        return await runtime.store.search_session_messages(
            session_id,
            query_text=query,
            limit=limit,
        )

    @router.get("/sessions/{session_id}/messages/{message_id}/context")
    async def get_session_message_context(
        session_id: str,
        message_id: str,
        before: Annotated[int, Query(ge=0, le=100)] = 50,
        after: Annotated[int, Query(ge=0, le=100)] = 50,
    ):
        """读取当前会话中目标消息前后的有限上下文。"""

        return await runtime.store.get_session_message_window(
            session_id,
            message_id,
            before=before,
            after=after,
        )

    @router.delete("/sessions/{session_id}/messages")
    async def clear_session_chat(session_id: str):
        """清空当前会话正文与正文型审计，保留长期知识和会话配置。"""

        return await runtime.chat.clear_chat(session_id)

    @router.delete("/messages")
    async def clear_all_chat():
        """清空全部会话正文与正文型审计，保留长期知识和会话配置。"""

        return await runtime.chat.clear_chat()

    @router.delete("/local-data")
    async def delete_all_local_user_data(
        payload: DeleteAllLocalUserDataRequest,
    ):
        """精确确认后删除全部本地用户数据；应用配置与凭据保持不变。"""

        return await runtime.chat.delete_all_local_user_data(
            payload.confirmation
        )

    @router.get("/sessions/{session_id}/learning/jargons")
    async def list_learned_jargons(session_id: str, limit: int = 500):
        if await runtime.store.get_session(session_id) is None:
            raise NotFoundError("会话不存在")
        return await runtime.store.list_jargons(session_id, limit=min(max(limit, 1), 1000))

    @router.get("/sessions/{session_id}/learning/expressions")
    async def list_learned_expressions(session_id: str, limit: int = 500):
        if await runtime.store.get_session(session_id) is None:
            raise NotFoundError("会话不存在")
        return await runtime.store.list_group_expressions(session_id, limit=min(max(limit, 1), 1000))

    @router.get("/sessions/{session_id}/learning/behaviors")
    async def list_learned_behaviors(session_id: str, limit: int = 500):
        if await runtime.store.get_session(session_id) is None:
            raise NotFoundError("会话不存在")
        return await runtime.store.list_behavior_patterns(session_id, limit=min(max(limit, 1), 1000))

    @router.get("/sessions/{session_id}/learning/indexes")
    async def get_social_learning_indexes(session_id: str):
        """查看表达簇和行为标签图，不返回原始 embedding 向量。"""

        if await runtime.store.get_session(session_id) is None:
            raise NotFoundError("会话不存在")
        return await runtime.store.get_social_learning_index_data(session_id)

    @router.get("/social-learning-runs")
    async def list_social_learning_runs(session_id: str | None = None, limit: int = 500):
        return await runtime.store.list_social_learning_runs(session_id, limit=min(max(limit, 1), 1000))

    @router.post("/social-learning-runs/{run_id}/retry")
    async def retry_social_learning_run(run_id: str):
        return await runtime.social_learning.retry_run(run_id)

    @router.post("/social-learning-maintenance")
    async def run_social_learning_maintenance():
        """立即扫描时效状态；衰减周期仍按最后强化时间幂等计算。"""

        return await runtime.social_learning.run_maintenance(force=True)

    @router.get("/sessions/{session_id}/decisions")
    async def list_decisions(session_id: str):
        if await runtime.store.get_session(session_id) is None:
            raise NotFoundError("会话不存在")
        decisions = await runtime.store.list_decisions(session_id)
        return [
            {
                **decision.model_dump(mode="json"),
                "retryable": await runtime.chat.reactive_reply_retryable(
                    session_id,
                    decision,
                ),
            }
            for decision in decisions
        ]

    @router.post("/sessions/{session_id}/turns/{turn_id}/retry-reply")
    async def retry_reply(session_id: str, turn_id: str):
        """人工重试未成功送达的被动回复，禁止重放已送达 Turn。"""

        return await runtime.chat.retry_reactive_reply(session_id, turn_id)

    @router.get("/sessions/{session_id}/engagement-policy")
    async def get_engagement_policy(session_id: str):
        return await runtime.engagement.get_policy(session_id)

    @router.put("/sessions/{session_id}/engagement-policy")
    async def update_engagement_policy(session_id: str, payload: EngagementPolicyUpdateRequest):
        policy = await runtime.engagement.update_policy(session_id, **payload.model_dump())
        runtime.proactive_scheduler.wake()
        runtime.drift_scheduler.wake()
        return policy

    @router.get("/sessions/{session_id}/group-participation-policy")
    async def get_group_participation_policy(session_id: str):
        return await runtime.chat.get_group_participation_policy(session_id)

    @router.put("/sessions/{session_id}/group-participation-policy")
    async def update_group_participation_policy(
        session_id: str,
        payload: GroupParticipationPolicyUpdateRequest,
    ):
        return await runtime.chat.update_group_participation_policy(
            session_id,
            **payload.model_dump(),
        )

    @router.get("/sessions/{session_id}/feeds")
    async def list_feeds(session_id: str):
        session = await runtime.store.get_session(session_id)
        if session is None:
            raise NotFoundError("会话不存在")
        return await runtime.store.list_feed_sources(session_id)

    @router.post("/sessions/{session_id}/feeds")
    async def create_feed(session_id: str, payload: FeedCreateRequest):
        source = await runtime.engagement.create_feed(session_id, **payload.model_dump())
        runtime.proactive_scheduler.wake()
        return source

    @router.put("/sessions/{session_id}/feeds/{feed_id}")
    async def update_feed(session_id: str, feed_id: str, payload: FeedUpdateRequest):
        existing = await runtime.store.get_feed_source(feed_id)
        if existing is None or existing.session_id != session_id:
            raise NotFoundError("订阅源不存在")
        source = await runtime.engagement.update_feed(feed_id, **payload.model_dump())
        runtime.proactive_scheduler.wake()
        return source

    @router.delete("/sessions/{session_id}/feeds/{feed_id}")
    async def delete_feed(session_id: str, feed_id: str):
        existing = await runtime.store.get_feed_source(feed_id)
        if existing is None or existing.session_id != session_id:
            raise NotFoundError("订阅源不存在")
        return await runtime.engagement.delete_feed(feed_id)

    @router.post("/sessions/{session_id}/feeds/{feed_id}/refresh")
    async def refresh_feed(session_id: str, feed_id: str):
        existing = await runtime.store.get_feed_source(feed_id)
        if existing is None or existing.session_id != session_id:
            raise NotFoundError("订阅源不存在")
        source = await runtime.engagement.refresh_feed(feed_id)
        runtime.proactive_scheduler.wake()
        return source

    @router.get("/sessions/{session_id}/proactive/candidates")
    async def list_proactive_candidates(session_id: str, status: str | None = None):
        if await runtime.store.get_session(session_id) is None:
            raise NotFoundError("会话不存在")
        statuses = None
        if status:
            try:
                statuses = {
                    ProactiveCandidateStatus(item.strip()) for item in status.split(",") if item.strip()
                }
            except ValueError as exc:
                raise InputValidationError("未知的主动候选状态") from exc
        return await runtime.store.list_proactive_candidates(session_id, statuses=statuses)

    @router.post("/sessions/{session_id}/proactive/candidates")
    async def create_proactive_candidate(session_id: str, payload: ProactiveCandidateCreateRequest):
        candidate, created = await runtime.engagement.submit_external_candidate(
            session_id,
            **payload.model_dump(),
        )
        runtime.proactive_scheduler.wake("candidate_submitted")
        return {"candidate": candidate, "created": created}

    @router.get("/sessions/{session_id}/proactive/presence")
    async def get_proactive_presence(session_id: str):
        return asdict(await runtime.proactive_scheduler.presence(session_id))

    @router.get("/sessions/{session_id}/proactive/runs")
    async def list_proactive_runs(session_id: str):
        if await runtime.store.get_session(session_id) is None:
            raise NotFoundError("会话不存在")
        return await runtime.store.list_proactive_runs(session_id)

    @router.post("/sessions/{session_id}/proactive/run-now")
    async def run_proactive_now(session_id: str):
        return await runtime.engagement.run_proactive(session_id, force=True)

    @router.get("/sessions/{session_id}/drift/runs")
    async def list_drift_runs(session_id: str):
        if await runtime.store.get_session(session_id) is None:
            raise NotFoundError("会话不存在")
        return await runtime.store.list_drift_runs(session_id)

    @router.post("/sessions/{session_id}/drift/run-now")
    async def run_drift_now(session_id: str):
        return await runtime.engagement.run_drift(session_id, force=True)

    @router.put("/simulations/sessions/{session_id}/members")
    async def update_session_members(session_id: str, payload: SessionMembersUpdateRequest):
        return await runtime.chat.update_session_members(
            session_id, payload.participants, payload.expected_revision
        )

    @router.post("/simulations/sessions/{session_id}/messages")
    async def send_message(session_id: str, payload: SendMessageRequest):
        session = await runtime.store.get_session(session_id)
        if session is None:
            raise NotFoundError("会话不存在")
        member = next(
            (
                participant
                for participant in session.participants
                if participant.external_user_id == payload.sender_id
            ),
            None,
        )
        if member is None:
            raise InputValidationError("发送者不是当前模拟会话成员")
        inbound = InboundMessage(
            platform=session.platform,
            account_id=session.account_id,
            external_message_id=payload.external_message_id or f"sim_{uuid4().hex}",
            external_chat_id=session.external_chat_id,
            sender_id=payload.sender_id,
            sender_name=member.display_name,
            chat_type=session.chat_type,
            components=payload.components,
        )
        return await runtime.chat.ingest(inbound)

    @router.post("/uploads")
    async def upload_attachment(file: Annotated[UploadFile, File()]):
        """上传受控图片、语音或文件，返回可直接提交的消息组件。"""

        content = await file.read(app_settings.chat.max_attachment_bytes + 1)
        return runtime.attachments.save_attachment(
            file.filename or "attachment",
            file.content_type or "application/octet-stream",
            content,
        )

    @router.get("/profiles")
    async def list_profiles(scope_key: str | None = None):
        return await runtime.store.list_facts(scope_key)

    @router.get("/profiles/{fact_id}")
    async def get_profile(fact_id: str):
        facts = await runtime.store.list_facts()
        match = next((fact for fact in facts if fact.id == fact_id), None)
        if match is None:
            raise NotFoundError("画像事实不存在")
        return match

    @router.get("/profile-extractions")
    async def list_extractions():
        return await runtime.store.list_extraction_runs()

    @router.post("/profile-extractions/{run_id}/retry")
    async def retry_extraction(run_id: str):
        return await runtime.chat.retry_extraction(run_id)

    @router.get("/memories")
    async def list_memories(
        session_id: str | None = None,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 500,
    ):
        statuses = None
        kinds = None
        try:
            if status:
                statuses = {MemoryStatus(item.strip()) for item in status.split(",") if item.strip()}
            if kind:
                kinds = {MemoryKind(item.strip()) for item in kind.split(",") if item.strip()}
        except ValueError as exc:
            raise InputValidationError("未知的记忆状态或类别") from exc
        return await runtime.store.list_memories(
            session_id=session_id,
            statuses=statuses,
            kinds=kinds,
            limit=min(max(limit, 1), 1000),
        )

    @router.get("/sessions/{session_id}/memories/recall")
    async def recall_memories(session_id: str, query: str, limit: int = 8):
        session = await runtime.store.get_session(session_id)
        if session is None:
            raise NotFoundError("会话不存在")
        return await runtime.memory.retrieve(
            session=session,
            query=query,
            limit=min(max(limit, 1), 30),
        )

    @router.post("/sessions/{session_id}/memories")
    async def create_memory(session_id: str, payload: MemoryCreateRequest):
        session = await runtime.store.get_session(session_id)
        if session is None:
            raise NotFoundError("会话不存在")
        if payload.subject_id is not None and payload.subject_id not in {
            item.external_user_id for item in session.participants
        } | {"agent"}:
            raise InputValidationError("记忆主体不属于当前会话")
        return await runtime.memory.remember(
            session=session,
            content=payload.content,
            kind=payload.kind,
            subject_id=payload.subject_id,
            importance=payload.importance,
            source_chain=MemorySourceChain.MANUAL,
        )

    @router.post("/sessions/{session_id}/memories/{memory_id}/forget")
    async def forget_memory(session_id: str, memory_id: str, payload: MemoryForgetRequest):
        session = await runtime.store.get_session(session_id)
        if session is None:
            raise NotFoundError("会话不存在")
        return await runtime.memory.forget(session=session, memory_id=memory_id, reason=payload.reason)

    @router.post("/sessions/{session_id}/memories/{memory_id}/correct")
    async def correct_memory(session_id: str, memory_id: str, payload: MemoryCorrectRequest):
        session = await runtime.store.get_session(session_id)
        if session is None:
            raise NotFoundError("会话不存在")
        return await runtime.memory.correct(
            session=session,
            memory_id=memory_id,
            corrected_content=payload.corrected_content,
            reason=payload.reason,
        )

    @router.get("/memory-consolidations")
    async def list_memory_consolidations(session_id: str | None = None, limit: int = 500):
        return await runtime.store.list_memory_consolidation_runs(session_id, limit=min(max(limit, 1), 1000))

    @router.delete("/sessions/{session_id}/memories")
    async def clear_session_memories(session_id: str):
        return await runtime.chat.clear_memory(session_id=session_id)

    @router.delete("/memories")
    async def clear_all_memories():
        return await runtime.chat.clear_memory()

    @router.post("/memory-consolidations/{run_id}/retry")
    async def retry_memory_consolidation(run_id: str):
        return await runtime.memory.retry_consolidation(run_id)

    @router.get("/persona")
    async def get_persona(character_id: str | None = None):
        return (
            runtime.personas.get(character_id) if character_id is not None else runtime.personas.get_active()
        )

    @router.get("/personas")
    async def list_personas():
        return {
            "active_character_id": runtime.personas.active_character_id,
            "assignments": runtime.personas.assignments(),
            "personas": runtime.personas.list(),
        }

    @router.put("/personas/assignments")
    async def update_persona_assignments(
        payload: PersonaAssignmentsUpdateRequest,
    ):
        """分别持久化私聊与群聊人格；任一无效时不写入部分状态。"""

        async with runtime.persona_lock:
            return runtime.personas.assign(
                private=payload.private,
                group=payload.group,
            )

    @router.post("/personas/{character_id}/activate")
    async def activate_persona(character_id: str):
        async with runtime.persona_lock:
            return runtime.personas.activate(character_id)

    @router.put("/persona")
    async def update_persona(
        payload: PersonaUpdateRequest,
        character_id: str | None = None,
    ):
        async with runtime.persona_lock:
            return runtime.personas.update(
                name=payload.name,
                persona_prompt=payload.persona_prompt,
                expected_revision=payload.expected_revision,
                character_id=character_id,
            )

    @router.get("/persona/portrait")
    async def get_persona_portrait(character_id: str | None = None):
        persona = (
            runtime.personas.get(character_id) if character_id is not None else runtime.personas.get_active()
        )
        if persona.portrait is None:
            raise NotFoundError("当前人格尚未设置形象")
        return FileResponse(
            persona.portrait.storage_path,
            media_type=persona.portrait.mime_type,
            filename=Path(persona.portrait.storage_path).name,
        )

    @router.post("/persona/portrait")
    async def update_persona_portrait(
        file: Annotated[UploadFile, File()],
        expected_revision: int,
        crop_to_nine_sixteen: bool = False,
        character_id: str | None = None,
    ):
        content = await file.read(app_settings.chat.max_attachment_bytes + 1)
        async with runtime.persona_lock:
            current = (
                runtime.personas.get(character_id)
                if character_id is not None
                else runtime.personas.get_active()
            )
            if current.revision != expected_revision:
                raise ConflictError(f"人格已被其他请求更新，当前 revision={current.revision}")
            base_path = app_settings.storage.data_dir / "personas" / current.character_id / "base_image.png"
            previous_base = base_path.read_bytes() if base_path.is_file() else None
            portrait = runtime.attachments.save_persona_portrait(
                current.character_id,
                file.content_type or "application/octet-stream",
                content,
                crop_to_nine_sixteen=crop_to_nine_sixteen,
            )
            updated = None
            try:
                updated = runtime.personas.update_portrait(
                    portrait,
                    expected_revision=expected_revision,
                    current_snapshot=current,
                )
                await runtime.expressions.clear_portrait_bound(current.character_id)
            except (IJAError, OSError, TypeError, ValueError):
                runtime.attachments.restore_persona_portrait(current.character_id, previous_base)
                if updated is not None:
                    runtime.personas.restore_snapshot(current, expected_revision=updated.revision)
                raise
            if current.portrait is not None and current.portrait.storage_path != portrait.storage_path:
                try:
                    runtime.attachments.remove_persona_portrait(current.portrait)
                except OSError:
                    logger.exception(
                        "旧人格形象清理失败",
                        extra={"character_id": current.character_id},
                    )
            return updated

    @router.delete("/persona/portrait")
    async def delete_persona_portrait(
        payload: RevisionRequest,
        character_id: str | None = None,
    ):
        async with runtime.persona_lock:
            current = (
                runtime.personas.get(character_id)
                if character_id is not None
                else runtime.personas.get_active()
            )
            if current.portrait is None:
                raise NotFoundError("当前人格尚未设置形象")
            updated = None
            try:
                updated = runtime.personas.update_portrait(
                    None,
                    expected_revision=payload.expected_revision,
                    current_snapshot=current,
                )
                await runtime.expressions.clear_portrait_bound(current.character_id)
            except (IJAError, OSError, TypeError, ValueError):
                if updated is not None:
                    runtime.personas.restore_snapshot(current, expected_revision=updated.revision)
                raise
            try:
                runtime.attachments.remove_persona_portrait(current.portrait)
            except OSError:
                logger.exception(
                    "已解除引用的人格形象清理失败",
                    extra={"character_id": current.character_id},
                )
            return updated

    def expression_payload(asset):
        """移除图库 API 中不应暴露的本地路径和内部规范化字段。"""

        return asset.model_dump(
            mode="json",
            exclude={"storage_path", "normalized_name", "generation_key"},
        )

    @router.get("/expressions")
    async def list_expressions(character_id: str | None = None):
        return [expression_payload(asset) for asset in await runtime.expressions.list_current(character_id)]

    @router.post("/expressions")
    async def upload_expression(
        file: Annotated[UploadFile, File()],
        name: Annotated[str, Form(min_length=1, max_length=40)],
        emotion: Annotated[str, Form(min_length=1, max_length=120)],
        character_id: str | None = None,
    ):
        """手动上传图片作为当前角色的可复用表情，不依赖图片生成模型。"""

        content = await file.read(app_settings.chat.max_attachment_bytes + 1)
        asset = await runtime.expressions.upload_expression(
            content=content,
            mime_type=file.content_type or "application/octet-stream",
            name=name,
            emotion=emotion,
            character_id=character_id,
        )
        return expression_payload(asset)

    @router.get("/expressions/{expression_id}/image")
    async def get_expression_image(
        expression_id: str,
        character_id: str | None = None,
    ):
        asset = await runtime.store.get_expression(expression_id)
        persona = (
            runtime.personas.get(character_id) if character_id is not None else runtime.personas.get_active()
        )
        if (
            asset is None
            or asset.character_id != persona.character_id
            or (
                asset.source_portrait_sha256 is not None
                and (persona.portrait is None or asset.source_portrait_sha256 != persona.portrait.sha256)
            )
        ):
            raise NotFoundError("表情不存在")
        path = runtime.attachments.validate_expression_source(asset)
        return FileResponse(path, media_type=asset.mime_type, filename=f"{asset.name}.png")

    @router.delete("/expressions/{expression_id}")
    async def delete_expression(
        expression_id: str,
        character_id: str | None = None,
    ):
        return expression_payload(
            await runtime.expressions.delete(
                expression_id,
                character_id=character_id,
            )
        )

    @router.patch("/expressions/{expression_id}")
    async def rename_expression(
        expression_id: str,
        payload: ExpressionRenameRequest,
        character_id: str | None = None,
    ):
        """重命名表情并同步源文件名；历史媒体副本不受影响。"""

        asset = await runtime.expressions.rename_expression(
            expression_id,
            payload.name,
            character_id=character_id,
        )
        return expression_payload(asset)

    @router.get("/messages/{message_id}/components/{component_index}/image")
    async def get_message_image(message_id: str, component_index: int):
        message = await runtime.store.get_message(message_id)
        if message is None:
            raise NotFoundError("消息不存在")
        if component_index < 0 or component_index >= len(message.components):
            raise NotFoundError("图片组件不存在")
        component = message.components[component_index]
        path = runtime.attachments.validate_history_image(component)
        return FileResponse(
            path,
            media_type=component.mime_type,
            filename=component.filename,
        )

    @router.get("/messages/{message_id}/components/{component_index}/attachment")
    async def get_message_attachment(message_id: str, component_index: int):
        """返回经摘要复核的历史语音或文件，不接受任意磁盘路径。"""

        message = await runtime.store.get_message(message_id)
        if message is None:
            raise NotFoundError("消息不存在")
        if component_index < 0 or component_index >= len(message.components):
            raise NotFoundError("附件组件不存在")
        component = message.components[component_index]
        path = runtime.attachments.validate_history_attachment(component)
        return FileResponse(
            path,
            media_type=component.mime_type,
            filename=component.filename,
        )

    @router.get("/config/status")
    async def config_status():
        return config_status_payload()

    @router.get("/platform-plugins/generations")
    async def platform_plugin_generations():
        return runtime.platform_plugins.generation_status()

    @router.get("/platform-plugins/capabilities")
    async def platform_plugin_capabilities():
        """返回经 manifest 校验的平台模态能力与实际启用状态。"""

        return {
            "platforms": runtime.channel_capabilities.snapshot(
                enabled_plugin_ids=set(runtime.platform_plugins.plugins)
            )
        }

    def current_onebot_groups() -> list[dict[str, Any]]:
        """读取当前运行配置中的 OneBot 群准入规则，并拒绝损坏的配置形态。"""

        onebot = runtime.settings.platform_plugins.options.get("onebot", {})
        raw_groups = onebot.get("groups", [])
        if not isinstance(raw_groups, list) or not all(
            isinstance(group, dict) for group in raw_groups
        ):
            raise InputValidationError("当前 OneBot groups 配置格式无效")
        try:
            normalized = OneBotGroupAccessUpdateRequest.model_validate(
                {"groups": raw_groups}
            )
        except ValidationError as exc:
            message = exc.errors()[0]["msg"] if exc.errors() else "群准入配置校验失败"
            raise InputValidationError(f"当前 OneBot groups 配置无效：{message}") from exc
        return [group.model_dump() for group in normalized.groups]

    @router.get("/platform-plugins/onebot/groups")
    async def get_onebot_groups():
        """返回 WebUI 可管理的 OneBot 群准入白名单。"""

        return {"groups": current_onebot_groups()}

    @router.put("/platform-plugins/onebot/groups")
    async def update_onebot_groups(payload: OneBotGroupAccessUpdateRequest):
        """保存 OneBot 群准入规则并以新 generation 原子热切换。"""

        async with runtime.platform_plugin_config_lock:
            previous_groups = current_onebot_groups()
            previous_options = {
                plugin_id: dict(options)
                for plugin_id, options in runtime.settings.platform_plugins.options.items()
            }
            groups = [group.model_dump() for group in payload.groups]
            next_options = {
                plugin_id: dict(options)
                for plugin_id, options in previous_options.items()
            }
            onebot_options = dict(next_options.get("onebot", {}))
            onebot_options["groups"] = groups
            next_options["onebot"] = onebot_options

            save_onebot_group_configs(app_settings.project_root, groups)
            runtime.settings.platform_plugins.options = next_options
            runtime.platform_plugins.replace_options(next_options)
            try:
                generation = await runtime.platform_plugins.hot_reload()
            except BaseException:
                runtime.settings.platform_plugins.options = previous_options
                runtime.platform_plugins.replace_options(previous_options)
                save_onebot_group_configs(app_settings.project_root, previous_groups)
                raise
            return {"groups": groups, "generation": generation}

    @router.post("/platform-plugins/reload")
    async def reload_platform_plugins():
        generation = await runtime.platform_plugins.hot_reload()
        return {
            "generation": generation,
            "status": runtime.platform_plugins.generation_status(),
        }

    @router.post("/platform-plugins/rollback")
    async def rollback_platform_plugins(generation: int | None = None):
        restored = await runtime.platform_plugins.rollback(generation)
        return {
            "generation": restored,
            "status": runtime.platform_plugins.generation_status(),
        }

    @router.put("/config/model")
    async def update_model_config(payload: ModelConfigUpdateRequest):
        """保存本机模型配置并立即切换后续聊天任务使用的 Provider。"""

        async with runtime.model_config_lock:
            try:
                model_settings = payload.merged_model_settings(
                    load_persisted_model_settings(
                        app_settings.project_root,
                        data_dir=app_settings.storage.data_dir,
                    )
                )
            except ValidationError as exc:
                message = exc.errors()[0]["msg"] if exc.errors() else "模型配置校验失败"
                raise InputValidationError(f"模型配置无效：{message}") from exc
            save_model_settings(
                app_settings.project_root,
                model_settings,
                data_dir=app_settings.storage.data_dir,
            )
            await runtime.reconfigure_model(model_settings)
            return config_status_payload()

    @router.put("/config/image-model")
    async def update_image_model_config(payload: ImageModelConfigUpdateRequest):
        """保存独立图片模型配置并立即切换后续表情生成。"""

        async with runtime.image_model_config_lock:
            try:
                image_model_settings = payload.merged_image_model_settings(
                    load_persisted_image_model_settings(
                        app_settings.project_root,
                        data_dir=app_settings.storage.data_dir,
                    )
                )
            except ValidationError as exc:
                message = exc.errors()[0]["msg"] if exc.errors() else "图片模型配置校验失败"
                raise InputValidationError(f"图片模型配置无效：{message}") from exc
            save_image_model_settings(
                app_settings.project_root,
                image_model_settings,
                data_dir=app_settings.storage.data_dir,
            )
            await runtime.reconfigure_image_model(image_model_settings)
            return config_status_payload()

    @router.put("/config/vision-model")
    async def update_vision_model_config(payload: VisionModelConfigUpdateRequest):
        """保存视觉理解配置；未配置独立模型时恢复由主 LLM 看图。"""

        async with runtime.vision_model_config_lock:
            try:
                vision_settings = payload.merged_vision_model_settings(
                    load_persisted_vision_model_settings(
                        app_settings.project_root,
                        data_dir=app_settings.storage.data_dir,
                    )
                )
            except ValidationError as exc:
                message = exc.errors()[0]["msg"] if exc.errors() else "视觉模型配置校验失败"
                raise InputValidationError(f"视觉模型配置无效：{message}") from exc
            save_vision_model_settings(
                app_settings.project_root,
                vision_settings,
                data_dir=app_settings.storage.data_dir,
            )
            await runtime.reconfigure_vision_model(vision_settings)
            return config_status_payload()

    @router.put("/config/detection-model")
    async def update_detection_model_config(payload: DetectionModelConfigUpdateRequest):
        """保存独立检测模型配置并立即切换后续输出过滤确认。"""

        async with runtime.detection_model_config_lock:
            try:
                detection_model_settings = payload.merged_detection_model_settings(
                    load_persisted_detection_model_settings(
                        app_settings.project_root,
                        data_dir=app_settings.storage.data_dir,
                    )
                )
            except ValidationError as exc:
                message = exc.errors()[0]["msg"] if exc.errors() else "检测模型配置校验失败"
                raise InputValidationError(f"检测模型配置无效：{message}") from exc
            save_detection_model_settings(
                app_settings.project_root,
                detection_model_settings,
                data_dir=app_settings.storage.data_dir,
            )
            await runtime.reconfigure_detection_model(detection_model_settings)
            return config_status_payload()

    @router.put("/config/embedding")
    async def update_embedding_config(payload: EmbeddingConfigUpdateRequest):
        """保存 embedding 授权配置并切换后续记忆与表达检索。"""

        async with runtime.embedding_config_lock:
            try:
                embedding_settings = payload.merged_embedding_settings(
                    load_persisted_embedding_settings(
                        app_settings.project_root,
                        data_dir=app_settings.storage.data_dir,
                    )
                )
            except ValidationError as exc:
                message = exc.errors()[0]["msg"] if exc.errors() else "Embedding 配置校验失败"
                raise InputValidationError(f"Embedding 配置无效：{message}") from exc
            save_embedding_settings(
                app_settings.project_root,
                embedding_settings,
                data_dir=app_settings.storage.data_dir,
            )
            await runtime.reconfigure_embedding(embedding_settings)
            return config_status_payload()

    @router.post("/model/probe")
    async def model_probe():
        with model_observation_scope(task="model.probe", profile="chat"):
            chat_result = await runtime.model.probe()
        if runtime.profile_model is runtime.model:
            profile_result = chat_result
        else:
            with model_observation_scope(
                task="model.probe",
                profile="profile",
            ):
                profile_result = await runtime.profile_model.probe()
        return {"chat": chat_result, "profile": profile_result}

    @router.get("/schedules")
    async def list_schedules(session_id: str | None = None):
        if session_id is not None and await runtime.store.get_session(session_id) is None:
            raise NotFoundError("会话不存在")
        return await runtime.store.list_schedules(session_id)

    @router.get("/schedules/{schedule_id}/runs")
    async def list_schedule_runs(schedule_id: str):
        if await runtime.store.get_schedule(schedule_id) is None:
            raise NotFoundError("周期任务不存在")
        return await runtime.store.list_schedule_runs(schedule_id)

    @router.put("/schedules/{schedule_id}")
    async def update_schedule(schedule_id: str, payload: ScheduleUpdateRequest):
        return await runtime.schedules.update(
            schedule_id,
            actor_id=None,
            expected_revision=payload.expected_revision,
            title=payload.title,
            instruction=payload.instruction,
            source_text=payload.source_text,
            timezone=payload.timezone,
            dtstart=payload.dtstart,
            rrule=payload.rrule,
        )

    @router.post("/schedules/{schedule_id}/pause")
    async def pause_schedule(schedule_id: str, payload: RevisionRequest):
        from domain.models import ScheduleStatus

        return await runtime.schedules.set_status(
            schedule_id,
            ScheduleStatus.PAUSED,
            actor_id=None,
            expected_revision=payload.expected_revision,
        )

    @router.post("/schedules/{schedule_id}/resume")
    async def resume_schedule(schedule_id: str, payload: RevisionRequest):
        from domain.models import ScheduleStatus

        return await runtime.schedules.set_status(
            schedule_id,
            ScheduleStatus.ACTIVE,
            actor_id=None,
            expected_revision=payload.expected_revision,
        )

    @router.post("/schedules/{schedule_id}/run-now")
    async def run_schedule_now(schedule_id: str):
        return await runtime.scheduler.run_now(schedule_id)

    @router.delete("/schedules/{schedule_id}")
    async def delete_schedule(schedule_id: str, payload: RevisionRequest):
        from domain.models import ScheduleStatus

        return await runtime.schedules.set_status(
            schedule_id,
            ScheduleStatus.DELETED,
            actor_id=None,
            expected_revision=payload.expected_revision,
        )

    @router.get("/tool-executions")
    async def list_tool_executions(
        session_id: str | None = None,
        turn_id: str | None = None,
        schedule_run_id: str | None = None,
    ):
        return await runtime.store.list_tool_executions(
            session_id=session_id,
            turn_id=turn_id,
            schedule_run_id=schedule_run_id,
        )

    @router.get("/model-attempts")
    async def list_model_attempts(
        session_id: str | None = None,
        task: str | None = None,
        limit: int = 100,
    ):
        """读取安全模型观测；返回结构中不存在 Prompt、输出正文或密钥。"""

        if (
            session_id is not None
            and await runtime.store.get_session(session_id) is None
        ):
            raise NotFoundError("会话不存在")
        return {
            "items": await runtime.store.list_model_attempts(
                session_id=session_id,
                task=task,
                limit=limit,
            )
        }

    @router.get("/model-attempts/summary")
    async def summarize_model_attempts(
        session_id: str | None = None,
        task: str | None = None,
    ):
        """返回可按会话或任务筛选的 token、耗时、错误与成本概览。"""

        if (
            session_id is not None
            and await runtime.store.get_session(session_id) is None
        ):
            raise NotFoundError("会话不存在")
        return await runtime.store.summarize_model_attempts(
            session_id=session_id,
            task=task,
        )

    @router.get("/logs")
    async def list_logs(
        level: str | None = None,
        logger_name: str | None = None,
        limit: int = 200,
    ):
        """返回最近日志快照，供网页端初次加载历史记录。"""

        return {
            "items": runtime.log_hub.snapshot(
                level=level, logger_name=logger_name, limit=max(1, min(limit, 1000))
            )
        }

    @router.get("/blacklist")
    async def list_blacklist():
        """列出本地黑名单，供 WebUI 管理与审计。"""

        return await runtime.blacklist.list()

    @router.post("/blacklist")
    async def create_blacklist(payload: BlacklistCreateRequest):
        """控制台手动拉黑某用户；source 固定为 manual。"""

        return await runtime.blacklist.block(
            platform=payload.platform,
            account_id=payload.account_id,
            external_user_id=payload.external_user_id,
            display_name=payload.display_name,
            reason=payload.reason,
            source=BlacklistSource.MANUAL,
            session_id=payload.session_id,
        )

    @router.delete("/blacklist/{entry_id}")
    async def delete_blacklist(entry_id: str):
        """将某用户从本地黑名单移除，恢复受理其后续消息。"""

        return await runtime.blacklist.unblock(entry_id)

    app.include_router(router)

    @app.websocket("/api/events")
    async def events_socket(websocket: WebSocket):
        failure = control_security.validate_request_boundary(
            websocket.headers,
            scheme=websocket.url.scheme,
        )
        if failure is None and not control_security.websocket_authenticated(
            websocket.headers,
            websocket.cookies,
        ):
            failure = control_security.authentication_failure()
        if (
            failure is None
            and control_security.authentication_enabled
            and websocket.headers.get("origin") is None
            and not control_security.bearer_authenticated(websocket.headers)
        ):
            failure = control_security.origin_required_failure()
        if failure is not None:
            await websocket.close(
                code=failure.websocket_code,
                reason=failure.code,
            )
            return

        await websocket.accept()
        queue = runtime.events.subscribe()
        try:
            replay = runtime.events.replay(websocket.query_params.get("cursor"))
            if replay.resync_required:
                await websocket.send_json(runtime.events.resync_event(replay.reason or "cursor_unavailable"))
                await websocket.close(code=4409, reason="resync_required")
                return

            last_replayed_sequence = 0
            for event in replay.events:
                await websocket.send_json(event)
                last_replayed_sequence = max(
                    last_replayed_sequence,
                    int(event["event_seq"]),
                )
            while True:
                event = await queue.get()
                # resync_required 是终止控制帧，不属于可按业务序号去重的普通事件。
                # 慢消费者帧会复用当前序号；它可能恰好等于 replay 尾事件。
                if event["type"] == "control.resync_required":
                    await websocket.send_json(event)
                    await websocket.close(code=4409, reason="resync_required")
                    return
                if int(event["event_seq"]) <= last_replayed_sequence:
                    continue
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        finally:
            runtime.events.unsubscribe(queue)

    frontend_dist = app_settings.project_root / "frontend" / "dist"
    if frontend_dist.exists():
        assets = frontend_dist / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=assets), name="frontend-assets")

        @app.get("/{path:path}")
        async def spa_fallback(path: str):
            candidate = (frontend_dist / path).resolve()
            if frontend_dist.resolve() in candidate.parents and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(frontend_dist / "index.html")

    return app
