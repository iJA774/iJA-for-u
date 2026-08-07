from io import BytesIO
from typing import cast

import pytest
from PIL import Image

from domain.errors import InputValidationError, PortraitAspectRatioError
from imaging import is_nine_sixteen, normalize_static_png


def encoded_image(
    image_format: str,
    size: tuple[int, int] = (90, 160),
    *,
    exif: Image.Exif | None = None,
) -> bytes:
    """生成测试用真实图片编码。"""

    output = BytesIO()
    save_options = {"exif": exif} if exif is not None else {}
    Image.new("RGB", size, "#b665d8").save(
        output, format=image_format, **save_options
    )
    return output.getvalue()


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "WEBP"])
def test_real_static_formats_are_normalized_to_metadata_free_png(image_format: str) -> None:
    normalized = normalize_static_png(
        encoded_image(image_format),
        crop_to_nine_sixteen=False,
        aspect_error=True,
        max_output_bytes=2 * 1024 * 1024,
    )
    assert normalized.content.startswith(b"\x89PNG\r\n\x1a\n")
    assert (normalized.width, normalized.height) == (90, 160)
    with Image.open(BytesIO(normalized.content)) as restored:
        assert restored.format == "PNG"
        assert restored.getexif() == {}


def test_exif_orientation_and_gif_first_frame_are_applied() -> None:
    exif = Image.Exif()
    exif[274] = 6
    rotated = normalize_static_png(
        encoded_image("JPEG", (160, 90), exif=exif),
        crop_to_nine_sixteen=False,
        aspect_error=True,
        max_output_bytes=2 * 1024 * 1024,
    )
    assert (rotated.width, rotated.height) == (90, 160)

    first = Image.new("RGB", (90, 160), "red")
    second = Image.new("RGB", (90, 160), "blue")
    output = BytesIO()
    first.save(output, format="GIF", save_all=True, append_images=[second])
    gif = normalize_static_png(
        output.getvalue(),
        crop_to_nine_sixteen=False,
        aspect_error=True,
        max_output_bytes=2 * 1024 * 1024,
    )
    with Image.open(BytesIO(gif.content)) as restored:
        red, green, blue = cast(
            tuple[int, int, int], restored.convert("RGB").getpixel((10, 10))
        )
        assert red > 200 and green < 30 and blue < 30


def test_aspect_mismatch_reports_original_size_then_center_crops() -> None:
    square = encoded_image("PNG", (160, 160))
    with pytest.raises(PortraitAspectRatioError) as caught:
        normalize_static_png(
            square,
            crop_to_nine_sixteen=False,
            aspect_error=True,
            max_output_bytes=2 * 1024 * 1024,
        )
    assert caught.value.details == {"width": 160, "height": 160, "target_ratio": "9:16"}

    cropped = normalize_static_png(
        square,
        crop_to_nine_sixteen=True,
        aspect_error=True,
        max_output_bytes=2 * 1024 * 1024,
    )
    assert (cropped.width, cropped.height) == (90, 160)
    assert is_nine_sixteen(cropped.width, cropped.height)


def test_generated_image_is_limited_to_864_by_1536_and_output_limit_is_enforced() -> None:
    large = encoded_image("PNG", (900, 1600))
    normalized = normalize_static_png(
        large,
        crop_to_nine_sixteen=True,
        aspect_error=False,
        max_output_bytes=2 * 1024 * 1024,
        max_nine_sixteen_size=(864, 1536),
    )
    assert (normalized.width, normalized.height) == (864, 1536)
    with pytest.raises(InputValidationError, match="超过上限"):
        normalize_static_png(
            large,
            crop_to_nine_sixteen=True,
            aspect_error=False,
            max_output_bytes=10,
        )
