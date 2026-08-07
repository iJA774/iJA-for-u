import hashlib

import pytest

from adapters.persistence import PersonaStore
from domain.errors import InputValidationError
from domain.models import ChatType


def test_persona_store_updates_prompt_and_rejects_manual_drift(settings) -> None:
    store = PersonaStore(
        project_root=settings.project_root,
        data_root=settings.storage.data_dir,
        active_character_id="default",
    )
    current = store.initialize()
    updated = store.update(
        name="阿佳",
        persona_prompt="表达清楚，少说套话。",
        expected_revision=current.revision,
    )
    prompt_path = settings.project_root / "prompts" / "persona" / "default.md"
    assert updated.name == "阿佳"
    assert updated.revision == current.revision + 1
    assert updated.prompt_sha256 == hashlib.sha256(prompt_path.read_bytes()).hexdigest()

    prompt_path.write_text("未经受控接口修改", encoding="utf-8")
    with pytest.raises(InputValidationError, match="摘要不匹配"):
        store.get_active()


def test_persona_store_accepts_crlf_prompt_with_lf_digest(settings) -> None:
    store = PersonaStore(
        project_root=settings.project_root,
        data_root=settings.storage.data_dir,
        active_character_id="default",
    )
    prompt_path = settings.project_root / "prompts" / "persona" / "default.md"
    prompt_path.write_bytes(prompt_path.read_bytes().replace(b"\n", b"\r\n"))

    persona = store.initialize()

    assert persona.character_id == "default"
    assert persona.prompt_sha256 == hashlib.sha256(
        prompt_path.read_bytes().replace(b"\r\n", b"\n")
    ).hexdigest()


def test_persona_store_lists_and_persists_active_selection(settings) -> None:
    store = PersonaStore(
        project_root=settings.project_root,
        data_root=settings.storage.data_dir,
        active_character_id="default",
    )
    store.initialize()

    assert {persona.character_id for persona in store.list()} == {
        "default",
        "skill_foundry",
        "uzi",
    }
    foundry = store.get("skill_foundry")
    assert foundry.name == "Skill 工匠"
    assert "不附加虚构身份、性格" in foundry.persona_prompt
    activated = store.activate("uzi")
    assert activated.name == "苏柚"
    assert store.get_active().character_id == "uzi"

    restarted = PersonaStore(
        project_root=settings.project_root,
        data_root=settings.storage.data_dir,
        active_character_id="default",
    )
    assert restarted.initialize().character_id == "uzi"


def test_persona_store_persists_distinct_private_and_group_assignments(settings) -> None:
    store = PersonaStore(
        project_root=settings.project_root,
        data_root=settings.storage.data_dir,
        active_character_id="default",
    )
    store.initialize()

    assert store.assign(private="uzi", group="skill_foundry") == {
        "private": "uzi",
        "group": "skill_foundry",
    }
    assert store.get_for_chat_type(ChatType.PRIVATE).character_id == "uzi"
    assert (
        store.get_for_chat_type(ChatType.GROUP).character_id
        == "skill_foundry"
    )

    restarted = PersonaStore(
        project_root=settings.project_root,
        data_root=settings.storage.data_dir,
        active_character_id="default",
    )
    restarted.initialize()
    assert restarted.assignments() == {
        "private": "uzi",
        "group": "skill_foundry",
    }
