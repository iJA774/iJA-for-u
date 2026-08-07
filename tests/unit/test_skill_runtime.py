import asyncio
import hashlib
from io import BytesIO
from pathlib import Path
from typing import cast

import pytest
from PIL import Image

from adapters.model import FakeImageModelProvider
from adapters.persistence import DatabaseStore, PersonaStore
from adapters.web_simulator import AttachmentStore
from application.expressions import ExpressionService
from domain.errors import ConflictError, InputValidationError, NotFoundError
from domain.models import ComponentType
from skill_runtime import SkillCatalog, SkillPluginManager
from tools import ToolContext, ToolRegistry, build_skill_tools


def png_bytes(size: tuple[int, int] = (90, 160)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, "#87b7ea").save(output, format="PNG")
    return output.getvalue()


def test_skill_catalog_strictly_validates_frontmatter_and_directory(settings, tmp_path) -> None:
    catalog = SkillCatalog(settings.project_root / "skills")
    assert "send-expression" in catalog.names
    assert "通常选择 `select`" in catalog.get("send-expression").body
    source_root = Path(__file__).parents[2]
    generic_tools = (source_root / "src" / "tools" / "skills.py").read_text(encoding="utf-8")
    generic_plugins = (source_root / "src" / "skill_runtime" / "plugins.py").read_text(encoding="utf-8")
    assert "send_expression" not in generic_tools
    assert "send-expression" not in generic_plugins

    invalid_root = tmp_path / "invalid-skills"
    invalid = invalid_root / "Bad_Name"
    invalid.mkdir(parents=True)
    (invalid / "SKILL.md").write_text("---\nname: Bad_Name\ndescription: bad\n---\nbody", encoding="utf-8")
    with pytest.raises(InputValidationError, match="目录名无效"):
        SkillCatalog(invalid_root)

    extra_root = tmp_path / "extra-skills"
    extra = extra_root / "extra"
    extra.mkdir(parents=True)
    (extra / "SKILL.md").write_text(
        "---\nname: extra\ndescription: test\nversion: 1\n---\nbody", encoding="utf-8"
    )
    with pytest.raises(InputValidationError, match="只能包含"):
        SkillCatalog(extra_root)

    traversal_root = tmp_path / "traversal-skills"
    traversal = traversal_root / "portable"
    traversal.mkdir(parents=True)
    (traversal / "SKILL.md").write_text(
        "---\nname: portable\ndescription: portable test\n---\nbody",
        encoding="utf-8",
    )
    (traversal / "runtime.toml").write_text(
        'api_version = 1\nentrypoint = "../escape.py:create_plugin"\ncapabilities = []\n',
        encoding="utf-8",
    )
    with pytest.raises(InputValidationError, match="entrypoint"):
        SkillPluginManager(SkillCatalog(traversal_root))


@pytest.mark.asyncio
async def test_instruction_only_skill_is_available_without_runtime(tmp_path) -> None:
    skill = tmp_path / "skills" / "conversation-guide"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: conversation-guide\n"
        "description: 提供一份对话整理指南\n"
        "---\n"
        "# 对话整理\n\n先归纳事实，再回答。",
        encoding="utf-8",
    )
    catalog = SkillCatalog(tmp_path / "skills")
    plugins = SkillPluginManager(catalog)
    registry = ToolRegistry(build_skill_tools(catalog, plugins))
    context = ToolContext(
        session_id="session-test",
        actor_id="u1",
        available_skills={"conversation-guide"},
    )

    assert await plugins.available_skills() == {"conversation-guide"}
    loaded = await registry.execute("load_skill", {"skill": "conversation-guide"}, context)
    assert loaded.value["skill"] == "conversation-guide"
    assert "先归纳事实" in str(loaded.value["instructions"])


@pytest.mark.asyncio
async def test_runtime_skill_missing_capability_is_not_exposed_as_instruction_only(
    tmp_path,
) -> None:
    skill = tmp_path / "skills" / "needs-host"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: needs-host\ndescription: 依赖宿主 capability 的 Skill\n---\n只有运行时就绪后才能使用。",
        encoding="utf-8",
    )
    (skill / "runtime.py").write_text(
        "def create_plugin(capabilities):\n    raise AssertionError('缺 capability 时不得加载工厂')\n",
        encoding="utf-8",
    )
    (skill / "runtime.toml").write_text(
        'api_version = 1\nentrypoint = "runtime.py:create_plugin"\ncapabilities = ["missing-host"]\n',
        encoding="utf-8",
    )
    catalog = SkillCatalog(tmp_path / "skills")
    plugins = SkillPluginManager(catalog)

    assert await plugins.available_skills() == set()


