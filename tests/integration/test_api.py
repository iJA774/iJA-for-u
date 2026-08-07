from io import BytesIO
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from adapters.model import FakeModelProvider
from api import create_app
from config import AppSettings, load_settings


def test_embedding_defaults_on_but_stays_local_until_model_is_configured(
    settings,
) -> None:
    app = create_app(settings=settings, model_override=FakeModelProvider())

    assert app.state.runtime.embedding is None
    assert app.state.runtime.social_learning.embedding is None

    with TestClient(app) as client:
        embedding = client.get("/api/config/status").json()["embedding"]

    assert embedding["enabled"] is True
    assert embedding["available"] is False
    assert embedding["name"] == ""
    assert embedding["api_key_configured"] is False


def test_platform_capability_api_is_strict_and_reports_enabled_state(
    settings,
) -> None:
    app = create_app(settings=settings, model_override=FakeModelProvider())

    with TestClient(app) as client:
        response = client.get("/api/platform-plugins/capabilities")

    assert response.status_code == 200
    platforms = {
        item["platform"]: item for item in response.json()["platforms"]
    }
    assert set(platforms) == {
        "web-simulator",
        "qq",
        "wechat-service-account",
        "onebot",
    }
    assert platforms["web-simulator"]["enabled"] is True
    assert platforms["qq"]["enabled"] is False
    assert platforms["qq"]["egress"]["image"] == "unsupported"
    assert platforms["onebot"]["egress"]["image"] == "supported"
    assert platforms["wechat-service-account"]["ingress"]["audio"] == (
        "placeholder"
    )
    assert platforms["onebot"]["limitations"]


def test_api_private_session_and_duplicate(settings) -> None:
    app = create_app(settings=settings, model_override=FakeModelProvider("你好"))
    with TestClient(app) as client:
        created = client.post(
            "/api/simulations/sessions",
            json={
                "chat_type": "private",
                "display_name": "私聊",
                "external_chat_id": "u1",
                "participants": [{"external_user_id": "u1", "display_name": "小明"}],
            },
        )
        assert created.status_code == 200
        session_id = created.json()["id"]
        payload = {
            "sender_id": "u1",
            "sender_name": "小明",
            "external_message_id": "same-id",
            "components": [{"type": "text", "text": "你好"}],
        }
        first = client.post(f"/api/simulations/sessions/{session_id}/messages", json=payload)
        second = client.post(f"/api/simulations/sessions/{session_id}/messages", json=payload)
        assert first.status_code == 200
        assert second.json()["duplicate"] is True

