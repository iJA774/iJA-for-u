"""只读加载项目 `skills/` 下的 SKILL.md。"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from domain.errors import InputValidationError, NotFoundError

SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


@dataclass(frozen=True, slots=True)
class SkillRecord:
    """经启动校验后的 Skill 元数据和正文。"""

    name: str
    description: str
    body: str
    path: Path
    required_scopes: frozenset[str] = frozenset()


class SkillCatalog:
    """启动时合并宿主与插件贡献的 Skill 根目录，不在 Turn 中扫描磁盘。"""

    def __init__(
        self,
        root: Path | Iterable[Path],
        *,
        required_scopes: Mapping[Path, frozenset[str]] | None = None,
    ) -> None:
        roots = [root] if isinstance(root, Path) else list(root)
        if not roots:
            raise InputValidationError("Skill 根目录列表不能为空")
        self.roots = tuple(item.resolve() for item in roots)
        if len(set(self.roots)) != len(self.roots):
            raise InputValidationError("Skill 根目录重复")
        # 保留单根目录时代的公开属性，避免调用方为了多根能力被迫改写。
        self.root = self.roots[0]
        scope_mapping = required_scopes or {}
        self._required_scopes = {
            path.resolve(): frozenset(scopes)
            for path, scopes in scope_mapping.items()
        }
        if set(self._required_scopes) - set(self.roots):
            raise InputValidationError("Skill scope 声明引用了未知根目录")
        self._records = self._scan()

    @property
    def names(self) -> set[str]:
        return set(self._records)

    def get(self, name: str) -> SkillRecord:
        try:
            return self._records[name]
        except KeyError as exc:
            raise NotFoundError(f"未知 Skill: {name}") from exc

    def summary(
        self,
        names: set[str],
        authorization_scopes: frozenset[str] = frozenset(),
    ) -> str:
        """只投影本轮实际可用 Skill 的触发说明。"""

        selected = [
            self._records[name]
            for name in sorted(names)
            if name in self._records
            and self._records[name].required_scopes <= authorization_scopes
        ]
        if not selected:
            return "本轮没有可用 Skill。"
        items = "\n".join(f"- `{item.name}`：{item.description}" for item in selected)
        return (
            "以下 Skill 可按需使用。决定使用后必须先调用 `load_skill` 读取完整指令，"
            f"再调用对应能力：\n{items}"
        )

    def _scan(self) -> dict[str, SkillRecord]:
        records: dict[str, SkillRecord] = {}
        for root in self.roots:
            if not root.exists():
                continue
            if not root.is_dir():
                raise InputValidationError(f"Skill 根路径不是目录: {root}")
            for directory in sorted(root.iterdir(), key=lambda item: item.name):
                if not directory.is_dir():
                    continue
                if not SKILL_NAME_PATTERN.fullmatch(directory.name):
                    raise InputValidationError(f"Skill 目录名无效: {directory.name}")
                skill_file = (directory / "SKILL.md").resolve()
                if root not in skill_file.parents or not skill_file.is_file():
                    raise InputValidationError(f"Skill 缺少 SKILL.md: {directory.name}")
                record = self._parse(
                    skill_file,
                    directory.name,
                    self._required_scopes.get(root, frozenset()),
                )
                if record.name in records:
                    previous = records[record.name]
                    raise InputValidationError(
                        f"Skill 名称重复: {record.name}: {previous.path.parent} / {directory}"
                    )
                records[record.name] = record
        return records

    @staticmethod
    def _parse(
        path: Path,
        directory_name: str,
        required_scopes: frozenset[str],
    ) -> SkillRecord:
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise InputValidationError(f"Skill 无法读取: {directory_name}") from exc
        lines = content.splitlines()
        if not lines or lines[0].strip() != "---":
            raise InputValidationError(f"Skill frontmatter 缺少起始分隔符: {directory_name}")
        try:
            end = next(index for index, line in enumerate(lines[1:], 1) if line.strip() == "---")
        except StopIteration as exc:
            raise InputValidationError(f"Skill frontmatter 缺少结束分隔符: {directory_name}") from exc
        try:
            metadata = yaml.safe_load("\n".join(lines[1:end]))
        except yaml.YAMLError as exc:
            raise InputValidationError(f"Skill frontmatter 不是合法 YAML: {directory_name}") from exc
        if not isinstance(metadata, dict) or set(metadata) != {"name", "description"}:
            raise InputValidationError(
                f"Skill frontmatter 只能包含 name 和 description: {directory_name}"
            )
        name = metadata.get("name")
        description = metadata.get("description")
        if not isinstance(name, str) or not SKILL_NAME_PATTERN.fullmatch(name):
            raise InputValidationError(f"Skill name 无效: {directory_name}")
        if name != directory_name:
            raise InputValidationError(f"Skill name 必须与目录名一致: {directory_name}")
        if not isinstance(description, str) or not description.strip():
            raise InputValidationError(f"Skill description 不能为空: {directory_name}")
        body = "\n".join(lines[end + 1 :]).strip()
        if not body:
            raise InputValidationError(f"Skill 正文不能为空: {directory_name}")
        return SkillRecord(
            name=name,
            description=description.strip(),
            body=body,
            path=path,
            required_scopes=required_scopes,
        )
