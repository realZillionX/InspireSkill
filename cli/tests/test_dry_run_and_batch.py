import json
import importlib
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.main import main as cli_main
from inspire.cli.utils.quota_resolver import ResolvedQuota
from inspire.platform.web import browser_api as browser_api_module


class DummyAPI:
    def __init__(self) -> None:
        self.training_calls: list[dict[str, Any]] = []
        self.hpc_calls: list[dict[str, Any]] = []

    def create_training_job(
        self, *, payload: dict[str, Any], session: object | None = None
    ) -> dict[str, Any]:
        del session
        self.training_calls.append(payload)
        return {"job_id": f"job-{len(self.training_calls)}", "name": payload["name"]}

    def create_hpc_job(
        self, *, payload: dict[str, Any], session: object | None = None
    ) -> dict[str, Any]:
        del session
        self.hpc_calls.append(payload)
        return {"job_id": f"hpc-job-{len(self.hpc_calls)}", "name": payload["job_name"]}


class FakeWebSession:
    workspace_id = "ws-77777777-7777-7777-7777-777777777777"
    storage_state: dict[str, Any] = {}
    all_workspace_names = {
        "ws-77777777-7777-7777-7777-777777777777": "cpu",
    }


def _patch_submit_deps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    shm_size: int | None = None,
) -> DummyAPI:
    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
        job_priority=5,
        path_aliases={"me": str(tmp_path / "remote")},
    )
    config.shm_size = shm_size
    config.projects = {"proj": "project-12345678-1234-1234-1234-123456789abc"}
    config.profiles = {
        "job": {
            "h200": {
                "workspace": "cpu",
                "project": "proj",
                "group": "H200 Room",
                "quota": "1,20,200",
                "image": "registry.local/train:latest",
            }
        }
    }

    def fake_from_files_and_env(
        cls,
        require_credentials: bool = True,
    ) -> tuple[config_module.Config, dict[str, str]]:  # type: ignore[override]
        return config, {}

    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(fake_from_files_and_env),
    )

    api = DummyAPI()
    monkeypatch.setattr(browser_api_module, "create_training_job", api.create_training_job)
    monkeypatch.setattr(browser_api_module, "create_hpc_job", api.create_hpc_job)

    project = browser_api_module.ProjectInfo(
        project_id="project-12345678-1234-1234-1234-123456789abc",
        name="Project One",
        workspace_id="ws-77777777-7777-7777-7777-777777777777",
    )
    monkeypatch.setattr(
        browser_api_module,
        "list_projects",
        lambda workspace_id=None, session=None: [project],
    )
    monkeypatch.setattr(
        browser_api_module,
        "check_scheduling_health",
        lambda workspace_id=None, project_ids=None, session=None: {},
    )
    monkeypatch.setattr(
        browser_api_module,
        "select_project",
        lambda projects, requested=None, **_: (project, None),
    )
    image = browser_api_module.ImageInfo(
        image_id="image-12345678-1234-1234-1234-123456789abc",
        url="registry.batch/notebook:latest",
        name="registry.batch/notebook:latest",
        framework="pytorch",
        version="latest",
    )
    monkeypatch.setattr(
        browser_api_module,
        "list_images",
        lambda workspace_id=None, source=None, session=None: [image],
    )

    def fake_resolve_quota(*, spec, workspace_id, session=None, **kwargs):  # noqa: ANN001
        return ResolvedQuota(
            quota_id="quota-12345678-1234-1234-1234-123456789abc",
            logic_compute_group_id="lcg-12345678-1234-1234-1234-123456789abc",
            compute_group_name="H200 Room",
            gpu_count=spec.gpu_count,
            cpu_count=spec.cpu_count,
            memory_gib=spec.memory_gib,
            gpu_type="H200" if spec.gpu_count else "",
            raw_price={
                "cpu_info": {"cpu_type": "Test"},
                "gpu_info": {"gpu_type": "NVIDIA_H200_SXM_141G"},
            },
        )

    batch_module = importlib.import_module("inspire.cli.commands.batch")
    hpc_module = importlib.import_module("inspire.cli.commands.hpc.hpc_commands")
    job_create_module = importlib.import_module("inspire.cli.commands.job.job_create")
    job_submit_module = importlib.import_module("inspire.cli.utils.job_submit")
    quota_module = importlib.import_module("inspire.cli.utils.quota_resolver")

    monkeypatch.setattr(batch_module, "get_web_session", lambda: FakeWebSession())
    monkeypatch.setattr(batch_module, "resolve_quota", fake_resolve_quota)
    monkeypatch.setattr(hpc_module, "get_web_session", lambda: FakeWebSession())
    monkeypatch.setattr(job_create_module, "get_web_session", lambda: FakeWebSession())
    monkeypatch.setattr(job_create_module, "resolve_quota", fake_resolve_quota)
    monkeypatch.setattr(
        job_submit_module.web_session_module,
        "get_web_session",
        lambda: FakeWebSession(),
    )
    monkeypatch.setattr(quota_module, "resolve_quota", fake_resolve_quota)

    return api