def test_message_search_api_is_scoped_and_validates_query(settings) -> None:
    """消息搜索 API 应支持用户名和正文，并阻止跨 Session 查询。"""

    app = create_app(
        settings=settings,
        model_override=FakeModelProvider("固定回复"),
    )

    with TestClient(app) as client:
        first_session = client.post(
            "/api/simulations/sessions",
            json={
                "chat_type": "private",
                "display_name": "第一个搜索会话",
                "external_chat_id": "message-search-first",
                "participants": [
                    {
                        "external_user_id": "first-user",
                        "display_name": "阿澄",
                    }
                ],
            },
        ).json()
        second_session = client.post(
            "/api/simulations/sessions",
            json={
                "chat_type": "private",
                "display_name": "第二个搜索会话",
                "external_chat_id": "message-search-second",
                "participants": [
                    {
                        "external_user_id": "second-user",
                        "display_name": "阿澄",
                    }
                ],
            },
        ).json()

        first_message = "今晚一起去吃赛博火锅"
        second_message = "另一个会话也提到了赛博火锅"

        first_sent = client.post(
            f"/api/simulations/sessions/{first_session['id']}/messages",
            json={
                "sender_id": "first-user",
                "sender_name": "阿澄",
                "external_message_id": "message-search-first-message",
                "components": [{"type": "text", "text": first_message}],
            },
        )
        second_sent = client.post(
            f"/api/simulations/sessions/{second_session['id']}/messages",
            json={
                "sender_id": "second-user",
                "sender_name": "阿澄",
                "external_message_id": "message-search-second-message",
                "components": [{"type": "text", "text": second_message}],
            },
        )
        assert first_sent.status_code == 200
        assert second_sent.status_code == 200

        content_response = client.get(
            f"/api/sessions/{first_session['id']}/messages/search",
            params={"query": "赛博火锅"},
        )
        assert content_response.status_code == 200
        content_matches = content_response.json()
        assert len(content_matches) == 1
        assert content_matches[0]["session_id"] == first_session["id"]
        assert content_matches[0]["components"][0]["text"] == first_message

        sender_response = client.get(
            f"/api/sessions/{first_session['id']}/messages/search",
            params={"query": "阿澄"},
        )
        assert sender_response.status_code == 200
        sender_matches = sender_response.json()
        assert len(sender_matches) == 1
        assert sender_matches[0]["sender_name"] == "阿澄"

        context_response = client.get(
            (
                f"/api/sessions/{first_session['id']}/messages/"
                f"{content_matches[0]['id']}/context"
            ),
            params={"before": 0, "after": 0},
        )
        assert context_response.status_code == 200
        assert [
            message["id"] for message in context_response.json()
        ] == [content_matches[0]["id"]]

        other_search_response = client.get(
            f"/api/sessions/{second_session['id']}/messages/search",
            params={"query": "赛博火锅"},
        )
        assert other_search_response.status_code == 200
        other_matches = other_search_response.json()
        assert len(other_matches) == 1

        cross_session_context = client.get(
            (
                f"/api/sessions/{first_session['id']}/messages/"
                f"{other_matches[0]['id']}/context"
            ),
            params={"before": 10, "after": 10},
        )
        assert cross_session_context.status_code == 404

        invalid_context_limit = client.get(
            (
                f"/api/sessions/{first_session['id']}/messages/"
                f"{content_matches[0]['id']}/context"
            ),
            params={"before": 101, "after": 10},
        )
        assert invalid_context_limit.status_code == 422

        empty_query = client.get(
            f"/api/sessions/{first_session['id']}/messages/search",
            params={"query": ""},
        )
        assert empty_query.status_code == 422

        invalid_limit = client.get(
            f"/api/sessions/{first_session['id']}/messages/search",
            params={"query": "阿澄", "limit": 0},
        )
        assert invalid_limit.status_code == 422

        missing_session = client.get(
            "/api/sessions/missing-session/messages/search",
            params={"query": "阿澄"},
        )
        assert missing_session.status_code == 404


def test_engagement_policy_api_defaults_and_updates(settings) -> None:
    app = create_app(settings=settings, model_override=FakeModelProvider())
    with TestClient(app) as client:
        created = client.post(
            "/api/simulations/sessions",
            json={
                "chat_type": "private",
                "display_name": "主动私聊",
                "external_chat_id": "engagement-api",
                "participants": [
                    {"external_user_id": "u1", "display_name": "小明"}
                ],
            },
        )
        session_id = created.json()["id"]
        default = client.get(
            f"/api/sessions/{session_id}/engagement-policy"
        )
        assert default.status_code == 200
        assert default.json()["proactive_enabled"] is True
        assert default.json()["drift_enabled"] is True

        updated = client.put(
            f"/api/sessions/{session_id}/engagement-policy",
            json={
                "proactive_enabled": True,
                "drift_enabled": True,
                "timezone": "Asia/Shanghai",
                "quiet_start": "21:30",
                "quiet_end": "08:30",
                "minimum_interval_minutes": 360,
            },
        )
        assert updated.status_code == 200
        assert updated.json()["drift_enabled"] is True
        assert client.get(
            f"/api/sessions/{session_id}/proactive/candidates"
        ).json() == []
        assert client.get(
            f"/api/sessions/{session_id}/drift/runs"
        ).json() == []


