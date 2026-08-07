from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from plugins._host import (
    ManagedServiceError,
    ManagedServiceManager,
    PluginContributionCatalog,
)
from skill_runtime import SkillCatalog, SkillPluginManager

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OWNER = {"actor_id": "user-1", "session_id": "session-1"}


def load_module(path: Path, name: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def foundry_modules():
    root = PROJECT_ROOT / "plugins" / "skill_foundry"
    return (
        load_module(root / "controller.py", "test_skill_foundry_controller"),
        load_module(
            root / "skills" / "skill-creator" / "runtime.py",
            "test_skill_creator_runtime",
        ),
    )


def create_params(name: str = "daily-review") -> dict[str, Any]:
    return {
        "name": name,
        "display_name": "每日复盘",
        "description": "用户明确要求复盘当天工作时使用。",
        "objective": "把当天完成项、阻塞和下一步整理为简洁复盘。",
        "steps": ["收集已完成事项", "区分事实、计划和阻塞", "输出下一步"],
        **OWNER,
    }


def test_foundry_builds_reviewable_instruction_skill_and_tracks_all_states(
    tmp_path, foundry_modules
) -> None:
    controller, _ = foundry_modules
    foundry = controller.SkillFoundry(tmp_path)

    job = foundry.create(create_params())

    assert job["state"] == "REVIEW_PENDING"
    assert [item["state"] for item in job["history"]] == [
        "DRAFT",
        "GENERATING",
        "VALIDATING",
        "TESTING",
        "REVIEW_PENDING",
    ]
    assert [item["step"] for item in job["checkpoints"]] == [
        "generate",
        "validate",
        "test",
    ]
    assert job["artifact_kind"] == "instruction-only"
    assert job["isolation"]["executes_generated_code"] is True
    assert job["isolation"]["artifact_kinds"] == [
        "instruction-only",
        "instruction-and-scripts",
    ]
    assert {item["path"] for item in job["review"]["files"]} == {
        "SKILL.md",
        "agents/openai.yaml",
    }
    draft = tmp_path / "data" / "skill-workbench" / job["job_id"] / "draft"
    assert "name: \"daily-review\"" in (draft / "SKILL.md").read_text(encoding="utf-8")
    assert not (tmp_path / "skills" / "daily-review").exists()


def test_foundry_builds_tests_installs_and_runs_reusable_script(
    tmp_path, foundry_modules
) -> None:
    controller, _ = foundry_modules
    foundry = controller.SkillFoundry(tmp_path)
    params = create_params("number-summary")
    params["scripts"] = [
        {
            "name": "summarize_numbers",
            "description": "统计数字列表的数量、总和与均值。",
            "content": (
                "import statistics\n\n"
                "def main(data):\n"
                "    values = data['values']\n"
                "    return {\n"
                "        'count': len(values),\n"
                "        'total': sum(values),\n"
                "        'mean': statistics.mean(values),\n"
                "    }\n"
            ),
            "smoke_input": {"values": [1, 2, 3]},
        }
    ]
    script = params.pop("scripts")[0]

    begun = foundry.begin(params)
    assert begun["state"] == "DRAFT"
    assert begun["request_summary"]["script_count"] == 0
    job = foundry.add_script(
        {
            "job_id": begun["job_id"],
            **script,
            **OWNER,
        }
    )

    assert job["state"] == "REVIEW_PENDING"
    assert job["artifact_kind"] == "instruction-and-scripts"
    assert [item["path"] for item in job["script_tests"]] == [
        "scripts/summarize_numbers.py"
    ]
    assert job["script_tests"][0]["policy_version"] == "ija-safe-python-v1"
    assert {
        item["path"] for item in job["review"]["script_files"]
    } == {"scripts/summarize_numbers.py"}
    preview = foundry.preview(
        {
            "job_id": job["job_id"],
            "path": "scripts/summarize_numbers.py",
            **OWNER,
        }
    )
    assert "def main(data)" in preview["content"]

    installed = foundry.approve(
        {
            "job_id": job["job_id"],
            "confirmation": job["approval"]["required_phrase"],
            **OWNER,
        }
    )
    assert installed["state"] == "INSTALLED"
    execution = foundry.execute_script(
        {
            "skill_name": "number-summary",
            "script_name": "summarize_numbers",
            "input": {"values": [4, 5, 6]},
            **OWNER,
        }
    )

    assert execution["result"] == {"count": 3, "total": 15, "mean": 5}
    assert execution["policy_version"] == "ija-safe-python-v1"
    audit_path = (
        tmp_path
        / "data"
        / "skill-workbench"
        / job["job_id"]
        / "executions"
        / f"{execution['execution_id']}.json"
    )
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert "input" not in audit
    assert "output" not in audit
    assert audit["caller"] == OWNER

    installed_script = (
        tmp_path / "skills" / "number-summary" / "scripts" / "summarize_numbers.py"
    )
    installed_script.write_text(
        "def main(data):\n    return {'tampered': True}\n",
        encoding="utf-8",
    )
    with pytest.raises(controller.FoundryError, match="冻结 revision"):
        foundry.execute_script(
            {
                "skill_name": "number-summary",
                "script_name": "summarize_numbers",
                "input": {"values": [1]},
                **OWNER,
            }
        )


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            "import os\n\ndef main(data):\n    return os.listdir('.')\n",
            "banned-import",
        ),
        (
            "def main(data):\n    return open('/etc/passwd').read()\n",
            "敏感路径",
        ),
        (
            "def main(data):\n    return (1).__class__.__mro__\n",
            "private-attribute",
        ),
        (
            "import fractions\n\ndef main(data):\n    return fractions.sys.version\n",
            "banned-module-attribute",
        ),
        (
            "from statistics import sys\n\ndef main(data):\n    return sys.version\n",
            "banned-import",
        ),
    ],
)
def test_foundry_rejects_unsafe_script_before_review(
    tmp_path, foundry_modules, source: str, message: str
) -> None:
    controller, _ = foundry_modules
    foundry = controller.SkillFoundry(tmp_path)
    params = create_params("unsafe-script")
    params["scripts"] = [
        {
            "name": "unsafe",
            "description": "不安全脚本。",
            "content": source,
            "smoke_input": {},
        }
    ]

    with pytest.raises(controller.FoundryError, match=message):
        foundry.create(params)

    assert not (tmp_path / "data" / "skill-workbench").exists()
    assert not (tmp_path / "skills" / "unsafe-script").exists()