@pytest.mark.asyncio
async def test_scoped_skill_and_tool_require_host_authorization_at_every_boundary(
    tmp_path,
) -> None:
    skills_root = tmp_path / "plugin-skills"
    skill = skills_root / "workspace-admin"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: workspace-admin\ndescription: 管理工作区 Skill\n---\n只能由宿主授权的工作区管理员使用。",
        encoding="utf-8",
    )
    (skill / "runtime.toml").write_text(
        'api_version = 1\nentrypoint = "runtime.py:create_plugin"\ncapabilities = []\n',
        encoding="utf-8",
    )
    (skill / "runtime.py").write_text(
        "from pydantic import BaseModel\n"
        "\n"
        "class Arguments(BaseModel):\n"
        "    pass\n"
        "\n"
        "async def handle(arguments, context):\n"
        "    return {'value': {'changed': True}}\n"
        "\n"
        "class Tool:\n"
        "    name = 'workspace_change'\n"
        "    description = '修改工作区'\n"
        "    arguments_model = Arguments\n"
        "    handler = staticmethod(handle)\n"
        "    terminal = False\n"
        "\n"
        "class Plugin:\n"
        "    name = 'workspace-admin'\n"
        "    def tools(self): return [Tool()]\n"
        "    async def is_available(self): return True\n"
        "    async def load(self, context): return {'ready': True}\n"
        "\n"
        "def create_plugin(capabilities): return Plugin()\n",
        encoding="utf-8",
    )
    required = frozenset({"workspace:skills:write"})
    catalog = SkillCatalog(
        skills_root,
        required_scopes={skills_root: required},
    )
    plugins = SkillPluginManager(catalog)
    registry = ToolRegistry(build_skill_tools(catalog, plugins))

    unauthorized_skills = await plugins.available_skills()
    assert unauthorized_skills == set()
    assert "workspace-admin" not in catalog.summary(unauthorized_skills)
    assert "workspace-admin" not in catalog.summary({"workspace-admin"})
    assert plugins.tool_names({"workspace-admin"}) == set()
    assert registry.definitions({"workspace_change"}) == []
    forged = ToolContext(
        session_id="session-test",
        actor_id="attacker",
        available_skills={"workspace-admin"},
    )
    with pytest.raises(InputValidationError, match="未授权使用工具"):
        await registry.execute("workspace_change", {}, forged)
    with pytest.raises(InputValidationError, match="未授权加载 Skill"):
        await registry.execute(
            "load_skill",
            {"skill": "workspace-admin"},
            forged,
        )

    authorized_scopes = frozenset({"workspace:skills:write"})
    assert await plugins.available_skills(authorized_scopes) == {"workspace-admin"}
    assert "workspace-admin" in catalog.summary(
        await plugins.available_skills(authorized_scopes),
        authorized_scopes,
    )
    assert plugins.tool_names(
        {"workspace-admin"},
        authorized_scopes,
    ) == {"workspace_change"}
    assert [
        item.name
        for item in registry.definitions(
            {"workspace_change"},
            authorization_scopes=authorized_scopes,
        )
    ] == ["workspace_change"]
    authorized = ToolContext(
        session_id="session-test",
        actor_id="admin",
        available_skills={"workspace-admin"},
        authorization_scopes=authorized_scopes,
    )
    await registry.execute(
        "load_skill",
        {"skill": "workspace-admin"},
        authorized,
    )
    result = await registry.execute("workspace_change", {}, authorized)
    assert result.value == {"changed": True}