def test_group_participation_policy_api_uses_revision_cas(settings) -> None:
    app = create_app(settings=settings, model_override=FakeModelProvider())
    with TestClient(app) as client:
        group_id = client.post(
            "/api/simulations/sessions",
            json={
                "chat_type": "group",
                "display_name": "策略群",
                "external_chat_id": "group-policy-api",
                "participants": [
                    {
                        "external_user_id": "u1",
                        "display_name": "小明",
                        "role": "owner",
                    }
                ],
            },
        ).json()["id"]
        default = client.get(
            f"/api/sessions/{group_id}/group-participation-policy"
        )
        assert default.status_code == 200
        assert default.json()["mode"] == "normal"
        assert default.json()["trigger_count"] == 3
        assert default.json()["frequency_factor"] == 0.9
        assert default.json()["revision"] == 1

        payload = {
            "mode": "focused",
            "trigger_count": 2,
            "frequency_factor": 0.9,
            "cooldown_seconds": 45,
            "expected_revision": 1,
        }
        updated = client.put(
            f"/api/sessions/{group_id}/group-participation-policy",
            json=payload,
        )
        assert updated.status_code == 200
        assert updated.json()["revision"] == 2
        assert updated.json()["state_version"] == 1

        stale = client.put(
            f"/api/sessions/{group_id}/group-participation-policy",
            json=payload,
        )
        assert stale.status_code == 409
        invalid = client.put(
            f"/api/sessions/{group_id}/group-participation-policy",
            json={**payload, "expected_revision": 2, "frequency_factor": 1.1},
        )
        assert invalid.status_code == 422

        private_id = client.post(
            "/api/simulations/sessions",
            json={
                "chat_type": "private",
                "display_name": "私聊",
                "external_chat_id": "private-no-group-policy",
                "participants": [
                    {"external_user_id": "u2", "display_name": "小红"}
                ],
            },
        ).json()["id"]
        no_policy = client.get(
            f"/api/sessions/{private_id}/group-participation-policy"
        )
        assert no_policy.status_code == 400


def test_memory_api_preserves_correction_and_retraction_history(settings) -> None:
    app = create_app(settings=settings, model_override=FakeModelProvider())
    with TestClient(app) as client:
        session_id = client.post(
            "/api/simulations/sessions",
            json={
                "chat_type": "private",
                "display_name": "记忆私聊",
                "external_chat_id": "memory-api",
                "participants": [
                    {"external_user_id": "u1", "display_name": "小明"}
                ],
            },
        ).json()["id"]
        created = client.post(
            f"/api/sessions/{session_id}/memories",
            json={
                "kind": "preference",
                "content": "用户喜欢柠檬茶",
                "subject_id": "u1",
                "importance": 0.9,
            },
        )
        assert created.status_code == 200
        original_id = created.json()["id"]

        corrected = client.post(
            f"/api/sessions/{session_id}/memories/{original_id}/correct",
            json={
                "corrected_content": "用户更喜欢无糖乌龙茶",
                "reason": "用户明确纠正",
            },
        )
        assert corrected.status_code == 200
        assert corrected.json()["supersedes_id"] == original_id
        corrected_id = corrected.json()["id"]

        forgotten = client.post(
            f"/api/sessions/{session_id}/memories/{corrected_id}/forget",
            json={"reason": "用户要求忘记"},
        )
        assert forgotten.status_code == 200
        assert forgotten.json()["status"] == "retracted"
        records = client.get(
            f"/api/memories?session_id={session_id}"
        ).json()
        assert {item["status"] for item in records} == {
            "superseded",
            "retracted",
        }


def test_persona_revision_conflict(settings) -> None:
    app = create_app(settings=settings, model_override=FakeModelProvider())
    with TestClient(app) as client:
        persona = client.get("/api/persona").json()
        payload = {
            "name": "新小佳",
            "persona_prompt": "说话更直接，但仍然尊重对方。",
            "expected_revision": persona["revision"],
        }
        updated = client.put("/api/persona", json=payload)
        assert updated.status_code == 200
        assert updated.json()["name"] == "新小佳"
        assert (settings.project_root / "prompts" / "persona" / "default.md").read_text(
            encoding="utf-8"
        ).strip() == payload["persona_prompt"]
        conflict = client.put("/api/persona", json=payload)
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "conflict"


def test_persona_catalog_and_activation(settings) -> None:
    app = create_app(settings=settings, model_override=FakeModelProvider())
    with TestClient(app) as client:
        catalog = client.get("/api/personas")
        assert catalog.status_code == 200
        assert catalog.json()["active_character_id"] == "default"
        assert catalog.json()["assignments"] == {
            "private": "default",
            "group": "default",
        }
        assert {
            item["character_id"] for item in catalog.json()["personas"]
        } == {"default", "skill_foundry", "uzi"}

        activated = client.post("/api/personas/uzi/activate")
        assert activated.status_code == 200
        assert activated.json()["character_id"] == "uzi"
        assert client.get("/api/persona").json()["character_id"] == "uzi"

        missing = client.post("/api/personas/not-found/activate")
        assert missing.status_code == 404


