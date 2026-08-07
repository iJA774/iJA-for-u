"""skill-creator 的可移植运行时入口。"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SKILL_NAME = "skill-creator"
CAPABILITY_NAME = "skill-builder"


class ManagedServiceClient(Protocol):
    """宿主注入的最小 JSON 请求客户端。"""

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...


class ReferenceDocument(BaseModel):
    """随 Skill 一起交付的可移植 Markdown 参考资料。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    content: str = Field(min_length=1, max_length=10_000)

    @field_validator("content")
    @classmethod
    def validate_content_bytes(cls, value: str) -> str:
        if len(value.encode("utf-8")) > 10_000:
            raise ValueError("reference content 不得超过 10000 UTF-8 bytes")
        return value


class ReusableScript(BaseModel):
    """遵循 main(data) JSON 契约的受限 Python 脚本。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=32_000)
    smoke_input: Any

    @field_validator("description")
    @classmethod
    def validate_description_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("script description 不得包含 CR 或 LF")
        if len(value.encode("utf-8")) > 900:
            raise ValueError("script description 不得超过 900 UTF-8 bytes")
        return value

    @model_validator(mode="after")
    def validate_script_budgets(self) -> ReusableScript:
        if len(self.content.encode("utf-8")) > 32_000:
            raise ValueError("script content 不得超过 32000 UTF-8 bytes")
        try:
            encoded = json.dumps(
                self.smoke_input,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("smoke_input 必须是有限的 JSON 值") from exc
        if len(encoded) > 16_000:
            raise ValueError("smoke_input 不得超过 16000 UTF-8 bytes")
        return self


class CreateDraftArguments(BaseModel):
    """说明型 Skill 草案需求；脚本型工作流使用 begin/add。"""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=500)
    objective: str = Field(min_length=1, max_length=4000)
    steps: list[str] = Field(default_factory=list, max_length=20)
    references: list[ReferenceDocument] = Field(default_factory=list, max_length=10)

    @field_validator("display_name", "description")
    @classmethod
    def validate_summary_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("display_name 和 description 不得包含 CR 或 LF")
        return value

    @model_validator(mode="after")
    def validate_byte_budgets(self) -> CreateDraftArguments:
        if len(self.display_name.encode("utf-8")) > 300:
            raise ValueError("display_name 不得超过 300 UTF-8 bytes")
        if len(self.description.encode("utf-8")) > 1_000:
            raise ValueError("description 不得超过 1000 UTF-8 bytes")
        if len(self.objective.encode("utf-8")) > 4_500:
            raise ValueError("objective 不得超过 4500 UTF-8 bytes")
        if sum(len(item.encode("utf-8")) for item in self.steps) > 3_500:
            raise ValueError("steps 总计不得超过 3500 UTF-8 bytes")
        if sum(len(item.content.encode("utf-8")) for item in self.references) > 50_000:
            raise ValueError("references 总计不得超过 50000 UTF-8 bytes")
        return self


class BeginDraftArguments(CreateDraftArguments):
    """创建尚未构建的脚本型 Skill 工作台。"""


class AddScriptArguments(BaseModel):
    """向 DRAFT 工作台添加一个脚本，并默认立即构建。"""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=32, max_length=32)
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    description: str = Field(min_length=1, max_length=300)
    content: str = Field(min_length=1, max_length=32_000)
    smoke_input: Any
    finalize: bool = True

    @model_validator(mode="after")
    def validate_script(self) -> AddScriptArguments:
        ReusableScript(
            name=self.name,
            description=self.description,
            content=self.content,
            smoke_input=self.smoke_input,
        )
        return self


class JobArguments(BaseModel):
    """按 ID 查询 Skill 构建任务。"""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(min_length=32, max_length=32)


class InstallArguments(JobArguments):
    """必须由用户原样提供的安装确认。"""

    confirmation: str = Field(min_length=1, max_length=300)


class RejectArguments(JobArguments):
    """拒绝一个绑定冻结 revision 的待审核草案。"""


class PreviewArguments(JobArguments):
    """逐个读取冻结草案中的 allowlisted 文本文件。"""

    path: str = Field(min_length=1, max_length=200)
    offset: int = Field(default=0, ge=0)
    chunk_size: int = Field(default=2_000, ge=1, le=3_000)


class RunScriptArguments(BaseModel):
    """运行已安装 Skill 的一个冻结脚本。"""

    model_config = ConfigDict(extra="forbid")

    skill_name: str = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)
    script_name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$", max_length=64)
    input: Any

    @model_validator(mode="after")
    def validate_input_budget(self) -> RunScriptArguments:
        try:
            encoded = json.dumps(
                self.input,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("input 必须是有限的 JSON 值") from exc
        if len(encoded) > 64_000:
            raise ValueError("input 不得超过 64000 UTF-8 bytes")
        return self


@dataclass(frozen=True, slots=True)
class PortableTool:
    """由宿主 Skill 装载器适配的工具描述。"""

    name: str
    description: str
    arguments_model: type[BaseModel]
    handler: Callable[[BaseModel, Any], Awaitable[dict[str, Any]]]
    terminal: bool = False


class SkillCreatorPlugin:
    """将模型动作收敛为托管服务的受审计请求。"""

    name = SKILL_NAME

    def __init__(self, client: ManagedServiceClient) -> None:
        self.client = client

    async def is_available(self) -> bool:
        # ping 是无状态健康检查；只有创建、查询、审批和拒绝等业务 RPC
        # 才必须绑定宿主注入的用户与 Session。
        state = await self.client.request("ping", {})
        return bool(state.get("available"))

    async def load(self, context: Any) -> dict[str, Any]:
        return await self.client.request("ping", self._identity(context))

    def tools(self) -> list[PortableTool]:
        return [
            PortableTool(
                name="create_skill_draft",
                description=(
                    "前置：先 load_skill('skill-creator')。只创建不含脚本的说明型 "
                    "Skill 草案；脚本型任务必须改用 begin_skill_draft，"
                    "不要把源码塞进本工具。只进入待审核状态，不安装。"
                ),
                arguments_model=CreateDraftArguments,
                handler=self._create,
            ),
            PortableTool(
                name="begin_skill_draft",
                description=(
                    "前置：先 load_skill('skill-creator')。脚本型 Skill 的第一步："
                    "只提交名称、描述、目标、步骤和 reference，返回 DRAFT job_id；"
                    "随后调用 add_skill_script。"
                ),
                arguments_model=BeginDraftArguments,
                handler=self._begin,
            ),
            PortableTool(
                name="add_skill_script",
                description=(
                    "前置：先 load_skill('skill-creator')。脚本型 Skill 的第二步："
                    "向 DRAFT job 添加一个 main(data) 脚本和合成 smoke_input；"
                    "finalize=true（默认）会立即扫描、实测并冻结为待审核草案。"
                ),
                arguments_model=AddScriptArguments,
                handler=self._add_script,
            ),
            PortableTool(
                name="finalize_skill_draft",
                description=(
                    "前置：先 load_skill('skill-creator')。仅当 begin 后不添加脚本，"
                    "或之前 add_skill_script(finalize=false) 时，构建现有 DRAFT。"
                ),
                arguments_model=JobArguments,
                handler=self._finalize,
            ),
            PortableTool(
                name="get_skill_build",
                description="查询 Skill 构建任务的状态、验证证据和待审批短语。",
                arguments_model=JobArguments,
                handler=self._get,
            ),
            PortableTool(
                name="preview_skill_draft_file",
                description=(
                    "按 owner 逐个读取冻结草案中的 SKILL.md、agents/openai.yaml "
                    "、references/*.md 或 scripts/*.py；按 next_offset 继续，"
                    "单次 JSON 严格小于 12KB。"
                ),
                arguments_model=PreviewArguments,
                handler=self._preview,
            ),
            PortableTool(
                name="run_skill_script",
                description=(
                    "在受限子进程中运行由工坊安装且 revision 未漂移的脚本；"
                    "只接受 JSON 输入并返回 JSON 输出，禁止文件、网络与子进程。"
                ),
                arguments_model=RunScriptArguments,
                handler=self._run_script,
            ),
            PortableTool(
                name="install_skill_draft",
                description="仅当当前用户消息与待审核任务的确认短语完全相等时，原子安装 Skill。",
                arguments_model=InstallArguments,
                handler=self._install,
            ),
            PortableTool(
                name="reject_skill_draft",
                description="在用户明确拒绝时结束待审核任务，绝不安装草案。",
                arguments_model=RejectArguments,
                handler=self._reject,
            ),
        ]

    def _require_loaded(self, context: Any) -> None:
        if self.name not in context.loaded_skills:
            raise ValueError("必须先调用 load_skill 读取 skill-creator")

    @staticmethod
    def _identity(context: Any) -> dict[str, str]:
        """读取宿主注入身份；模型参数不能提供或覆盖这两个字段。"""

        actor_id = getattr(context, "actor_id", None)
        session_id = getattr(context, "session_id", None)
        if not isinstance(actor_id, str) or not actor_id:
            raise ValueError("Skill 工坊缺少可审计的 actor_id")
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("Skill 工坊缺少可审计的 session_id")
        return {"actor_id": actor_id, "session_id": session_id}

    async def _create(self, arguments: BaseModel, context: Any) -> dict[str, Any]:
        self._require_loaded(context)
        args = CreateDraftArguments.model_validate(arguments)
        job = await self.client.request(
            "create",
            {**args.model_dump(), **self._identity(context)},
        )
        return {"value": {"job": job}}

    async def _begin(self, arguments: BaseModel, context: Any) -> dict[str, Any]:
        self._require_loaded(context)
        args = BeginDraftArguments.model_validate(arguments)
        job = await self.client.request(
            "begin",
            {**args.model_dump(), **self._identity(context)},
        )
        return {"value": {"job": job}}

    async def _add_script(self, arguments: BaseModel, context: Any) -> dict[str, Any]:
        self._require_loaded(context)
        args = AddScriptArguments.model_validate(arguments)
        job = await self.client.request(
            "add_script",
            {**args.model_dump(), **self._identity(context)},
        )
        return {"value": {"job": job}}

    async def _finalize(self, arguments: BaseModel, context: Any) -> dict[str, Any]:
        self._require_loaded(context)
        args = JobArguments.model_validate(arguments)
        job = await self.client.request(
            "finalize",
            {**args.model_dump(), **self._identity(context)},
        )
        return {"value": {"job": job}}

    async def _get(self, arguments: BaseModel, context: Any) -> dict[str, Any]:
        self._require_loaded(context)
        args = JobArguments.model_validate(arguments)
        job = await self.client.request(
            "get",
            {**args.model_dump(), **self._identity(context)},
        )
        return {"value": {"job": job}}

    async def _preview(self, arguments: BaseModel, context: Any) -> dict[str, Any]:
        self._require_loaded(context)
        args = PreviewArguments.model_validate(arguments)
        result = await self.client.request(
            "preview",
            {**args.model_dump(), **self._identity(context)},
        )
        return {"value": {"file": result}}

    async def _run_script(self, arguments: BaseModel, context: Any) -> dict[str, Any]:
        self._require_loaded(context)
        args = RunScriptArguments.model_validate(arguments)
        result = await self.client.request(
            "execute_script",
            {**args.model_dump(), **self._identity(context)},
        )
        return {"value": {"execution": result}}

    async def _install(self, arguments: BaseModel, context: Any) -> dict[str, Any]:
        self._require_loaded(context)
        args = InstallArguments.model_validate(arguments)
        identity = self._identity(context)
        job = await self.client.request(
            "get",
            {"job_id": args.job_id, **identity},
        )
        approval = job.get("approval")
        expected = approval.get("required_phrase") if isinstance(approval, dict) else None
        source_text = getattr(context, "source_text", None)
        if not isinstance(expected, str) or args.confirmation != expected:
            raise ValueError("安装确认与待审核任务返回的短语不一致")
        if not isinstance(source_text, str) or source_text.strip() != expected:
            raise ValueError("当前用户消息必须只包含完整安装确认短语")
        installed = await self.client.request(
            "approve",
            {
                "job_id": args.job_id,
                "confirmation": expected,
                **identity,
            },
        )
        return {"value": {"job": installed, "restart_may_be_required": True}}

    async def _reject(self, arguments: BaseModel, context: Any) -> dict[str, Any]:
        self._require_loaded(context)
        args = RejectArguments.model_validate(arguments)
        identity = self._identity(context)
        job = await self.client.request(
            "get",
            {"job_id": args.job_id, **identity},
        )
        rejection = job.get("rejection")
        expected = rejection.get("required_phrase") if isinstance(rejection, dict) else None
        source_text = getattr(context, "source_text", None)
        if not isinstance(expected, str) or not isinstance(source_text, str):
            raise ValueError("任务没有可用的拒绝确认短语")
        if source_text.strip() != expected:
            raise ValueError("当前用户消息必须只包含完整拒绝确认短语")
        rejected = await self.client.request(
            "reject",
            {
                "job_id": args.job_id,
                "confirmation": expected,
                **identity,
            },
        )
        return {"value": {"job": rejected}}


def create_plugin(capabilities: dict[str, Any]) -> SkillCreatorPlugin:
    """创建 Skill runtime；所有有权副作用均委托给声明能力。"""

    try:
        client = capabilities[CAPABILITY_NAME]
    except KeyError as exc:
        raise RuntimeError(f"宿主缺少能力: {CAPABILITY_NAME}") from exc
    return SkillCreatorPlugin(client)
