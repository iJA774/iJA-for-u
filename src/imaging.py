"""图片解码、9:16 规范化与安全校验。"""

from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO

from PIL import Image, ImageOps, UnidentifiedImageError

from domain.errors import InputValidationError, PortraitAspectRatioError

MAX_IMAGE_PIXELS = 40_000_000
Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS


@dataclass(frozen=True, slots=True)
class NormalizedImage:
    """去元数据后的静态 PNG。"""

    content: bytes
    width: int
    height: int


def matches_image_signature(mime_type: str, content: bytes) -> bool:
    """验证当前允许格式的文件魔数。"""

    signatures = {
        "image/jpeg": content.startswith(b"\xff\xd8\xff"),
        "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/gif": content.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": content.startswith(b"RIFF") and content[8:12] == b"WEBP",
    }
    return signatures.get(mime_type, False)


def is_nine_sixteen(width: int, height: int) -> bool:
    """使用整数等式判断精确 9:16，避免浮点误差。"""

    return width * 16 == height * 9


def normalize_static_png(
    content: bytes,
    *,
    crop_to_nine_sixteen: bool,
    aspect_error: bool,
    max_output_bytes: int,
    max_nine_sixteen_size: tuple[int, int] | None = None,
    enforce_nine_sixteen: bool = True,
) -> NormalizedImage:
    """应用方向、取首帧并规范化为静态 PNG；角色形象可选择强制 9:16。"""

    try:
        with Image.open(BytesIO(content)) as source:
            source.seek(0)
            source_width, source_height = source.size
            if (
                source_width <= 0
                or source_height <= 0
                or source_width * source_height > MAX_IMAGE_PIXELS
            ):
                raise InputValidationError("图片尺寸无效或像素数量超出安全上限")
            frame = ImageOps.exif_transpose(source.copy())
    except InputValidationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise InputValidationError("图片无法解码或像素数量超出安全上限") from exc

    width, height = frame.size
    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        raise InputValidationError("图片尺寸无效或像素数量超出安全上限")
    if enforce_nine_sixteen and not is_nine_sixteen(width, height):
        if not crop_to_nine_sixteen:
            if aspect_error:
                raise PortraitAspectRatioError(
                    "人物形象必须为 9:16；请修改图片或选择自动居中裁剪",
                    details={"width": width, "height": height, "target_ratio": "9:16"},
                )
            raise InputValidationError("生成图片不是 9:16")
        target_width = min(width, height * 9 // 16)
        target_height = min(height, width * 16 // 9)
        target_width -= target_width % 9
        target_height -= target_height % 16
        if target_width <= 0 or target_height <= 0:
            raise InputValidationError("图片尺寸过小，无法裁剪为 9:16")
        left = (width - target_width) // 2
        top = (height - target_height) // 2
        frame = frame.crop((left, top, left + target_width, top + target_height))

    if max_nine_sixteen_size is not None:
        max_width, max_height = max_nine_sixteen_size
        if not is_nine_sixteen(max_width, max_height):
            raise ValueError("最大输出尺寸必须是 9:16")
        if frame.width > max_width or frame.height > max_height:
            unit = min(frame.width // 9, frame.height // 16, max_width // 9, max_height // 16)
            if unit <= 0:
                raise InputValidationError("图片尺寸过小，无法缩放为 9:16")
            frame = frame.resize((unit * 9, unit * 16), Image.Resampling.LANCZOS)

    if frame.mode not in {"RGB", "RGBA"}:
        frame = frame.convert("RGBA" if "A" in frame.getbands() else "RGB")
    output = BytesIO()
    frame.save(output, format="PNG", optimize=True)
    encoded = output.getvalue()
    if len(encoded) > max_output_bytes:
        raise InputValidationError(f"规范化后的 PNG 超过上限 {max_output_bytes} 字节")
    final_width, final_height = frame.size
    if enforce_nine_sixteen and not is_nine_sixteen(final_width, final_height):
        raise InputValidationError("图片规范化后仍不是精确 9:16")
    return NormalizedImage(encoded, final_width, final_height)


def inspect_dimensions(content: bytes) -> tuple[int, int]:
    """安全读取图片首帧经 EXIF 修正后的尺寸。"""

    try:
        with Image.open(BytesIO(content)) as source:
            source.seek(0)
            source_width, source_height = source.size
            if (
                source_width <= 0
                or source_height <= 0
                or source_width * source_height > MAX_IMAGE_PIXELS
            ):
                raise InputValidationError("图片尺寸无效或像素数量超出安全上限")
            frame = ImageOps.exif_transpose(source.copy())
    except InputValidationError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise InputValidationError("图片无法解码或像素数量超出安全上限") from exc
    width, height = frame.size
    if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
        raise InputValidationError("图片尺寸无效或像素数量超出安全上限")
    return width, height
