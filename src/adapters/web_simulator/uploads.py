"""受控附件、人物形象与历史媒体存储。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

from domain.errors import InputValidationError
from domain.models import ComponentType, ExpressionAsset, MessageComponent, PersonaPortrait, new_id
from imaging import matches_image_signature, normalize_static_png


@dataclass(frozen=True, slots=True)
class StoredExpressionImage:
    """规范化后表情源文件的持久化元数据。"""

    storage_path: str
    mime_type: str
    size: int
    sha256: str
    width: int
    height: int


class AttachmentStore:
    """唯一拥有聊天附件、形象和可见历史媒体写入副作用的组件。"""

    _CONTENT_ADDRESSED_FILE_RE = re.compile(r"^[0-9a-f]{64}\.[a-z0-9]+$")
    ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    ALLOWED_AUDIO_TYPES = {
        "audio/amr",
        "audio/mpeg",
        "audio/mp4",
        "audio/ogg",
        "audio/wav",
        "audio/x-wav",
    }
    ALLOWED_FILE_TYPES = {
        "application/json",
        "application/octet-stream",
        "application/pdf",
        "application/zip",
        "text/plain",
    }

    def __init__(
        self,
        *,
        uploads_root: Path,
        personas_root: Path,
        max_bytes: int,
        media_root: Path | None = None,
    ) -> None:
        self.root = uploads_root.resolve()
        self.personas_root = personas_root.resolve()
        self.media_root = (media_root or uploads_root.parent / "media").resolve()
        self.max_bytes = max_bytes
        self.root.mkdir(parents=True, exist_ok=True)
        self.personas_root.mkdir(parents=True, exist_ok=True)
        self.media_root.mkdir(parents=True, exist_ok=True)

    def save_image(self, filename: str, mime_type: str, content: bytes) -> MessageComponent:
        """保存普通消息图片并返回可校验的消息组件。"""

        component = self.save_attachment(filename, mime_type, content)
        if component.type != ComponentType.IMAGE_REF:
            raise InputValidationError("声明的内容不是图片")
        return component

    def save_attachment(
        self, filename: str, mime_type: str, content: bytes
    ) -> MessageComponent:
        """保存图片、语音或普通文件并返回内容寻址组件。"""

        component_type = self._validate_attachment(mime_type, content)
        digest = hashlib.sha256(content).hexdigest()
        suffix = self._suffix_for(mime_type)
        target = (self.root / f"{digest}{suffix}").resolve()
        if self.root not in target.parents:
            raise InputValidationError("附件路径越界")
        if not target.exists():
            self._atomic_write(target, content)
        return MessageComponent(
            type=component_type,
            attachment_id=new_id("attachment"),
            filename=Path(filename).name,
            mime_type=mime_type,
            size=len(content),
            sha256=digest,
            storage_path=str(target),
        )

    def save_persona_portrait(
        self,
        character_id: str,
        mime_type: str,
        content: bytes,
        *,
        crop_to_nine_sixteen: bool = False,
    ) -> PersonaPortrait:
        """将上传形象规范化为精确 9:16 的 `base_image.png`。"""

        self._validate_character_id(character_id)
        self._validate_image(mime_type, content)
        normalized = normalize_static_png(
            content,
            crop_to_nine_sixteen=crop_to_nine_sixteen,
            aspect_error=True,
            max_output_bytes=self.max_bytes,
        )
        persona_root = (self.personas_root / character_id).resolve()
        if self.personas_root not in persona_root.parents:
            raise InputValidationError("人格形象目录越界")
        persona_root.mkdir(parents=True, exist_ok=True)
        target = (persona_root / "base_image.png").resolve()
        temporary_path = persona_root / ".base_image.png.tmp"
        try:
            temporary_path.write_bytes(normalized.content)
            temporary_path.replace(target)
        finally:
            temporary_path.unlink(missing_ok=True)
        digest = hashlib.sha256(normalized.content).hexdigest()
        return PersonaPortrait(
            storage_path=str(target),
            filename="base_image.png",
            sha256=digest,
            mime_type="image/png",
            size=len(normalized.content),
            width=normalized.width,
            height=normalized.height,
            aspect_valid=True,
        )

    def restore_persona_portrait(self, character_id: str, content: bytes | None) -> None:
        """人格配置更新失败时恢复被替换的旧文件。"""

        self._validate_character_id(character_id)
        target = (self.personas_root / character_id / "base_image.png").resolve()
        if self.personas_root not in target.parents:
            raise InputValidationError("人格形象路径越界")
        if content is None:
            target.unlink(missing_ok=True)
            return
        temporary_path = target.with_suffix(".png.restore.tmp")
        try:
            temporary_path.write_bytes(content)
            temporary_path.replace(target)
        finally:
            temporary_path.unlink(missing_ok=True)

    def save_expression_source(
        self, character_id: str, filename: str, content: bytes
    ) -> StoredExpressionImage:
        """规范化并原子保存表情源文件。"""

        self._validate_character_id(character_id)
        if not content or len(content) > self.max_bytes:
            raise InputValidationError("图片模型返回的图片为空或超过大小上限")
        normalized = normalize_static_png(
            content,
            crop_to_nine_sixteen=False,
            aspect_error=False,
            max_output_bytes=self.max_bytes,
            enforce_nine_sixteen=False,
        )
        expressions_root = (self.personas_root / character_id / "expressions").resolve()
        if self.personas_root not in expressions_root.parents:
            raise InputValidationError("表情目录越界")
        expressions_root.mkdir(parents=True, exist_ok=True)
        target = (expressions_root / filename).resolve()
        if expressions_root not in target.parents or target.suffix.lower() != ".png":
            raise InputValidationError("表情文件名无效")
        temporary_path = target.with_suffix(".png.tmp")
        try:
            temporary_path.write_bytes(normalized.content)
            temporary_path.replace(target)
        finally:
            temporary_path.unlink(missing_ok=True)
        return StoredExpressionImage(
            storage_path=str(target),
            mime_type="image/png",
            size=len(normalized.content),
            sha256=hashlib.sha256(normalized.content).hexdigest(),
            width=normalized.width,
            height=normalized.height,
        )

    def publish_expression(self, asset: ExpressionAsset) -> MessageComponent:
        """把可复用源素材发布为历史消息使用的内容寻址副本。"""

        source = Path(asset.storage_path).resolve()
        persona_root = (self.personas_root / asset.character_id / "expressions").resolve()
        if persona_root not in source.parents or not source.is_file():
            raise InputValidationError("表情源文件不存在或路径不受控制")
        content = source.read_bytes()
        self._validate_asset_content(asset, content)
        target = (self.media_root / f"{asset.sha256}.png").resolve()
        if self.media_root not in target.parents:
            raise InputValidationError("历史媒体路径越界")
        if target.exists():
            published = target.read_bytes()
            if (
                len(published) != asset.size
                or hashlib.sha256(published).hexdigest() != asset.sha256
                or not matches_image_signature("image/png", published)
            ):
                self._atomic_write(target, content)
        else:
            self._atomic_write(target, content)
        self._validate_asset_content(asset, target.read_bytes())
        return MessageComponent(
            type=ComponentType.IMAGE_REF,
            attachment_id=asset.id,
            filename=f"{asset.name}.png",
            mime_type="image/png",
            size=asset.size,
            sha256=asset.sha256,
            storage_path=str(target),
            description=f"角色表情「{asset.name}」：{asset.emotion}",
            is_expression=True,
        )

    def remove_expression_source(self, asset: ExpressionAsset) -> None:
        """删除图库源素材，不触碰已经发布到历史媒体目录的副本。"""

        target = Path(asset.storage_path).resolve()
        expression_root = (self.personas_root / asset.character_id / "expressions").resolve()
        if expression_root not in target.parents:
            raise InputValidationError("表情源文件路径越界")
        target.unlink(missing_ok=True)

    def validate_expression_source(self, asset: ExpressionAsset) -> Path:
        """为图库预览验证源文件的目录、摘要与 MIME。"""

        target = Path(asset.storage_path).resolve()
        expression_root = (self.personas_root / asset.character_id / "expressions").resolve()
        if expression_root not in target.parents or not target.is_file():
            raise InputValidationError("表情源文件不存在或路径不受控制")
        self._validate_asset_content(asset, target.read_bytes())
        return target

    def remove_persona_portrait(self, portrait: PersonaPortrait) -> None:
        """删除已解除引用的人格形象。"""

        target = Path(portrait.storage_path).resolve()
        if self.personas_root not in target.parents:
            raise InputValidationError("人格形象路径越界")
        target.unlink(missing_ok=True)

    def validate_image_ref(self, component: MessageComponent) -> None:
        """校验入站引用确实指向上传目录内的受控内容。"""

        if component.type != ComponentType.IMAGE_REF:
            raise InputValidationError("图片校验器只接受 image_ref 组件")
        self._validate_component_in_roots(component, (self.root,))

    def read_image_ref(self, component: MessageComponent) -> bytes:
        """重新校验并读取入站图片，供视觉模型内联使用。"""

        if component.type != ComponentType.IMAGE_REF:
            raise InputValidationError("图片读取器只接受 image_ref 组件")
        target = self._validate_component_in_roots(component, (self.root,))
        return target.read_bytes()

    def validate_attachment_ref(self, component: MessageComponent) -> None:
        """校验任意入站附件确实指向上传目录内的受控内容。"""

        self._validate_component_in_roots(component, (self.root,))

    def read_attachment_ref(self, component: MessageComponent) -> bytes:
        """重新校验并读取任意受控聊天附件。"""

        return self._validate_component_in_roots(component, (self.root,)).read_bytes()

    def validate_history_image(self, component: MessageComponent) -> Path:
        """校验消息展示图片只来自上传或历史媒体目录。"""

        if component.type != ComponentType.IMAGE_REF:
            raise InputValidationError("图片展示接口只接受 image_ref 组件")
        return self._validate_component_in_roots(component, (self.root, self.media_root))

    def validate_history_attachment(self, component: MessageComponent) -> Path:
        """校验历史附件只来自上传或历史媒体目录。"""

        return self._validate_component_in_roots(component, (self.root, self.media_root))

    def garbage_collect_history_files(
        self,
        candidate_paths: set[str],
        referenced_paths: set[str],
    ) -> dict[str, int]:
        """删除本次被清正文独占的内容寻址附件，不触碰未提交上传。"""

        referenced = {
            os.path.normcase(str(Path(value).resolve()))
            for value in referenced_paths
        }
        deleted_count = 0
        deleted_bytes = 0
        for raw_path in candidate_paths:
            path = Path(raw_path).resolve()
            if not any(root in path.parents for root in (self.root, self.media_root)):
                raise InputValidationError("待回收媒体路径不属于受控历史目录")
            if not self._CONTENT_ADDRESSED_FILE_RE.fullmatch(path.name):
                raise InputValidationError("待回收媒体不是内容寻址文件")
            if os.path.normcase(str(path)) in referenced or not path.exists():
                continue
            if path.is_symlink() or not path.is_file():
                raise InputValidationError("待回收媒体不是普通文件")
            size = path.stat().st_size
            path.unlink()
            deleted_count += 1
            deleted_bytes += size
        return {
            "media_file_count": deleted_count,
            "media_bytes": deleted_bytes,
        }

    def purge_all_user_media(self) -> dict[str, int]:
        """删除上传、历史媒体及人格图片；保留无隐私正文的 active.toml。"""

        deleted_count = 0
        deleted_bytes = 0
        for root in (self.root, self.media_root, self.personas_root):
            for path in self._owned_files(root):
                if (
                    root == self.personas_root
                    and path.resolve() == (self.personas_root / "active.toml").resolve()
                ):
                    continue
                size = path.stat().st_size
                path.unlink()
                deleted_count += 1
                deleted_bytes += size
            for directory in sorted(
                (item for item in root.rglob("*") if item.is_dir()),
                key=lambda item: len(item.parts),
                reverse=True,
            ):
                directory.rmdir()
        return {
            "media_file_count": deleted_count,
            "media_bytes": deleted_bytes,
        }

    @staticmethod
    def _owned_files(root: Path) -> list[Path]:
        """枚举受控根内普通文件，遇到链接时拒绝继续删除。"""

        files: list[Path] = []
        for path in root.rglob("*"):
            if path.is_symlink():
                raise InputValidationError(f"受控媒体目录包含符号链接: {path.name}")
            if path.is_file():
                files.append(path)
        return files

    def _validate_component_in_roots(
        self, component: MessageComponent, roots: tuple[Path, ...]
    ) -> Path:
        if component.type not in {
            ComponentType.IMAGE_REF,
            ComponentType.AUDIO_REF,
            ComponentType.FILE_REF,
        }:
            raise InputValidationError("附件校验器只接受受控附件组件")
        target = Path(component.storage_path or "").resolve()
        if not any(root in target.parents for root in roots) or not target.is_file():
            raise InputValidationError("附件引用不存在或路径不受控制")
        content = target.read_bytes()
        if len(content) > self.max_bytes or len(content) != component.size:
            raise InputValidationError("附件引用大小与已保存内容不一致")
        if hashlib.sha256(content).hexdigest() != component.sha256:
            raise InputValidationError("附件引用摘要与已保存内容不一致")
        if self._validate_attachment(component.mime_type or "", content) != component.type:
            raise InputValidationError("附件引用类型与已保存内容不一致")
        return target

    def _validate_image(self, mime_type: str, content: bytes) -> None:
        if mime_type not in self.ALLOWED_IMAGE_TYPES:
            raise InputValidationError(f"不支持的图片类型: {mime_type}")
        if not content:
            raise InputValidationError("图片文件不能为空")
        if len(content) > self.max_bytes:
            raise InputValidationError(f"图片超过上限 {self.max_bytes} 字节")
        if not matches_image_signature(mime_type, content):
            raise InputValidationError("图片内容与声明的 MIME 类型不一致")

    def _validate_attachment(self, mime_type: str, content: bytes) -> ComponentType:
        if not content:
            raise InputValidationError("附件文件不能为空")
        if len(content) > self.max_bytes:
            raise InputValidationError(f"附件超过上限 {self.max_bytes} 字节")
        if mime_type in self.ALLOWED_IMAGE_TYPES:
            self._validate_image(mime_type, content)
            return ComponentType.IMAGE_REF
        if mime_type in self.ALLOWED_AUDIO_TYPES:
            if not self._matches_audio_signature(mime_type, content):
                raise InputValidationError("语音内容与声明的 MIME 类型不一致")
            return ComponentType.AUDIO_REF
        if mime_type in self.ALLOWED_FILE_TYPES:
            self._validate_file_signature(mime_type, content)
            return ComponentType.FILE_REF
        raise InputValidationError(f"不支持的附件类型: {mime_type}")

    @staticmethod
    def _matches_audio_signature(mime_type: str, content: bytes) -> bool:
        if mime_type == "audio/mpeg":
            return content.startswith(b"ID3") or (
                len(content) >= 2 and content[0] == 0xFF and content[1] & 0xE0 == 0xE0
            )
        if mime_type == "audio/ogg":
            return content.startswith(b"OggS")
        if mime_type in {"audio/wav", "audio/x-wav"}:
            return len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WAVE"
        if mime_type == "audio/amr":
            return content.startswith(b"#!AMR\n")
        if mime_type == "audio/mp4":
            return len(content) >= 12 and content[4:8] == b"ftyp"
        return False

    @staticmethod
    def _validate_file_signature(mime_type: str, content: bytes) -> None:
        if mime_type == "application/pdf" and not content.startswith(b"%PDF-"):
            raise InputValidationError("PDF 内容与声明的 MIME 类型不一致")
        if mime_type == "application/zip" and not content.startswith(
            (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
        ):
            raise InputValidationError("ZIP 内容与声明的 MIME 类型不一致")
        if mime_type in {"text/plain", "application/json"}:
            if b"\x00" in content:
                raise InputValidationError("文本附件包含 NUL 字节")
            try:
                decoded = content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise InputValidationError("文本附件必须使用 UTF-8") from exc
            if mime_type == "application/json":
                try:
                    json.loads(decoded)
                except json.JSONDecodeError as exc:
                    raise InputValidationError("JSON 附件内容无效") from exc

    @staticmethod
    def _validate_asset_content(asset: ExpressionAsset, content: bytes) -> None:
        if len(content) != asset.size or hashlib.sha256(content).hexdigest() != asset.sha256:
            raise InputValidationError("表情源文件大小或摘要与记录不一致")
        if not matches_image_signature(asset.mime_type, content):
            raise InputValidationError("表情源文件与 MIME 类型不一致")

    @staticmethod
    def _atomic_write(target: Path, content: bytes) -> None:
        """在目标目录落临时文件并原子替换，避免历史媒体出现半写文件。"""

        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary:
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
                temporary_path = Path(temporary.name)
            temporary_path.replace(target)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @staticmethod
    def _suffix_for(mime_type: str) -> str:
        return {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "audio/amr": ".amr",
            "audio/mpeg": ".mp3",
            "audio/mp4": ".m4a",
            "audio/ogg": ".ogg",
            "audio/wav": ".wav",
            "audio/x-wav": ".wav",
            "application/json": ".json",
            "application/octet-stream": ".bin",
            "application/pdf": ".pdf",
            "application/zip": ".zip",
            "text/plain": ".txt",
        }[mime_type]

    @staticmethod
    def _validate_character_id(character_id: str) -> None:
        if re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", character_id) is None:
            raise InputValidationError("character_id 格式无效")