def test_persona_assignments_are_independent_and_edit_target_is_explicit(settings) -> None:
    app = create_app(settings=settings, model_override=FakeModelProvider())
    with TestClient(app) as client:
        assigned = client.put(
            "/api/personas/assignments",
            json={"private": "uzi", "group": "skill_foundry"},
        )
        assert assigned.status_code == 200
        assert assigned.json() == {
            "private": "uzi",
            "group": "skill_foundry",
        }
        catalog = client.get("/api/personas").json()
        assert catalog["assignments"] == assigned.json()

        uzi = next(
            item for item in catalog["personas"] if item["character_id"] == "uzi"
        )
        updated = client.put(
            "/api/persona?character_id=uzi",
            json={
                "name": "私聊苏柚",
                "persona_prompt": uzi["persona_prompt"],
                "expected_revision": uzi["revision"],
            },
        )
        assert updated.status_code == 200
        assert updated.json()["character_id"] == "uzi"
        assert client.get("/api/persona").json()["character_id"] == "default"


def test_persona_portrait_upload_read_and_delete(settings) -> None:
    app = create_app(settings=settings, model_override=FakeModelProvider())
    source = BytesIO()
    Image.new("RGB", (576, 1024), "#7158e2").save(source, format="PNG")
    png = source.getvalue()
    with TestClient(app) as client:
        persona = client.get("/api/persona").json()
        uploaded = client.post(
            f"/api/persona/portrait?expected_revision={persona['revision']}",
            files={"file": ("portrait.png", png, "image/png")},
        )
        assert uploaded.status_code == 200
        body = uploaded.json()
        assert body["portrait"]["mime_type"] == "image/png"
        assert body["portrait"]["filename"] == "base_image.png"
        assert (body["portrait"]["width"], body["portrait"]["height"]) == (576, 1024)
        assert body["portrait"]["aspect_valid"] is True
        portrait_path = settings.storage.data_dir / "personas" / "default"
        assert portrait_path in Path(body["portrait"]["storage_path"]).parents
        with Image.open(BytesIO(client.get("/api/persona/portrait").content)) as restored:
            assert restored.size == (576, 1024)

        deleted = client.request(
            "DELETE",
            "/api/persona/portrait",
            json={"expected_revision": body["revision"]},
        )
        assert deleted.status_code == 200
        assert deleted.json()["portrait"] is None
        assert not (portrait_path / "base_image.png").exists()


def test_portrait_aspect_mismatch_requires_explicit_crop(settings) -> None:
    app = create_app(settings=settings, model_override=FakeModelProvider())
    source = BytesIO()
    Image.new("RGB", (160, 160), "#d79b45").save(source, format="PNG")
    with TestClient(app) as client:
        persona = client.get("/api/persona").json()
        rejected = client.post(
            f"/api/persona/portrait?expected_revision={persona['revision']}",
            files={"file": ("square.png", source.getvalue(), "image/png")},
        )
        assert rejected.status_code == 400
        assert rejected.json()["error"]["code"] == "portrait_aspect_ratio_mismatch"
        assert rejected.json()["details"] == {
            "width": 160,
            "height": 160,
            "target_ratio": "9:16",
        }
        cropped = client.post(
            (
                "/api/persona/portrait"
                f"?expected_revision={persona['revision']}&crop_to_nine_sixteen=true"
            ),
            files={"file": ("square.png", source.getvalue(), "image/png")},
        )
        assert cropped.status_code == 200
        portrait = cropped.json()["portrait"]
        assert (portrait["width"], portrait["height"]) == (90, 160)
        assert portrait["aspect_valid"] is True


def test_uploaded_expression_image_is_readable_without_persona_portrait(
    settings,
) -> None:
    """手工/采集表情不绑定角色立绘，列表可见时图片也必须可读取。"""

    app = create_app(settings=settings, model_override=FakeModelProvider())
    source = BytesIO()
    Image.new("RGB", (96, 96), "#f2a900").save(source, format="PNG")
    png = source.getvalue()
    with TestClient(app) as client:
        assert client.get("/api/persona").json()["portrait"] is None
        uploaded = client.post(
            "/api/expressions",
            data={"name": "开心", "emotion": "开心、庆祝"},
            files={"file": ("happy.png", png, "image/png")},
        )
        assert uploaded.status_code == 200
        body = uploaded.json()
        assert body["source_portrait_sha256"] is None

        restored = client.get(f"/api/expressions/{body['id']}/image")
        assert restored.status_code == 200
        assert restored.headers["content-type"] == "image/png"
        with Image.open(BytesIO(restored.content)) as image:
            assert image.size == (96, 96)


