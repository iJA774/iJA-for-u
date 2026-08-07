"""Skill 工坊控制面：持有任务状态，并作为正式安装的唯一 owner。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, cast

SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SCRIPT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
STATES = {
    "DRAFT",
    "GENERATING",
    "VALIDATING",
    "TESTING",
    "REVIEW_PENDING",
    "INSTALLING",
    "INSTALLED",
    "REJECTED",
    "FAILED",
}
TERMINAL_STATES = {"INSTALLED", "REJECTED", "FAILED"}
# 生成输入本身受 UTF-8 总预算约束；24KB 为 SKILL.md 的换行、结构和
# reference 链接保留余量，同时继续拒绝异常膨胀的工件。
MAX_SKILL_FILE_BYTES = 24_000
MAX_AUXILIARY_FILE_BYTES = 10_500
MAX_PUBLIC_PREVIEW_BYTES = 6_000
DEFAULT_PREVIEW_CHARS = 2_000
MAX_PREVIEW_CHARS = 3_000
MAX_PREVIEW_RESPONSE_JSON_BYTES = 12_000
MAX_PUBLIC_JOB_BYTES = 30_000
MAX_SCRIPT_FILE_BYTES = 32_000
MAX_SCRIPT_INPUT_BYTES = 16_000
MAX_SCRIPT_EXECUTION_INPUT_BYTES = 64_000
MAX_SCRIPT_EXECUTION_OUTPUT_BYTES = 64_000
SANDBOX_TIMEOUT_SECONDS = 4


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _with_windows_lock_retry(operation: Callable[[], Any]) -> Any:
    """Windows 扫描器短暂占用新文件时进行有界重试。"""

    for attempt in range(5):
        try:
            return operation()
        except PermissionError:
            if os.name != "nt" or attempt == 4:
                raise
            time.sleep(0.05 * (2**attempt))
    raise AssertionError("控制面文件操作重试循环未返回")


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    """在同一目录原子替换 JSON，避免中断留下半份权威状态。"""

    _with_windows_lock_retry(lambda: path.parent.mkdir(parents=True, exist_ok=True))
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    _with_windows_lock_retry(
        lambda: temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    )
    _with_windows_lock_retry(lambda: os.replace(temporary, path))


class FoundryError(ValueError):
    """可安全返回给调用方的工坊契约错误。"""


@dataclass(frozen=True, slots=True)
class StepOutcome:
    """一次 Builder 步骤的可验证结果；未提供证据时不能推进为完成。"""

    step: str
    ok: bool
    detail: str
    evidence: dict[str, Any]


class SkillFoundry:
    """管理 Skill 草案的确定性生成、校验、审批和安装。"""

    def __init__(
        self,
        project_root: Path,
        *,
        worker_script: Path | None = None,
        sandbox_script: Path | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        data_root = self.project_root / "data"
        workbench_candidate = data_root / "skill-workbench"
        skills_candidate = self.project_root / "skills"
        if data_root.is_symlink() or workbench_candidate.is_symlink():
            raise FoundryError("Skill 工坊数据目录不得经过符号链接")
        if skills_candidate.is_symlink():
            raise FoundryError("Skill 安装目录不得是符号链接")
        self.workbench_root = workbench_candidate.resolve()
        self.skills_root = skills_candidate.resolve()
        self.worker_script = (worker_script or Path(__file__).with_name("worker.py")).resolve()
        self.sandbox_script = (
            sandbox_script or Path(__file__).with_name("sandbox_runner.py")
        ).resolve()
        if self.workbench_root.parent.parent != self.project_root:
            raise FoundryError("Skill 工坊数据目录越界")
        if self.skills_root.parent != self.project_root:
            raise FoundryError("Skill 安装目录越界")
        if not self.worker_script.is_file():
            raise FoundryError("Skill Builder Worker 不存在")
        if not self.sandbox_script.is_file():
            raise FoundryError("Skill Script Sandbox 不存在")

    def ping(self) -> dict[str, Any]:
        """返回真实能力边界，避免把进程隔离误称为 OS 强沙箱。"""

        return {
            "available": True,
            "artifact_kinds": ["instruction-only", "instruction-and-scripts"],
            "executes_generated_code": True,
            "script_contract": "受限 Python main(data)：JSON 输入与 JSON 输出",
            "network_policy": "静态拒绝 + restricted builtins + Python audit deny",
            "filesystem_policy": "生成脚本运行时禁止文件系统访问",
            "isolation": (
                "独立短生命周期进程 + 最小环境 + wall timeout；"
                "支持时附加 OS CPU/内存上限，但不是容器级强沙箱"
            ),
            "install_requires_explicit_approval": True,
        }

    def create(self, params: dict[str, Any]) -> dict[str, Any]:
        """创建并运行一次有界的 Skill 构建、扫描和 smoke test 任务。"""

        job = self._initialize_job(params)
        return self._build_job(job)

    def begin(self, params: dict[str, Any]) -> dict[str, Any]:
        """创建轻量 DRAFT 工作台，供小输出预算模型分轮添加脚本。"""

        if "scripts" in params:
            raise FoundryError("begin 不接受 scripts；请用 add_script 逐个添加")
        job = self._initialize_job({**params, "scripts": []})
        return self._public_job(job)

    def add_script(self, params: dict[str, Any]) -> dict[str, Any]:
        """向 DRAFT 添加一个通过落盘前预检的脚本，并可立即完成构建。"""

        owner = self._required_owner(params)
        job_id = self._required_job_id(params)
        job = self._load(job_id)
        self._assert_owner(job, owner)
        if job["state"] != "DRAFT":
            raise FoundryError("只有 DRAFT 工作台可以添加脚本")
        finalize = params.get("finalize", True)
        if not isinstance(finalize, bool):
            raise FoundryError("finalize 必须是 boolean")
        script_keys = {"name", "description", "content", "smoke_input"}
        if not script_keys.issubset(params):
            raise FoundryError("add_script 缺少脚本字段")
        script = self._normalize_scripts(
            [{key: params[key] for key in script_keys}]
        )[0]
        if any(item["name"] == script["name"] for item in job["request"]["scripts"]):
            raise FoundryError("同一工作台的 script name 不得重复")
        combined = [*job["request"]["scripts"], script]
        if len(combined) > 8:
            raise FoundryError("单个 Skill 最多包含 8 个脚本")
        self._normalize_scripts(combined)
        self._preflight_scripts([script])
        job["request"]["scripts"] = combined
        job["artifact_kind"] = "instruction-and-scripts"
        self._transition(
            job,
            "DRAFT",
            f"已添加受限脚本 scripts/{script['name']}.py",
        )
        self._save(job)
        return self._build_job(job) if finalize else self._public_job(job)

    def finalize(self, params: dict[str, Any]) -> dict[str, Any]:
        """构建已准备好的 DRAFT；用于无脚本或多脚本分轮工作台。"""

        owner = self._required_owner(params)
        job = self._load(self._required_job_id(params))
        self._assert_owner(job, owner)
        return self._build_job(job)

    def _initialize_job(self, params: dict[str, Any]) -> dict[str, Any]:
        """集中校验输入并持久化不含产物的 DRAFT。"""

        owner = self._required_owner(params)
        name = self._required_text(params, "name", maximum=64)
        description = self._required_single_line(params, "description", maximum=500)
        objective = self._required_text(params, "objective", maximum=4000)
        display_name = self._required_single_line(params, "display_name", maximum=80)
        if len(description.encode("utf-8")) > 1_000:
            raise FoundryError("description 的 UTF-8 内容不得超过 1000 bytes")
        if len(objective.encode("utf-8")) > 4_500:
            raise FoundryError("objective 的 UTF-8 内容不得超过 4500 bytes")
        if len(display_name.encode("utf-8")) > 300:
            raise FoundryError("display_name 的 UTF-8 内容不得超过 300 bytes")
        if not SKILL_NAME_PATTERN.fullmatch(name):
            raise FoundryError("Skill name 必须是小写 kebab-case")
        raw_steps = params.get("steps", [])
        if (
            not isinstance(raw_steps, list)
            or len(raw_steps) > 20
            or any(not isinstance(item, str) or not item.strip() or len(item) > 500 for item in raw_steps)
        ):
            raise FoundryError("steps 必须是至多 20 项的非空字符串列表")
        if sum(len(item.encode("utf-8")) for item in raw_steps) > 3_500:
            raise FoundryError("steps 的 UTF-8 总内容不得超过 3500 bytes")
        raw_references = params.get("references", [])
        if not isinstance(raw_references, list) or len(raw_references) > 10:
            raise FoundryError("references 必须是至多 10 项的列表")
        references: list[dict[str, str]] = []
        total_reference_size = 0
        for item in raw_references:
            if not isinstance(item, dict) or set(item) != {"name", "content"}:
                raise FoundryError("reference 必须只包含 name 和 content")
            reference_name = item.get("name")
            content = item.get("content")
            if (
                not isinstance(reference_name, str)
                or not SKILL_NAME_PATTERN.fullmatch(reference_name)
                or not isinstance(content, str)
                or not content.strip()
                or len(content) > 10_000
                or len(content.encode("utf-8")) > 10_000
            ):
                raise FoundryError("reference 名称或内容无效")
            total_reference_size += len(content.encode("utf-8"))
            references.append({"name": reference_name, "content": content.strip()})
        if total_reference_size > 50_000:
            raise FoundryError("references 的 UTF-8 总内容不得超过 50000 bytes")
        scripts = self._normalize_scripts(params.get("scripts", []))
        self._preflight_scripts(scripts)
        artifact_kind = (
            "instruction-and-scripts" if scripts else "instruction-only"
        )
        job_id = uuid.uuid4().hex
        job_root = self._job_root(job_id)
        job_root.mkdir(parents=True, exist_ok=False)
        job: dict[str, Any] = {
            "schema_version": 2,
            "job_id": job_id,
            "skill_name": name,
            "artifact_kind": artifact_kind,
            "owner": owner,
            "request": {
                "display_name": display_name,
                "description": description,
                "objective": objective,
                "steps": [item.strip() for item in raw_steps],
                "references": references,
                "scripts": scripts,
            },
            "state": "DRAFT",
            "history": [],
            "checkpoints": [],
            "created_at": _now(),
            "updated_at": _now(),
            "validation": None,
            "script_tests": [],
            "artifacts": [],
            "failure": None,
            "isolation": self.ping(),
        }
        self._transition(job, "DRAFT", "已创建有界 Skill 构建任务")
        self._save(job)
        return job

    def _preflight_scripts(self, scripts: list[dict[str, Any]]) -> None:
        """在任何源码持久化前调用独立沙箱做静态预检。"""

        for script in scripts:
            preflight = self._run_sandbox(
                action="analyze",
                script_name=f"scripts/{script['name']}.py",
                source=script["content"],
            )
            security = preflight.get("security")
            if (
                preflight.get("ok") is not True
                or not isinstance(security, dict)
                or security.get("ok") is not True
            ):
                detail = str(
                    security.get("findings")
                    if isinstance(security, dict)
                    else preflight.get("error") or "未提供失败原因"
                )
                raise FoundryError(
                    f"脚本静态安全预检失败 scripts/{script['name']}.py: {detail[:1800]}"
                )

    def _build_job(self, job: dict[str, Any]) -> dict[str, Any]:
        """从 DRAFT 运行生成、验证、测试并冻结 revision。"""

        if job["state"] != "DRAFT":
            raise FoundryError("只有 DRAFT 工作台可以开始构建")
        if job["checkpoints"] or job["artifacts"] or job["validation"] is not None:
            raise FoundryError("DRAFT 已包含构建产物或 checkpoint")
        try:
            self._transition(job, "GENERATING", "独立 Builder Worker 正在写入草案目录")
            self._save(job)
            generated = self._run_builder(job)
            self._checkpoint(
                job,
                StepOutcome(
                    step="generate",
                    ok=generated
                    == {
                        "generated": True,
                        "artifact_kind": job["artifact_kind"],
                    },
                    detail="Builder Worker 已返回与请求一致的受限产物声明",
                    evidence=generated,
                ),
            )
            self._transition(job, "VALIDATING", "开始静态校验草案")
            self._save(job)
            validation = self._validate_draft(job)
            job["validation"] = validation
            self._checkpoint(
                job,
                StepOutcome(
                    step="validate",
                    ok=validation.get("ok") is True,
                    detail="草案结构、路径和 frontmatter 已通过静态校验",
                    evidence=validation,
                ),
            )
            self._transition(job, "TESTING", "运行确定性包测试与脚本沙箱 smoke test")
            self._save(job)
            script_tests = self._test_draft(job)
            job["script_tests"] = script_tests
            job["artifacts"] = self._artifact_manifest(job)
            self._checkpoint(
                job,
                StepOutcome(
                    step="test",
                    ok=bool(job["artifacts"]),
                    detail="包测试与脚本沙箱测试已通过，产物 revision 已冻结",
                    evidence={
                        "artifacts": job["artifacts"],
                        "script_tests": script_tests,
                    },
                ),
            )
            self._transition(job, "REVIEW_PENDING", "草案已通过验证，等待用户显式批准")
            self._save(job)
        except (FoundryError, OSError, subprocess.SubprocessError) as exc:
            job["failure"] = {
                "type": type(exc).__name__,
                "message": str(exc).strip() or "未提供错误说明",
            }
            self._transition(job, "FAILED", "构建失败；没有安装任何 Skill")
            self._save(job)
        return self._public_job(job)

    def get(self, params: dict[str, Any]) -> dict[str, Any]:
        """读取一项权威任务状态。"""

        owner = self._required_owner(params)
        job_id = self._required_job_id(params)
        job = self._load(job_id)
        self._assert_owner(job, owner)
        if job["state"] == "INSTALLING":
            self._reconcile_install(job)
        return self._public_job(job)

    def list_jobs(self, params: dict[str, Any]) -> dict[str, Any]:
        """按更新时间倒序返回任务摘要。"""

        owner = self._required_owner(params)
        raw_limit = params.get("limit", 20)
        if not isinstance(raw_limit, int) or isinstance(raw_limit, bool) or not 1 <= raw_limit <= 100:
            raise FoundryError("limit 必须是 1 到 100 的整数")
        if not self.workbench_root.exists():
            return {"jobs": []}
        jobs: list[dict[str, Any]] = []
        for state_path in self.workbench_root.glob("*/job.json"):
            try:
                if state_path.is_symlink():
                    raise FoundryError(f"任务状态不得是符号链接: {state_path.parent.name}")
                job = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise FoundryError(f"任务状态损坏: {state_path.parent.name}") from exc
            self._validate_job_schema(job, expected_job_id=state_path.parent.name)
            if job["owner"] != owner:
                continue
            if job["state"] == "INSTALLING":
                self._reconcile_install(job)
            public = self._public_job(job)
            jobs.append(
                {
                    "job_id": public["job_id"],
                    "skill_name": public["skill_name"],
                    "state": public["state"],
                    "updated_at": public["updated_at"],
                }
            )
        jobs.sort(key=lambda item: item["updated_at"], reverse=True)
        return {"jobs": jobs[:raw_limit]}

    def approve(self, params: dict[str, Any]) -> dict[str, Any]:
        """校验完整安装确认短语后原子安装；已有同名 Skill 一律保留并拒绝覆盖。"""

        job_id = self._required_job_id(params)
        owner = self._required_owner(params)
        job = self._load(job_id)
        self._assert_owner(job, owner)
        if job["state"] == "INSTALLING":
            self._reconcile_install(job)
        expected_confirmation = self._expected_install_phrase(job)
        confirmation = params.get("confirmation")
        if confirmation != expected_confirmation:
            raise FoundryError("安装确认未完整绑定 Skill、任务和冻结 revision")
        if job["state"] == "INSTALLED":
            return self._public_job(job)
        if job["state"] != "REVIEW_PENDING":
            raise FoundryError("只有 REVIEW_PENDING 草案可以安装")
        self._validate_draft(job)
        if self._artifact_manifest(job) != job["artifacts"]:
            raise FoundryError("草案在待审核期间已发生变化，安装确认随该 revision 失效")
        target = (self.skills_root / job["skill_name"]).resolve()
        if target.parent != self.skills_root:
            raise FoundryError("Skill 安装路径越界")
        if target.exists() or target.is_symlink():
            raise FoundryError("同名 Skill 已存在；第一版拒绝覆盖，以保留旧版本")
        self.skills_root.mkdir(parents=True, exist_ok=True)
        # 暂存目录必须避开宿主冷启动会枚举的 skills/。它仍位于项目同一卷，
        # 因而最终目录提交可以使用原子 rename/replace。
        staging = self._job_root(job_id) / "installing"
        if staging.exists() or staging.is_symlink():
            raise FoundryError("检测到同任务的未清理安装暂存目录")
        job["install_transaction"] = {
            "target_path": target.relative_to(self.project_root).as_posix(),
            "staging_path": staging.relative_to(self.project_root).as_posix(),
            "revision": self._revision_digest(job["artifacts"]),
        }
        self._transition(job, "INSTALLING", "安装意图已持久化，等待文件系统提交")
        self._save(job)
        self._reconcile_install(job)
        return self._public_job(job)

    def reject(self, params: dict[str, Any]) -> dict[str, Any]:
        """显式拒绝待审核草案，保留审计记录但绝不安装。"""

        job_id = self._required_job_id(params)
        owner = self._required_owner(params)
        job = self._load(job_id)
        self._assert_owner(job, owner)
        if job["state"] == "INSTALLING":
            self._reconcile_install(job)
        if job["state"] != "REVIEW_PENDING":
            raise FoundryError("只有 REVIEW_PENDING 草案可以拒绝")
        if params.get("confirmation") != self._expected_reject_phrase(job):
            raise FoundryError("拒绝确认未完整绑定 Skill、任务和冻结 revision")
        job["rejection_reason"] = "用户明确拒绝当前冻结 revision"
        self._transition(job, "REJECTED", "用户拒绝草案")
        self._save(job)
        return self._public_job(job)

    def preview(self, params: dict[str, Any]) -> dict[str, Any]:
        """按 owner 分块返回冻结草案文本，单次 JSON 响应严格有界。"""

        owner = self._required_owner(params)
        job_id = self._required_job_id(params)
        relative = self._required_text(params, "path", maximum=200)
        offset = params.get("offset", 0)
        chunk_size = params.get("chunk_size", DEFAULT_PREVIEW_CHARS)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise FoundryError("预览 offset 必须是非负整数")
        if (
            isinstance(chunk_size, bool)
            or not isinstance(chunk_size, int)
            or not 1 <= chunk_size <= MAX_PREVIEW_CHARS
        ):
            raise FoundryError(
                f"预览 chunk_size 必须是 1 到 {MAX_PREVIEW_CHARS} 的整数"
            )
        if not self._allowed_preview_path(relative):
            raise FoundryError(
                "只能预览 SKILL.md、agents/openai.yaml、references/*.md 或 scripts/*.py"
            )
        job = self._load(job_id)
        self._assert_owner(job, owner)
        if job["state"] == "INSTALLING":
            self._reconcile_install(job)
        expected_manifest = job["artifacts"]
        if not expected_manifest or self._artifact_manifest(job) != expected_manifest:
            raise FoundryError("草案与冻结 revision 不一致，禁止预览")
        artifact = next(
            (item for item in expected_manifest if item["path"] == relative),
            None,
        )
        if artifact is None:
            raise FoundryError("请求文件不属于当前冻结 revision")
        path = (self._draft_root(job) / relative).resolve()
        draft_root = self._draft_root(job)
        if path.parent != draft_root and draft_root not in path.parents:
            raise FoundryError("预览路径越界")
        if path.is_symlink() or not path.is_file():
            raise FoundryError("预览文件不存在或是符号链接")
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FoundryError("预览文件不是 UTF-8 文本") from exc
        if offset > len(text):
            raise FoundryError("预览 offset 超出文件字符范围")

        end = min(len(text), offset + chunk_size)

        def build_result(chunk_end: int) -> dict[str, Any]:
            complete = chunk_end == len(text)
            return {
                "job_id": job_id,
                "skill_name": job["skill_name"],
                "state": job["state"],
                "revision": self._revision_digest(expected_manifest),
                "path": relative,
                "size_bytes": len(raw),
                "total_characters": len(text),
                "sha256": artifact["sha256"],
                "offset": offset,
                "content": text[offset:chunk_end],
                "next_offset": None if complete else chunk_end,
                "complete": complete,
            }

        result = build_result(end)
        while (
            len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
            >= MAX_PREVIEW_RESPONSE_JSON_BYTES
            and end > offset
        ):
            end = offset + max(1, (end - offset) // 2)
            result = build_result(end)
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) >= (
            MAX_PREVIEW_RESPONSE_JSON_BYTES
        ):
            raise FoundryError("预览响应无法满足 12KB 硬限制")
        return result

    def execute_script(self, params: dict[str, Any]) -> dict[str, Any]:
        """只运行由工坊安装且 revision 未漂移的受限脚本。"""

        caller = self._required_owner(params)
        skill_name = self._required_text(params, "skill_name", maximum=64)
        script_name = self._required_text(params, "script_name", maximum=64)
        if not SKILL_NAME_PATTERN.fullmatch(skill_name):
            raise FoundryError("skill_name 必须是小写 kebab-case")
        if not SCRIPT_NAME_PATTERN.fullmatch(script_name):
            raise FoundryError("script_name 必须是小写字母开头的 snake_case")
        input_value = params.get("input")
        try:
            input_json = json.dumps(
                input_value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise FoundryError("脚本 input 必须是有限的 JSON 值") from exc
        if len(input_json.encode("utf-8")) > MAX_SCRIPT_EXECUTION_INPUT_BYTES:
            raise FoundryError(
                f"脚本 input 不得超过 {MAX_SCRIPT_EXECUTION_INPUT_BYTES} UTF-8 bytes"
            )

        job = self._find_installed_job(skill_name)
        installed_root = (self.skills_root / skill_name).resolve()
        if (
            installed_root.parent != self.skills_root
            or installed_root.is_symlink()
            or not installed_root.is_dir()
        ):
            raise FoundryError("已安装 Skill 目录不存在或越界")
        if self._manifest_for_root(installed_root) != job["artifacts"]:
            raise FoundryError("已安装 Skill 与工坊冻结 revision 不一致，拒绝执行")
        relative = f"scripts/{script_name}.py"
        artifact = next(
            (item for item in job["artifacts"] if item["path"] == relative),
            None,
        )
        if artifact is None:
            raise FoundryError("请求脚本不属于已安装 Skill 的冻结 revision")
        path = (installed_root / relative).resolve()
        if (
            installed_root not in path.parents
            or path.is_symlink()
            or not path.is_file()
        ):
            raise FoundryError("请求脚本路径不存在或越界")
        source = path.read_text(encoding="utf-8")
        source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
        expected_test = next(
            (
                item
                for item in job["script_tests"]
                if item["path"] == relative
            ),
            None,
        )
        if (
            expected_test is None
            or expected_test["source_sha256"] != artifact["sha256"]
            or source_sha256 != artifact["sha256"]
        ):
            raise FoundryError("脚本缺少与冻结源码一致的安全测试证据")

        outcome = self._run_sandbox(
            action="execute",
            script_name=relative,
            source=source,
            input_value=input_value,
        )
        security = outcome.get("security")
        if (
            outcome.get("ok") is not True
            or not isinstance(security, dict)
            or security.get("ok") is not True
            or security.get("source_sha256") != artifact["sha256"]
        ):
            raise FoundryError(
                "脚本沙箱执行失败: "
                + str(outcome.get("error") or security or "未提供失败原因")[:1800]
            )
        output_json = json.dumps(
            outcome["result"],
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        if len(output_json.encode("utf-8")) > MAX_SCRIPT_EXECUTION_OUTPUT_BYTES:
            raise FoundryError("脚本输出超过控制面上限")
        execution_id = uuid.uuid4().hex
        audit = {
            "schema_version": 1,
            "execution_id": execution_id,
            "job_id": job["job_id"],
            "skill_name": skill_name,
            "script_name": script_name,
            "caller": caller,
            "source_sha256": source_sha256,
            "input_sha256": hashlib.sha256(input_json.encode("utf-8")).hexdigest(),
            "output_sha256": hashlib.sha256(output_json.encode("utf-8")).hexdigest(),
            "policy_version": security["policy_version"],
            "executed_at": _now(),
        }
        _atomic_json(
            self._job_root(job["job_id"]) / "executions" / f"{execution_id}.json",
            audit,
        )
        return {
            "execution_id": execution_id,
            "skill_name": skill_name,
            "script_name": script_name,
            "revision": self._revision_digest(job["artifacts"]),
            "source_sha256": source_sha256,
            "policy_version": security["policy_version"],
            "limits": outcome.get("limits", {}),
            "result": outcome["result"],
        }

    def _find_installed_job(self, skill_name: str) -> dict[str, Any]:
        """查找全局安装的唯一权威任务，并拒绝歧义或损坏状态。"""

        matches: list[dict[str, Any]] = []
        if not self.workbench_root.is_dir():
            raise FoundryError("Skill 工坊没有可用的安装记录")
        for state_path in self.workbench_root.glob("*/job.json"):
            if state_path.is_symlink():
                raise FoundryError("Skill 构建任务状态不得是符号链接")
            try:
                job = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                raise FoundryError(f"任务状态损坏: {state_path.parent.name}") from exc
            self._validate_job_schema(job, expected_job_id=state_path.parent.name)
            if job["skill_name"] == skill_name and job["state"] == "INSTALLED":
                matches.append(job)
        if not matches:
            raise FoundryError("没有找到由工坊安装的同名 Skill")
        if len(matches) != 1:
            raise FoundryError("同名 Skill 存在多个安装记录，拒绝歧义执行")
        return matches[0]

    def _run_builder(self, job: dict[str, Any]) -> dict[str, Any]:
        draft_root = self._draft_root(job)
        draft_root.mkdir(parents=True, exist_ok=False)
        payload = {
            "draft_root": str(draft_root),
            "skill_name": job["skill_name"],
            **job["request"],
        }
        env = {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
        if os.name == "nt":
            # Windows 启动 Python 需要系统目录，但不继承任何业务密钥。
            for name in ("SYSTEMROOT", "WINDIR"):
                if value := os.environ.get(name):
                    env[name] = value
        completed = subprocess.run(
            [sys.executable, "-I", str(self.worker_script)],
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            capture_output=True,
            cwd=self._job_root(job["job_id"]),
            env=env,
            timeout=5,
            check=False,
        )
        if completed.returncode != 0:
            stderr = completed.stderr.strip()[-2000:]
            raise FoundryError(f"Builder Worker 失败: {stderr or '没有错误输出'}")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise FoundryError("Builder Worker 返回了无效 JSON") from exc
        if result != {
            "generated": True,
            "artifact_kind": job["artifact_kind"],
        }:
            raise FoundryError("Builder Worker 返回结果不符合协议")
        return result

    def _checkpoint(self, job: dict[str, Any], outcome: StepOutcome) -> None:
        """只持久化有证据的成功结果；失败必须进入 FAILED，不能假成功。"""

        if not outcome.ok or not outcome.evidence:
            raise FoundryError(f"Builder 步骤未通过验证: {outcome.step}")
        job["checkpoints"].append(asdict(outcome))
        self._save(job)

    def _validate_draft(self, job: dict[str, Any]) -> dict[str, Any]:
        return self._validate_skill_directory(
            self._draft_root(job),
            expected_name=job["skill_name"],
        )

    def _validate_skill_directory(self, root: Path, *, expected_name: str) -> dict[str, Any]:
        if root.is_symlink():
            raise FoundryError("Skill 草案根目录不得是符号链接")
        resolved = root.resolve()
        if not resolved.is_dir():
            raise FoundryError("草案目录不存在")
        files: set[str] = set()
        for path in resolved.rglob("*"):
            if path.is_symlink():
                raise FoundryError("Skill 草案不得包含符号链接")
            if path.is_file():
                files.add(path.relative_to(resolved).as_posix())
        required_files = {"SKILL.md", "agents/openai.yaml"}
        if not required_files.issubset(files):
            raise FoundryError("草案缺少 SKILL.md 或 agents/openai.yaml")
        extras = files - required_files
        if any(
            not (
                (
                    item.startswith("references/")
                    and item.endswith(".md")
                    and item.count("/") == 1
                )
                or (
                    item.startswith("scripts/")
                    and item.endswith(".py")
                    and item.count("/") == 1
                    and SCRIPT_NAME_PATTERN.fullmatch(
                        PurePosixPath(item).stem
                    )
                )
            )
            for item in extras
        ):
            raise FoundryError("草案只允许额外包含 references/*.md 或 scripts/*.py")
        script_files = sorted(item for item in files if item.startswith("scripts/"))
        for relative in files:
            raw = (resolved / relative).read_bytes()
            if relative == "SKILL.md":
                maximum = MAX_SKILL_FILE_BYTES
            elif relative.startswith("scripts/"):
                maximum = MAX_SCRIPT_FILE_BYTES
            else:
                maximum = MAX_AUXILIARY_FILE_BYTES
            if len(raw) > maximum:
                raise FoundryError(f"Skill 文本文件超过 {maximum} bytes: {relative}")
            try:
                raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise FoundryError(f"Skill 文件不是 UTF-8 文本: {relative}") from exc
        skill_text = (resolved / "SKILL.md").read_text(encoding="utf-8")
        lines = skill_text.splitlines()
        if not lines or lines[0].strip() != "---":
            raise FoundryError("SKILL.md 缺少 frontmatter")
        try:
            end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
        except StopIteration as exc:
            raise FoundryError("SKILL.md frontmatter 未闭合") from exc
        metadata: dict[str, str] = {}
        for line in lines[1:end]:
            key, separator, raw_value = line.partition(":")
            if not separator or key.strip() not in {"name", "description"}:
                raise FoundryError("SKILL.md frontmatter 只能包含 name 和 description")
            if key.strip() in metadata:
                raise FoundryError("SKILL.md frontmatter 字段不得重复")
            try:
                value = json.loads(raw_value.strip())
            except json.JSONDecodeError:
                value = raw_value.strip()
            if not isinstance(value, str) or not value.strip():
                raise FoundryError("SKILL.md frontmatter 值无效")
            metadata[key.strip()] = value
        if set(metadata) != {"name", "description"}:
            raise FoundryError("SKILL.md frontmatter 字段不完整")
        if metadata["name"] != expected_name:
            raise FoundryError("Skill name 与任务不一致")
        if not "\n".join(lines[end + 1 :]).strip():
            raise FoundryError("SKILL.md 正文不能为空")
        agent_text = (resolved / "agents" / "openai.yaml").read_text(encoding="utf-8")
        for required in ("interface:", "display_name:", "short_description:", "default_prompt:"):
            if required not in agent_text:
                raise FoundryError(f"agents/openai.yaml 缺少字段: {required}")
        return {
            "ok": True,
            "files": sorted(files),
            "script_files": script_files,
        }

    def _test_draft(self, job: dict[str, Any]) -> list[dict[str, Any]]:
        """重复解析包，并在隔离进程中对每个脚本执行真实 smoke test。"""

        validation = self._validate_draft(job)
        if not {"SKILL.md", "agents/openai.yaml"}.issubset(validation["files"]):
            raise FoundryError("Skill 包测试未覆盖预期文件")
        if self.project_root.as_posix() in (
            self._draft_root(job) / "SKILL.md"
        ).read_text(encoding="utf-8"):
            raise FoundryError("Skill 草案包含本机项目绝对路径")
        expected_scripts = {
            f"scripts/{item['name']}.py": item for item in job["request"]["scripts"]
        }
        actual_scripts = {
            item for item in validation["files"] if item.startswith("scripts/")
        }
        if set(expected_scripts) != actual_scripts:
            raise FoundryError("生成脚本文件与构建请求不一致")
        results: list[dict[str, Any]] = []
        for relative, script in sorted(expected_scripts.items()):
            source = (self._draft_root(job) / relative).read_text(encoding="utf-8")
            outcome = self._run_sandbox(
                action="execute",
                script_name=relative,
                source=source,
                input_value=script["smoke_input"],
            )
            security = outcome.get("security")
            if (
                outcome.get("ok") is not True
                or not isinstance(security, dict)
                or security.get("ok") is not True
                or security.get("source_sha256")
                != hashlib.sha256(source.encode("utf-8")).hexdigest()
            ):
                detail = str(outcome.get("error") or "未提供失败原因")
                findings = security.get("findings") if isinstance(security, dict) else None
                if findings:
                    detail = f"{detail}; findings={findings}"
                raise FoundryError(f"脚本沙箱测试失败 {relative}: {detail[:1800]}")
            results.append(
                {
                    "path": relative,
                    "policy_version": security["policy_version"],
                    "source_sha256": security["source_sha256"],
                    "imports": security["imports"],
                    "ast_nodes": security["ast_nodes"],
                    "limits": outcome.get("limits", {}),
                    "smoke_output_sha256": hashlib.sha256(
                        json.dumps(
                            outcome["result"],
                            ensure_ascii=False,
                            allow_nan=False,
                            separators=(",", ":"),
                        ).encode("utf-8")
                    ).hexdigest(),
                }
            )
        return results

    def _run_sandbox(
        self,
        *,
        action: str,
        script_name: str,
        source: str,
        input_value: Any = None,
    ) -> dict[str, Any]:
        """用最小环境启动沙箱进程，并严格校验其有界 JSON 响应。"""

        payload: dict[str, Any] = {
            "action": action,
            "script_name": script_name,
            "source": source,
        }
        if action == "execute":
            payload["input"] = input_value
        env = {
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
        if os.name == "nt":
            for name in ("SYSTEMROOT", "WINDIR"):
                if value := os.environ.get(name):
                    env[name] = value
        try:
            completed = subprocess.run(
                [sys.executable, "-I", str(self.sandbox_script)],
                input=json.dumps(payload, ensure_ascii=False, allow_nan=False),
                text=True,
                encoding="utf-8",
                capture_output=True,
                cwd=self.sandbox_script.parent,
                env=env,
                timeout=SANDBOX_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise FoundryError(
                f"脚本沙箱超过 {SANDBOX_TIMEOUT_SECONDS} 秒 wall timeout"
            ) from exc
        if completed.returncode != 0:
            raise FoundryError(
                "脚本沙箱进程异常退出: "
                + (completed.stderr.strip()[-1000:] or str(completed.returncode))
            )
        if len(completed.stdout.encode("utf-8")) > 96_000:
            raise FoundryError("脚本沙箱响应超过 96KB")
        try:
            result = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise FoundryError("脚本沙箱返回了无效 JSON") from exc
        if not isinstance(result, dict):
            raise FoundryError("脚本沙箱响应必须是 JSON 对象")
        return result

    def _artifact_manifest(self, job: dict[str, Any]) -> list[dict[str, str]]:
        return self._manifest_for_root(self._draft_root(job))

    @staticmethod
    def _revision_digest(artifacts: list[dict[str, str]]) -> str:
        """生成与有序完整 manifest 绑定的稳定 revision。"""

        payload = json.dumps(
            artifacts,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _manifest_for_root(root: Path) -> list[dict[str, str]]:
        """计算目录内所有普通文件的完整路径与内容摘要。"""

        if root.is_symlink():
            raise FoundryError("产物根目录不得是符号链接")
        entries = list(root.rglob("*"))
        if any(item.is_symlink() for item in entries):
            raise FoundryError("产物 revision 不得包含符号链接")
        artifacts: list[dict[str, str]] = []
        files = [item for item in entries if item.is_file()]
        for path in sorted(files, key=lambda item: item.relative_to(root).as_posix()):
            artifacts.append(
                {
                    "path": path.relative_to(root).as_posix(),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
            )
        return artifacts

    def _transition(self, job: dict[str, Any], state: str, detail: str) -> None:
        if state not in STATES:
            raise FoundryError(f"未知状态: {state}")
        current = job.get("state")
        allowed = {
            None: {"DRAFT"},
            "DRAFT": {"DRAFT", "GENERATING"},
            "GENERATING": {"VALIDATING", "FAILED"},
            "VALIDATING": {"TESTING", "FAILED"},
            "TESTING": {"REVIEW_PENDING", "FAILED"},
            "REVIEW_PENDING": {"INSTALLING", "REJECTED"},
            "INSTALLING": {"INSTALLED"},
        }
        if current in TERMINAL_STATES or state not in allowed.get(current, set()):
            raise FoundryError(f"非法状态转换: {current} -> {state}")
        timestamp = _now()
        job["state"] = state
        job["updated_at"] = timestamp
        job["history"].append({"state": state, "at": timestamp, "detail": detail})

    def _public_job(self, job: dict[str, Any]) -> dict[str, Any]:
        request = job["request"]
        history = [
            {
                "state": item["state"],
                "at": item["at"],
                "detail": item["detail"][:300],
            }
            for item in job["history"]
        ]
        checkpoints = [
            {
                "step": item["step"],
                "ok": item["ok"],
                "detail": item["detail"][:300],
            }
            for item in job["checkpoints"]
        ]
        result: dict[str, Any] = {
            "schema_version": job["schema_version"],
            "job_id": job["job_id"],
            "skill_name": job["skill_name"],
            "artifact_kind": job["artifact_kind"],
            "state": job["state"],
            "created_at": job["created_at"],
            "updated_at": job["updated_at"],
            "request_summary": {
                "display_name": request["display_name"],
                "description": request["description"],
                "objective_preview": self._bounded_text(
                    request["objective"],
                    maximum_bytes=1_000,
                ),
                "step_count": len(request["steps"]),
                "reference_count": len(request["references"]),
                "script_count": len(request["scripts"]),
            },
            "history": history,
            "checkpoints": checkpoints,
            "validation": job["validation"],
            "script_tests": job["script_tests"],
            "failure": (
                {
                    "type": job["failure"]["type"],
                    "message": job["failure"]["message"][:1_000],
                }
                if job["failure"] is not None
                else None
            ),
            "isolation": job["isolation"],
        }
        if job["artifacts"]:
            review = self._review_projection(job)
            result["review"] = review
        if job["state"] in {"REVIEW_PENDING", "INSTALLING", "INSTALLED"}:
            result["approval"] = {
                "required_phrase": self._expected_install_phrase(job),
                "note": "必须由同一用户在同一 Session 的新消息中只发送该短语。",
            }
        if job["state"] == "REVIEW_PENDING":
            result["rejection"] = {
                "required_phrase": self._expected_reject_phrase(job),
                "note": "拒绝时当前用户消息也必须与该短语完全相等。",
            }
        for optional in ("approved_by", "installed_path", "rejection_reason"):
            if optional in job:
                result[optional] = job[optional]
        if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) >= MAX_PUBLIC_JOB_BYTES:
            raise FoundryError("公共任务投影超过 30KB 内部上限")
        return result

    def _review_projection(self, job: dict[str, Any]) -> dict[str, Any]:
        """投影有界预览和文件清单，不把 references 正文塞进工具结果。"""

        draft_root = self._draft_root(job)
        files: list[dict[str, Any]] = []
        previews: dict[str, dict[str, Any]] = {}
        for artifact in job["artifacts"]:
            path = (draft_root / artifact["path"]).resolve()
            if path.parent != draft_root and draft_root not in path.parents:
                raise FoundryError("冻结产物路径越界")
            if path.is_symlink() or not path.is_file():
                raise FoundryError("冻结产物文件不存在或是符号链接")
            raw = path.read_bytes()
            files.append(
                {
                    "path": artifact["path"],
                    "size_bytes": len(raw),
                    "sha256": artifact["sha256"],
                }
            )
            if artifact["path"] in {"SKILL.md", "agents/openai.yaml"}:
                preview_raw = raw[:MAX_PUBLIC_PREVIEW_BYTES]
                previews[artifact["path"]] = {
                    "content": preview_raw.decode("utf-8", errors="ignore"),
                    "truncated": len(raw) > MAX_PUBLIC_PREVIEW_BYTES,
                }
        return {
            "revision": self._revision_digest(job["artifacts"]),
            "files": files,
            "previews": previews,
            "reference_files": [
                item for item in files if item["path"].startswith("references/")
            ],
            "script_files": [
                item for item in files if item["path"].startswith("scripts/")
            ],
        }

    def _expected_install_phrase(self, job: dict[str, Any]) -> str:
        return (
            f"确认全局安装 {job['skill_name']} {job['job_id']} "
            f"{self._revision_digest(job['artifacts'])}"
        )

    def _expected_reject_phrase(self, job: dict[str, Any]) -> str:
        return (
            f"拒绝草案 {job['skill_name']} {job['job_id']} "
            f"{self._revision_digest(job['artifacts'])}"
        )

    @staticmethod
    def _allowed_preview_path(path: str) -> bool:
        return path in {"SKILL.md", "agents/openai.yaml"} or bool(
            re.fullmatch(
                r"(?:references/[a-z0-9]+(?:-[a-z0-9]+)*\.md|"
                r"scripts/[a-z][a-z0-9_]{0,63}\.py)",
                path,
            )
        )

    @staticmethod
    def _bounded_text(value: str, *, maximum_bytes: int) -> str:
        raw = value.encode("utf-8")
        if len(raw) <= maximum_bytes:
            return value
        return raw[:maximum_bytes].decode("utf-8", errors="ignore")

    def _save(self, job: dict[str, Any]) -> None:
        self._validate_job_schema(job, expected_job_id=job.get("job_id"))
        _atomic_json(self._job_root(job["job_id"]) / "job.json", job)

    def _load(self, job_id: str) -> dict[str, Any]:
        path = self._job_root(job_id) / "job.json"
        if path.is_symlink():
            raise FoundryError("Skill 构建任务状态不得是符号链接")
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FoundryError("Skill 构建任务不存在") from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise FoundryError("Skill 构建任务状态损坏") from exc
        self._validate_job_schema(job, expected_job_id=job_id)
        return job

    def _reconcile_install(self, job: dict[str, Any]) -> None:
        """依据冻结 revision 恢复 INSTALLING；任何歧义都保持状态并响亮失败。"""

        if job["state"] != "INSTALLING":
            raise FoundryError("只有 INSTALLING 任务可以执行安装恢复")
        transaction = job["install_transaction"]
        target = (self.project_root / transaction["target_path"]).resolve()
        staging = (self.project_root / transaction["staging_path"]).resolve()
        expected_target = (self.skills_root / job["skill_name"]).resolve()
        expected_staging = (self._job_root(job["job_id"]) / "installing").resolve()
        if target != expected_target or staging != expected_staging:
            raise FoundryError("安装事务路径与任务不一致")
        if (
            target.parent != self.skills_root
            or staging.parent != self._job_root(job["job_id"])
        ):
            raise FoundryError("安装事务路径越界")
        expected_manifest = job["artifacts"]
        expected_revision = self._revision_digest(expected_manifest)
        if transaction["revision"] != expected_revision:
            raise FoundryError("安装事务 revision 与任务不一致")
        if target.is_symlink() or staging.is_symlink():
            raise FoundryError("安装目标或暂存目录不得是符号链接")

        if target.exists():
            try:
                self._validate_skill_directory(target, expected_name=job["skill_name"])
            except FoundryError as exc:
                raise FoundryError("安装目标已被不匹配的外部 Skill 占用") from exc
            if self._manifest_for_root(target) != expected_manifest:
                raise FoundryError("安装目标已被不匹配的外部 Skill 占用")
            if staging.exists():
                try:
                    self._validate_skill_directory(
                        staging,
                        expected_name=job["skill_name"],
                    )
                except FoundryError as exc:
                    raise FoundryError("安装暂存目录与冻结 revision 不一致") from exc
                if self._manifest_for_root(staging) != expected_manifest:
                    raise FoundryError("安装暂存目录与冻结 revision 不一致")
                shutil.rmtree(staging)
        else:
            if not staging.exists():
                if self._artifact_manifest(job) != expected_manifest:
                    raise FoundryError("原始草案与冻结 revision 不一致，无法恢复安装")
                shutil.copytree(self._draft_root(job), staging, symlinks=False)
            try:
                self._validate_skill_directory(staging, expected_name=job["skill_name"])
            except FoundryError as exc:
                raise FoundryError("安装暂存内容与冻结 revision 不一致") from exc
            if self._manifest_for_root(staging) != expected_manifest:
                raise FoundryError("安装暂存内容与冻结 revision 不一致")
            self._replace_directory(staging, target)
            self._validate_skill_directory(target, expected_name=job["skill_name"])
            if self._manifest_for_root(target) != expected_manifest:
                raise FoundryError("安装提交后的目标与冻结 revision 不一致")

        job["approved_by"] = job["owner"]["actor_id"]
        job["installed_path"] = transaction["target_path"]
        self._transition(job, "INSTALLED", "安装文件与冻结 revision 一致，完成状态提交")
        self._save(job)

    @staticmethod
    def _replace_directory(source: Path, target: Path) -> None:
        """提交目录；Windows 扫描器短暂占用新文件时进行有界重试。"""

        for attempt in range(5):
            try:
                os.replace(source, target)
                return
            except PermissionError:
                if (
                    os.name != "nt"
                    or attempt == 4
                    or not source.is_dir()
                    or target.exists()
                ):
                    raise
                time.sleep(0.05 * (2**attempt))
        raise AssertionError("目录替换重试循环未返回")

    def _validate_job_schema(
        self,
        job: object,
        *,
        expected_job_id: object,
    ) -> None:
        """集中校验权威任务状态，损坏数据不得逃逸为 KeyError/TypeError。"""

        if not isinstance(job, dict):
            raise FoundryError("Skill 构建任务状态必须是 JSON 对象")
        required = {
            "schema_version",
            "job_id",
            "skill_name",
            "artifact_kind",
            "owner",
            "request",
            "state",
            "history",
            "checkpoints",
            "created_at",
            "updated_at",
            "validation",
            "script_tests",
            "artifacts",
            "failure",
            "isolation",
        }
        optional = {
            "approved_by",
            "installed_path",
            "rejection_reason",
            "install_transaction",
        }
        if not required.issubset(job) or set(job) - required - optional:
            raise FoundryError("Skill 构建任务状态字段不完整")
        job_id = job.get("job_id")
        if (
            job.get("schema_version") != 2
            or not isinstance(job_id, str)
            or not re.fullmatch(r"[0-9a-f]{32}", job_id)
            or job_id != expected_job_id
        ):
            raise FoundryError("Skill 构建任务身份无效")
        skill_name = job.get("skill_name")
        if not isinstance(skill_name, str) or not SKILL_NAME_PATTERN.fullmatch(skill_name):
            raise FoundryError("Skill 构建任务 skill_name 无效")
        if job.get("artifact_kind") not in {
            "instruction-only",
            "instruction-and-scripts",
        }:
            raise FoundryError("Skill 构建任务 artifact_kind 无效")
        owner = job.get("owner")
        if not self._valid_owner(owner):
            raise FoundryError("Skill 构建任务 owner 无效")
        owner_fields = cast(dict[str, str], owner)
        request = job.get("request")
        if not isinstance(request, dict) or set(request) != {
            "display_name",
            "description",
            "objective",
            "steps",
            "references",
            "scripts",
        }:
            raise FoundryError("Skill 构建任务 request 无效")
        for key, maximum in (
            ("display_name", 80),
            ("description", 500),
            ("objective", 4000),
        ):
            value = request.get(key)
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise FoundryError(f"Skill 构建任务 request.{key} 无效")
        if (
            "\n" in request["display_name"]
            or "\r" in request["display_name"]
            or "\n" in request["description"]
            or "\r" in request["description"]
            or len(request["display_name"].encode("utf-8")) > 300
            or len(request["description"].encode("utf-8")) > 1_000
            or len(request["objective"].encode("utf-8")) > 4_500
        ):
            raise FoundryError("Skill 构建任务摘要字段包含换行或超过 bytes 限制")
        steps = request.get("steps")
        if (
            not isinstance(steps, list)
            or len(steps) > 20
            or any(not isinstance(item, str) or not item.strip() or len(item) > 500 for item in steps)
        ):
            raise FoundryError("Skill 构建任务 request.steps 无效")
        if sum(len(item.encode("utf-8")) for item in steps) > 3_500:
            raise FoundryError("Skill 构建任务 request.steps 超过 bytes 限制")
        references = request.get("references")
        if not isinstance(references, list) or len(references) > 10:
            raise FoundryError("Skill 构建任务 request.references 无效")
        for item in references:
            if not isinstance(item, dict) or set(item) != {"name", "content"}:
                raise FoundryError("Skill 构建任务 reference 无效")
            name = item.get("name")
            content = item.get("content")
            if (
                not isinstance(name, str)
                or not SKILL_NAME_PATTERN.fullmatch(name)
                or not isinstance(content, str)
                or not content.strip()
                or len(content) > 10_000
                or len(content.encode("utf-8")) > 10_000
            ):
                raise FoundryError("Skill 构建任务 reference 内容无效")
        if sum(len(item["content"].encode("utf-8")) for item in references) > 50_000:
            raise FoundryError("Skill 构建任务 references 超过 bytes 限制")
        try:
            normalized_scripts = self._normalize_scripts(request.get("scripts"))
        except FoundryError as exc:
            raise FoundryError("Skill 构建任务 request.scripts 无效") from exc
        if normalized_scripts != request["scripts"]:
            raise FoundryError("Skill 构建任务 request.scripts 未规范化")
        expected_artifact_kind = (
            "instruction-and-scripts"
            if normalized_scripts
            else "instruction-only"
        )
        if job["artifact_kind"] != expected_artifact_kind:
            raise FoundryError("Skill 构建任务 artifact_kind 与 scripts 不一致")
        state = job.get("state")
        if state not in STATES:
            raise FoundryError("Skill 构建任务 state 无效")
        history = job.get("history")
        if not isinstance(history, list) or not history:
            raise FoundryError("Skill 构建任务 history 无效")
        for item in history:
            if (
                not isinstance(item, dict)
                or set(item) != {"state", "at", "detail"}
                or item.get("state") not in STATES
                or not self._nonempty_string(item.get("at"), maximum=100)
                or not self._nonempty_string(item.get("detail"), maximum=1000)
            ):
                raise FoundryError("Skill 构建任务 history 条目无效")
        if history[-1]["state"] != state:
            raise FoundryError("Skill 构建任务 history 与 state 不一致")
        checkpoints = job.get("checkpoints")
        if not isinstance(checkpoints, list):
            raise FoundryError("Skill 构建任务 checkpoints 无效")
        for item in checkpoints:
            if (
                not isinstance(item, dict)
                or set(item) != {"step", "ok", "detail", "evidence"}
                or not self._nonempty_string(item.get("step"), maximum=100)
                or item.get("ok") is not True
                or not self._nonempty_string(item.get("detail"), maximum=1000)
                or not isinstance(item.get("evidence"), dict)
                or not item["evidence"]
            ):
                raise FoundryError("Skill 构建任务 checkpoint 条目无效")
        checkpoint_steps = [item["step"] for item in checkpoints]
        if checkpoint_steps != ["generate", "validate", "test"][: len(checkpoint_steps)]:
            raise FoundryError("Skill 构建任务 checkpoint 顺序无效")
        for field in ("created_at", "updated_at"):
            if not self._nonempty_string(job.get(field), maximum=100):
                raise FoundryError(f"Skill 构建任务 {field} 无效")
        validation = job.get("validation")
        if validation is not None and (
            not isinstance(validation, dict)
            or validation.get("ok") is not True
            or not isinstance(validation.get("files"), list)
            or any(not isinstance(item, str) or not item for item in validation["files"])
        ):
            raise FoundryError("Skill 构建任务 validation 无效")
        script_tests = job.get("script_tests")
        if not isinstance(script_tests, list):
            raise FoundryError("Skill 构建任务 script_tests 无效")
        expected_script_paths = [
            f"scripts/{item['name']}.py" for item in normalized_scripts
        ]
        actual_script_paths: list[str] = []
        for item in script_tests:
            if (
                not isinstance(item, dict)
                or set(item)
                != {
                    "path",
                    "policy_version",
                    "source_sha256",
                    "imports",
                    "ast_nodes",
                    "limits",
                    "smoke_output_sha256",
                }
                or not self._nonempty_string(item.get("path"), maximum=200)
                or not self._nonempty_string(item.get("policy_version"), maximum=100)
                or not isinstance(item.get("source_sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", item["source_sha256"]) is None
                or not isinstance(item.get("imports"), list)
                or any(
                    not isinstance(name, str) or len(name) > 100
                    for name in item["imports"]
                )
                or not isinstance(item.get("ast_nodes"), int)
                or isinstance(item.get("ast_nodes"), bool)
                or not 0 <= item["ast_nodes"] <= 3_000
                or not isinstance(item.get("limits"), dict)
                or any(
                    not isinstance(key, str) or not isinstance(value, str)
                    for key, value in item["limits"].items()
                )
                or not isinstance(item.get("smoke_output_sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", item["smoke_output_sha256"]) is None
            ):
                raise FoundryError("Skill 构建任务 script_tests 条目无效")
            actual_script_paths.append(item["path"])
        if actual_script_paths not in ([], sorted(expected_script_paths)):
            raise FoundryError("Skill 构建任务 script_tests 与 scripts 不一致")
        artifacts = job.get("artifacts")
        if not isinstance(artifacts, list):
            raise FoundryError("Skill 构建任务 artifacts 无效")
        artifact_paths: list[str] = []
        for item in artifacts:
            if (
                not isinstance(item, dict)
                or set(item) != {"path", "sha256"}
                or not self._nonempty_string(item.get("path"), maximum=500)
                or not isinstance(item.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", item["sha256"]) is None
            ):
                raise FoundryError("Skill 构建任务 artifact 条目无效")
            artifact_path = PurePosixPath(item["path"])
            if (
                artifact_path.is_absolute()
                or ".." in artifact_path.parts
                or "." in artifact_path.parts
                or "\\" in item["path"]
            ):
                raise FoundryError("Skill 构建任务 artifact 路径越界")
            artifact_paths.append(item["path"])
        if artifact_paths != sorted(set(artifact_paths)):
            raise FoundryError("Skill 构建任务 artifacts 必须按路径排序且不得重复")
        failure = job.get("failure")
        if failure is not None and (
            not isinstance(failure, dict)
            or set(failure) != {"type", "message"}
            or not self._nonempty_string(failure.get("type"), maximum=200)
            or not self._nonempty_string(failure.get("message"), maximum=3000)
        ):
            raise FoundryError("Skill 构建任务 failure 无效")
        isolation = job.get("isolation")
        if (
            not isinstance(isolation, dict)
            or set(isolation)
            != {
                "available",
                "artifact_kinds",
                "executes_generated_code",
                "script_contract",
                "network_policy",
                "filesystem_policy",
                "isolation",
                "install_requires_explicit_approval",
            }
            or isolation.get("available") is not True
            or isolation.get("artifact_kinds")
            != ["instruction-only", "instruction-and-scripts"]
            or isolation.get("executes_generated_code") is not True
            or not self._nonempty_string(isolation.get("script_contract"), maximum=500)
            or not self._nonempty_string(isolation.get("network_policy"), maximum=200)
            or not self._nonempty_string(
                isolation.get("filesystem_policy"), maximum=300
            )
            or not self._nonempty_string(isolation.get("isolation"), maximum=500)
            or isolation.get("install_requires_explicit_approval") is not True
        ):
            raise FoundryError("Skill 构建任务 isolation 无效")
        if state in {"REVIEW_PENDING", "INSTALLING", "INSTALLED", "REJECTED"} and (
            validation is None
            or not artifacts
            or len(checkpoints) != 3
            or actual_script_paths != sorted(expected_script_paths)
        ):
            raise FoundryError("Skill 构建任务缺少已验证 revision")
        transaction = job.get("install_transaction")
        if state in {"INSTALLING", "INSTALLED"}:
            expected_transaction = {
                "target_path": f"skills/{skill_name}",
                "staging_path": (
                    f"data/skill-workbench/{job_id}/installing"
                ),
                "revision": self._revision_digest(artifacts),
            }
            if transaction != expected_transaction:
                raise FoundryError("安装任务缺少有效 install_transaction")
        elif "install_transaction" in job:
            raise FoundryError("非安装任务不得包含 install_transaction")
        if state == "INSTALLED" and (
            not self._nonempty_string(job.get("approved_by"), maximum=200)
            or not self._nonempty_string(job.get("installed_path"), maximum=500)
            or job.get("approved_by") != owner_fields["actor_id"]
            or job.get("installed_path") != f"skills/{skill_name}"
        ):
            raise FoundryError("已安装任务缺少审批或安装信息")
        if state == "REJECTED" and not self._nonempty_string(
            job.get("rejection_reason"), maximum=1000
        ):
            raise FoundryError("已拒绝任务缺少理由")
        if state == "FAILED" and failure is None:
            raise FoundryError("失败任务缺少 failure")
        if state != "FAILED" and failure is not None:
            raise FoundryError("非失败任务不得包含 failure")
        if state != "INSTALLED" and (
            "approved_by" in job or "installed_path" in job
        ):
            raise FoundryError("未安装任务不得包含安装信息")
        if state != "REJECTED" and "rejection_reason" in job:
            raise FoundryError("未拒绝任务不得包含拒绝理由")

    @staticmethod
    def _nonempty_string(value: object, *, maximum: int) -> bool:
        return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum

    @classmethod
    def _valid_owner(cls, owner: object) -> bool:
        return (
            isinstance(owner, dict)
            and set(owner) == {"actor_id", "session_id"}
            and cls._nonempty_string(owner.get("actor_id"), maximum=200)
            and cls._nonempty_string(owner.get("session_id"), maximum=200)
        )

    def _required_owner(self, params: dict[str, Any]) -> dict[str, str]:
        owner = {
            "actor_id": params.get("actor_id"),
            "session_id": params.get("session_id"),
        }
        if not self._valid_owner(owner):
            raise FoundryError("Skill 构建任务必须绑定有效的 actor_id 和 session_id")
        return {
            "actor_id": str(owner["actor_id"]),
            "session_id": str(owner["session_id"]),
        }

    @staticmethod
    def _assert_owner(job: dict[str, Any], owner: dict[str, str]) -> None:
        if job["owner"] != owner:
            raise FoundryError("Skill 构建任务不属于当前用户或会话")

    def _job_root(self, job_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise FoundryError("job_id 格式无效")
        candidate = self.workbench_root / job_id
        if candidate.is_symlink():
            raise FoundryError("任务目录不得是符号链接")
        path = candidate.resolve()
        if path.parent != self.workbench_root:
            raise FoundryError("任务目录越界")
        return path

    def _draft_root(self, job: dict[str, Any]) -> Path:
        job_root = self._job_root(job["job_id"])
        candidate = job_root / "draft"
        if candidate.is_symlink():
            raise FoundryError("草案目录不得是符号链接")
        path = candidate.resolve()
        if path.parent != job_root:
            raise FoundryError("草案目录越界")
        return path

    @staticmethod
    def _required_text(params: dict[str, Any], key: str, *, maximum: int) -> str:
        value = params.get(key)
        if not isinstance(value, str) or not value.strip() or len(value) > maximum:
            raise FoundryError(f"{key} 必须是 1 到 {maximum} 字符的非空字符串")
        return value.strip()

    @classmethod
    def _required_single_line(
        cls,
        params: dict[str, Any],
        key: str,
        *,
        maximum: int,
    ) -> str:
        value = cls._required_text(params, key, maximum=maximum)
        if "\n" in value or "\r" in value:
            raise FoundryError(f"{key} 不得包含 CR 或 LF")
        return value

    @classmethod
    def _normalize_scripts(cls, raw_scripts: object) -> list[dict[str, Any]]:
        """集中校验模型提供的源码与合成 smoke input，禁止把凭据写入任务。"""

        if not isinstance(raw_scripts, list) or len(raw_scripts) > 8:
            raise FoundryError("scripts 必须是至多 8 项的列表")
        scripts: list[dict[str, Any]] = []
        names: set[str] = set()
        total_source_bytes = 0
        total_input_bytes = 0
        for item in raw_scripts:
            if not isinstance(item, dict) or set(item) != {
                "name",
                "description",
                "content",
                "smoke_input",
            }:
                raise FoundryError(
                    "script 必须只包含 name、description、content 和 smoke_input"
                )
            name = item.get("name")
            description = item.get("description")
            content = item.get("content")
            if (
                not isinstance(name, str)
                or not SCRIPT_NAME_PATTERN.fullmatch(name)
                or name in names
            ):
                raise FoundryError("script name 必须唯一且为小写 snake_case")
            if (
                not isinstance(description, str)
                or not description.strip()
                or len(description) > 300
                or "\r" in description
                or "\n" in description
                or len(description.encode("utf-8")) > 900
            ):
                raise FoundryError("script description 必须是有界单行文本")
            if (
                not isinstance(content, str)
                or not content.strip()
                or len(content.encode("utf-8")) > MAX_SCRIPT_FILE_BYTES
            ):
                raise FoundryError(
                    f"script content 必须是至多 {MAX_SCRIPT_FILE_BYTES} bytes 的非空文本"
                )
            cls._assert_synthetic_smoke_input(item.get("smoke_input"))
            try:
                smoke_json = json.dumps(
                    item.get("smoke_input"),
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                )
            except (TypeError, ValueError, OverflowError) as exc:
                raise FoundryError("script smoke_input 必须是有限的 JSON 值") from exc
            smoke_bytes = len(smoke_json.encode("utf-8"))
            if smoke_bytes > MAX_SCRIPT_INPUT_BYTES:
                raise FoundryError(
                    f"单个 script smoke_input 不得超过 {MAX_SCRIPT_INPUT_BYTES} bytes"
                )
            total_source_bytes += len(content.encode("utf-8"))
            total_input_bytes += smoke_bytes
            names.add(name)
            scripts.append(
                {
                    "name": name,
                    "description": description.strip(),
                    "content": content.rstrip() + "\n",
                    "smoke_input": item.get("smoke_input"),
                }
            )
        if total_source_bytes > 160_000:
            raise FoundryError("scripts 源码总计不得超过 160000 bytes")
        if total_input_bytes > 64_000:
            raise FoundryError("scripts smoke_input 总计不得超过 64000 bytes")
        return scripts

    @classmethod
    def _assert_synthetic_smoke_input(cls, value: Any) -> None:
        """拒绝明显凭据字段；smoke test 只能使用合成、非敏感 JSON。"""

        sensitive_keys = {
            "access_token",
            "api_key",
            "apikey",
            "authorization",
            "cookie",
            "password",
            "passwd",
            "private_key",
            "secret",
            "token",
        }
        pending: list[tuple[Any, int]] = [(value, 0)]
        seen_nodes = 0
        while pending:
            current, depth = pending.pop()
            seen_nodes += 1
            if seen_nodes > 2_000 or depth > 20:
                raise FoundryError("script smoke_input 结构过深或过大")
            if isinstance(current, dict):
                for key, child in current.items():
                    if not isinstance(key, str):
                        raise FoundryError("script smoke_input 对象键必须是字符串")
                    normalized = key.strip().lower().replace("-", "_")
                    if normalized in sensitive_keys:
                        raise FoundryError(
                            f"script smoke_input 不得包含敏感字段: {key}"
                        )
                    pending.append((child, depth + 1))
            elif isinstance(current, list):
                pending.extend((child, depth + 1) for child in current)
            elif current is not None and not isinstance(
                current, (str, int, float, bool)
            ):
                raise FoundryError("script smoke_input 只能包含 JSON 值")

    def _required_job_id(self, params: dict[str, Any]) -> str:
        return self._required_text(params, "job_id", maximum=32)


def _dispatch(foundry: SkillFoundry, method: str, params: dict[str, Any]) -> dict[str, Any]:
    handlers = {
        "ping": foundry.ping,
        "create": foundry.create,
        "begin": foundry.begin,
        "add_script": foundry.add_script,
        "finalize": foundry.finalize,
        "get": foundry.get,
        "list": foundry.list_jobs,
        "preview": foundry.preview,
        "execute_script": foundry.execute_script,
        "approve": foundry.approve,
        "reject": foundry.reject,
    }
    handler = handlers.get(method)
    if handler is None:
        raise FoundryError(f"未知方法: {method}")
    return handler() if method == "ping" else handler(params)


def serve(project_root: Path) -> int:
    """通过逐行 JSON RPC 服务宿主；每个请求都返回同一 request_id。"""

    foundry = SkillFoundry(project_root)
    sys.stdout.write(
        json.dumps(
            {"type": "ready", "api_version": 1, "service": "builder"},
            ensure_ascii=False,
        )
        + "\n"
    )
    sys.stdout.flush()
    for raw_line in sys.stdin:
        request_id: Any = None
        try:
            request = json.loads(raw_line)
            if not isinstance(request, dict):
                raise FoundryError("请求必须是 JSON 对象")
            if request.get("type") == "shutdown":
                return 0
            if request.get("type") != "request":
                raise FoundryError("请求 type 必须是 request 或 shutdown")
            request_id = request.get("request_id")
            method = request.get("method")
            params = request.get("params", {})
            if not isinstance(request_id, str) or not request_id:
                raise FoundryError("request_id 无效")
            if not isinstance(method, str) or not isinstance(params, dict):
                raise FoundryError("method 或 params 无效")
            response = {
                "type": "response",
                "request_id": request_id,
                "result": _dispatch(foundry, method, params),
            }
        except (FoundryError, OSError, json.JSONDecodeError) as exc:
            response = {
                "type": "response",
                "request_id": request_id,
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="iJA Skill 工坊控制服务")
    parser.add_argument("--project-root", type=Path, required=True)
    arguments = parser.parse_args()
    return serve(arguments.project_root)


if __name__ == "__main__":
    raise SystemExit(main())
