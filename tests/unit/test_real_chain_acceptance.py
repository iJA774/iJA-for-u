import json
from pathlib import Path

import pytest

from adapters.model import FakeModelProvider
from ports import ModelRequest, ModelResult, ModelToolCall
from scripts.real_chain_acceptance import (
    build_isolated_settings,
    evaluate_acceptance,
    render_markdown,
    run_acceptance,
    sanitize_for_report,
    validate_isolated_data_dir,
)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_build_isolated_settings_preserves_model_without_mutating_source(
    settings,
    tmp_path: Path,
) -> None:
    settings.platform_plugins.enabled = ["onebot"]
    settings.platform_plugins.options = {"onebot": {"access_token": "secret"}}
    settings.image_model.enabled = False
    isolated_dir = tmp_path / "acceptance"

    isolated = build_isolated_settings(settings, data_dir=isolated_dir)

    assert isolated.storage.data_dir == isolated_dir
    assert isolated.model == settings.model
    assert isolated.platform_plugins.enabled == []
    assert isolated.platform_plugins.disabled == []
    assert isolated.platform_plugins.options == {}
    assert isolated.image_model.enabled is False
    assert isolated.detection_model.enabled is False
    assert isolated.vision_model.mode == "main"
    assert isolated.social_learning.enabled is True
    assert isolated.social_learning.minimum_new_user_messages == 4
    assert isolated.social_learning.jargon_reuse_min_occurrences == 4
    assert settings.platform_plugins.enabled == ["onebot"]
    assert settings.platform_plugins.options["onebot"]["access_token"] == "secret"


def test_validate_isolated_data_dir_rejects_authoritative_tree(
    tmp_path: Path,
) -> None:
    authoritative = tmp_path / "data"
    authoritative.mkdir()

    with pytest.raises(ValueError, match="权威数据目录"):
        validate_isolated_data_dir(
            authoritative / "acceptance",
            authoritative_data_dir=authoritative,
        )

    accepted = validate_isolated_data_dir(
        tmp_path / "isolated",
        authoritative_data_dir=authoritative,
    )
    assert accepted == (tmp_path / "isolated").resolve()


def test_report_sanitization_redacts_nested_credentials_and_urls() -> None:
    raw = {
        "api_key": "sk-should-never-appear",
        "nested": {
            "base_url": "https://private.example/v1",
            "message": (
                "request https://private.example/v1 failed; "
                "Authorization: Bearer abc.def.ghi"
            ),
        },
        "control_token": "local-token-value",
    }

    safe = sanitize_for_report(raw)
    rendered = render_markdown(
        {
            "run_id": "test",
            "status": "error",
            "elapsed_ms": 1,
            "checks": {},
            "sessions": {},
            "tool_executions": [safe],
            "error": safe["nested"],
        }
    )

    assert safe["api_key"] == "[已脱敏]"
    assert safe["nested"]["base_url"] == "[已脱敏]"
    assert "private.example" not in rendered
    assert "abc.def.ghi" not in rendered
    assert "sk-should-never-appear" not in rendered
    assert "local-token-value" not in rendered


def test_evaluate_acceptance_requires_real_expression_tool_when_requested() -> None:
    learning = {
        "jargons": [{"term": "yyds"}],
        "expressions": [{"style": "短句"}],
        "behaviors": [{"action": "接梗"}],
    }
    report = {
        "sessions": {
            "private": {
                "learning": learning,
                "acceptance_turn": {
                    "assistant_text": "我嘞个，yyds 😂",
                    "messages": [],
                },
            },
            "group": {
                "learning": learning,
                "acceptance_turn": {
                    "assistant_text": "稳麻了",
                    "messages": [
                        {
                            "components": [
                                {"type": "image_ref", "is_expression": True}
                            ]
                        }
                    ],
                },
            },
        },
        "tool_executions": [
            {"tool_name": "send_expression", "status": "completed"}
        ],
        "model_attempts": {
            "attempt_count": 2,
            "success_count": 2,
        },
        "sticker": {
            "collected_assets": [
                {
                    "name": "真实链路验收贴纸",
                    "emotion": "开心",
                    "source": "collected",
                }
            ]
        },
    }

    checks = evaluate_acceptance(report, sticker_expected=True)

    assert all(checks.values())


class _AcceptanceModel(FakeModelProvider):
    """只在最终验收轮按真实 Skill 协议发送已收集表情。"""

    async def complete(self, request: ModelRequest) -> ModelResult:
        available = {tool.name for tool in request.tools or []}
        user_text = next(
            (
                message.content or ""
                for message in reversed(request.messages)
                if message.role == "user"
            ),
            "",
        )
        if "表情库里有验收贴纸" in user_text and "send_expression" in available:
            return ModelResult(
                tool_calls=[
                    ModelToolCall(
                        id="acceptance-send-expression",
                        name="send_expression",
                        arguments=json.dumps(
                            {
                                "action": "select",
                                "emotion": "开心、赞同、庆祝",
                                "selection_query": "真实链路验收贴纸，开心赞同庆祝",
                                "caption": "我嘞个，yyds，稳麻了 😂",
                            },
                            ensure_ascii=False,
                        ),
                    )
                ],
                finish_reason="tool_calls",
            )
        if "表情库里有验收贴纸" in user_text and "load_skill" in available:
            return ModelResult(
                tool_calls=[
                    ModelToolCall(
                        id="acceptance-load-expression-skill",
                        name="load_skill",
                        arguments=json.dumps(
                            {"skill": "send-expression"},
                            ensure_ascii=False,
                        ),
                    )
                ],
                finish_reason="tool_calls",
            )
        return await super().complete(request)


@pytest.mark.asyncio
async def test_run_acceptance_executes_full_fake_chain_with_real_expression_skill(
    settings,
    tmp_path: Path,
) -> None:
    isolated = build_isolated_settings(
        settings,
        data_dir=tmp_path / "acceptance-data",
    )
    provider = _AcceptanceModel()
    sticker = _PROJECT_ROOT / "scripts" / "assets" / "acceptance-sticker.png"

    report = await run_acceptance(
        isolated,
        sticker_path=sticker,
        run_id="fake-full-chain",
        model_override=provider,
        profile_model_override=provider,
    )

    assert report["status"] == "passed", json.dumps(
        report,
        ensure_ascii=False,
        indent=2,
    )
    assert all(report["checks"].values())
    assert report["model_attempts"]["attempt_count"] > 0
    assert report["model_attempts"]["success_count"] > 0
    assert report["sticker"]["collected_assets"]
    assert any(
        item["tool_name"] == "send_expression"
        and item["status"] == "completed"
        for item in report["tool_executions"]
    )
