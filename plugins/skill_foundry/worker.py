"""在写路径受限的 Worker 中生成 Skill 文本与待检脚本。"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
SCRIPT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _with_windows_lock_retry(operation: Any) -> Any:
    """Windows 扫描器短暂占用新文件时重试；其他错误立即抛出。"""

    for attempt in range(5):
        try:
            return operation()
        except PermissionError:
            if os.name != "nt" or attempt == 4:
                raise
            time.sleep(0.05 * (2**attempt))
    raise AssertionError("Worker 文件操作重试循环未返回")


def _mkdir(path: Path) -> None:
    _with_windows_lock_retry(path.mkdir)


def _write_text(path: Path, content: str) -> None:
    _with_windows_lock_retry(lambda: path.write_text(content, encoding="utf-8"))


def _write_bytes(path: Path, content: bytes) -> None:
    _with_windows_lock_retry(lambda: path.write_bytes(content))


def _install_audit_policy(draft_root: Path) -> None:
    """限制 Python 层网络与写路径；该策略不宣称是 OS 强沙箱。"""

    allowed_write_root = draft_root.resolve()

    def require_draft_path(raw_path: Any, *, operation: str) -> None:
        if not isinstance(raw_path, (str, bytes, os.PathLike)):
            raise PermissionError(f"Builder Worker 拒绝无法校验路径的操作: {operation}")
        resolved = Path(os.fsdecode(raw_path)).resolve()
        if resolved != allowed_write_root and allowed_write_root not in resolved.parents:
            raise PermissionError(
                f"Builder Worker 只能在当前任务 draft 内执行 {operation}"
            )

    def audit(event: str, args: tuple[Any, ...]) -> None:
        if event.startswith("socket."):
            raise PermissionError("Builder Worker 默认禁止网络访问")
        if event in {"subprocess.Popen", "os.system", "os.exec", "os.spawn"}:
            raise PermissionError("Builder Worker 禁止创建子进程")
        if event == "open" and args:
            raw_path = args[0]
            raw_mode = args[1] if len(args) > 1 else "r"
            writes = (
                any(flag in raw_mode for flag in ("w", "a", "x", "+"))
                if isinstance(raw_mode, str)
                else isinstance(raw_mode, int)
                and bool(
                    raw_mode
                    & (
                        os.O_WRONLY
                        | os.O_RDWR
                        | os.O_APPEND
                        | os.O_CREAT
                        | os.O_TRUNC
                    )
                )
            )
            if writes:
                require_draft_path(raw_path, operation="文件写入")
        if event in {"os.mkdir", "os.remove", "os.rmdir"} and args:
            require_draft_path(args[0], operation=event)
        if event in {"os.rename", "os.replace"} and len(args) >= 2:
            require_draft_path(args[0], operation=event)
            require_draft_path(args[1], operation=event)
        if event in {"os.link", "os.symlink"}:
            raise PermissionError("Builder Worker 禁止创建硬链接或符号链接")

    sys.addaudithook(audit)


def _required(payload: dict[str, Any], key: str, maximum: int) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{key} 无效")
    return value.strip()


def _required_single_line(payload: dict[str, Any], key: str, maximum: int) -> str:
    value = _required(payload, key, maximum)
    if "\n" in value or "\r" in value:
        raise ValueError(f"{key} 不得包含 CR 或 LF")
    return value


def build(payload: dict[str, Any]) -> dict[str, Any]:
    """以模型已结构化的需求生成可移植 Skill 包，但不在 Worker 内执行脚本。"""

    draft_root = Path(_required(payload, "draft_root", 4096)).resolve()
    name = _required(payload, "skill_name", 64)
    display_name = _required_single_line(payload, "display_name", 80)
    description = _required_single_line(payload, "description", 500)
    objective = _required(payload, "objective", 4000)
    steps = payload.get("steps", [])
    references = payload.get("references", [])
    scripts = payload.get("scripts", [])
    if not NAME_PATTERN.fullmatch(name):
        raise ValueError("skill_name 必须是 kebab-case")
    if (
        len(display_name.encode("utf-8")) > 300
        or len(description.encode("utf-8")) > 1_000
        or len(objective.encode("utf-8")) > 4_500
    ):
        raise ValueError("Skill 摘要或目标超过 UTF-8 bytes 限制")
    if not isinstance(steps, list) or len(steps) > 20 or any(
        not isinstance(item, str) or not item.strip() or len(item) > 500 for item in steps
    ):
        raise ValueError("steps 无效")
    if sum(len(item.encode("utf-8")) for item in steps) > 3_500:
        raise ValueError("steps 超过 UTF-8 bytes 限制")
    if not isinstance(references, list) or len(references) > 10:
        raise ValueError("references 无效")
    normalized_references: list[tuple[str, str]] = []
    for item in references:
        if not isinstance(item, dict) or set(item) != {"name", "content"}:
            raise ValueError("reference 格式无效")
        reference_name = item.get("name")
        content = item.get("content")
        if (
            not isinstance(reference_name, str)
            or not NAME_PATTERN.fullmatch(reference_name)
            or not isinstance(content, str)
            or not content.strip()
            or len(content) > 10_000
            or len(content.encode("utf-8")) > 10_000
        ):
            raise ValueError("reference 名称或内容无效")
        normalized_references.append((reference_name, content.strip()))
    if not isinstance(scripts, list) or len(scripts) > 8:
        raise ValueError("scripts 无效")
    normalized_scripts: list[tuple[str, str, str]] = []
    for item in scripts:
        if not isinstance(item, dict) or set(item) != {
            "name",
            "description",
            "content",
            "smoke_input",
        }:
            raise ValueError("script 格式无效")
        script_name = item.get("name")
        script_description = item.get("description")
        content = item.get("content")
        smoke_input = item.get("smoke_input")
        if (
            not isinstance(script_name, str)
            or not SCRIPT_NAME_PATTERN.fullmatch(script_name)
            or not isinstance(script_description, str)
            or not script_description.strip()
            or len(script_description) > 300
            or "\r" in script_description
            or "\n" in script_description
            or not isinstance(content, str)
            or not content.strip()
            or len(content.encode("utf-8")) > 32_000
        ):
            raise ValueError("script 名称、描述或内容无效")
        try:
            smoke_json = json.dumps(
                smoke_input,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("script smoke_input 必须是 JSON 值") from exc
        if len(smoke_json.encode("utf-8")) > 16_000:
            raise ValueError("script smoke_input 超过 16000 bytes")
        normalized_scripts.append(
            (script_name, script_description.strip(), content.rstrip() + "\n")
        )
    if not draft_root.is_dir() or draft_root.is_symlink():
        raise ValueError("draft_root 必须是宿主预先创建的真实目录")
    _install_audit_policy(draft_root)
    workflow = (
        "\n".join(f"{index}. {item.strip()}" for index, item in enumerate(steps, 1))
        if steps
        else "1. 读取当前请求与已加载上下文。\n2. 按目标完成任务，并明确报告无法验证的部分。"
    )
    skill_text = (
        "---\n"
        f"name: {json.dumps(name, ensure_ascii=False)}\n"
        f"description: {json.dumps(description, ensure_ascii=False)}\n"
        "---\n\n"
        f"# {display_name}\n\n"
        "## 目标\n\n"
        f"{objective}\n\n"
        "## 工作流\n\n"
        f"{workflow}\n\n"
        "## 使用边界\n\n"
        "- 只使用宿主本轮明确提供的工具和上下文，不假定未声明的能力。\n"
        "- 涉及外部发送、安装、删除、覆盖或其他副作用时，先遵守宿主确认策略。\n"
        "- 只有工具返回可验证成功结果后，才能声称动作已经完成。\n"
        "- 依赖缺失或结果无法验证时明确说明，不用默认值或假成功掩盖失败。\n"
    )
    if normalized_references:
        skill_text += "\n## 参考资料\n\n" + "\n".join(
            f"- [{reference_name}](references/{reference_name}.md)"
            for reference_name, _ in normalized_references
        )
        skill_text += "\n"
    if normalized_scripts:
        skill_text += (
            "\n## 可复用脚本\n\n"
            "以下脚本只能通过 Skill 工坊的 `run_skill_script` 工具运行。"
            "先加载 `skill-creator`，再传入当前 Skill 名、脚本名和 JSON 输入；"
            "不要用 shell、Python 或其他方式绕开沙箱直接执行。\n\n"
        )
        skill_text += "\n".join(
            f"- `scripts/{script_name}.py`：{script_description}"
            for script_name, script_description, _ in normalized_scripts
        )
        skill_text += (
            "\n\n脚本统一实现 `main(data)`，输入与返回值都必须可序列化为 JSON。"
            "脚本不能读取文件、环境变量或网络，也不能创建子进程。\n"
        )
    agent_text = (
        "interface:\n"
        f"  display_name: {json.dumps(display_name, ensure_ascii=False)}\n"
        f"  short_description: {json.dumps(description[:120], ensure_ascii=False)}\n"
        f"  default_prompt: {json.dumps(f'使用 ${name} 完成对应任务。', ensure_ascii=False)}\n"
    )
    agents_root = draft_root / "agents"
    _mkdir(agents_root)
    _write_text(draft_root / "SKILL.md", skill_text)
    _write_text(agents_root / "openai.yaml", agent_text)
    if normalized_references:
        references_root = draft_root / "references"
        _mkdir(references_root)
        for reference_name, content in normalized_references:
            _write_text(
                references_root / f"{reference_name}.md",
                f"# {reference_name}\n\n{content}\n",
            )
    if normalized_scripts:
        scripts_root = draft_root / "scripts"
        _mkdir(scripts_root)
        for script_name, _, content in normalized_scripts:
            # 固定 LF，确保静态扫描摘要、冻结 manifest 与安装后复检跨平台一致。
            _write_bytes(
                scripts_root / f"{script_name}.py",
                content.encode("utf-8"),
            )
    return {
        "generated": True,
        "artifact_kind": (
            "instruction-and-scripts" if normalized_scripts else "instruction-only"
        ),
    }


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read())
        if not isinstance(payload, dict):
            raise ValueError("输入必须是 JSON 对象")
        result = build(payload)
    except (ValueError, OSError, json.JSONDecodeError, PermissionError) as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
