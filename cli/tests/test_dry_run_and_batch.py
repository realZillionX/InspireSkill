import json
import importlib
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.main import main as cli_main
from inspire.cli.utils import job_submit
from inspire.cli.utils.quota_resolver import ResolvedQuota
from inspire.platform.web import browser_api as browser_api_module


class DummyAPI:
    def __init__(self) -> None:
        self.training_calls: list[dict[str, Any]] = []
        self.hpc_calls: list[dict[str, Any]] = []
        self.train_capability_calls: list[str] = []
        self.project_list_calls = 0
        self.scheduling_health_calls = 0
        self.priority_menu_calls = 0
        self.image_catalog_calls: list[str] = []

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


def _assert_public_batch_output(value: Any) -> None:
    forbidden = {
        "create_body",
        "create_kwargs",
        "debug",
        "payload",
        "progress",
        "raw",
        "request",
        "response",
        "result",
        "scanned",
        "source",
    }
    if isinstance(value, dict):
        for key, child in value.items():
            normalized = str(key).replace("-", "_").lower()
            assert normalized not in forbidden
            assert normalized not in {"id", "ids"}
            assert not normalized.endswith(("_id", "_ids"))
            _assert_public_batch_output(child)
    elif isinstance(value, list):
        for child in value:
            _assert_public_batch_output(child)


