"""从任意工作目录校验 iJA Skill 的最小可移植包结构。"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def validate(skill_root: Path) -> dict[str, object]:
    """校验目录名、frontmatter、界面元数据和路径安全。"""

    root = skill_root.resolve()
    if not root.is_dir() or not NAME_PATTERN.fullmatch(root.name):
        raise ValueError("Skill 路径必须指向 kebab-case 命名的目录")
    skill_file = root / "SKILL.md"
    agent_file = root / "agents" / "openai.yaml"
    if not skill_file.is_file() or not agent_file.is_file():
        raise ValueError("Skill 必须包含 SKILL.md 和 agents/openai.yaml")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("Skill 包不得包含符号链接")
    content = skill_file.read_text(encoding="utf-8")
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("SKILL.md 缺少 frontmatter 起始分隔符")
    try:
        end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
    except StopIteration as exc:
        raise ValueError("SKILL.md frontmatter 未闭合") from exc
    metadata: dict[str, str] = {}
    for line in lines[1:end]:
        key, separator, raw_value = line.partition(":")
        key = key.strip()
        if not separator or key not in {"name", "description"}:
            raise ValueError("frontmatter 只能包含 name 和 description")
        if key in metadata:
            raise ValueError("frontmatter 字段不得重复")
        try:
            value = json.loads(raw_value.strip())
        except json.JSONDecodeError:
            value = raw_value.strip()
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"frontmatter {key} 不能为空")
        metadata[key] = value
    if set(metadata) != {"name", "description"} or metadata["name"] != root.name:
        raise ValueError("frontmatter 字段不完整或 name 与目录不一致")
    if not "\n".join(lines[end + 1 :]).strip():
        raise ValueError("SKILL.md 正文不能为空")
    agent_content = agent_file.read_text(encoding="utf-8")
    for field in ("interface:", "display_name:", "short_description:", "default_prompt:"):
        if field not in agent_content:
            raise ValueError(f"agents/openai.yaml 缺少字段: {field}")
    files = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    )
    return {"valid": True, "name": root.name, "files": files}


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 iJA Skill 可移植包")
    parser.add_argument("skill_dir", type=Path)
    arguments = parser.parse_args()
    try:
        result = validate(arguments.skill_dir)
    except (ValueError, OSError, UnicodeError) as exc:
        sys.stderr.write(f"校验失败: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
