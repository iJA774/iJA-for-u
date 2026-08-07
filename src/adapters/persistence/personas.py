"""以 TOML 身份配置和 Markdown Prompt 保存人格权威数据。"""

from __future__ import annotations

import hashlib
import re
import tomllib
from datetime import datetime
from pathlib import Path
from threading import RLock

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from tomli_w import dumps

from domain.errors import ConflictError, InputValidationError, NotFoundError
from domain.models import Persona, PersonaPortrait, utc_now
from imaging import inspect_dimensions, is_nine_sixteen, matches_image_signature


class PersonaFileConfig(BaseModel):
    """磁盘人格配置；角色表达正文由同名 Markdown 文件独立保存。"""

    character_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=80)
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    portrait: PersonaPortrait | None = None
    revision: int = Field(ge=1)
    updated_at: datetime


class ActivePersonaConfig(BaseModel):
    """数据目录中的人格选择；保留旧字段仅用于一次性读取迁移。"""

    model_config = ConfigDict(extra="forbid")

    active_character_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_-]{0,63}$",
    )
    private_character_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_-]{0,63}$",
    )
    group_character_id: str | None = Field(
        default=None,
        pattern=r"^[a-z][a-z0-9_-]{0,63}$",
    )


class PersonaStore:
    """人格文件的唯一读写 owner，并验证 Prompt 与形象引用没有漂移。"""

    def __init__(self, *, project_root: Path, data_root: Path, active_character_id: str) -> None:
        self.config_root = (project_root / "config" / "personas").resolve()
        self.prompt_root = (project_root / "prompts" / "persona").resolve()
        self.data_root = (data_root / "personas").resolve()
        self.active_config_path = self.data_root / "active.toml"
        self.active_character_id = active_character_id
        self.private_character_id = active_character_id
        self.group_character_id = active_character_id
        self._lock = RLock()

    def initialize(self) -> Persona:
        """启动时加载持久化选择并完整校验所有人格。"""

        with self._lock:
            self.data_root.mkdir(parents=True, exist_ok=True)
            (
                self.active_character_id,
                self.private_character_id,
                self.group_character_id,
            ) = self._read_selection()
            personas = self.list()
            available_ids = {persona.character_id for persona in personas}
            for scene, character_id in (
                ("编辑", self.active_character_id),
                ("私聊", self.private_character_id),
                ("群聊", self.group_character_id),
            ):
                if character_id not in available_ids:
                    raise NotFoundError(f"{scene}人格不存在: {character_id}")
            return self.get_active()

    def list(self) -> list[Persona]:
        """列出配置目录中所有完整且通过摘要校验的人格。"""

        with self._lock:
            if not self.config_root.is_dir():
                raise NotFoundError("人格配置目录不存在")
            character_ids = sorted(path.stem for path in self.config_root.glob("*.toml"))
            if not character_ids:
                raise NotFoundError("没有可用人格")
            return [self.get(character_id) for character_id in character_ids]

    def get_active(self) -> Persona:
        """读取控制台当前编辑的人格；聊天链路应使用 get_for_chat_type。"""

        return self.get(self.active_character_id)

    def activate(self, character_id: str) -> Persona:
        """兼容旧控制台：切换编辑人格，并同时指派给私聊和群聊。"""

        self._validate_character_id(character_id)
        with self._lock:
            persona = self.get(character_id)
            self.active_character_id = character_id
            self.private_character_id = character_id
            self.group_character_id = character_id
            self._persist_selection()
            return persona

    def select_for_chat_type(self, chat_type: str, character_id: str) -> Persona:
        """原子更新私聊或群聊的人格指派，不影响另一场景。"""

        if chat_type not in {"private", "group"}:
            raise InputValidationError("chat_type 只能是 private 或 group")
        self._validate_character_id(character_id)
        with self._lock:
            persona = self.get(character_id)
            if chat_type == "private":
                self.private_character_id = character_id
            else:
                self.group_character_id = character_id
            self._persist_selection()
            return persona

    def assign(self, *, private: str, group: str) -> dict[str, str]:
        """校验后一次性持久化私聊与群聊人格指派。"""

        self._validate_character_id(private)
        self._validate_character_id(group)
        with self._lock:
            self.get(private)
            self.get(group)
            previous = (self.private_character_id, self.group_character_id)
            self.private_character_id = private
            self.group_character_id = group
            try:
                self._persist_selection()
            except OSError:
                self.private_character_id, self.group_character_id = previous
                raise
            return self.assignments()

    def get_for_chat_type(self, chat_type: object) -> Persona:
        """按会话类型读取人格，避免跨群聊/私聊共享可变全局选择。"""

        value = getattr(chat_type, "value", chat_type)
        if value == "private":
            return self.get(self.private_character_id)
        if value == "group":
            return self.get(self.group_character_id)
        raise InputValidationError("chat_type 只能是 private 或 group")

    def assignments(self) -> dict[str, str]:
        """返回 WebUI 可安全展示的私聊/群聊人格指派。"""

        return {
            "private": self.private_character_id,
            "group": self.group_character_id,
        }

    def get(self, character_id: str) -> Persona:
        """按安全的角色 ID 读取人格。"""

        self._validate_character_id(character_id)
        with self._lock:
            config_path = self.config_root / f"{character_id}.toml"
            prompt_path = self.prompt_root / f"{character_id}.md"
            if not config_path.is_file():
                raise NotFoundError(f"人格配置不存在: {character_id}")
            if not prompt_path.is_file():
                raise NotFoundError(f"人格提示词不存在: {character_id}")
            try:
                with config_path.open("rb") as file:
                    config = PersonaFileConfig.model_validate(tomllib.load(file))
            except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
                raise InputValidationError(f"人格配置损坏: {character_id}") from exc
            if config.character_id != character_id:
                raise InputValidationError("人格文件名与 character_id 不一致")
            try:
                prompt_bytes = prompt_path.read_bytes()
                prompt = prompt_bytes.decode("utf-8").strip()
            except (OSError, UnicodeError) as exc:
                raise InputValidationError(f"人格提示词无法读取: {character_id}") from exc
            digest = self._prompt_digest(prompt_bytes)
            if digest != config.prompt_sha256:
                raise InputValidationError(f"人格提示词摘要不匹配: {character_id}")
            if config.portrait is not None:
                config.portrait = self._validate_portrait(character_id, config.portrait)
            try:
                return Persona(
                    character_id=config.character_id,
                    name=config.name,
                    persona_prompt=prompt,
                    prompt_sha256=config.prompt_sha256,
                    portrait=config.portrait,
                    revision=config.revision,
                    updated_at=config.updated_at,
                )
            except ValidationError as exc:
                raise InputValidationError(f"人格内容无效: {character_id}") from exc

    def update(
        self,
        *,
        name: str,
        persona_prompt: str,
        expected_revision: int,
        character_id: str | None = None,
    ) -> Persona:
        """原子替换指定人格的名称与 Prompt，并使用 revision 防止覆盖。"""

        with self._lock:
            current = (
                self.get(character_id)
                if character_id is not None
                else self.get_active()
            )
            self._require_revision(current, expected_revision)
            try:
                candidate = Persona(
                    character_id=current.character_id,
                    name=name,
                    persona_prompt=persona_prompt,
                    prompt_sha256="0" * 64,
                    portrait=current.portrait,
                    revision=current.revision + 1,
                    updated_at=utc_now(),
                )
            except ValidationError as exc:
                raise InputValidationError("人格名称或提示词无效") from exc
            prompt_bytes = (candidate.persona_prompt + "\n").encode("utf-8")
            digest = self._prompt_digest(prompt_bytes)
            candidate.prompt_sha256 = digest
            self._replace_persona_files(candidate, prompt_bytes=prompt_bytes)
            return self.get(current.character_id)

    def update_portrait(
        self,
        portrait: PersonaPortrait | None,
        *,
        expected_revision: int,
        current_snapshot: Persona | None = None,
    ) -> Persona:
        """更新形象引用；覆盖同名 base_image 时使用写入前的受控快照。"""

        with self._lock:
            current = current_snapshot or self.get_active()
            self._require_revision(current, expected_revision)
            if portrait is not None:
                portrait = self._validate_portrait(current.character_id, portrait)
            updated = current.model_copy(
                update={
                    "portrait": portrait,
                    "revision": current.revision + 1,
                    "updated_at": utc_now(),
                }
            )
            self._replace_config(updated)
            return updated

    def restore_snapshot(self, snapshot: Persona, *, expected_revision: int) -> Persona:
        """仅供跨存储更新失败时恢复写入前快照，不制造虚假的新 revision。"""

        with self._lock:
            config_path = self.config_root / f"{snapshot.character_id}.toml"
            try:
                with config_path.open("rb") as file:
                    current_config = PersonaFileConfig.model_validate(tomllib.load(file))
            except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
                raise InputValidationError("待恢复的人格配置损坏") from exc
            if current_config.revision != expected_revision:
                raise ConflictError(
                    f"人格已被其他请求更新，当前 revision={current_config.revision}"
                )
            if snapshot.character_id != current_config.character_id:
                raise InputValidationError("恢复快照不属于当前启用角色")
            self._replace_config(snapshot)
            return snapshot

    def clear_all_portraits(self) -> dict[str, int]:
        """解除所有人格形象引用；人格名称、Prompt 与场景指派仍是应用配置。"""

        with self._lock:
            originals = [persona for persona in self.list() if persona.portrait is not None]
            updated = [
                persona.model_copy(
                    update={
                        "portrait": None,
                        "revision": persona.revision + 1,
                        "updated_at": utc_now(),
                    }
                )
                for persona in originals
            ]
            applied: list[Persona] = []
            try:
                for original, candidate in zip(originals, updated, strict=True):
                    self._replace_config(candidate)
                    applied.append(original)
            except BaseException:
                rollback_failures: list[Exception] = []
                for original in reversed(applied):
                    try:
                        self._replace_config(original)
                    except Exception as exc:
                        rollback_failures.append(exc)
                if rollback_failures:
                    raise RuntimeError(
                        "清空人格形象引用失败，且配置回滚不完整"
                    ) from rollback_failures[0]
                raise
            return {"portrait_reference_count": len(updated)}

    def _replace_persona_files(self, persona: Persona, *, prompt_bytes: bytes) -> None:
        prompt_path = self.prompt_root / f"{persona.character_id}.md"
        config_path = self.config_root / f"{persona.character_id}.toml"
        prompt_tmp = prompt_path.with_suffix(".md.tmp")
        config_tmp = config_path.with_suffix(".toml.tmp")
        try:
            prompt_tmp.write_bytes(prompt_bytes)
            config_tmp.write_text(self._serialize_config(persona), encoding="utf-8")
            prompt_tmp.replace(prompt_path)
            config_tmp.replace(config_path)
        finally:
            prompt_tmp.unlink(missing_ok=True)
            config_tmp.unlink(missing_ok=True)

    def _replace_config(self, persona: Persona) -> None:
        path = self.config_root / f"{persona.character_id}.toml"
        temporary_path = path.with_suffix(".toml.tmp")
        try:
            temporary_path.write_text(self._serialize_config(persona), encoding="utf-8")
            temporary_path.replace(path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _read_selection(self) -> tuple[str, str, str]:
        if not self.active_config_path.exists():
            return (
                self.active_character_id,
                self.private_character_id,
                self.group_character_id,
            )
        try:
            with self.active_config_path.open("rb") as file:
                config = ActivePersonaConfig.model_validate(tomllib.load(file))
        except (OSError, tomllib.TOMLDecodeError, ValidationError) as exc:
            raise InputValidationError("当前人格选择配置损坏") from exc
        legacy = config.active_character_id
        private_character_id = config.private_character_id or legacy
        group_character_id = config.group_character_id or legacy
        if private_character_id is None or group_character_id is None:
            raise InputValidationError("人格选择必须同时配置 private 与 group")
        editor_character_id = legacy or private_character_id
        return editor_character_id, private_character_id, group_character_id

    def _persist_selection(self) -> None:
        """原子持久化编辑目标及两个聊天场景的人格指派。"""

        self.data_root.mkdir(parents=True, exist_ok=True)
        temporary_path = self.active_config_path.with_suffix(".toml.tmp")
        try:
            temporary_path.write_text(
                dumps(
                    {
                        "active_character_id": self.active_character_id,
                        "private_character_id": self.private_character_id,
                        "group_character_id": self.group_character_id,
                    }
                ),
                encoding="utf-8",
            )
            temporary_path.replace(self.active_config_path)
        finally:
            temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _serialize_config(persona: Persona) -> str:
        config = PersonaFileConfig(
            character_id=persona.character_id,
            name=persona.name,
            prompt_sha256=persona.prompt_sha256,
            portrait=persona.portrait,
            revision=persona.revision,
            updated_at=persona.updated_at,
        )
        return dumps(config.model_dump(mode="python", exclude_none=True))

    @staticmethod
    def _prompt_digest(prompt_bytes: bytes) -> str:
        """计算与操作系统换行符无关的人格 Prompt 摘要。"""

        canonical_bytes = prompt_bytes.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
        return hashlib.sha256(canonical_bytes).hexdigest()

    def _validate_portrait(
        self, character_id: str, portrait: PersonaPortrait
    ) -> PersonaPortrait:
        """校验形象文件并补齐旧配置缺少的尺寸状态。"""

        persona_root = (self.data_root / character_id).resolve()
        target = Path(portrait.storage_path).resolve()
        if persona_root not in target.parents or not target.is_file():
            raise InputValidationError("人格形象不存在或路径不受控制")
        try:
            content = target.read_bytes()
        except OSError as exc:
            raise InputValidationError("人格形象无法读取") from exc
        if len(content) != portrait.size:
            raise InputValidationError("人格形象大小与配置不一致")
        if hashlib.sha256(content).hexdigest() != portrait.sha256:
            raise InputValidationError("人格形象摘要与配置不一致")
        if not matches_image_signature(portrait.mime_type, content):
            raise InputValidationError("人格形象内容与配置 MIME 不一致")
        width, height = inspect_dimensions(content)
        return portrait.model_copy(
            update={
                "filename": portrait.filename or Path(portrait.storage_path).name,
                "width": width,
                "height": height,
                "aspect_valid": is_nine_sixteen(width, height),
            }
        )

    @staticmethod
    def _require_revision(persona: Persona, expected_revision: int) -> None:
        if persona.revision != expected_revision:
            raise ConflictError(f"人格已被其他请求更新，当前 revision={persona.revision}")

    @staticmethod
    def _validate_character_id(character_id: str) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", character_id) is None:
            raise InputValidationError("character_id 格式无效")