def _patch_submit_deps(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    shm_size: int | None = None,
    enable_notification: bool = False,
) -> DummyAPI:
    config = config_module.Config(
        username="user",
        password="pass",
        base_url="https://example.invalid",
        path_aliases={"me": str(tmp_path / "remote")},
    )
    config.shm_size = shm_size
    config.job_enable_notification = enable_notification
    config.projects = {"proj": "Project One"}
    config.profiles = {
        "job": {
            "h200": {
                "workspace": "cpu",
                "project": "Project One",
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
    def get_train_schedule_capabilities(
        workspace_id: str,
        session: object | None = None,
    ) -> browser_api_module.TrainScheduleCapabilities:
        del session
        api.train_capability_calls.append(workspace_id)
        return browser_api_module.TrainScheduleCapabilities(specified_nodes=True)

    monkeypatch.setattr(
        browser_api_module,
        "get_train_schedule_capabilities",
        get_train_schedule_capabilities,
    )

    project = browser_api_module.ProjectInfo(
        project_id="project-12345678-1234-1234-1234-123456789abc",
        name="Project One",
        workspace_id="ws-77777777-7777-7777-7777-777777777777",
    )
    def list_projects(workspace_id=None, session=None):  # noqa: ANN001
        del workspace_id, session
        api.project_list_calls += 1
        return [project]

    def check_scheduling_health(workspace_id=None, project_ids=None, session=None):  # noqa: ANN001
        del workspace_id, project_ids, session
        api.scheduling_health_calls += 1
        return set()

    monkeypatch.setattr(browser_api_module, "list_projects", list_projects)
    monkeypatch.setattr(
        browser_api_module,
        "check_scheduling_health",
        check_scheduling_health,
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
        priority_loader = kwargs.get("priority_levels_loader")
        if priority_loader is not None:
            priority_loader()
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
    projects_module = importlib.import_module("inspire.platform.web.browser_api.projects")
    images_module = importlib.import_module("inspire.platform.web.browser_api.images")
    quota_module = importlib.import_module("inspire.cli.utils.quota_resolver")

    def load_priority_levels(**_kwargs):
        api.priority_menu_calls += 1
        return {}

    def list_images_by_source(*, source, session=None, workspace_id=None):  # noqa: ANN001
        del session, workspace_id
        api.image_catalog_calls.append(source)
        if source != "official":
            return []
        return [
            browser_api_module.CustomImageInfo(
                image_id="image-train",
                url="registry.batch/train:latest",
                name="train-image",
                framework="pytorch",
                version="v1",
                source="SOURCE_OFFICIAL",
                status="READY",
                description="",
                created_at="",
            )
        ]

    monkeypatch.setattr(batch_module, "get_web_session", lambda: FakeWebSession())
    monkeypatch.setattr(batch_module, "resolve_quota", fake_resolve_quota)
    monkeypatch.setattr(
        batch_module,
        "load_quota_priority_levels",
        load_priority_levels,
    )
    monkeypatch.setattr(images_module, "list_images_by_source", list_images_by_source)
    monkeypatch.setattr(hpc_module, "get_web_session", lambda: FakeWebSession())
    monkeypatch.setattr(projects_module, "list_projects", lambda **_kwargs: [project])
    monkeypatch.setattr(job_create_module, "get_web_session", lambda: FakeWebSession())
    monkeypatch.setattr(job_create_module, "resolve_quota", fake_resolve_quota)
    monkeypatch.setattr(
        job_submit_module.web_session_module,
        "get_web_session",
        lambda: FakeWebSession(),
    )
    monkeypatch.setattr(quota_module, "resolve_quota", fake_resolve_quota)

    return api


def _write_job_batch(path: Path, *, count: int) -> None:
    path.write_text(
        json.dumps(
            {
                "defaults": {
                    "type": "job",
                    "profile": "h200",
                    "priority": 7,
                    "framework": "pytorch",
                    "nodes": 1,
                },
                "matrix": {"case": list(range(count))},
                "jobs": [
                    {
                        "name": "train-{case}",
                        "command": "python train.py --case {case}",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


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
            "Project One",
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
            "--specified-node",
            "qb-prod-gpu1701",
            "--specified-node",
            "qb-prod-gpu1701",
            "--specified-node",
            "qb-prod-gpu1702",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["success"] is True
    assert payload["data"]["name"] == "dry-job"
    assert payload["data"]["workspace"] == "cpu"
    assert payload["data"]["project"] == "Project One"
    assert payload["data"]["compute_group"] == "H200 Room"
    assert payload["data"]["resource"] == {
        "gpu": 1,
        "cpu": 20,
        "memory_gib": 200,
        "gpu_type": "H200",
    }
    assert payload["data"]["enable_notification"] is False
    assert payload["data"]["exclude_nodes"] == [
        "qb-prod-gpu1736",
        "qb-prod-gpu1737",
    ]
    assert payload["data"]["specified_nodes"] == [
        "qb-prod-gpu1701",
        "qb-prod-gpu1702",
    ]
    _assert_public_batch_output(payload["data"])
    assert api.training_calls == []


def test_job_create_rejects_a_node_that_is_both_specified_and_excluded(
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
            "contradictory-placement",
            "--quota",
            "1,20,200",
            "--command",
            "python train.py",
            "--workspace",
            "cpu",
            "--project",
            "Project One",
            "--group",
            "H200 Room",
            "--image",
            "registry.local/train:latest",
            "--exclude-node",
            "qb-prod-gpu1701",
            "--specified-node",
            "qb-prod-gpu1701",
            "--dry-run",
        ],
    )

    assert result.exit_code != 0
    assert "cannot be both specified and excluded" in result.output
    assert api.training_calls == []


def test_job_create_reports_a_rate_limited_catalog_not_an_empty_one(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Issue #68: `(workspace has no quotas)` was the visible face of the bug."""
    from inspire.cli.utils.quota_resolver import resolve_quota as real_resolve_quota
    from inspire.platform.web.session import TransientAPIError

    # Bound before the shared harness swaps in its stub, so this exercises the
    # real resolver against a rate-limited price loader.
    _patch_submit_deps(monkeypatch, tmp_path)
    job_create_module = importlib.import_module("inspire.cli.commands.job.job_create")

    def _real_resolve(**kwargs):  # noqa: ANN202
        return real_resolve_quota(
            **kwargs,
            groups=[{"logic_compute_group_id": "lcg-a", "name": "H200 Room"}],
            prices_loader=lambda _group_id: (_ for _ in ()).throw(
                TransientAPIError("API returned 429: Too Many Requests", status=429)
            ),
        )

    monkeypatch.setattr(job_create_module, "resolve_quota", _real_resolve)

    result = CliRunner().invoke(
        cli_main,
        [
            "job",
            "create",
            "--name",
            "quota-cache-repro",
            "--quota",
            "1,10,100",
            "--command",
            "echo ok",
            "--workspace",
            "cpu",
            "--project",
            "Project One",
            "--group",
            "H200 Room",
            "--image",
            "registry.local/train:latest",
            "--dry-run",
        ],
    )

    assert result.exit_code != 0
    assert "workspace has no quotas" not in result.output
    assert "429" in result.output


@pytest.mark.parametrize(
    ("config_default", "flag", "expected"),
    (
        (False, "--enable-notification", True),
        (True, "--no-enable-notification", False),
        (True, None, True),
    ),
)
def test_job_create_notification_precedence_controls_top_level_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    config_default: bool,
    flag: str | None,
    expected: bool,
) -> None:
    api = _patch_submit_deps(
        monkeypatch,
        tmp_path,
        enable_notification=config_default,
    )

    args = [
        "--json",
        "job",
        "create",
        "--name",
        "notify-job",
        "--profile",
        "h200",
        "--command",
        "python train.py",
    ]
    if flag is not None:
        args.append(flag)
    args.append("--dry-run")

    result = CliRunner().invoke(
        cli_main,
        args,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"]["enable_notification"] is expected
    assert api.training_calls == []


def test_job_create_notification_reaches_live_create_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = _patch_submit_deps(monkeypatch, tmp_path, enable_notification=True)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "job",
            "create",
            "--name",
            "notify-job",
            "--profile",
            "h200",
            "--command",
            "python train.py",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"] == {
        "name": "notify-job",
        "status": "created",
    }
    assert api.training_calls[0]["enable_notification"] is True
    assert "enable_notification" not in api.training_calls[0]["framework_config"][0]


def test_training_plan_exclude_nodes_reads_top_level_payload() -> None:
    plan = job_submit.JobSubmissionPlan(
        create_kwargs={
            "exclude_nodes": ["qb-prod-gpu1736"],
            "framework_config": [{"exclude_nodes": ["nested-node"]}],
        },
        log_path=None,
        wrapped_command="bash -c 'echo hi'",
        max_time_ms=None,
        project_name="Project One",
        workspace_id="ws-77777777-7777-7777-7777-777777777777",
        quota=ResolvedQuota(
            quota_id="quota-12345678-1234-1234-1234-123456789abc",
            logic_compute_group_id="lcg-12345678-1234-1234-1234-123456789abc",
            compute_group_name="H200 Room",
            gpu_count=1,
            cpu_count=20,
            memory_gib=200,
            gpu_type="H200",
            raw_price={},
        ),
    )

    assert job_submit.training_plan_exclude_nodes(plan) == ["qb-prod-gpu1736"]


def test_training_plan_specified_nodes_reads_top_level_payload() -> None:
    plan = job_submit.JobSubmissionPlan(
        create_kwargs={
            "specified_nodes": ["qb-prod-gpu1701"],
            "framework_config": [{"specified_nodes": ["nested-node"]}],
        },
        log_path=None,
        wrapped_command="bash -c 'echo hi'",
        max_time_ms=None,
        project_name="Project One",
        workspace_id="ws-77777777-7777-7777-7777-777777777777",
        quota=ResolvedQuota(
            quota_id="quota-12345678-1234-1234-1234-123456789abc",
            logic_compute_group_id="lcg-12345678-1234-1234-1234-123456789abc",
            compute_group_name="H200 Room",
            gpu_count=1,
            cpu_count=20,
            memory_gib=200,
            gpu_type="H200",
            raw_price={},
        ),
    )

    assert job_submit.training_plan_specified_nodes(plan) == ["qb-prod-gpu1701"]


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
    assert result.output.startswith("Create plan: hpc-dry\n")
    assert "No HPC job was submitted." not in result.output
    assert "lcg-12345678-1234-1234-1234-123456789abc" not in result.output
    assert "<redacted>" in result.output
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
    assert payload["data"]["name"] == "profile-job"
    assert payload["data"]["image"] == "registry.local/train:latest"
    assert payload["data"]["project"] == "Project One"
    _assert_public_batch_output(payload["data"])
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
    assert payload["data"]["shared_memory_gib"] == 64
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
    assert payload["data"]["shared_memory_gib"] == 48
    assert api.training_calls == []


def test_job_create_human_dry_run_shows_resolved_shm_size(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = _patch_submit_deps(monkeypatch, tmp_path, shm_size=40)

    result = CliRunner().invoke(
        cli_main,
        [
            "job",
            "create",
            "--name",
            "human-shm-job",
            "--profile",
            "h200",
            "--command",
            "python train.py",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Shared memory: 40 GiB" in result.output
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
                            "project": "Project One",
                            "group": "H200 Room",
                            "image": "train-image:v1",
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
                    "enable_notification": True,
                    "exclude_nodes": ["qb-prod-gpu17{seed}"],
                    "specified_nodes": ["qb-prod-gpu18{seed}"],
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
    assert [item["name"] for item in items] == ["train-s1", "train-s2"]
    assert items[0]["workspace"] == "cpu"
    assert items[0]["project"] == "Project One"
    assert items[0]["compute_group"] == "H200 Room"
    assert items[0]["image"] == "train-image:v1"
    assert items[1]["command"] == "python train.py --seed 2"
    assert items[0]["exclude_nodes"] == ["qb-prod-gpu171"]
    assert items[1]["exclude_nodes"] == ["qb-prod-gpu172"]
    assert items[0]["specified_nodes"] == ["qb-prod-gpu181"]
    assert items[1]["specified_nodes"] == ["qb-prod-gpu182"]
    assert api.train_capability_calls == [
        "ws-77777777-7777-7777-7777-777777777777"
    ]
    assert api.project_list_calls == 1
    assert api.scheduling_health_calls == 1
    assert api.priority_menu_calls == 1
    assert api.image_catalog_calls == ["official"]
    assert items[0]["shared_memory_gib"] == 96
    assert items[1]["shared_memory_gib"] == 96
    assert items[0]["priority"] == 7
    assert items[0]["notifications"] is True
    assert set(payload["data"]) == {"items"}
    _assert_public_batch_output(payload["data"])
    assert api.training_calls == []


def test_job_batch_rejects_specified_nodes_when_workspace_disables_them(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = _patch_submit_deps(monkeypatch, tmp_path)
    monkeypatch.setattr(
        browser_api_module,
        "get_train_schedule_capabilities",
        lambda workspace_id, session=None: browser_api_module.TrainScheduleCapabilities(
            specified_nodes=False
        ),
    )
    batch_path = tmp_path / "disabled-specified-nodes.json"
    batch_path.write_text(
        json.dumps(
            {
                "defaults": {"type": "job", "profile": "h200"},
                "jobs": [
                    {
                        "name": "pinned-job",
                        "command": "python train.py",
                        "specified_nodes": ["qb-prod-gpu181"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli_main,
        ["job", "batch", str(batch_path), "--dry-run"],
    )

    assert result.exit_code != 0
    assert "does not enable specified-node placement" in result.output
    assert api.training_calls == []


@pytest.mark.parametrize(
    ("output_args", "expected_shown", "expected_metadata"),
    (
        (
            (),
            20,
            {"shown": 20, "total": 25, "truncated": True},
        ),
        (
            ("--all",),
            25,
            {},
        ),
        (
            ("--limit", "7"),
            7,
            {"shown": 7, "total": 25, "truncated": True},
        ),
    ),
)
def test_job_batch_result_output_budget_does_not_limit_execution(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    output_args: tuple[str, ...],
    expected_shown: int,
    expected_metadata: dict[str, int | bool],
) -> None:
    api = _patch_submit_deps(monkeypatch, tmp_path)
    batch_path = tmp_path / "budget.json"
    _write_job_batch(batch_path, count=25)

    result = CliRunner().invoke(
        cli_main,
        ["--json", "job", "batch", str(batch_path), *output_args],
    )

    assert result.exit_code == 0, result.output
    assert len(api.training_calls) == 25
    payload = json.loads(result.output)["data"]
    assert [item["name"] for item in payload["items"]] == [
        f"train-{index}" for index in range(expected_shown)
    ]
    assert set(payload) == {"items", *expected_metadata}
    for key, value in expected_metadata.items():
        assert payload[key] == value
    _assert_public_batch_output(payload)


def test_job_batch_rejects_limit_with_all_before_loading_batch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    batch_path = tmp_path / "conflict.json"
    batch_path.write_text("{}", encoding="utf-8")
    batch_module = importlib.import_module("inspire.cli.commands.batch")

    def fail_before_api(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("conflicting output options must fail before batch/API work")

    monkeypatch.setattr(batch_module, "_load_config", fail_before_api)
    monkeypatch.setattr(batch_module, "get_web_session", fail_before_api)
    monkeypatch.setattr(
        batch_module.browser_api_module,
        "create_training_job",
        fail_before_api,
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "job",
            "batch",
            str(batch_path),
            "--all",
            "--limit",
            "3",
        ],
    )

    assert result.exit_code != 0
    payload = json.loads(result.output)
    assert payload["success"] is False
    assert payload["error"]["type"] == "ValidationError"
    assert (
        payload["error"]["message"]
        == "Use either --limit or --all, not both."
    )


def test_batch_notification_item_overrides_config_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = _patch_submit_deps(monkeypatch, tmp_path, enable_notification=True)
    batch_path = tmp_path / "notification-default.json"
    batch_path.write_text(
        json.dumps(
            {
                "profiles": {
                    "job": {
                        "h200": {
                            "quota": "1,20,200",
                            "workspace": "cpu",
                            "project": "Project One",
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
                },
                "jobs": [
                    {"name": "inherits", "command": "python train.py"},
                    {
                        "name": "disabled",
                        "command": "python train.py",
                        "enable_notification": False,
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

    assert result.exit_code == 0, result.output
    items = json.loads(result.output)["data"]["items"]
    assert [item["notifications"] for item in items] == [
        True,
        False,
    ]
    _assert_public_batch_output(items)
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
                            "project": "Project One",
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


def test_batch_rejects_platform_ids_in_name_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = _patch_submit_deps(monkeypatch, tmp_path)
    raw_project_id = "project-12345678-1234-1234-1234-123456789abc"
    batch_path = tmp_path / "raw-id.json"
    batch_path.write_text(
        json.dumps(
            {
                "profiles": {
                    "job": {
                        "bad": {
                            "quota": "1,20,200",
                            "workspace": "cpu",
                            "project": raw_project_id,
                            "group": "H200 Room",
                            "image": "registry.batch/train:latest",
                        }
                    }
                },
                "jobs": [
                    {
                        "type": "job",
                        "profile": "bad",
                        "name": "train",
                        "command": "python train.py",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli_main,
        ["job", "batch", str(batch_path), "--dry-run"],
    )

    assert result.exit_code != 0
    assert "project name" in result.output
    assert raw_project_id not in result.output
    assert api.training_calls == []


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
project = "Project One"
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
enable_notification = true

[matrix]
lr = ["1e-4", "2e-4"]

[[jobs]]
name = "train-{lr}"
command = "python train.py --lr {lr}"
""".strip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "job", "batch", str(batch_path)],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["data"] == {
        "items": [
            {"name": "train-1e-4"},
            {"name": "train-2e-4"},
        ],
    }
    _assert_public_batch_output(payload["data"])
    assert [call["name"] for call in api.training_calls] == ["train-1e-4", "train-2e-4"]
    assert {call["framework_config"][0]["image"] for call in api.training_calls} == {
        "registry.batch/train:latest"
    }
    assert {call["task_priority"] for call in api.training_calls} == {7}
    assert {call["enable_notification"] for call in api.training_calls} == {True}


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
                            "project": "Project One",
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
                            "project": "Project One",
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
    assert [item["name"] for item in items] == ["nb-s1", "nb-s2"]
    assert items[0]["workspace"] == "cpu"
    assert items[0]["project"] == "Project One"
    assert items[0]["compute_group"] == "H200 Room"
    assert items[0]["image"] == "registry.batch/notebook:latest"
    assert items[0]["shared_memory_gib"] == 32
    _assert_public_batch_output(payload["data"])


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
                            "project": "Project One",
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


def _patch_batch_dataset_resolution(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Record what the batch path asks the platform to validate."""
    batch_module = importlib.import_module("inspire.cli.commands.batch")
    seen: list[Any] = []

    def fake_resolve(mounts, *, workspace_id, session=None):  # noqa: ANN001, ANN202
        seen.append((list(mounts), workspace_id))
        return [
            {
                "dataset_id": m.dataset,
                "version_id": m.version,
                "path": f"store/{m.dataset}/{m.version}",
            }
            for m in mounts
        ]

    monkeypatch.setattr(batch_module, "resolve_dataset_info", fake_resolve)
    return seen


def test_job_batch_entry_carries_datasets_env_and_reservations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = _patch_submit_deps(monkeypatch, tmp_path)
    seen = _patch_batch_dataset_resolution(monkeypatch)
    batch_path = tmp_path / "batch.toml"
    batch_path.write_text(
        """
[profiles.job.h200]
quota = "1,20,200"
workspace = "cpu"
project = "Project One"
group = "H200 Room"
image = "registry.batch/train:latest"

[defaults]
type = "job"
profile = "h200"
nodes = 1

[[jobs]]
name = "train"
command = "python train.py"
dataset = ["pixabay-81k:v0", "videoufo:v1"]
env = { HF_HOME = "/tmp/hf", RANK = 0 }
description = "batch smoke"
keep_after_success = 1
keep_after_failure = 2
public_path_readonly = true
auto_fault_tolerance = true
fault_tolerance_retry_interval = 30
""".strip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli_main, ["--json", "job", "batch", str(batch_path)])

    assert result.exit_code == 0, result.output
    assert len(api.training_calls) == 1
    payload = api.training_calls[0]
    assert payload["dataset_info"] == [
        {"dataset_id": "pixabay-81k", "version_id": "v0", "path": "store/pixabay-81k/v0"},
        {"dataset_id": "videoufo", "version_id": "v1", "path": "store/videoufo/v1"},
    ]
    assert payload["envs"] == [
        {"name": "HF_HOME", "value": "/tmp/hf"},
        {"name": "RANK", "value": "0"},
    ]
    assert payload["description"] == "batch smoke"
    assert payload["reserve_on_success_ms"] == str(1 * 3600 * 1000)
    assert payload["reserve_on_fail_ms"] == str(2 * 3600 * 1000)
    assert payload["is_publicpath_readonly"] is True
    assert payload["fault_tolerance_retry_interval_sec"] == 30
    # The workspace the mounts were validated against is the item's own.
    assert [spec.dataset for spec, _ in [(m, w) for mounts, w in seen for m in mounts]] == [
        "pixabay-81k",
        "videoufo",
    ]


def test_job_batch_entry_without_new_fields_sends_todays_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An entry that never mentions the new keys must not gain new payload keys."""
    api = _patch_submit_deps(monkeypatch, tmp_path)
    _patch_batch_dataset_resolution(monkeypatch)
    batch_path = tmp_path / "batch.json"
    _write_job_batch(batch_path, count=1)

    result = CliRunner().invoke(cli_main, ["--json", "job", "batch", str(batch_path)])

    assert result.exit_code == 0, result.output
    payload = api.training_calls[0]
    for absent in (
        "dataset_info",
        "envs",
        "description",
        "reserve_on_success_ms",
        "reserve_on_fail_ms",
        "is_publicpath_readonly",
        "fault_tolerance_retry_interval_sec",
    ):
        assert absent not in payload, f"{absent} leaked into an entry that never set it"


def test_hpc_batch_entry_carries_dataset_and_retention(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = _patch_submit_deps(monkeypatch, tmp_path)
    _patch_batch_dataset_resolution(monkeypatch)
    batch_path = tmp_path / "batch.toml"
    batch_path.write_text(
        """
[profiles.hpc.cpu]
quota = "0,20,100"
workspace = "cpu"
project = "Project One"
group = "H200 Room"
image = "registry.batch/hpc:latest"

[defaults]
type = "hpc"
profile = "cpu"

[[jobs]]
name = "prep"
entrypoint = "srun python prep.py"
dataset = "pixabay-81k:v0"
description = "hpc smoke"
keep_after_finish = 0.5
max_time = 3
public_path_readonly = true
enable_notification = true
""".strip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli_main, ["--json", "hpc", "batch", str(batch_path)])

    assert result.exit_code == 0, result.output
    payload = api.hpc_calls[0]
    assert payload["dataset_info"] == [
        {"dataset_id": "pixabay-81k", "version_id": "v0", "path": "store/pixabay-81k/v0"}
    ]
    assert payload["description"] == "hpc smoke"
    assert payload["ttl_after_job_finish_seconds"] == 1800
    assert payload["is_publicpath_readonly"] is True
    assert payload["enable_notification"] is True
    assert payload["sbatch_script"]["job_max_time"] == "0-03:00:00"


def test_batch_rejects_a_malformed_dataset_spec_before_submitting(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    api = _patch_submit_deps(monkeypatch, tmp_path)
    _patch_batch_dataset_resolution(monkeypatch)
    batch_path = tmp_path / "batch.toml"
    batch_path.write_text(
        """
[profiles.job.h200]
quota = "1,20,200"
workspace = "cpu"
project = "Project One"
group = "H200 Room"
image = "registry.batch/train:latest"

[defaults]
type = "job"
profile = "h200"
nodes = 1

[[jobs]]
name = "train"
command = "python train.py"
dataset = "pixabay-81k"
""".strip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli_main, ["job", "batch", str(batch_path)])

    assert result.exit_code != 0
    assert "<dataset>:<version>" in result.output
    assert api.training_calls == []


def test_ray_batch_entry_carries_the_readonly_guard(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A batch entry must be able to say everything `ray create` can."""
    _patch_submit_deps(monkeypatch, tmp_path)
    batch_module = importlib.import_module("inspire.cli.commands.batch")
    bodies: list[dict[str, Any]] = []
    monkeypatch.setattr(
        browser_api_module,
        "create_ray_job",
        lambda body, session=None: bodies.append(body) or {"ray_job_id": "ray-1"},
    )
    # Ray resolves the image through the per-source catalogues, not `list_images`.
    monkeypatch.setattr(
        browser_api_module,
        "list_images_by_source",
        lambda source=None, session=None, workspace_id=None: [
            browser_api_module.CustomImageInfo(
                image_id="image-12345678-1234-1234-1234-123456789abc",
                url="registry.batch/notebook:latest",
                name="registry.batch/notebook:latest",
                framework="",
                version="",
                source="public",
                status="SUCCESS",
                description="",
                created_at="",
            )
        ],
    )
    monkeypatch.setattr(batch_module, "get_web_session", lambda: FakeWebSession())
    batch_path = tmp_path / "batch.toml"
    batch_path.write_text(
        """
[profiles.ray.cpu]
quota = "0,20,80"
workspace = "cpu"
project = "Project One"
group = "H200 Room"
image = "registry.batch/notebook:latest"

[defaults]
type = "ray"
profile = "cpu"

[[jobs]]
name = "pipeline"
command = "python driver.py"
public_path_readonly = true
workers = ["name=w;image=registry.batch/notebook:latest;group=H200 Room;quota=0,20,80;min=1;max=2"]
""".strip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli_main, ["--json", "ray", "batch", str(batch_path)])

    assert result.exit_code == 0, result.output
    assert bodies and bodies[0]["is_publicpath_readonly"] is True


def test_ray_batch_entry_without_the_guard_omits_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _patch_submit_deps(monkeypatch, tmp_path)
    batch_module = importlib.import_module("inspire.cli.commands.batch")
    bodies: list[dict[str, Any]] = []
    monkeypatch.setattr(
        browser_api_module,
        "create_ray_job",
        lambda body, session=None: bodies.append(body) or {"ray_job_id": "ray-1"},
    )
    # Ray resolves the image through the per-source catalogues, not `list_images`.
    monkeypatch.setattr(
        browser_api_module,
        "list_images_by_source",
        lambda source=None, session=None, workspace_id=None: [
            browser_api_module.CustomImageInfo(
                image_id="image-12345678-1234-1234-1234-123456789abc",
                url="registry.batch/notebook:latest",
                name="registry.batch/notebook:latest",
                framework="",
                version="",
                source="public",
                status="SUCCESS",
                description="",
                created_at="",
            )
        ],
    )
    monkeypatch.setattr(batch_module, "get_web_session", lambda: FakeWebSession())
    batch_path = tmp_path / "batch.toml"
    batch_path.write_text(
        """
[profiles.ray.cpu]
quota = "0,20,80"
workspace = "cpu"
project = "Project One"
group = "H200 Room"
image = "registry.batch/notebook:latest"

[defaults]
type = "ray"
profile = "cpu"

[[jobs]]
name = "pipeline"
command = "python driver.py"
workers = ["name=w;image=registry.batch/notebook:latest;group=H200 Room;quota=0,20,80;min=1;max=2"]
""".strip(),
        encoding="utf-8",
    )

    result = CliRunner().invoke(cli_main, ["--json", "ray", "batch", str(batch_path)])

    assert result.exit_code == 0, result.output
    assert bodies and "is_publicpath_readonly" not in bodies[0]
