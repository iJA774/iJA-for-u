"""本地控制面认证、来源边界、探针和事件回放契约。"""

from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from starlette.websockets import WebSocketDisconnect

from adapters.model import FakeModelProvider
from api import create_app

CONTROL_TOKEN = "test-control-token-32-characters"


def _secure_settings(settings):
    server = settings.server.model_copy(
        update={
            "control_token": SecretStr(CONTROL_TOKEN),
            "trusted_origins": ["http://testserver"],
            "event_history_capacity": 8,
        }
    )
    return settings.model_copy(update={"server": server})


def _single_slot_event_settings(settings):
    """构造可确定触发 subscribe/replay 窗口溢出的隔离配置。"""

    secured = _secure_settings(settings)
    return secured.model_copy(
        update={
            "server": secured.server.model_copy(
                update={"event_subscriber_queue_capacity": 1}
            ),
            # 此用例只验证事件协议，不启动无关的学习维护扫描。
            "social_learning": secured.social_learning.model_copy(
                update={"enabled": False}
            ),
        }
    )


def test_control_api_requires_token_and_exchanges_httponly_cookie(
    settings,
    caplog,
) -> None:
    app = create_app(
        settings=_secure_settings(settings),
        model_override=FakeModelProvider(),
    )
    with TestClient(app) as client:
        missing = client.get("/api/sessions")
        wrong = client.get(
            "/api/sessions",
            headers={"Authorization": "Bearer definitely-wrong-token"},
        )
        authenticated = client.get(
            "/api/sessions",
            headers={"Authorization": f"Bearer {CONTROL_TOKEN}"},
        )
        session = client.post(
            "/api/control/session",
            headers={"Authorization": f"Bearer {CONTROL_TOKEN}"},
        )
        cookie_authenticated = client.get("/api/sessions")

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert wrong.status_code == 401
    assert authenticated.status_code == 200
    assert session.status_code == 200
    assert "HttpOnly" in session.headers["set-cookie"]
    assert "SameSite=strict" in session.headers["set-cookie"]
    assert "Max-Age=34560000" in session.headers["set-cookie"]
    assert cookie_authenticated.status_code == 200
    assert CONTROL_TOKEN not in caplog.text
    assert CONTROL_TOKEN not in " ".join(
        (
            missing.text,
            wrong.text,
            authenticated.text,
            session.text,
            session.headers["set-cookie"],
        )
    )


def test_control_boundary_rejects_malicious_host_and_origin(settings) -> None:
    app = create_app(
        settings=_secure_settings(settings),
        model_override=FakeModelProvider(),
    )
    authorization = {"Authorization": f"Bearer {CONTROL_TOKEN}"}
    with TestClient(app) as client:
        bad_host = client.get(
            "/api/sessions",
            headers={**authorization, "Host": "localhost.evil"},
        )
        bad_origin = client.get(
            "/api/sessions",
            headers={**authorization, "Origin": "https://attacker.invalid"},
        )
        null_origin = client.get(
            "/api/health",
            headers={"Origin": "null"},
        )

    assert bad_host.status_code == 400
    assert bad_host.json()["error"]["code"] == "untrusted_host"
    assert bad_origin.status_code == 403
    assert bad_origin.json()["error"]["code"] == "untrusted_origin"
    assert null_origin.status_code == 403


def test_cookie_write_requires_origin_but_bearer_cli_does_not(settings) -> None:
    app = create_app(
        settings=_secure_settings(settings),
        model_override=FakeModelProvider(),
    )
    with TestClient(app) as client:
        session = client.post(
            "/api/control/session",
            headers={"Authorization": f"Bearer {CONTROL_TOKEN}"},
        )
        assert session.status_code == 200

        cookie_without_origin = client.post(
            "/api/simulations/sessions",
            json={
                "chat_type": "private",
                "display_name": "Cookie 来源测试",
                "external_chat_id": "cookie-origin",
                "participants": [
                    {"external_user_id": "u1", "display_name": "小明"}
                ],
            },
        )
        cookie_with_origin = client.post(
            "/api/simulations/sessions",
            headers={"Origin": "http://testserver"},
            json={
                "chat_type": "private",
                "display_name": "可信来源",
                "external_chat_id": "trusted-cookie-origin",
                "participants": [
                    {"external_user_id": "u1", "display_name": "小明"}
                ],
            },
        )
        bearer_without_origin = client.post(
            "/api/simulations/sessions",
            headers={"Authorization": f"Bearer {CONTROL_TOKEN}"},
            json={
                "chat_type": "private",
                "display_name": "CLI",
                "external_chat_id": "bearer-cli",
                "participants": [
                    {"external_user_id": "u1", "display_name": "小明"}
                ],
            },
        )

    assert cookie_without_origin.status_code == 403
    assert cookie_without_origin.json()["error"]["code"] == "origin_required"
    assert cookie_with_origin.status_code == 200
    assert bearer_without_origin.status_code == 200