def test_job_create_dry_run_resolves_plan_without_create_api(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = _patch_submit_deps(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "job",
            "create",
            "--name",
            "dry-job",
            "--quota",
            "1,20,200",
            "--command",
            "python train.py",
            "--workspace",
            "cpu",
            "--project",
            "proj",
            "--group",
            "H200 Room",
            "--image",
            "registry.local/train:latest",
            "--nodes",
            "1",
            "--exclude-node",
            "qb-prod-gpu1736",
            "--exclude-node",
            "qb-prod-gpu1736",
            "--exclude-node",
            "qb-prod-gpu1737",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["dry_run"] is True
    assert payload["data"]["create_kwargs"]["name"] == "dry-job"
    assert payload["data"]["create_kwargs"]["framework_config"][0]["exclude_nodes"] == [
        "qb-prod-gpu1736",
        "qb-prod-gpu1737",
    ]
    assert payload["data"]["project_name"] == "Project One"
    assert "project_id" not in payload["data"]["create_kwargs"]
    assert api.training_calls == []


def test_hpc_dry_run_human_scrubs_raw_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = _patch_submit_deps(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        cli_main,
        [
            "hpc",
            "create",
            "--name",
            "hpc-dry",
            "--entrypoint",
            "srun echo lcg-12345678-1234-1234-1234-123456789abc",
            "--group",
            "H200 Room",
            "--quota",
            "0,32,256",
            "--workspace",
            "cpu",
            "--project",
            "proj",
            "--image",
            "registry.local/hpc:latest",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "No HPC job was submitted." in result.output
    assert "lcg-12345678-1234-1234-1234-123456789abc" not in result.output
    assert "<compute-group-id>" in result.output
    assert api.hpc_calls == []


def test_job_create_profile_fills_condition_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = _patch_submit_deps(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "job",
            "create",
            "--name",
            "profile-job",
            "--profile",
            "h200",
            "--command",
            "python train.py",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    create_kwargs = payload["data"]["create_kwargs"]
    assert create_kwargs["name"] == "profile-job"
    assert create_kwargs["framework_config"][0]["image"] == "registry.local/train:latest"
    assert payload["data"]["project_name"] == "Project One"
    assert "project_id" not in create_kwargs
    assert api.training_calls == []


def test_job_create_shm_size_overrides_config_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = _patch_submit_deps(monkeypatch, tmp_path, shm_size=32)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "job",
            "create",
            "--name",
            "shm-job",
            "--profile",
            "h200",
            "--command",
            "python train.py",
            "--shm-size",
            "64",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    framework_config = payload["data"]["create_kwargs"]["framework_config"][0]
    assert framework_config["shm_gi"] == 64
    assert api.training_calls == []


def test_job_create_uses_config_shm_size_when_flag_absent(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = _patch_submit_deps(monkeypatch, tmp_path, shm_size=48)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "job",
            "create",
            "--name",
            "config-shm-job",
            "--profile",
            "h200",
            "--command",
            "python train.py",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    framework_config = payload["data"]["create_kwargs"]["framework_config"][0]
    assert framework_config["shm_gi"] == 48
    assert api.training_calls == []


def test_job_create_rejects_shm_size_above_quota_memory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = _patch_submit_deps(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "job",
            "create",
            "--name",
            "oversized-shm-job",
            "--profile",
            "h200",
            "--command",
            "python train.py",
            "--shm-size",
            "256",
            "--dry-run",
        ],
    )

    assert result.exit_code != 0
    assert (
        "Shared memory size (256 GiB) must be <= quota memory (200 GiB)"
        in result.output
    )
    assert api.training_calls == []


def test_job_create_rejects_config_shm_size_above_quota_memory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = _patch_submit_deps(monkeypatch, tmp_path, shm_size=256)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "job",
            "create",
            "--name",
            "oversized-config-shm-job",
            "--profile",
            "h200",
            "--command",
            "python train.py",
            "--dry-run",
        ],
    )

    assert result.exit_code != 0
    assert (
        "Shared memory size (256 GiB) must be <= quota memory (200 GiB)"
        in result.output
    )
    assert api.training_calls == []


def test_job_create_rejects_profile_with_explicit_condition_field(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = _patch_submit_deps(monkeypatch, tmp_path)

    result = CliRunner().invoke(
        cli_main,
        [
            "job",
            "create",
            "--name",
            "profile-job",
            "--profile",
            "h200",
            "--workspace",
            "cpu",
            "--command",
            "python train.py",
            "--dry-run",
        ],
    )

    assert result.exit_code != 0
    assert "--profile cannot be combined with scheduling fields: --workspace" in result.output
    assert api.training_calls == []


def test_batch_matrix_dry_run_expands_json_without_submit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = _patch_submit_deps(monkeypatch, tmp_path)
    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps(
            {
                "profiles": {
                    "job": {
                        "h200": {
                            "quota": "1,20,200",
                            "workspace": "cpu",
                            "project": "proj",
                            "group": "H200 Room",
                            "image": "registry.batch/train:latest",
                        }
                    }
                },
                "defaults": {
                    "type": "job",
                    "profile": "h200",
                    "priority": 7,
                    "framework": "pytorch",
                    "nodes": 1,
                    "max_time": 24,
                    "auto_fault_tolerance": False,
                    "fault_tolerance_max_retry": 0,
                    "exclude_nodes": ["qb-prod-gpu17{seed}"],
                    "shm_size": 96,
                },
                "matrix": {"seed": [1, 2]},
                "jobs": [
                    {
                        "name": "train-s{seed}",
                        "command": "python train.py --seed {seed}",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "job", "batch", str(batch_path), "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    items = payload["data"]["items"]
    assert [item["create_kwargs"]["name"] for item in items] == ["train-s1", "train-s2"]
    assert "--seed 2" in items[1]["create_kwargs"]["command"]
    assert items[0]["create_kwargs"]["framework_config"][0]["image"] == (
        "registry.batch/train:latest"
    )
    assert items[0]["create_kwargs"]["framework_config"][0]["exclude_nodes"] == [
        "qb-prod-gpu171"
    ]
    assert items[1]["create_kwargs"]["framework_config"][0]["exclude_nodes"] == [
        "qb-prod-gpu172"
    ]
    assert items[0]["create_kwargs"]["framework_config"][0]["shm_gi"] == 96
    assert items[1]["create_kwargs"]["framework_config"][0]["shm_gi"] == 96
    assert items[0]["create_kwargs"]["task_priority"] == 7
    assert api.training_calls == []


def test_batch_rejects_shm_size_above_quota_memory(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = _patch_submit_deps(monkeypatch, tmp_path)
    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps(
            {
                "profiles": {
                    "job": {
                        "h200": {
                            "quota": "1,20,200",
                            "workspace": "cpu",
                            "project": "proj",
                            "group": "H200 Room",
                            "image": "registry.batch/train:latest",
                        }
                    }
                },
                "defaults": {
                    "type": "job",
                    "profile": "h200",
                    "shm_size": 256,
                },
                "jobs": [
                    {"name": "train", "command": "python train.py"},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "job", "batch", str(batch_path), "--dry-run"],
    )

    assert result.exit_code != 0
    assert (
        "Shared memory size (256 GiB) must be <= quota memory (200 GiB)"
        in result.output
    )
    assert api.training_calls == []


def test_batch_requires_jobs_array(tmp_path: Path) -> None:
    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps(
            {
                "type": "job",
                "name": "train",
                "command": "python train.py",
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli_main, ["job", "batch", str(batch_path), "--dry-run"])

    assert result.exit_code != 0
    assert "jobs must be a non-empty array" in result.output


def test_batch_matrix_submit_calls_create_for_each_item(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = _patch_submit_deps(monkeypatch, tmp_path)
    batch_path = tmp_path / "batch.toml"
    batch_path.write_text(
        """
[profiles.job.h200]
quota = "1,20,200"
workspace = "cpu"
project = "proj"
group = "H200 Room"
image = "registry.batch/train:latest"

[defaults]
type = "job"
profile = "h200"
priority = 7
framework = "pytorch"
nodes = 1
max_time = 24
auto_fault_tolerance = false
fault_tolerance_max_retry = 0

[matrix]
lr = ["1e-4", "2e-4"]

[[jobs]]
name = "train-{lr}"
command = "python train.py --lr {lr}"
""".strip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli_main, ["job", "batch", str(batch_path)])

    assert result.exit_code == 0, result.output
    assert "Submitted 2 job batch item(s)" in result.output
    assert [call["name"] for call in api.training_calls] == ["train-1e-4", "train-2e-4"]
    assert {call["framework_config"][0]["image"] for call in api.training_calls} == {
        "registry.batch/train:latest"
    }
    assert {call["task_priority"] for call in api.training_calls} == {7}


def test_batch_does_not_fall_back_to_config_job_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = _patch_submit_deps(monkeypatch, tmp_path)
    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps(
            {
                "profiles": {
                    "job": {
                        "h200": {
                            "quota": "1,20,200",
                            "workspace": "cpu",
                            "project": "proj",
                            "group": "H200 Room",
                        }
                    }
                },
                "defaults": {
                    "type": "job",
                    "profile": "h200",
                    "priority": 7,
                    "framework": "pytorch",
                    "nodes": 1,
                    "max_time": 24,
                    "auto_fault_tolerance": False,
                    "fault_tolerance_max_retry": 0,
                },
                "jobs": [
                    {"name": "train", "command": "python train.py"},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli_main, ["job", "batch", str(batch_path), "--dry-run"])

    assert result.exit_code != 0
    assert "missing required condition field: image" in result.output
    assert api.training_calls == []


def test_batch_rejects_profile_merged_with_condition_override(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_submit_deps(monkeypatch, tmp_path)
    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps(
            {
                "profiles": {
                    "job": {
                        "h200": {
                            "quota": "1,20,200",
                            "workspace": "cpu",
                            "project": "proj",
                            "group": "H200 Room",
                            "image": "registry.batch/default:latest",
                        }
                    }
                },
                "defaults": {
                    "type": "job",
                    "profile": "h200",
                    "priority": 6,
                    "framework": "pytorch",
                    "nodes": 1,
                    "max_time": 24,
                    "auto_fault_tolerance": False,
                    "fault_tolerance_max_retry": 0,
                },
                "jobs": [
                    {"name": "train-default", "command": "python train.py"},
                    {
                        "name": "train-override",
                        "command": "python train.py",
                        "image": "registry.batch/override:latest",
                        "priority": 8,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "job", "batch", str(batch_path), "--dry-run"],
    )

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert "--profile cannot be combined with scheduling fields: --image" in payload["error"][
        "message"
    ]


def test_notebook_batch_matrix_dry_run_expands_json_without_submit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_submit_deps(monkeypatch, tmp_path)
    batch_path = tmp_path / "notebooks.json"
    batch_path.write_text(
        json.dumps(
            {
                "profiles": {
                    "notebook": {
                        "cpu": {
                            "quota": "0,4,32",
                            "workspace": "cpu",
                            "project": "proj",
                            "group": "H200 Room",
                            "image": "registry.batch/notebook:latest",
                        }
                    }
                },
                "defaults": {
                    "type": "notebook",
                    "profile": "cpu",
                    "priority": 5,
                    "shm_size": 32,
                    "auto_stop": False,
                },
                "matrix": {"seed": [1, 2]},
                "notebooks": [
                    {"name": "nb-s{seed}"},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "notebook", "batch", str(batch_path), "--dry-run"],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    items = payload["data"]["items"]
    assert [item["create_kwargs"]["name"] for item in items] == ["nb-s1", "nb-s2"]
    assert items[0]["kind"] == "notebook"
    assert items[0]["create_kwargs"]["shared_memory_size"] == 32


def test_batch_requires_training_fields_after_expansion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = _patch_submit_deps(monkeypatch, tmp_path)
    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps(
            {
                "profiles": {
                    "job": {
                        "h200": {
                            "quota": "1,20,200",
                            "workspace": "cpu",
                            "project": "proj",
                            "group": "H200 Room",
                            "image": "registry.batch/train:latest",
                        }
                    }
                },
                "defaults": {
                    "type": "job",
                    "profile": "h200",
                    "priority": 7,
                    "framework": "pytorch",
                    "nodes": 1,
                    "max_time": 24,
                    "auto_fault_tolerance": False,
                    "fault_tolerance_max_retry": 0,
                },
                "matrix": {"cmd": [""]},
                "jobs": [
                    {"name": "train", "command": "{cmd}"},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli_main, ["job", "batch", str(batch_path), "--dry-run"])

    assert result.exit_code != 0
    assert "missing required string field: command" in result.output
    assert api.training_calls == []


def test_batch_hpc_requires_fields_after_expansion(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = _patch_submit_deps(monkeypatch, tmp_path)
    batch_path = tmp_path / "batch.json"
    batch_path.write_text(
        json.dumps(
            {
                "profiles": {
                    "hpc": {
                        "cpu": {
                            "quota": "0,32,256",
                            "workspace": "cpu",
                            "project": "proj",
                            "group": "H200 Room",
                        }
                    }
                },
                "defaults": {
                    "type": "hpc",
                    "profile": "cpu",
                    "image_type": "SOURCE_PRIVATE",
                    "priority": 7,
                    "instance_count": 1,
                    "number_of_tasks": 1,
                    "memory_per_cpu": 8,
                    "enable_hyper_threading": False,
                },
                "jobs": [
                    {"name": "hpc", "entrypoint": "srun python train.py"},
                ],
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli_main, ["hpc", "batch", str(batch_path), "--dry-run"])

    assert result.exit_code != 0
    assert "missing required condition field: image" in result.output
    assert api.hpc_calls == []
