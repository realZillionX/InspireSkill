"""Regression tests for create-payload field names the platform is strict about.

Each of these was a live failure: the CLI sent a field the platform does not
accept, and the error it answered with pointed somewhere else.
"""

from __future__ import annotations

from typing import Any

import pytest

from inspire.cli.utils import image_resolver
from inspire.cli.utils.image_resolver import IMAGE_TYPE, resolve_image_url
from inspire.config import ConfigError


class _Image:
    def __init__(self, name: str, version: str, url: str, source: str) -> None:
        self.name = name
        self.version = version
        self.url = url
        self.source = source


_CATALOGUE = {
    "official": [
        _Image(
            "ngc-pytorch:25.02-cuda12.8.0-py3",
            "25.02-cuda12.8.0-py3",
            "docker.example/base/ngc-pytorch:25.02-cuda12.8.0-py3",
            "SOURCE_OFFICIAL",
        )
    ],
    "public": [],
    "private": [],
}


@pytest.fixture()
def catalogue(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    def fake_list(*, source: str, session: Any, workspace_id=None):  # noqa: ANN001
        calls.append(source)
        return _CATALOGUE.get(source, [])

    import inspire.platform.web.browser_api.images as images_module

    monkeypatch.setattr(images_module, "list_images_by_source", fake_list)
    return calls


def test_display_name_resolves_to_registry_url(catalogue) -> None:
    # The platform matches on the URL; a display name is rejected with
    # 无法找到对应镜像.
    assert (
        resolve_image_url("ngc-pytorch:25.02-cuda12.8.0-py3", session=object(), workspace_id="ws-test")
        == "docker.example/base/ngc-pytorch:25.02-cuda12.8.0-py3"
    )


def test_registry_url_passes_through_without_a_lookup(catalogue) -> None:
    url = "docker.example/inspire-studio/not-in-catalogue:v1"
    assert resolve_image_url(url, session=object(), workspace_id="ws-test") == url
    # `--image` accepts NAME|URL, and a URL the catalogue does not list still
    # has to reach the platform, so no catalogue call is made at all.
    assert catalogue == []


def test_unknown_display_name_is_a_config_error(catalogue) -> None:
    with pytest.raises(ConfigError, match="not found in official/public/private"):
        resolve_image_url("no-such-image:v9", session=object(), workspace_id="ws-test")


def test_command_local_image_catalogue_is_reused(catalogue) -> None:
    cache: image_resolver.ImageCatalogCache = {}

    for _ in range(2):
        assert resolve_image_url(
            "ngc-pytorch:25.02-cuda12.8.0-py3",
            session=object(),
            workspace_id="ws-test",
            catalog_cache=cache,
        ).startswith("docker.example/")

    assert catalogue == ["official"]


def test_job_payload_sends_url_not_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        image_resolver,
        "resolve_image_url",
        lambda raw, *, session=None, debug=False, **_kwargs: "docker.example/base/resolved:v1",
    )
    import inspire.cli.utils.job_submit as job_submit

    monkeypatch.setattr(
        job_submit,
        "resolve_image_url",
        lambda raw, *, session=None, debug=False, **_kwargs: "docker.example/base/resolved:v1",
    )

    from inspire.cli.utils.quota_resolver import ResolvedQuota

    plan = job_submit.build_training_job_plan(
        config=_FakeConfig(),
        name="demo",
        command="echo hi",
        quota=ResolvedQuota(
            quota_id="q-1",
            logic_compute_group_id="lcg-1",
            compute_group_name="grp",
            gpu_count=0,
            cpu_count=10,
            memory_gib=200,
            gpu_type="",
            raw_price={"cpu_count": 10, "memory_size_gib": 200, "gpu_count": 0},
        ),
        framework="pytorch",
        project_id="project-1",
        workspace_id="ws-1",
        image="ngc-pytorch:25.02-cuda12.8.0-py3",
        priority=10,
        nodes=1,
        max_time_hours=None,
        session=object(),
    )
    framework_config = plan.create_kwargs["framework_config"][0]
    assert framework_config["image"] == "docker.example/base/resolved:v1"
    assert framework_config["image_type"] == IMAGE_TYPE


class _FakeConfig:
    path_aliases: dict[str, str] = {}
    remote_env: dict[str, str] = {}
    shm_size = None
    job_auto_fault_tolerance = False
    job_fault_tolerance_max_retry = None


def test_hpc_payload_uses_priority_not_task_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    from inspire.cli.commands.hpc import hpc_commands

    monkeypatch.setattr(
        hpc_commands,
        "resolve_image_url",
        lambda raw, *, session=None, debug=False, **_kwargs: "docker.example/base/resolved:v1",
    )

    payload = hpc_commands.build_hpc_create_payload(
        name="demo",
        logic_compute_group_id="lcg-1",
        project_id="project-1",
        workspace_id="ws-1",
        image="videothinkbench-hpc-slurm-base:v1",
        image_type="SOURCE_PUBLIC",
        entrypoint="srun echo ok",
        quota_id="q-1",
        instance_count=1,
        task_priority=10,
        number_of_tasks=1,
        cpus_per_task=4,
        memory_per_cpu=4,
        enable_hyper_threading=False,
        resource_spec_price={"cpu_count": 4, "memory_size_gib": 16},
        session=object(),
    )

    # v2 CreateJobConsole answers a `task_priority` payload with
    # "priority must be set", which reads as missing rather than misnamed.
    assert payload["priority"] == 10
    assert "task_priority" not in payload
    assert payload["slurm_cluster_spec"]["image"] == "docker.example/base/resolved:v1"


def test_hpc_job_info_reads_job_name() -> None:
    from inspire.platform.web.browser_api.hpc_jobs import HPCJobInfo

    # The wire field is `job_name`; reading `name` left every job nameless.
    info = HPCJobInfo.from_api_response({"job_id": "hpc-1", "job_name": "demo"})
    assert info.name == "demo"


def test_ray_job_info_reads_creator_and_priority_name() -> None:
    from inspire.platform.web.browser_api.ray_jobs import RayJobInfo

    # The wire fields are `creator` and `priority_name`; `created_by` and
    # `priority` are always null, so reading only those left the owner blank.
    info = RayJobInfo.from_api_response(
        {
            "ray_job_id": "rj-1",
            "name": "demo",
            "created_by": None,
            "creator": {"id": "user-1", "name": "Alice"},
            "priority": None,
            "priority_name": "10",
        }
    )
    assert info.created_by_id == "user-1"
    assert info.created_by_name == "Alice"
    assert info.priority == 10