def test_health_and_readiness_have_distinct_lifecycle_semantics(settings) -> None:
    app = create_app(settings=settings, model_override=FakeModelProvider())
    client_without_lifespan = TestClient(app)

    health_before_start = client_without_lifespan.get("/api/health")
    readiness_before_start = client_without_lifespan.get("/api/readiness")

    assert health_before_start.status_code == 200
    assert health_before_start.json()["status"] == "alive"
    assert readiness_before_start.status_code == 503
    assert readiness_before_start.json()["runtime_state"] == "created"
    assert readiness_before_start.json()["checks"]["runtime_state"] is False

    with TestClient(app) as started_client:
        readiness = started_client.get("/api/readiness")

    assert readiness.status_code == 200
    assert readiness.json()["status"] == "ready"
    assert all(readiness.json()["checks"].values())


def test_readiness_reports_dependency_degradation_without_failing_liveness(
    settings,
    monkeypatch,
) -> None:
    app = create_app(settings=settings, model_override=FakeModelProvider())

    async def database_unavailable() -> bool:
        return False

    with TestClient(app) as client:
        monkeypatch.setattr(
            app.state.runtime.store,
            "readiness_check",
            database_unavailable,
        )
        health = client.get("/api/health")
        readiness = client.get("/api/readiness")

    assert health.status_code == 200
    assert readiness.status_code == 503
    assert readiness.json()["checks"]["database"] is False
    assert readiness.json()["checks"]["runtime_state"] is True


def test_public_platform_callback_is_not_captured_by_control_authentication(
    settings,
) -> None:
    app = create_app(
        settings=_secure_settings(settings),
        model_override=FakeModelProvider(),
    )
    with TestClient(app) as client:
        response = client.get(
            "/platform-plugins/missing/callback",
            headers={
                "Host": "public-callback.example",
                "Origin": "https://platform.example",
            },
        )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_websocket_rejects_unauthenticated_and_untrusted_clients(settings) -> None:
    app = create_app(
        settings=_secure_settings(settings),
        model_override=FakeModelProvider(),
    )
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as unauthenticated:
            with client.websocket_connect("/api/events"):
                pass
        with pytest.raises(WebSocketDisconnect) as bad_origin:
            with client.websocket_connect(
                "/api/events",
                headers={
                    "Authorization": f"Bearer {CONTROL_TOKEN}",
                    "Origin": "https://attacker.invalid",
                },
            ):
                pass

    assert unauthenticated.value.code == 4401
    assert bad_origin.value.code == 4403
    assert CONTROL_TOKEN not in str(unauthenticated.value)
    assert CONTROL_TOKEN not in str(bad_origin.value)


def test_websocket_disconnect_replays_events_after_cursor(settings) -> None:
    app = create_app(
        settings=_secure_settings(settings),
        model_override=FakeModelProvider(),
    )
    with TestClient(app) as client:
        session = client.post(
            "/api/control/session",
            headers={"Authorization": f"Bearer {CONTROL_TOKEN}"},
        )
        assert session.status_code == 200
        websocket_headers = {"Origin": "http://testserver"}
        with client.websocket_connect(
            "/api/events",
            headers=websocket_headers,
        ) as socket:
            app.state.runtime.events.publish_nowait("test.first", {"value": 1})
            first = socket.receive_json()

        app.state.runtime.events.publish_nowait("test.second", {"value": 2})
        app.state.runtime.events.publish_nowait("test.third", {"value": 3})

        cursor = quote(first["cursor"], safe="")
        with client.websocket_connect(
            f"/api/events?cursor={cursor}",
            headers=websocket_headers,
        ) as socket:
            second = socket.receive_json()
            third = socket.receive_json()

    assert [first["type"], second["type"], third["type"]] == [
        "test.first",
        "test.second",
        "test.third",
    ]
    assert first["event_seq"] < second["event_seq"] < third["event_seq"]
    assert len({first["stream_id"], second["stream_id"], third["stream_id"]}) == 1


def test_websocket_replay_does_not_drop_equal_sequence_resync(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """subscribe/replay 窗口溢出时，同序号终止帧也必须送达并关闭连接。"""

    app = create_app(
        settings=_single_slot_event_settings(settings),
        model_override=FakeModelProvider(),
    )
    events = app.state.runtime.events
    original_replay = events.replay

    def replay_after_overflow(requested_cursor):
        events.publish_nowait("test.replay-one", {"value": 1})
        events.publish_nowait("test.replay-two", {"value": 2})
        return original_replay(requested_cursor)

    monkeypatch.setattr(events, "replay", replay_after_overflow)
    with TestClient(app) as client:
        session = client.post(
            "/api/control/session",
            headers={"Authorization": f"Bearer {CONTROL_TOKEN}"},
        )
        assert session.status_code == 200
        initial_sequence = events.current_sequence
        cursor = quote(events.current_cursor, safe="")
        with client.websocket_connect(
            f"/api/events?cursor={cursor}",
            headers={"Origin": "http://testserver"},
        ) as socket:
            first = socket.receive_json()
            second = socket.receive_json()
            terminal = socket.receive_json()
            with pytest.raises(WebSocketDisconnect) as closed:
                socket.receive_json()

    assert [first["event_seq"], second["event_seq"]] == [
        initial_sequence + 1,
        initial_sequence + 2,
    ]
    assert terminal["type"] == "control.resync_required"
    assert terminal["event_seq"] == second["event_seq"]
    assert terminal["payload"]["reason"] == "slow_consumer"
    assert closed.value.code == 4409