@pytest.mark.asyncio
async def test_expression_tool_requires_load_and_supports_image_only_then_caption(settings) -> None:
    store = DatabaseStore(settings.storage.database_path)
    await store.initialize()
    personas = PersonaStore(
        project_root=settings.project_root,
        data_root=settings.storage.data_dir,
        active_character_id="default",
    )
    current = personas.initialize()
    attachments = AttachmentStore(
        uploads_root=settings.storage.data_dir / "uploads",
        personas_root=settings.storage.data_dir / "personas",
        max_bytes=settings.chat.max_attachment_bytes,
    )
    portrait = attachments.save_persona_portrait(current.character_id, "image/png", png_bytes())
    personas.update_portrait(portrait, expected_revision=current.revision)
    image_model = FakeImageModelProvider()
    expressions = ExpressionService(
        store=store,
        personas=personas,
        attachments=attachments,
        image_provider=image_model,
    )
    catalog = SkillCatalog(settings.project_root / "skills")
    plugins = SkillPluginManager(
        catalog,
        capabilities={"expression-library": expressions},
    )
    assert plugins.plugin_names == {"send-expression"}
    assert plugins.tool_names({"send-expression"}) == {"send_expression"}
    assert SkillPluginManager(catalog).plugin_names == set()
    registry = ToolRegistry(build_skill_tools(catalog, plugins))
    arguments = {
        "action": "generate",
        "name": "开心挥手",
        "emotion": "开心",
        "image_prompt": "笑着挥手",
    }
    unloaded = ToolContext(
        session_id="session-test",
        actor_id="u1",
        available_skills={"send-expression"},
        supported_egress_components=frozenset(
            {ComponentType.IMAGE_REF.value}
        ),
    )
    with pytest.raises(InputValidationError, match="必须先调用"):
        await registry.execute("send_expression", arguments, unloaded)

    await registry.execute("load_skill", {"skill": "send-expression"}, unloaded)
    generated = await registry.execute("send_expression", arguments, unloaded)
    assert generated.reply_draft is not None
    assert [item.type for item in generated.reply_draft.components] == [ComponentType.IMAGE_REF]
    assert image_model.calls == 1
    generated_image = generated.reply_draft.components[0]
    history_path = Path(generated_image.storage_path or "")
    await asyncio.to_thread(history_path.write_bytes, b"truncated")
    with pytest.raises(InputValidationError, match="已经尝试"):
        await registry.execute("send_expression", arguments, unloaded)

    reuse_context = ToolContext(
        session_id="session-test",
        actor_id="u1",
        available_skills={"send-expression"},
        supported_egress_components=frozenset(
            {ComponentType.IMAGE_REF.value}
        ),
    )
    loaded = await registry.execute("load_skill", {"skill": "send-expression"}, reuse_context)
    loaded_runtime = cast(dict[str, object], loaded.value["runtime"])
    assert loaded_runtime["expression_names"] == ["开心挥手"]
    reused = await registry.execute(
        "send_expression",
        {
            "action": "reuse",
            "name": "开心挥手",
            "emotion": "很开心",
            "caption": "也替你开心！",
        },
        reuse_context,
    )
    assert reused.reply_draft is not None
    assert [item.type for item in reused.reply_draft.components] == [
        ComponentType.TEXT,
        ComponentType.IMAGE_REF,
    ]
    repaired = await asyncio.to_thread(history_path.read_bytes)
    assert len(repaired) == generated_image.size
    assert hashlib.sha256(repaired).hexdigest() == generated_image.sha256
    assert image_model.calls == 1

    select_context = ToolContext(
        session_id="session-test",
        actor_id="u1",
        available_skills={"send-expression"},
        supported_egress_components=frozenset(
            {ComponentType.IMAGE_REF.value}
        ),
    )
    await registry.execute("load_skill", {"skill": "send-expression"}, select_context)
    selected = await registry.execute(
        "send_expression",
        {
            "action": "select",
            "emotion": "开心",
            "selection_query": "庆祝好消息，想用挥手表达开心",
        },
        select_context,
    )
    assert selected.reply_draft is not None
    assert selected.value["action"] == "select"
    assert selected.value["name"] == "开心挥手"
    assert selected.value["expression_id"] == selected.reply_draft.expression_id
    assert selected.value["selection"]["mode"] == "semantic_direct"
    assert image_model.calls == 1
    with pytest.raises(ConflictError, match="已变化"):
        await expressions.prepare_reply(
            action="reuse",
            name="开心挥手",
            normalized_name="开心挥手",
            filename="开心挥手.png",
            emotion="开心",
            image_prompt=None,
            model_prompt=None,
            caption=None,
            expected_character_id="default",
            expected_expression_id="expression_replaced",
        )

    inexact_context = ToolContext(
        session_id="session-test",
        actor_id="u1",
        available_skills={"send-expression"},
        supported_egress_components=frozenset(
            {ComponentType.IMAGE_REF.value}
        ),
    )
    await registry.execute("load_skill", {"skill": "send-expression"}, inexact_context)
    with pytest.raises(NotFoundError, match="逐字使用"):
        await registry.execute(
            "send_expression",
            {
                "action": "reuse",
                "name": " 开心挥手 ",
                "emotion": "开心",
            },
            inexact_context,
        )

    conflict_context = ToolContext(
        session_id="session-test",
        actor_id="u1",
        available_skills={"send-expression"},
        supported_egress_components=frozenset(
            {ComponentType.IMAGE_REF.value}
        ),
    )
    await registry.execute("load_skill", {"skill": "send-expression"}, conflict_context)
    with pytest.raises(ConflictError, match="已存在"):
        await registry.execute("send_expression", arguments, conflict_context)
    assert ExpressionService.MAX_EXPRESSIONS_PER_CHARACTER == 200

    original = (await expressions.list_current())[0]
    for index in range(1, 200):
        await store.create_expression(
            original.model_copy(
                update={
                    "id": f"expression_capacity_{index}",
                    "name": f"容量表情{index}",
                    "normalized_name": f"capacity-{index}",
                }
            )
        )
    full_context = ToolContext(
        session_id="session-test",
        actor_id="u1",
        available_skills={"send-expression"},
        supported_egress_components=frozenset(
            {ComponentType.IMAGE_REF.value}
        ),
    )
    full_loaded = await registry.execute("load_skill", {"skill": "send-expression"}, full_context)
    full_runtime = cast(dict[str, object], full_loaded.value["runtime"])
    assert len(cast(list[str], full_runtime["expression_names"])) == 200
    assert full_runtime["remaining_capacity"] == 0
    assert full_runtime["can_generate"] is False
    with pytest.raises(ConflictError, match="200 个上限"):
        await registry.execute(
            "send_expression",
            {
                "action": "generate",
                "name": "第 201 个表情",
                "emotion": "开心",
                "image_prompt": "笑着挥手",
            },
            full_context,
        )
    assert image_model.calls == 1
    await store.close()