def test_foundry_rejects_sensitive_smoke_input_before_persistence(
    tmp_path, foundry_modules
) -> None:
    controller, _ = foundry_modules
    foundry = controller.SkillFoundry(tmp_path)
    params = create_params("sensitive-input")
    params["scripts"] = [
        {
            "name": "identity",
            "description": "返回输入。",
            "content": "def main(data):\n    return data\n",
            "smoke_input": {"api_key": "not-a-real-key"},
        }
    ]

    with pytest.raises(controller.FoundryError, match="敏感字段"):
        foundry.create(params)

    assert not (tmp_path / "data" / "skill-workbench").exists()


def test_foundry_stops_nonterminating_script_without_installing(
    tmp_path, foundry_modules
) -> None:
    controller, _ = foundry_modules
    foundry = controller.SkillFoundry(tmp_path)
    params = create_params("nonterminating-script")
    params["scripts"] = [
        {
            "name": "spin",
            "description": "用于验证超时保护。",
            "content": "def main(data):\n    while True:\n        pass\n",
            "smoke_input": {},
        }
    ]

    job = foundry.create(params)

    assert job["state"] == "FAILED"
    assert "沙箱" in job["failure"]["message"]
    assert not (tmp_path / "skills" / "nonterminating-script").exists()


def test_foundry_supports_reviewable_multi_file_reference_package(
    tmp_path, foundry_modules
) -> None:
    controller, _ = foundry_modules
    foundry = controller.SkillFoundry(tmp_path)
    params = create_params("reference-skill")
    params["references"] = [
        {"name": "output-contract", "content": "输出必须包含事实、阻塞和下一步。"},
        {"name": "examples", "content": "示例只使用虚构数据。"},
    ]

    job = foundry.create(params)

    assert job["state"] == "REVIEW_PENDING"
    paths = {item["path"] for item in job["review"]["files"]}
    assert paths == {
        "SKILL.md",
        "agents/openai.yaml",
        "references/examples.md",
        "references/output-contract.md",
    }
    skill_file = (
        tmp_path / "data" / "skill-workbench" / job["job_id"] / "draft" / "SKILL.md"
    )
    assert "[output-contract](references/output-contract.md)" in skill_file.read_text(
        encoding="utf-8"
    )
    revision = job["review"]["revision"]
    assert job["approval"]["required_phrase"] == (
        f"确认全局安装 reference-skill {job['job_id']} {revision}"
    )
    assert job["rejection"]["required_phrase"] == (
        f"拒绝草案 reference-skill {job['job_id']} {revision}"
    )


def test_public_job_is_bounded_and_references_are_individually_previewable(
    tmp_path, foundry_modules
) -> None:
    controller, _ = foundry_modules
    foundry = controller.SkillFoundry(tmp_path)
    params = create_params("bounded-review")
    params["references"] = [
        {"name": f"reference-{index}", "content": "审" * 3_000}
        for index in range(5)
    ]

    job = foundry.create(params)
    encoded = json.dumps(job, ensure_ascii=False).encode("utf-8")
    loaded = foundry.get({"job_id": job["job_id"], **OWNER})

    assert len(encoded) < 32_000
    assert len(json.dumps(loaded, ensure_ascii=False).encode("utf-8")) < 32_000
    assert "request" not in job
    assert len(job["review"]["reference_files"]) == 5
    assert all("content" not in item for item in job["review"]["reference_files"])
    assert set(job["review"]["previews"]) == {"SKILL.md", "agents/openai.yaml"}

    preview = foundry.preview(
        {
            "job_id": job["job_id"],
            "path": "references/reference-3.md",
            **OWNER,
        }
    )
    assert preview["revision"] == job["review"]["revision"]
    assert preview["size_bytes"] < 11_500
    assert preview["content"].replace("\r\n", "\n").startswith("# reference-3\n\n")
    assert "审" * 100 in preview["content"]
    assert preview["offset"] == 0
    assert preview["next_offset"] is not None
    assert preview["complete"] is False

    with pytest.raises(controller.FoundryError, match="不属于"):
        foundry.preview(
            {
                "job_id": job["job_id"],
                "path": "SKILL.md",
                "actor_id": "other-user",
                "session_id": "session-1",
            }
        )
    with pytest.raises(controller.FoundryError, match="只能预览"):
        foundry.preview(
            {
                "job_id": job["job_id"],
                "path": "../job.json",
                **OWNER,
            }
        )
    with pytest.raises(controller.FoundryError, match="不属于当前冻结 revision"):
        foundry.preview(
            {
                "job_id": job["job_id"],
                "path": "references/not-present.md",
                **OWNER,
            }
        )