def test_api_rejects_invalid_components_forged_quotes_and_non_members(settings) -> None:
    app = create_app(settings=settings, model_override=FakeModelProvider())
    with TestClient(app) as client:
        session = client.post(
            "/api/simulations/sessions",
            json={
                "chat_type": "group",
                "display_name": "边界测试群",
                "external_chat_id": "boundary-group",
                "participants": [{"external_user_id": "u1", "display_name": "小明"}],
            },
        ).json()
        endpoint = f"/api/simulations/sessions/{session['id']}/messages"
        invalid = client.post(
            endpoint,
            json={"sender_id": "u1", "sender_name": "小明", "components": [{"type": "text"}]},
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "request_validation_error"

        non_member = client.post(
            endpoint,
            json={
                "sender_id": "intruder",
                "sender_name": "冒充者",
                "components": [{"type": "text", "text": "你好"}],
            },
        )
        assert non_member.status_code == 400

        forged_quote = client.post(
            endpoint,
            json={
                "sender_id": "u1",
                "sender_name": "小明",
                "components": [{"type": "quote", "message_id": "missing-message"}],
            },
        )
        assert forged_quote.status_code == 400
        assert "引用消息不存在" in forged_quote.json()["error"]["message"]


def test_control_frame_is_not_persisted(settings) -> None:
    app = create_app(settings=settings, model_override=FakeModelProvider())
    with TestClient(app) as client:
        session = client.post(
            "/api/simulations/sessions",
            json={
                "chat_type": "private",
                "display_name": "控制帧测试",
                "external_chat_id": "control-user",
                "participants": [{"external_user_id": "u1", "display_name": "小明"}],
            },
        ).json()
        result = client.post(
            f"/api/simulations/sessions/{session['id']}/messages",
            json={
                "sender_id": "u1",
                "sender_name": "小明",
                "components": [{"type": "text", "text": "/stop"}],
            },
        )
        assert result.json()["control"] is True
        assert client.get(f"/api/sessions/{session['id']}/messages").json() == []


def test_model_config_is_applied_without_returning_api_key(settings, tmp_path) -> None:
    isolated_settings = AppSettings.model_validate(
        settings.model_dump() | {"project_root": tmp_path, "storage": settings.storage.model_dump()}
    )
    app = create_app(settings=isolated_settings, model_override=FakeModelProvider())
    client = TestClient(app)

    response = client.put(
        "/api/config/model",
        json={
            "mode": "openai",
            "protocol": "openai_responses",
            "base_url": "https://model.example/v1",
            "name": "test-model",
            "profile_protocol": "anthropic_messages",
            "profile_base_url": "https://profile.example/v1",
            "profile_name": "profile-model",
            "api_key": "secret-for-test-only",
            "profile_api_key": "profile-secret-for-test-only",
            "clear_api_key": False,
            "clear_profile_api_key": False,
            "supports_vision": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["model"] == {
        "mode": "openai",
        "protocol": "openai_responses",
        "base_url": "https://model.example/v1",
        "name": "test-model",
        "profile_protocol": "anthropic_messages",
        "profile_base_url": "https://profile.example/v1",
        "profile_name": "profile-model",
        "api_key_configured": True,
        "api_key_saved_locally": True,
            "profile_api_key_configured": True,
            "profile_api_key_saved_locally": True,
            "supports_json_object": True,
            "supports_tools": True,
            "supports_vision": True,
            "supports_streaming": False,
            "task_profiles": {},
        }
    assert "secret-for-test-only" not in response.text
    assert "profile-secret-for-test-only" not in response.text
    local_config = (tmp_path / "config" / "model.local.toml").read_text(encoding="utf-8")
    assert "secret-for-test-only" not in local_config
    assert "profile-secret-for-test-only" not in local_config
    assert 'api_key_ref = "model.api_key"' in local_config
    assert 'profile_api_key_ref = "model.profile_api_key"' in local_config
    encrypted_store = (
        settings.storage.data_dir / "secrets" / "credentials.local.json"
    ).read_text(encoding="utf-8")
    assert "secret-for-test-only" not in encrypted_store
    assert "profile-secret-for-test-only" not in encrypted_store
    assert app.state.runtime.settings.model.name == "test-model"
    assert app.state.runtime.settings.model.for_profile().protocol == "anthropic_messages"
    assert app.state.runtime.settings.model.supports_vision is True


def test_embedding_config_is_independent_and_never_returns_api_key(settings) -> None:
    app = create_app(settings=settings, model_override=FakeModelProvider())
    with TestClient(app) as client:
        response = client.put(
            "/api/config/embedding",
            json={
                "enabled": True,
                "base_url": "https://embedding.example/v1",
                "name": "embedding-test-model",
                "api_key": "embedding-secret-for-test-only",
                "clear_api_key": False,
            },
        )
        assert response.status_code == 200
        assert response.json()["embedding"] == {
            "enabled": True,
            "available": True,
            "base_url": "https://embedding.example/v1",
            "name": "embedding-test-model",
            "timeout_seconds": 30.0,
            "api_key_configured": True,
            "api_key_saved_locally": True,
        }
        assert "embedding-secret-for-test-only" not in response.text
        assert app.state.runtime.settings.embedding.name == "embedding-test-model"
        assert (
            app.state.runtime.social_learning.embedding
            is app.state.runtime.embedding
        )


def test_image_model_config_is_independent_and_never_returns_api_key(settings) -> None:
    app = create_app(settings=settings, model_override=FakeModelProvider())
    with TestClient(app) as client:
        response = client.put(
            "/api/config/image-model",
            json={
                "enabled": True,
                "base_url": "https://images.example/v1",
                "name": "image-test-model",
                "api_key": "image-secret-for-test-only",
                "clear_api_key": False,
            },
        )
        assert response.status_code == 200
        assert response.json()["image_model"] == {
            "enabled": True,
                "base_url": "https://images.example/v1",
                "name": "image-test-model",
                "timeout_seconds": 120.0,
                "api_key_configured": True,
            "api_key_saved_locally": True,
        }
        assert "image-secret-for-test-only" not in response.text
        assert app.state.runtime.settings.model.mode == "fake"
        assert app.state.runtime.settings.image_model.enabled is True


def test_vision_model_config_is_optional_hot_switched_and_never_returns_api_key(
    settings,
) -> None:
    """独立视觉模型可热切换；恢复 main 后图片继续交给主聊天模型。"""

    app = create_app(settings=settings, model_override=FakeModelProvider())
    with TestClient(app) as client:
        response = client.put(
            "/api/config/vision-model",
            json={
                "mode": "external",
                "protocol": "openai_responses",
                "base_url": "https://vision.example/v1",
                "name": "vision-test-model",
                "timeout_seconds": 45,
                "wait_seconds": 3,
                "api_key": "vision-secret-for-test-only",
                "clear_api_key": False,
            },
        )
        assert response.status_code == 200
        assert response.json()["vision_model"] == {
            "mode": "external",
            "protocol": "openai_responses",
            "base_url": "https://vision.example/v1",
            "name": "vision-test-model",
            "timeout_seconds": 45.0,
            "wait_seconds": 3.0,
            "api_key_configured": True,
            "api_key_saved_locally": True,
        }
        assert "vision-secret-for-test-only" not in response.text
        runtime = app.state.runtime
        assert runtime.vision_model is not None
        assert runtime.vision.uses_external_model is True
        local_config = (
            settings.project_root / "config" / "vision-model.local.toml"
        ).read_text(encoding="utf-8")
        assert "vision-secret-for-test-only" not in local_config
        assert 'api_key_ref = "vision_model.api_key"' in local_config

        restored = client.put(
            "/api/config/vision-model",
            json={
                "mode": "main",
                "protocol": "openai_chat",
                "base_url": "https://api.openai.com/v1",
                "name": "",
                "timeout_seconds": 60,
                "wait_seconds": 5,
                "clear_api_key": True,
            },
        )
        assert restored.status_code == 200
        assert restored.json()["vision_model"]["mode"] == "main"
        assert restored.json()["vision_model"]["api_key_configured"] is False
        assert runtime.vision_model is None
        assert runtime.vision.uses_external_model is False


def test_detection_model_config_is_saved_and_hot_switched_without_returning_api_key(
    settings,
) -> None:
    """WebUI 保存审核模型后，filter resolver 应立即获得新的独立 Provider。"""

    app = create_app(settings=settings, model_override=FakeModelProvider())
    with TestClient(app) as client:
        response = client.put(
            "/api/config/detection-model",
            json={
                "enabled": True,
                "base_url": "https://audit.example/v1",
                "name": "audit-test-model",
                "timeout_seconds": 45,
                "api_key": "audit-secret-for-test-only",
                "clear_api_key": False,
            },
        )

        assert response.status_code == 200
        assert response.json()["detection_model"] == {
            "enabled": True,
            "base_url": "https://audit.example/v1",
            "name": "audit-test-model",
            "timeout_seconds": 45.0,
            "api_key_configured": True,
            "api_key_saved_locally": True,
        }
        assert "audit-secret-for-test-only" not in response.text
        runtime = app.state.runtime
        assert runtime.settings.detection_model.name == "audit-test-model"
        assert runtime.detection_model is not None
        assert runtime.platform_plugins._resolve_detection_model() is runtime.detection_model
        local_config = (
            settings.project_root / "config" / "detection-model.local.toml"
        ).read_text(encoding="utf-8")
        assert "audit-secret-for-test-only" not in local_config
        assert 'api_key_ref = "detection_model.api_key"' in local_config


def test_onebot_group_access_can_be_managed_from_control_api(settings) -> None:
    app = create_app(settings=settings, model_override=FakeModelProvider())
    with TestClient(app) as client:
        before = client.get('/api/platform-plugins/generations').json()['current_generation']
        response = client.put(
            '/api/platform-plugins/onebot/groups',
            json={
                'groups': [
                    {
                        'group_id': ' 987654321 ',
                        'require_at': False,
                        'allow_from': [' 111111 ', '111111', '222222'],
                    }
                ]
            },
        )
        assert response.status_code == 200
        assert response.json() == {
            'groups': [
                {
                    'group_id': '987654321',
                    'require_at': False,
                    'allow_from': ['111111', '222222'],
                }
            ],
            'generation': before + 1,
        }
        assert client.get('/api/platform-plugins/onebot/groups').json() == {
            'groups': response.json()['groups']
        }
        assert (
            app.state.runtime.settings.platform_plugins.options['onebot']['groups']
            == response.json()['groups']
        )

    local_config = settings.project_root / 'config' / 'platform-plugins.local.toml'
    text = local_config.read_text(encoding='utf-8')
    assert '987654321' in text
    assert '111111' in text
    assert '222222' in text

    reloaded = load_settings(settings.project_root)
    assert reloaded.platform_plugins.options['onebot']['groups'] == [
        {
            'group_id': '987654321',
            'require_at': False,
            'allow_from': ['111111', '222222'],
        }
    ]


def test_onebot_group_access_rejects_duplicate_groups(settings) -> None:
    app = create_app(settings=settings, model_override=FakeModelProvider())
    with TestClient(app) as client:
        response = client.put(
            '/api/platform-plugins/onebot/groups',
            json={
                'groups': [
                    {'group_id': '123', 'require_at': True, 'allow_from': []},
                    {'group_id': '123', 'require_at': False, 'allow_from': []},
                ]
            },
        )
    assert response.status_code == 422


def test_session_member_roles_use_revision_and_require_one_owner(settings) -> None:
    app = create_app(settings=settings, model_override=FakeModelProvider())
    with TestClient(app) as client:
        created = client.post(
            "/api/simulations/sessions",
            json={
                "chat_type": "group",
                "display_name": "角色群",
                "external_chat_id": "role-group",
                "participants": [
                    {"external_user_id": "u1", "display_name": "群主", "role": "owner"},
                    {"external_user_id": "u2", "display_name": "成员", "role": "member"},
                ],
            },
        ).json()
        invalid = client.put(
            f"/api/simulations/sessions/{created['id']}/members",
            json={
                "expected_revision": created["revision"],
                "participants": [
                    {"external_user_id": "u1", "display_name": "群主", "role": "owner"},
                    {"external_user_id": "u2", "display_name": "成员", "role": "owner"},
                ],
            },
        )
        assert invalid.status_code == 400
        updated = client.put(
            f"/api/simulations/sessions/{created['id']}/members",
            json={
                "expected_revision": created["revision"],
                "participants": [
                    {"external_user_id": "u1", "display_name": "群主", "role": "owner"},
                    {"external_user_id": "u2", "display_name": "管理员", "role": "admin"},
                ],
            },
        )
        assert updated.status_code == 200
        assert updated.json()["revision"] == created["revision"] + 1
        stale = client.put(
            f"/api/simulations/sessions/{created['id']}/members",
            json={
                "expected_revision": created["revision"],
                "participants": updated.json()["participants"],
            },
        )
        assert stale.status_code == 409


def test_api_logs_returns_startup_entries(settings) -> None:
    app = create_app(settings=settings, model_override=FakeModelProvider())
    with TestClient(app) as client:
        body = client.get("/api/logs").json()
        assert "items" in body
        messages = [item["message"] for item in body["items"]]
        assert any("iJA 服务启动" in msg for msg in messages)
        assert any("iJA 服务就绪" in msg for msg in messages)
        # 每条都应包含可序列化的基础字段
        sample = body["items"][0]
        assert {"id", "ts", "level", "logger", "message"}.issubset(sample)


def test_api_logs_level_filter_and_session_creation(settings) -> None:
    app = create_app(settings=settings, model_override=FakeModelProvider("你好"))
    with TestClient(app) as client:
        client.post(
            "/api/simulations/sessions",
            json={
                "chat_type": "private",
                "display_name": "私聊日志",
                "external_chat_id": "log-u1",
                "participants": [{"external_user_id": "u1", "display_name": "小明"}],
            },
        )
        body = client.get("/api/logs?level=INFO").json()
        assert all(item["level"] == "INFO" for item in body["items"])
        messages = [item["message"] for item in body["items"]]
        assert any("会话已创建" in msg for msg in messages)
        created_entry = next(item for item in body["items"] if "会话已创建" in item["message"])
        assert created_entry["extra"]["chat_type"] == "private"


def test_api_logs_respects_limit(settings) -> None:
    """limit 参数应限制返回条数，避免网页端一次性拉取过多。"""

    app = create_app(settings=settings, model_override=FakeModelProvider())
    with TestClient(app) as client:
        body = client.get("/api/logs?limit=1").json()
        assert len(body["items"]) <= 1


def test_delete_session_api_removes_session_and_clears_local_data(settings) -> None:
    """删除聊天窗应物理移除会话本身及其全部聊天记录与记忆。"""

    app = create_app(settings=settings, model_override=FakeModelProvider())
    with TestClient(app) as client:
        created = client.post(
            "/api/simulations/sessions",
            json={
                "chat_type": "private",
                "display_name": "待删除私聊",
                "external_chat_id": "delete-api",
                "participants": [
                    {"external_user_id": "u1", "display_name": "小明"}
                ],
            },
        )
        assert created.status_code == 200
        session_id = created.json()["id"]
        client.post(
            f"/api/simulations/sessions/{session_id}/messages",
            json={
                "sender_id": "u1",
                "sender_name": "小明",
                "components": [{"type": "text", "text": "记录会随会话删除"}],
            },
        )
        client.post(
            f"/api/sessions/{session_id}/memories",
            json={
                "kind": "event",
                "content": "会随会话删除的记忆",
                "importance": 0.8,
            },
        )
        assert client.get(f"/api/sessions/{session_id}/messages").json()
        assert client.get(f"/api/memories?session_id={session_id}").json()

        deleted = client.delete(f"/api/sessions/{session_id}")
        assert deleted.status_code == 200
        assert deleted.json()["session_id"] == session_id

        # 会话从列表消失，关联的消息与记忆一并不可访问。
        sessions = client.get("/api/sessions").json()
        assert all(item["id"] != session_id for item in sessions)
        assert client.get(f"/api/sessions/{session_id}/messages").status_code == 404
        assert client.get(f"/api/memories?session_id={session_id}").json() == []
        # 再次删除已不存在的会话应返回 404。
        assert client.delete(f"/api/sessions/{session_id}").status_code == 404
