import ast
import json
import os
import subprocess
import sys
from io import BytesIO

from PIL import Image


def png_bytes(size: tuple[int, int] = (90, 160)) -> bytes:
    output = BytesIO()
    Image.new("RGB", size, "#d987a5").save(output, format="PNG")
    return output.getvalue()


def run_cli(script, cwd, *arguments: str) -> subprocess.CompletedProcess[str]:
    """从 Skill 目录外以 Windows 默认编码执行，验证 CLI 自行固定 UTF-8。"""

    env = os.environ.copy()
    env.update({"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"})
    return subprocess.run(
        [sys.executable, str(script), *arguments],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
    )


def test_expression_skill_python_does_not_import_host_project(settings) -> None:
    skill_root = settings.project_root / "skills" / "send-expression"
    forbidden = {
        "adapters",
        "api",
        "application",
        "bootstrap",
        "config",
        "domain",
        "ports",
        "skill_runtime",
        "tools",
    }
    for path in skill_root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported = {alias.name.split(".", 1)[0] for alias in node.names}
                assert imported.isdisjoint(forbidden), f"{path} 导入了宿主模块"
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                assert node.module.split(".", 1)[0] not in forbidden, (
                    f"{path} 导入了宿主模块"
                )


def test_expression_skill_cli_is_portable_for_inspect_reuse_and_dry_run(
    settings, tmp_path
) -> None:
    script = (
        settings.project_root
        / "skills"
        / "send-expression"
        / "scripts"
        / "expression_cli.py"
    )
    base_image = tmp_path / "base_image.png"
    library = tmp_path / "library"
    media = tmp_path / "media"
    library.mkdir()
    base_image.write_bytes(png_bytes())

    inspected = run_cli(
        script,
        tmp_path,
        "inspect",
        "--base-image",
        str(base_image),
        "--library",
        str(library),
    )
    assert inspected.returncode == 0, inspected.stderr
    state = json.loads(inspected.stdout)
    assert state["base_image_size"] == [90, 160]
    assert state["expression_names"] == []

    expression_image = library / "开心挥手.png"
    expression_image.write_bytes(png_bytes((160, 90)))
    (library / "开心挥手.json").write_text(
        json.dumps(
            {
                "name": "开心挥手",
                "base_image_sha256": state["base_image_sha256"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    reused = run_cli(
        script,
        tmp_path,
        "prepare",
        "--action",
        "reuse",
        "--base-image",
        str(base_image),
        "--library",
        str(library),
        "--media",
        str(media),
        "--name",
        "开心挥手",
        "--emotion",
        "开心",
        "--caption",
        "一起开心！",
    )
    assert reused.returncode == 0, reused.stderr
    draft = json.loads(reused.stdout)
    assert [item["type"] for item in draft["components"]] == ["text", "image_ref"]
    assert (draft["components"][1]["width"], draft["components"][1]["height"]) == (
        160,
        90,
    )
    assert (media / f"{draft['components'][1]['sha256']}.png").is_file()

    dry_run = run_cli(
        script,
        tmp_path,
        "prepare",
        "--action",
        "generate",
        "--base-image",
        str(base_image),
        "--library",
        str(tmp_path / "empty-library"),
        "--media",
        str(media),
        "--name",
        "害羞捂脸",
        "--emotion",
        "害羞",
        "--image-prompt",
        "脸红并轻轻捂住脸",
        "--dry-run",
    )
    assert dry_run.returncode == 0, dry_run.stderr
    payload = json.loads(dry_run.stdout)
    assert payload["dry_run"] is True
    assert payload["endpoint"] == "/images/edits"
    assert "base_image" in payload["prompt"]


def test_expression_skill_cli_requires_exact_reuse_name(settings, tmp_path) -> None:
    script = (
        settings.project_root
        / "skills"
        / "send-expression"
        / "scripts"
        / "expression_cli.py"
    )
    base_image = tmp_path / "base_image.png"
    library = tmp_path / "library"
    library.mkdir()
    base_image.write_bytes(png_bytes())
    inspected = run_cli(
        script,
        tmp_path,
        "inspect",
        "--base-image",
        str(base_image),
        "--library",
        str(library),
    )
    state = json.loads(inspected.stdout)
    (library / "开心挥手.png").write_bytes(png_bytes())
    (library / "开心挥手.json").write_text(
        json.dumps(
            {
                "name": "开心挥手",
                "base_image_sha256": state["base_image_sha256"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    rejected = run_cli(
        script,
        tmp_path,
        "prepare",
        "--action",
        "reuse",
        "--base-image",
        str(base_image),
        "--library",
        str(library),
        "--media",
        str(tmp_path / "media"),
        "--name",
        " 开心挥手 ",
        "--emotion",
        "开心",
    )
    assert rejected.returncode == 1
    assert "逐字使用现有名称" in rejected.stderr