def test_preview_traverses_maximum_multilingual_skill_in_bounded_chunks(
    tmp_path, foundry_modules
) -> None:
    controller, _ = foundry_modules
    foundry = controller.SkillFoundry(tmp_path)
    params = create_params("maximum-preview")
    params.update(
        {
            "display_name": "名" * 80,
            "description": "述" * 333,
            "objective": "目" * 1_500,
            "steps": ["步" * 388 for _ in range(3)],
            "references": [
                {
                    "name": (
                        prefix := f"reference-{index}-"
                    )
                    + "x" * (64 - len(prefix)),
                    "content": "参" * 500,
                }
                for index in range(10)
            ],
        }
    )

    job = foundry.create(params)
    assert job["state"] == "REVIEW_PENDING"
    skill_path = (
        tmp_path
        / "data"
        / "skill-workbench"
        / job["job_id"]
        / "draft"
        / "SKILL.md"
    )
    expected = skill_path.read_bytes().decode("utf-8")
    assert len(expected.encode("utf-8")) > 10_500

    offset = 0
    chunks: list[str] = []
    while True:
        preview = foundry.preview(
            {
                "job_id": job["job_id"],
                "path": "SKILL.md",
                "offset": offset,
                "chunk_size": 3_000,
                **OWNER,
            }
        )
        assert preview["offset"] == offset
        assert preview["size_bytes"] == len(expected.encode("utf-8"))
        assert preview["total_characters"] == len(expected)
        assert len(json.dumps(preview, ensure_ascii=False).encode("utf-8")) < 12_000
        chunks.append(preview["content"])
        if preview["complete"]:
            assert preview["next_offset"] is None
            break
        assert isinstance(preview["next_offset"], int)
        assert preview["next_offset"] > offset
        offset = preview["next_offset"]
        assert len(chunks) < 20

    assert len(chunks) > 1
    assert "".join(chunks) == expected
    with pytest.raises(controller.FoundryError, match="offset 超出"):
        foundry.preview(
            {
                "job_id": job["job_id"],
                "path": "SKILL.md",
                "offset": len(expected) + 1,
                **OWNER,
            }
        )


def test_summary_fields_reject_crlf_and_state_has_no_plaintext_nonce(
    tmp_path, foundry_modules
) -> None:
    controller, runtime = foundry_modules
    foundry = controller.SkillFoundry(tmp_path)
    for field in ("display_name", "description"):
        params = create_params(f"newline-{field.replace('_', '-')}")
        params[field] = "安全摘要\n注入下一行"
        with pytest.raises(controller.FoundryError, match="不得包含 CR 或 LF"):
            foundry.create(params)
        model_params = {
            key: value
            for key, value in params.items()
            if key not in {"actor_id", "session_id"}
        }
        with pytest.raises(ValueError, match="不得包含 CR 或 LF"):
            runtime.CreateDraftArguments.model_validate(model_params)

    job = foundry.create(create_params("no-plaintext-nonce"))
    state_path = (
        tmp_path / "data" / "skill-workbench" / job["job_id"] / "job.json"
    )
    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert "approval_token" not in persisted
    assert "token" not in json.dumps(persisted, ensure_ascii=False).lower()


def test_foundry_requires_exact_install_confirmation_and_preserves_existing_skill(
    tmp_path, foundry_modules
) -> None:
    controller, _ = foundry_modules
    foundry = controller.SkillFoundry(tmp_path)
    job = foundry.create(create_params())

    with pytest.raises(controller.FoundryError, match="安装确认"):
        foundry.approve(
            {
                "job_id": job["job_id"],
                "confirmation": "错误确认",
                **OWNER,
            }
        )

    existing = tmp_path / "skills" / "daily-review"
    existing.mkdir(parents=True)
    marker = existing / "keep.txt"
    marker.write_text("old", encoding="utf-8")
    confirmation = job["approval"]["required_phrase"]
    with pytest.raises(controller.FoundryError, match="拒绝覆盖"):
        foundry.approve(
            {
                "job_id": job["job_id"],
                "confirmation": confirmation,
                **OWNER,
            }
        )
    assert marker.read_text(encoding="utf-8") == "old"
    assert foundry.get({"job_id": job["job_id"], **OWNER})["state"] == "REVIEW_PENDING"


def test_foundry_installs_only_after_approval_and_reject_is_terminal(
    tmp_path, foundry_modules
) -> None:
    controller, _ = foundry_modules
    foundry = controller.SkillFoundry(tmp_path)
    approved = foundry.create(create_params("approved-skill"))
    confirmation = approved["approval"]["required_phrase"]

    installed = foundry.approve(
        {
            "job_id": approved["job_id"],
            "confirmation": confirmation,
            **OWNER,
        }
    )
    assert installed["state"] == "INSTALLED"
    assert (tmp_path / "skills" / "approved-skill" / "SKILL.md").is_file()

    rejected = foundry.create(create_params("rejected-skill"))
    rejected_install_confirmation = rejected["approval"]["required_phrase"]
    result = foundry.reject(
        {
            "job_id": rejected["job_id"],
            "confirmation": rejected["rejection"]["required_phrase"],
            **OWNER,
        }
    )
    assert result["state"] == "REJECTED"
    assert not (tmp_path / "skills" / "rejected-skill").exists()
    with pytest.raises(controller.FoundryError, match="REVIEW_PENDING"):
        foundry.approve(
            {
                "job_id": rejected["job_id"],
                "confirmation": rejected_install_confirmation,
                **OWNER,
            }
        )


