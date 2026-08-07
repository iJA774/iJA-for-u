#!/usr/bin/env python3
"""可从任意项目运行的角色表情文件图库 CLI。"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any

try:
    import httpx
    from PIL import Image, ImageOps, UnidentifiedImageError
except ImportError as exc:  # pragma: no cover - 依赖缺失路径由独立进程测试
    print("缺少依赖；请安装 httpx 和 Pillow。", file=sys.stderr)
    raise SystemExit(2) from exc

from expression_core import build_generation_prompt, normalize_expression_name

MAX_EXPRESSIONS = 200
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000


def _fail(message: str) -> None:
    raise ValueError(message)


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _normalize_png(content: bytes, *, require_ratio: bool) -> tuple[bytes, int, int]:
    if not content or len(content) > MAX_IMAGE_BYTES:
        _fail("图片为空或超过 20 MiB 上限")
    try:
        with Image.open(BytesIO(content)) as source:
            source.seek(0)
            width, height = source.size
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                _fail("图片尺寸无效或像素数量超过上限")
            frame = ImageOps.exif_transpose(source.copy())
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("图片无法安全解码") from exc
    width, height = frame.size
    if require_ratio and width * 16 != height * 9:
        _fail(f"base_image 必须为精确 9:16，当前为 {width}x{height}")
    if frame.width > 1536 or frame.height > 1536:
        scale = min(1536 / frame.width, 1536 / frame.height)
        frame = frame.resize(
            (max(1, round(frame.width * scale)), max(1, round(frame.height * scale))),
            Image.Resampling.LANCZOS,
        )
    if frame.mode not in {"RGB", "RGBA"}:
        frame = frame.convert("RGBA" if "A" in frame.getbands() else "RGB")
    output = BytesIO()
    frame.save(output, format="PNG", optimize=True)
    encoded = output.getvalue()
    if len(encoded) > MAX_IMAGE_BYTES:
        _fail("规范化 PNG 超过 20 MiB 上限")
    return encoded, frame.width, frame.height


def _metadata_files(library: Path) -> list[Path]:
    return sorted(library.glob("*.json"), key=lambda item: item.name) if library.is_dir() else []


def _load_metadata(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"表情元数据损坏: {path.name}") from exc
    if not isinstance(value, dict):
        _fail(f"表情元数据不是对象: {path.name}")
    return value


def _inspect(base_image: Path, library: Path) -> dict[str, Any]:
    base_png, width, height = _normalize_png(base_image.read_bytes(), require_ratio=True)
    base_sha = hashlib.sha256(base_png).hexdigest()
    names: list[str] = []
    for metadata_path in _metadata_files(library):
        metadata = _load_metadata(metadata_path)
        image_path = library / f"{metadata_path.stem}.png"
        if metadata.get("base_image_sha256") == base_sha and image_path.is_file():
            names.append(str(metadata.get("name", metadata_path.stem)))
    return {
        "base_image_sha256": base_sha,
        "base_image_size": [width, height],
        "expression_names": names,
        "remaining_capacity": max(0, MAX_EXPRESSIONS - len(_metadata_files(library))),
    }


def _image_edit(base_png: bytes, prompt: str) -> bytes:
    base_url = os.getenv("SEND_EXPRESSION_IMAGE_BASE_URL", "https://api.openai.com/v1")
    api_key = os.getenv("SEND_EXPRESSION_IMAGE_API_KEY", "").strip()
    model = os.getenv("SEND_EXPRESSION_IMAGE_MODEL", "").strip()
    timeout = float(os.getenv("SEND_EXPRESSION_IMAGE_TIMEOUT_SECONDS", "120"))
    if not api_key or not model:
        _fail("生成需要 SEND_EXPRESSION_IMAGE_API_KEY 和 SEND_EXPRESSION_IMAGE_MODEL")
    try:
        with httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
        ) as client:
            response = client.post(
                "/images/edits",
                data={
                    "model": model,
                    "prompt": prompt,
                    "n": "1",
                    "size": "1024x1536",
                    "output_format": "png",
                },
                files={"image": ("base_image.png", base_png, "image/png")},
            )
    except httpx.TimeoutException as exc:
        raise ValueError("图片模型请求超时；不会自动重试") from exc
    except httpx.NetworkError as exc:
        raise ValueError("图片模型网络请求失败；不会自动重试") from exc
    if response.status_code in {401, 403}:
        _fail("图片模型鉴权失败")
    if response.status_code == 429:
        _fail("图片模型触发限流；不会自动重试")
    if response.status_code >= 500:
        _fail(f"图片模型服务错误 HTTP {response.status_code}；不会自动重试")
    if response.status_code >= 400:
        _fail(f"图片模型拒绝请求 HTTP {response.status_code}")
    try:
        payload = response.json()
        encoded = payload["data"][0]["b64_json"]
        if not isinstance(encoded, str) or not encoded:
            _fail("b64_json 不是非空字符串")
        return base64.b64decode(encoded, validate=True)
    except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError("图片模型响应不符合严格 b64_json 契约") from exc


def _prepare(args: argparse.Namespace) -> dict[str, Any]:
    base_png, _, _ = _normalize_png(args.base_image.read_bytes(), require_ratio=True)
    base_sha = hashlib.sha256(base_png).hexdigest()
    expression_name = normalize_expression_name(args.name)
    image_path = args.library / expression_name.filename
    metadata_path = image_path.with_suffix(".json")
    if args.action == "reuse":
        if not metadata_path.is_file() or not image_path.is_file():
            _fail(f"不存在可复用表情: {args.name}")
        metadata = _load_metadata(metadata_path)
        if metadata.get("name") != args.name:
            _fail(f"复用时必须逐字使用现有名称: {metadata.get('name')}")
        if metadata.get("base_image_sha256") != base_sha:
            _fail("该表情不属于当前 base_image 版本")
        source_png, width, height = _normalize_png(
            image_path.read_bytes(), require_ratio=False
        )
    else:
        if metadata_path.exists() or image_path.exists():
            _fail(f"表情名称已存在: {expression_name.visible}")
        if len(_metadata_files(args.library)) >= MAX_EXPRESSIONS:
            _fail("表情库已达到 200 个上限")
        prompt = build_generation_prompt(args.emotion, args.image_prompt or "")
        if args.dry_run:
            return {
                "dry_run": True,
                "endpoint": "/images/edits",
                "name": expression_name.visible,
                "filename": expression_name.filename,
                "prompt": prompt,
            }
        generated = _image_edit(base_png, prompt)
        source_png, width, height = _normalize_png(generated, require_ratio=False)
        digest = hashlib.sha256(source_png).hexdigest()
        metadata = {
            "name": expression_name.visible,
            "normalized_name": expression_name.normalized,
            "emotion": args.emotion.strip(),
            "description": (args.image_prompt or "").strip(),
            "base_image_sha256": base_sha,
            "sha256": digest,
            "width": width,
            "height": height,
        }
        _atomic_write(image_path, source_png)
        _atomic_write(
            metadata_path,
            json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    digest = hashlib.sha256(source_png).hexdigest()
    history_path = args.media / f"{digest}.png"
    if not args.dry_run:
        _atomic_write(history_path, source_png)
    components: list[dict[str, Any]] = []
    if (args.caption or "").strip():
        components.append({"type": "text", "text": args.caption.strip()})
    components.append(
        {
            "type": "image_ref",
            "filename": expression_name.filename,
            "path": str(history_path.resolve()),
            "sha256": digest,
            "width": width,
            "height": height,
        }
    )
    return {"prepared": True, "action": args.action, "components": components}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "独立检查、生成或复用角色表情。生成要求 9:16 基础形象，"
            "已有表情不限宽高比；生成请求不自动重试。"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    inspect_parser = subparsers.add_parser("inspect", help="检查 base_image 和当前图库")
    inspect_parser.add_argument("--base-image", type=Path, required=True)
    inspect_parser.add_argument("--library", type=Path, required=True)
    prepare_parser = subparsers.add_parser("prepare", help="复用或生成并准备回复组件")
    prepare_parser.add_argument("--action", choices=("reuse", "generate"), required=True)
    prepare_parser.add_argument("--base-image", type=Path, required=True)
    prepare_parser.add_argument("--library", type=Path, required=True)
    prepare_parser.add_argument("--media", type=Path, required=True)
    prepare_parser.add_argument("--name", required=True)
    prepare_parser.add_argument("--emotion", required=True)
    prepare_parser.add_argument("--image-prompt")
    prepare_parser.add_argument("--caption")
    prepare_parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        if args.command == "inspect":
            result = _inspect(args.base_image, args.library)
        else:
            if args.action == "generate" and not (args.image_prompt or "").strip():
                _fail("generate 必须提供 --image-prompt")
            result = _prepare(args)
    except (OSError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