def test_foundry_jobs_are_bound_to_actor_and_session(
    tmp_path, foundry_modules
) -> None:
    controller, _ = foundry_modules
    foundry = controller.SkillFoundry(tmp_path)

    missing_owner = create_params("missing-owner")
    missing_owner.pop("actor_id")
    with pytest.raises(controller.FoundryError, match="actor_id 和 session_id"):
        foundry.create(missing_owner)

    job = foundry.create(create_params("owned-skill"))
    assert job["state"] == "REVIEW_PENDING", job
    confirmation = job["approval"]["required_phrase"]
    for foreign_owner in (
        {"actor_id": "user-2", "session_id": "session-1"},
        {"actor_id": "user-1", "session_id": "session-2"},
    ):
        with pytest.raises(controller.FoundryError, match="不属于"):
            foundry.get({"job_id": job["job_id"], **foreign_owner})
        with pytest.raises(controller.FoundryError, match="不属于"):
            foundry.approve(
                {
                    "job_id": job["job_id"],
                    "confirmation": confirmation,
                    **foreign_owner,
                }
            )
        with pytest.raises(controller.FoundryError, match="不属于"):
            foundry.reject(
                {
                    "job_id": job["job_id"],
                    "confirmation": job["rejection"]["required_phrase"],
                    **foreign_owner,
                }
            )
    assert foundry.get({"job_id": job["job_id"], **OWNER})["state"] == "REVIEW_PENDING"


def test_worker_builds_only_inside_draft_and_does_not_expand_environment(tmp_path) -> None:
    worker = PROJECT_ROOT / "plugins" / "skill_foundry" / "worker.py"
    draft = tmp_path / "draft"
    draft.mkdir()
    payload = {
        "draft_root": str(draft),
        **create_params(),
        "skill_name": "safe-skill",
    }
    env = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "SHOULD_NOT_LEAK": "secret"}
    completed = subprocess.run(
        [sys.executable, "-I", str(worker)],
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        capture_output=True,
        cwd=tmp_path,
        env=env,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    combined = "\n".join(path.read_text(encoding="utf-8") for path in draft.rglob("*") if path.is_file())
    assert "secret" not in combined


def test_worker_audit_policy_denies_network_process_links_and_outside_mutation(
    tmp_path,
) -> None:
    worker = PROJECT_ROOT / "plugins" / "skill_foundry" / "worker.py"
    draft = tmp_path / "draft"
    draft.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("must-stay", encoding="utf-8")
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    renamed = tmp_path / "renamed.txt"
    probe = (
        "import importlib.util, os, pathlib, socket, subprocess, sys\n"
        f"spec=importlib.util.spec_from_file_location('worker_probe',{str(worker)!r})\n"
        "module=importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        f"module._install_audit_policy(pathlib.Path({str(draft)!r}))\n"
        "denied=[]\n"
        "try:\n socket.socket()\n"
        "except PermissionError:\n denied.append('network')\n"
        f"try:\n pathlib.Path({str(outside)!r}).write_text('forbidden')\n"
        "except PermissionError:\n denied.append('outside-write')\n"
        f"try:\n os.remove({str(outside)!r})\n"
        "except PermissionError:\n denied.append('outside-remove')\n"
        f"try:\n os.rmdir({str(outside_dir)!r})\n"
        "except PermissionError:\n denied.append('outside-rmdir')\n"
        f"try:\n os.rename({str(outside)!r},{str(renamed)!r})\n"
        "except PermissionError:\n denied.append('outside-rename')\n"
        f"try:\n os.replace({str(outside)!r},{str(renamed)!r})\n"
        "except PermissionError:\n denied.append('outside-replace')\n"
        "try:\n subprocess.run([sys.executable,'-c','pass'],check=False)\n"
        "except PermissionError:\n denied.append('subprocess')\n"
        f"try:\n os.link({str(outside)!r},"
        f"str(pathlib.Path({str(draft)!r})/'hard-link'))\n"
        "except PermissionError:\n denied.append('hardlink')\n"
        f"try:\n os.symlink({str(outside)!r},"
        f"str(pathlib.Path({str(draft)!r})/'link'))\n"
        "except PermissionError:\n denied.append('symlink')\n"
        f"inside=pathlib.Path({str(draft)!r})/'inside'\n"
        "inside.mkdir()\n"
        "(inside/'ok.txt').write_text('ok')\n"
        "print(','.join(denied))\n"
    )

    completed = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == (
        "network,outside-write,outside-remove,outside-rmdir,outside-rename,"
        "outside-replace,subprocess,hardlink,symlink"
    )
    assert outside.read_text(encoding="utf-8") == "must-stay"
    assert outside_dir.is_dir()
    assert not renamed.exists()
    assert (draft / "inside" / "ok.txt").read_text(encoding="utf-8") == "ok"


def test_controller_passes_worker_a_minimal_environment(
    tmp_path, foundry_modules, monkeypatch
) -> None:
    controller, _ = foundry_modules
    foundry = controller.SkillFoundry(tmp_path)
    captured: dict[str, Any] = {}

    def fake_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.update(kwargs)
        payload = json.loads(kwargs["input"])
        draft = Path(payload["draft_root"])
        (draft / "agents").mkdir()
        (draft / "SKILL.md").write_text(
            '---\nname: "env-check"\ndescription: "检查环境"\n---\n\n# 检查\n',
            encoding="utf-8",
        )
        (draft / "agents" / "openai.yaml").write_text(
            'interface:\n  display_name: "检查"\n'
            '  short_description: "检查环境"\n'
            '  default_prompt: "检查"\n',
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout='{"generated": true, "artifact_kind": "instruction-only"}',
            stderr="",
        )

    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setattr(controller.subprocess, "run", fake_run)
    job = foundry.create(create_params("env-check"))

    assert job["state"] == "REVIEW_PENDING"
    assert "OPENAI_API_KEY" not in captured["env"]
    assert set(captured["env"]) <= {"PYTHONIOENCODING", "PYTHONUTF8", "SYSTEMROOT", "WINDIR"}
    worker_payload = json.loads(captured["input"])
    assert "project_root" not in worker_payload
    assert "database" not in worker_payload
    assert captured["cwd"] == (
        tmp_path / "data" / "skill-workbench" / job["job_id"]
    ).resolve()


def test_worker_crash_is_recorded_as_failed_state(
    tmp_path, foundry_modules, monkeypatch
) -> None:
    controller, _ = foundry_modules
    foundry = controller.SkillFoundry(tmp_path)

    def failed_run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=args,
            returncode=9,
            stdout="",
            stderr="worker crashed",
        )

    monkeypatch.setattr(controller.subprocess, "run", failed_run)
    job = foundry.create(create_params("failed-skill"))

    assert job["state"] == "FAILED"
    assert job["history"][-1]["state"] == "FAILED"
    assert job["failure"]["type"] == "FoundryError"
    assert not (tmp_path / "skills" / "failed-skill").exists()


def test_worker_timeout_is_recorded_without_installing(
    tmp_path, foundry_modules, monkeypatch
) -> None:
    controller, _ = foundry_modules
    foundry = controller.SkillFoundry(tmp_path)

    def timed_out(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=kwargs["timeout"])

    monkeypatch.setattr(controller.subprocess, "run", timed_out)
    job = foundry.create(create_params("timeout-skill"))

    assert job["state"] == "FAILED"
    assert job["failure"]["type"] == "TimeoutExpired"
    assert not (tmp_path / "skills" / "timeout-skill").exists()


def test_approval_confirmation_is_bound_to_reviewed_artifact_revision(
    tmp_path, foundry_modules
) -> None:
    controller, _ = foundry_modules
    foundry = controller.SkillFoundry(tmp_path)
    job = foundry.create(create_params("revision-check"))
    draft_file = (
        tmp_path
        / "data"
        / "skill-workbench"
        / job["job_id"]
        / "draft"
        / "SKILL.md"
    )
    draft_file.write_text(
        draft_file.read_text(encoding="utf-8") + "\n未经审核的变化\n",
        encoding="utf-8",
    )
    confirmation = job["approval"]["required_phrase"]

    with pytest.raises(controller.FoundryError, match="revision"):
        foundry.approve(
            {
                "job_id": job["job_id"],
                "confirmation": confirmation,
                **OWNER,
            }
        )
    assert not (tmp_path / "skills" / "revision-check").exists()


def test_install_rechecks_staging_manifest_before_replace(
    tmp_path, foundry_modules, monkeypatch
) -> None:
    controller, _ = foundry_modules
    foundry = controller.SkillFoundry(tmp_path)
    job = foundry.create(create_params("staging-revision"))
    confirmation = job["approval"]["required_phrase"]
    original_copytree = controller.shutil.copytree

    def tampering_copytree(source: Path, target: Path, **kwargs: Any) -> Path:
        # shutil.copytree 的递归也会按模块全局名回调 copytree；调用期间先恢复
        # 原函数，确保只在顶层复制完成后篡改一次暂存产物。
        controller.shutil.copytree = original_copytree
        try:
            result = original_copytree(source, target, **kwargs)
        finally:
            controller.shutil.copytree = tampering_copytree
        (target / "SKILL.md").write_text(
            (target / "SKILL.md").read_text(encoding="utf-8") + "\n复制窗口篡改\n",
            encoding="utf-8",
        )
        return result

    monkeypatch.setattr(controller.shutil, "copytree", tampering_copytree)
    with pytest.raises(controller.FoundryError, match="暂存内容.*revision"):
        foundry.approve(
            {
                "job_id": job["job_id"],
                "confirmation": confirmation,
                **OWNER,
            }
        )

    assert not (tmp_path / "skills" / "staging-revision").exists()
    staging = (
        tmp_path
        / "data"
        / "skill-workbench"
        / job["job_id"]
        / "installing"
    )
    assert staging.exists()
    assert "staging-revision" not in SkillCatalog(tmp_path / "skills").names
    restarted = controller.SkillFoundry(tmp_path)
    with pytest.raises(controller.FoundryError, match="暂存内容.*revision"):
        restarted.get({"job_id": job["job_id"], **OWNER})
    assert foundry._load(job["job_id"])["state"] == "INSTALLING"


def test_install_recovers_crash_after_copy_before_replace_without_breaking_catalog(
    tmp_path, foundry_modules, monkeypatch
) -> None:
    controller, _ = foundry_modules
    foundry = controller.SkillFoundry(tmp_path)
    job = foundry.create(create_params("recover-copied"))
    confirmation = job["approval"]["required_phrase"]
    staging = (
        tmp_path
        / "data"
        / "skill-workbench"
        / job["job_id"]
        / "installing"
    ).resolve()
    target = (tmp_path / "skills" / "recover-copied").resolve()
    original_replace = controller.os.replace

    def crash_after_copy(source: Path | str, destination: Path | str) -> None:
        if Path(source).resolve() == staging and Path(destination).resolve() == target:
            assert (staging / "SKILL.md").is_file()
            raise OSError("模拟复制完成、目录替换前崩溃")
        original_replace(source, destination)

    monkeypatch.setattr(controller.os, "replace", crash_after_copy)
    with pytest.raises(OSError, match="复制完成、目录替换前崩溃"):
        foundry.approve(
            {
                "job_id": job["job_id"],
                "confirmation": confirmation,
                **OWNER,
            }
        )
    monkeypatch.setattr(controller.os, "replace", original_replace)

    assert staging.is_dir()
    assert not target.exists()
    cold_catalog = SkillCatalog(tmp_path / "skills")
    assert "recover-copied" not in cold_catalog.names
    restarted = controller.SkillFoundry(tmp_path)
    recovered = restarted.get({"job_id": job["job_id"], **OWNER})
    assert recovered["state"] == "INSTALLED"
    assert [item["state"] for item in recovered["history"]][-2:] == [
        "INSTALLING",
        "INSTALLED",
    ]
    assert not staging.exists()
    assert (target / "SKILL.md").is_file()
    assert "recover-copied" in SkillCatalog(tmp_path / "skills").names


def test_install_recovers_crash_after_replace_before_terminal_save(
    tmp_path, foundry_modules
) -> None:
    controller, _ = foundry_modules
    foundry = controller.SkillFoundry(tmp_path)
    job = foundry.create(create_params("recover-replaced"))
    confirmation = job["approval"]["required_phrase"]
    original_save = foundry._save

    def crash_on_terminal_save(state: dict[str, Any]) -> None:
        if state["state"] == "INSTALLED":
            raise OSError("模拟替换后终态保存失败")
        original_save(state)

    foundry._save = crash_on_terminal_save
    with pytest.raises(OSError, match="终态保存失败"):
        foundry.approve(
            {
                "job_id": job["job_id"],
                "confirmation": confirmation,
                **OWNER,
            }
        )
    assert (tmp_path / "skills" / "recover-replaced" / "SKILL.md").is_file()

    restarted = controller.SkillFoundry(tmp_path)
    recovered = restarted.get({"job_id": job["job_id"], **OWNER})
    assert recovered["state"] == "INSTALLED"
    assert recovered["installed_path"] == "skills/recover-replaced"


def test_install_recovery_refuses_mismatched_external_target(
    tmp_path, foundry_modules
) -> None:
    controller, _ = foundry_modules
    foundry = controller.SkillFoundry(tmp_path)
    job = foundry.create(create_params("recover-conflict"))
    confirmation = job["approval"]["required_phrase"]

    def crash_before_replace(_: dict[str, Any]) -> None:
        raise OSError("模拟预提交后崩溃")

    foundry._reconcile_install = crash_before_replace
    with pytest.raises(OSError):
        foundry.approve(
            {
                "job_id": job["job_id"],
                "confirmation": confirmation,
                **OWNER,
            }
        )

    draft = (
        tmp_path / "data" / "skill-workbench" / job["job_id"] / "draft"
    )
    target = tmp_path / "skills" / "recover-conflict"
    shutil.copytree(draft, target)
    marker = target / "external-marker.md"
    marker.write_text("外部 Skill，不得覆盖。", encoding="utf-8")

    restarted = controller.SkillFoundry(tmp_path)
    with pytest.raises(controller.FoundryError, match="外部 Skill"):
        restarted.get({"job_id": job["job_id"], **OWNER})
    assert marker.read_text(encoding="utf-8") == "外部 Skill，不得覆盖。"
    assert restarted._load(job["job_id"])["state"] == "INSTALLING"


def test_corrupt_or_symlink_job_state_fails_as_foundry_error(
    tmp_path, foundry_modules, monkeypatch
) -> None:
    controller, _ = foundry_modules
    foundry = controller.SkillFoundry(tmp_path)
    job = foundry.create(create_params("corrupt-state"))
    state_path = (
        tmp_path / "data" / "skill-workbench" / job["job_id"] / "job.json"
    )
    state_path.write_text(
        json.dumps({"job_id": job["job_id"], "state": "REVIEW_PENDING"}),
        encoding="utf-8",
    )
    with pytest.raises(controller.FoundryError, match="字段不完整"):
        foundry.get({"job_id": job["job_id"], **OWNER})

    original_is_symlink = controller.Path.is_symlink

    def report_job_state_symlink(path: Path) -> bool:
        return path == state_path or original_is_symlink(path)

    monkeypatch.setattr(controller.Path, "is_symlink", report_job_state_symlink)
    with pytest.raises(controller.FoundryError, match="符号链接"):
        foundry.get({"job_id": job["job_id"], **OWNER})


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.__setitem__("owner", {"actor_id": "user-1"}),
        lambda value: value.__setitem__("created_at", 123),
        lambda value: value["request"].__setitem__("steps", "not-a-list"),
        lambda value: value.__setitem__("history", None),
        lambda value: value["checkpoints"][0].__setitem__("evidence", {}),
        lambda value: value["artifacts"][0].__setitem__("sha256", "broken"),
        lambda value: value.__setitem__("isolation", {"available": True}),
    ],
)
def test_authoritative_job_schema_rejects_corrupt_accessed_fields(
    tmp_path, foundry_modules, mutate
) -> None:
    controller, _ = foundry_modules
    foundry = controller.SkillFoundry(tmp_path)
    job = foundry.create(create_params("schema-check"))
    state_path = (
        tmp_path / "data" / "skill-workbench" / job["job_id"] / "job.json"
    )
    baseline = json.loads(state_path.read_text(encoding="utf-8"))
    corrupted = deepcopy(baseline)
    mutate(corrupted)
    state_path.write_text(
        json.dumps(corrupted, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(controller.FoundryError, match="Skill 构建任务"):
        foundry.get({"job_id": job["job_id"], **OWNER})


@pytest.mark.asyncio
async def test_managed_service_protocol_runs_real_controller(tmp_path) -> None:
    plugin_target = tmp_path / "plugins" / "skill_foundry"
    shutil.copytree(PROJECT_ROOT / "plugins" / "skill_foundry", plugin_target)
    catalog = PluginContributionCatalog(tmp_path)
    skills = SkillCatalog([tmp_path / "skills", *catalog.skill_roots])
    manager = ManagedServiceManager(catalog.managed_services)
    skill_plugins = SkillPluginManager(skills, capabilities=manager.capabilities)

    await manager.start()
    try:
        assert "skill-creator" in await skill_plugins.available_skills()
        assert {
            "create_skill_draft",
            "begin_skill_draft",
            "add_skill_script",
            "finalize_skill_draft",
            "get_skill_build",
            "preview_skill_draft_file",
            "run_skill_script",
            "install_skill_draft",
            "reject_skill_draft",
        }.issubset(skill_plugins.tool_names({"skill-creator"}))
        client = manager.capabilities["skill-builder"]
        ping = await client.request("ping", {})
        assert ping["available"] is True
        job = await client.request("create", create_params("managed-skill"))
        assert job["state"] == "REVIEW_PENDING"
        assert not (tmp_path / "skills" / "managed-skill").exists()
        script_params = create_params("managed-script")
        begun = await client.request("begin", script_params)
        assert begun["state"] == "DRAFT"
        script_job = await client.request(
            "add_script",
            {
                "job_id": begun["job_id"],
                "name": "double",
                "description": "将输入数字乘以二。",
                "content": "def main(data):\n    return {'value': data['value'] * 2}\n",
                "smoke_input": {"value": 2},
                "finalize": True,
                **OWNER,
            },
        )
        assert script_job["state"] == "REVIEW_PENDING", script_job.get("failure")
        installed = await client.request(
            "approve",
            {
                "job_id": script_job["job_id"],
                "confirmation": script_job["approval"]["required_phrase"],
                **OWNER,
            },
        )
        assert installed["state"] == "INSTALLED"
        executed = await client.request(
            "execute_script",
            {
                "skill_name": "managed-script",
                "script_name": "double",
                "input": {"value": 21},
                **OWNER,
            },
        )
        assert executed["result"] == {"value": 42}
        state_path = (
            tmp_path / "data" / "skill-workbench" / job["job_id"] / "job.json"
        )
        state_path.write_text(
            json.dumps({"job_id": job["job_id"], "state": "REVIEW_PENDING"}),
            encoding="utf-8",
        )
        with pytest.raises(ManagedServiceError, match="状态字段不完整"):
            await client.request("get", {"job_id": job["job_id"], **OWNER})
        assert (await client.request("ping", {}))["available"] is True
    finally:
        await manager.stop()

    shutil.rmtree(plugin_target)
    without_plugin = PluginContributionCatalog(tmp_path)
    assert without_plugin.skill_roots == ()
    assert without_plugin.managed_services == ()
    assert SkillCatalog(tmp_path / "skills").names == {"managed-script"}


class FakeClient:
    def __init__(self, job: dict[str, Any]) -> None:
        self.job = job
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        if method == "get":
            return self.job
        if method == "approve":
            return {**self.job, "state": "INSTALLED"}
        if method == "ping":
            return {"available": True}
        return self.job


@pytest.mark.asyncio
async def test_runtime_availability_uses_stateless_service_health_check(
    foundry_modules,
) -> None:
    _, runtime = foundry_modules

    class HealthClient:
        def __init__(self, available: bool) -> None:
            self.available = available
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((method, params))
            return {"available": self.available}

    healthy = HealthClient(True)
    unavailable = HealthClient(False)

    assert await runtime.SkillCreatorPlugin(healthy).is_available() is True
    assert await runtime.SkillCreatorPlugin(unavailable).is_available() is False
    assert healthy.calls == [("ping", {})]
    assert unavailable.calls == [("ping", {})]


@pytest.mark.asyncio
async def test_runtime_injects_non_model_identity_into_business_requests(
    foundry_modules,
) -> None:
    _, runtime = foundry_modules
    client = FakeClient({"job_id": "c" * 32, "state": "REVIEW_PENDING"})
    plugin = runtime.SkillCreatorPlugin(client)
    context = SimpleNamespace(
        loaded_skills={"skill-creator"},
        actor_id="user-1",
        session_id="session-1",
    )
    create_arguments = runtime.CreateDraftArguments(
        name="owned-skill",
        display_name="归属 Skill",
        description="用户明确要求创建归属测试 Skill 时使用。",
        objective="验证任务归属。",
    )

    await plugin.load(context)
    await plugin._create(create_arguments, context)
    await plugin._get(runtime.JobArguments(job_id="c" * 32), context)

    assert client.calls[0] == ("ping", OWNER)
    assert client.calls[1][0] == "create"
    assert client.calls[1][1]["actor_id"] == "user-1"
    assert client.calls[1][1]["session_id"] == "session-1"
    assert client.calls[2] == (
        "get",
        {"job_id": "c" * 32, **OWNER},
    )

    context.actor_id = None
    with pytest.raises(ValueError, match="actor_id"):
        await plugin._get(runtime.JobArguments(job_id="c" * 32), context)
    context.actor_id = "user-1"
    context.session_id = None
    with pytest.raises(ValueError, match="session_id"):
        await plugin._get(runtime.JobArguments(job_id="c" * 32), context)
    assert len(client.calls) == 3


@pytest.mark.asyncio
async def test_runtime_uses_staged_script_workflow_with_small_atomic_calls(
    foundry_modules,
) -> None:
    _, runtime = foundry_modules
    job_id = "d" * 32
    client = FakeClient({"job_id": job_id, "state": "DRAFT"})
    plugin = runtime.SkillCreatorPlugin(client)
    context = SimpleNamespace(
        loaded_skills={"skill-creator"},
        actor_id="user-1",
        session_id="session-1",
    )
    metadata = runtime.BeginDraftArguments(
        name="action-summary",
        display_name="行动项汇总",
        description="用户要求按负责人汇总行动项时使用。",
        objective="输出每位负责人的完成情况。",
        steps=["按负责人分组", "统计完成情况"],
    )

    await plugin._begin(metadata, context)
    await plugin._add_script(
        runtime.AddScriptArguments(
            job_id=job_id,
            name="summarize",
            description="汇总行动项。",
            content="def main(data):\n    return data\n",
            smoke_input={"items": []},
        ),
        context,
    )

    assert client.calls == [
        (
            "begin",
            {
                **metadata.model_dump(),
                **OWNER,
            },
        ),
        (
            "add_script",
            {
                "job_id": job_id,
                "name": "summarize",
                "description": "汇总行动项。",
                "content": "def main(data):\n    return data\n",
                "smoke_input": {"items": []},
                "finalize": True,
                **OWNER,
            },
        ),
    ]


@pytest.mark.asyncio
async def test_runtime_preview_propagates_bounded_chunk_cursor(
    foundry_modules,
) -> None:
    _, runtime = foundry_modules
    job_id = "f" * 32
    client = FakeClient({"job_id": job_id, "state": "REVIEW_PENDING"})
    plugin = runtime.SkillCreatorPlugin(client)
    context = SimpleNamespace(
        loaded_skills={"skill-creator"},
        actor_id="user-1",
        session_id="session-1",
    )

    await plugin._preview(
        runtime.PreviewArguments(
            job_id=job_id,
            path="SKILL.md",
            offset=2_000,
            chunk_size=1_500,
        ),
        context,
    )

    assert client.calls == [
        (
            "preview",
            {
                "job_id": job_id,
                "path": "SKILL.md",
                "offset": 2_000,
                "chunk_size": 1_500,
                **OWNER,
            },
        )
    ]
    with pytest.raises(ValueError):
        runtime.PreviewArguments(
            job_id=job_id,
            path="SKILL.md",
            offset=-1,
        )
    with pytest.raises(ValueError):
        runtime.PreviewArguments(
            job_id=job_id,
            path="SKILL.md",
            chunk_size=3_001,
        )


@pytest.mark.asyncio
async def test_runtime_runs_installed_script_with_host_identity(
    foundry_modules,
) -> None:
    _, runtime = foundry_modules
    client = FakeClient({"state": "INSTALLED"})
    plugin = runtime.SkillCreatorPlugin(client)
    context = SimpleNamespace(
        loaded_skills={"skill-creator"},
        actor_id="user-1",
        session_id="session-1",
    )

    result = await plugin._run_script(
        runtime.RunScriptArguments(
            skill_name="managed-script",
            script_name="double",
            input={"value": 21},
        ),
        context,
    )

    assert result["value"]["execution"] == {"state": "INSTALLED"}
    assert client.calls == [
        (
            "execute_script",
            {
                "skill_name": "managed-script",
                "script_name": "double",
                "input": {"value": 21},
                **OWNER,
            },
        )
    ]


@pytest.mark.asyncio
async def test_runtime_cannot_approve_without_phrase_in_current_user_message(
    foundry_modules,
) -> None:
    _, runtime = foundry_modules
    job_id = "a" * 32
    revision = "d" * 64
    phrase = f"确认全局安装 demo-skill {job_id} {revision}"
    client = FakeClient(
        {
            "job_id": job_id,
            "state": "REVIEW_PENDING",
            "approval": {"required_phrase": phrase},
        }
    )
    plugin = runtime.SkillCreatorPlugin(client)
    context = SimpleNamespace(
        loaded_skills={"skill-creator"},
        source_text=f"请不要执行：{phrase}",
        actor_id="user-1",
        session_id="session-1",
    )
    arguments = runtime.InstallArguments(job_id=job_id, confirmation=phrase)

    with pytest.raises(ValueError, match="只包含完整安装确认短语"):
        await plugin._install(arguments, context)
    assert [method for method, _ in client.calls] == ["get"]

    context.source_text = phrase
    result = await plugin._install(arguments, context)
    assert result["value"]["job"]["state"] == "INSTALLED"
    assert client.calls[-1] == (
        "approve",
        {
            "job_id": job_id,
            "confirmation": phrase,
            **OWNER,
        },
    )


@pytest.mark.asyncio
async def test_runtime_reject_requires_explicit_phrase_in_current_user_message(
    foundry_modules,
) -> None:
    _, runtime = foundry_modules
    job_id = "b" * 32
    revision = "e" * 64
    phrase = f"拒绝草案 demo-skill {job_id} {revision}"
    job = {
        "job_id": job_id,
        "state": "REVIEW_PENDING",
        "rejection": {"required_phrase": phrase},
    }
    client = FakeClient(job)
    plugin = runtime.SkillCreatorPlugin(client)
    context = SimpleNamespace(
        loaded_skills={"skill-creator"},
        source_text=f"我引用但不执行：{phrase}",
        actor_id="user-1",
        session_id="session-1",
    )
    arguments = runtime.RejectArguments(job_id=job_id)

    with pytest.raises(ValueError, match="只包含完整拒绝确认短语"):
        await plugin._reject(arguments, context)
    assert client.calls == [("get", {"job_id": job_id, **OWNER})]

    context.source_text = phrase
    result = await plugin._reject(arguments, context)
    assert result["value"]["job"] == job
    assert client.calls[-2:] == [
        ("get", {"job_id": job_id, **OWNER}),
        (
            "reject",
            {"job_id": job_id, "confirmation": phrase, **OWNER},
        ),
    ]
