"""`inspire dataset` command surface: budgets, name-only output, verdicts."""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from inspire.cli.commands.dataset import dataset_commands as dataset_commands_module
from inspire.cli.main import main as cli_main
from inspire.platform.web import plaza as plaza_module
from inspire.platform.web import session as web_session_module
from inspire.platform.web.browser_api.datasets import DatasetValidation

# The plaza's own handles. They resolve nothing on the qz side — mounting with
# them is refused outright — so no CLI surface may ever print one.
CATALOGUE_HANDLE = 1710
VERSION_HANDLE = 2310


class _FakeWebSession:
    workspace_id = "ws-test-workspace"
    all_workspace_ids = ["ws-test-workspace"]
    all_workspace_names = {"ws-test-workspace": "CPU资源空间"}
    storage_state: dict[str, Any] = {}


def _summary(code: str = "pixabay-81k", **overrides: Any) -> plaza_module.DatasetSummary:
    values: dict[str, Any] = {
        "code": code,
        "project": "面向多模态与世界模型的基础架构研究",
        "owner": "孙宇涛",
        "maintainer": "孙宇涛",
        "grade": "S3",
        "state": "active",
        "description": "Pixabay-81K 是从 Pixabay 整理的视频数据集。",
        "tags": ("视频生成",),
        "accessible": True,
        "created_at": "2026-08-13",
        "updated_at": "2026-08-13",
        "dataset_id": CATALOGUE_HANDLE,
    }
    values.update(overrides)
    return plaza_module.DatasetSummary(**values)


def _detail(**overrides: Any) -> plaza_module.DatasetDetail:
    values: dict[str, Any] = {
        "code": "pixabay-81k",
        "project": "面向多模态与世界模型的基础架构研究",
        "owner": "孙宇涛",
        "maintainer": "孙宇涛",
        "grade": "S3",
        "state": "active",
        "description": "Pixabay-81K 是从 Pixabay 整理的视频数据集。",
        "tags": ("视频生成",),
        "accessible": True,
        "data_type": "raw",
        "source_type": "self_import",
        "license_name": "CC BY 4.0",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "created_at": "2026-08-13",
        "updated_at": "2026-08-13",
        "versions": [
            plaza_module.DatasetVersion(
                code="v0",
                state="active",
                files_count=81279,
                files_size_mib=2816752,
                data_formats=("MP4",),
                updated_at="2026-08-13 17:59",
                version_id=VERSION_HANDLE,
            )
        ],
        "dataset_id": CATALOGUE_HANDLE,
    }
    values.update(overrides)
    return plaza_module.DatasetDetail(**values)


@pytest.fixture(autouse=True)
def _fake_web_session(monkeypatch):  # noqa: ANN001, ANN201
    monkeypatch.setattr(web_session_module, "get_web_session", lambda **_kwargs: _FakeWebSession())


def _patch_list(monkeypatch, pages: list[tuple[list[Any], int]]) -> list[dict[str, Any]]:  # noqa: ANN001
    calls: list[dict[str, Any]] = []
    queue = list(pages)

    def fake_list_datasets(**kwargs: Any) -> tuple[list[Any], int]:
        calls.append(kwargs)
        return queue.pop(0) if queue else ([], 0)

    monkeypatch.setattr(plaza_module, "list_datasets", fake_list_datasets)
    monkeypatch.setattr(plaza_module, "resolve_tag_ids", lambda names, **_kwargs: [47] if names else [])
    return calls


def _json_data(output: str) -> Any:
    return json.loads(output)["data"]


# ---------------------------------------------------------------------------
# help
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", (["list"], ["show"], ["validate"]))
def test_dataset_help_is_name_only(command: list[str]) -> None:
    result = CliRunner().invoke(cli_main, ["dataset", *command, "--help"])

    assert result.exit_code == 0, result.output
    for forbidden in ("dataset_id", "DATASET_ID", "version_id", "numeric"):
        assert forbidden not in result.output


def test_dataset_list_help_exposes_the_collection_budget() -> None:
    result = CliRunner().invoke(cli_main, ["dataset", "list", "--help"])

    assert result.exit_code == 0
    assert "-n, --limit INTEGER RANGE" in " ".join(result.output.split())
    assert "--all" in result.output


def test_dataset_validate_help_requires_a_workspace() -> None:
    result = CliRunner().invoke(cli_main, ["dataset", "validate", "pixabay-81k:v0"])

    assert result.exit_code != 0
    assert "Missing option '--workspace'" in result.output


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_dataset_list_defaults_to_the_collection_budget(monkeypatch) -> None:  # noqa: ANN001
    calls = _patch_list(monkeypatch, [([_summary()], 531)])

    result = CliRunner().invoke(cli_main, ["dataset", "list"])

    assert result.exit_code == 0, result.output
    assert calls[0]["page_size"] == 20
    assert calls[0]["page"] == 1
    assert "Showing 1 of 531. Use --all for the full list." in result.output


def test_dataset_list_all_refetches_the_whole_catalogue(monkeypatch) -> None:  # noqa: ANN001
    calls = _patch_list(
        monkeypatch,
        [([_summary()], 531), ([_summary(f"d-{index}") for index in range(531)], 531)],
    )

    result = CliRunner().invoke(cli_main, ["--json", "dataset", "list", "--all"])

    assert result.exit_code == 0, result.output
    assert [call["page_size"] for call in calls] == [20, 531]
    payload = _json_data(result.output)
    assert len(payload["items"]) == 531
    assert "truncated" not in payload


def test_dataset_list_rejects_limit_with_all() -> None:
    result = CliRunner().invoke(cli_main, ["dataset", "list", "--limit", "1", "--all"])

    assert result.exit_code != 0
    assert "Use either --limit or --all, not both." in result.output


def test_dataset_list_passes_keyword_and_resolved_tags(monkeypatch) -> None:  # noqa: ANN001
    calls = _patch_list(monkeypatch, [([_summary()], 1)])

    result = CliRunner().invoke(
        cli_main, ["dataset", "list", "--keyword", "pixabay", "--tag", "视频生成"]
    )

    assert result.exit_code == 0, result.output
    assert calls[0]["keyword"] == "pixabay"
    # The tag's numeric handle is resolved from its name and stays internal.
    assert calls[0]["tag_ids"] == [47]


def test_dataset_list_json_never_carries_a_catalogue_handle(monkeypatch) -> None:  # noqa: ANN001
    _patch_list(monkeypatch, [([_summary()], 1)])

    result = CliRunner().invoke(cli_main, ["--json", "dataset", "list"])

    assert result.exit_code == 0, result.output
    assert str(CATALOGUE_HANDLE) not in result.output
    item = _json_data(result.output)["items"][0]
    assert item["name"] == "pixabay-81k"
    assert item["access"] == "yes"
    assert not any(key.endswith("_id") for key in item)


def test_dataset_list_shows_access_and_state(monkeypatch) -> None:  # noqa: ANN001
    _patch_list(
        monkeypatch,
        [([_summary("pexels-245k", accessible=False, state="processing")], 1)],
    )

    result = CliRunner().invoke(cli_main, ["dataset", "list"])

    assert result.exit_code == 0, result.output
    row = next(line for line in result.output.splitlines() if line.startswith("pexels-245k"))
    name, _project, grade, state, access, *_rest = row.split()
    assert (name, grade, state) == ("pexels-245k", "S3", "processing")
    # A closed dataset is refused at mount time, not at list time, so the list
    # has to say so: roughly a fifth of the catalogue is closed to an account.
    assert access == "no"


def test_dataset_list_reports_an_unknown_tag_with_real_ones(monkeypatch) -> None:  # noqa: ANN001
    def _raise(names, **_kwargs):  # noqa: ANN001
        raise plaza_module.UnknownDatasetTagError(list(names), ["视频生成", "图像生成"])

    monkeypatch.setattr(plaza_module, "resolve_tag_ids", _raise)

    result = CliRunner().invoke(cli_main, ["dataset", "list", "--tag", "没有这个"])

    assert result.exit_code == 12
    assert "Unknown dataset tag: 没有这个" in result.output
    assert "视频生成" in result.output


def test_dataset_list_reports_an_unavailable_catalogue(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(plaza_module, "resolve_tag_ids", lambda names, **_kwargs: [])

    def _raise(**_kwargs):  # noqa: ANN003
        raise plaza_module.PlazaError("boom: /Users/alice/private.log")

    monkeypatch.setattr(plaza_module, "list_datasets", _raise)

    result = CliRunner().invoke(cli_main, ["dataset", "list"])

    assert result.exit_code == 13
    assert "Could not list datasets." in result.output
    assert "/Users/alice/private.log" not in result.output


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def _patch_show(monkeypatch, detail: plaza_module.DatasetDetail) -> list[Any]:  # noqa: ANN001
    seen: list[Any] = []

    def fake_resolve(code: str, **_kwargs: Any) -> plaza_module.DatasetSummary:
        seen.append(code)
        return _summary(detail.code, dataset_id=detail.dataset_id)

    def fake_detail(dataset_id: int, **_kwargs: Any) -> plaza_module.DatasetDetail:
        seen.append(dataset_id)
        return detail

    monkeypatch.setattr(plaza_module, "resolve_dataset_by_code", fake_resolve)
    monkeypatch.setattr(plaza_module, "get_dataset_detail", fake_detail)
    return seen


def test_dataset_show_prints_the_mount_spec_and_container_path(monkeypatch) -> None:  # noqa: ANN001
    seen = _patch_show(monkeypatch, _detail())

    result = CliRunner().invoke(cli_main, ["dataset", "show", "pixabay-81k"])

    assert result.exit_code == 0, result.output
    # The code is resolved to a handle internally, and only internally.
    assert seen == ["pixabay-81k", CATALOGUE_HANDLE]
    assert "--dataset pixabay-81k:v0  ->  /inspire/dataset/pixabay-81k/v0" in result.output
    assert "2.7 TiB" in result.output
    assert str(CATALOGUE_HANDLE) not in result.output
    assert str(VERSION_HANDLE) not in result.output


def test_dataset_show_json_is_name_only(monkeypatch) -> None:  # noqa: ANN001
    _patch_show(monkeypatch, _detail())

    result = CliRunner().invoke(cli_main, ["--json", "dataset", "show", "pixabay-81k"])

    assert result.exit_code == 0, result.output
    payload = _json_data(result.output)
    assert payload["name"] == "pixabay-81k"
    assert payload["versions"][0]["version"] == "v0"
    assert payload["versions"][0]["path"] == "/inspire/dataset/pixabay-81k/v0"
    assert str(CATALOGUE_HANDLE) not in result.output
    assert str(VERSION_HANDLE) not in result.output


def test_dataset_show_clips_a_readme_sized_description(monkeypatch) -> None:  # noqa: ANN001
    _patch_show(monkeypatch, _detail(description="word " * 500))

    result = CliRunner().invoke(cli_main, ["--json", "dataset", "show", "pixabay-81k"])

    description = _json_data(result.output)["description"]
    assert len(description) <= dataset_commands_module.DESCRIPTION_BUDGET
    assert description.endswith("...")


def test_dataset_show_warns_when_the_account_has_no_access(monkeypatch) -> None:  # noqa: ANN001
    _patch_show(monkeypatch, _detail(accessible=False))

    result = CliRunner().invoke(cli_main, ["dataset", "show", "pixabay-81k"])

    assert result.exit_code == 0, result.output
    assert "Access: no" in result.output
    assert "may be refused" in result.output


def test_dataset_show_suggests_checking_a_version_that_carries_data(monkeypatch) -> None:  # noqa: ANN001
    _patch_show(
        monkeypatch,
        _detail(
            code="pexels-245k",
            versions=[
                plaza_module.DatasetVersion(code="v0", state="error"),
                plaza_module.DatasetVersion(code="v1", state="active"),
            ],
        ),
    )

    result = CliRunner().invoke(cli_main, ["dataset", "show", "pexels-245k"])

    assert result.exit_code == 0, result.output
    # Every version is still listed; the worked example points at a live one.
    assert "--dataset pexels-245k:v0" in result.output
    assert "inspire dataset validate pexels-245k:v1 --workspace" in result.output


def test_dataset_show_handles_a_dataset_with_no_version(monkeypatch) -> None:  # noqa: ANN001
    _patch_show(monkeypatch, _detail(state="wanted", versions=[]))

    result = CliRunner().invoke(cli_main, ["dataset", "show", "pixabay-81k"])

    assert result.exit_code == 0, result.output
    assert "no mountable version" in result.output


def test_dataset_show_rejects_a_catalogue_handle_with_a_name_hint(monkeypatch) -> None:  # noqa: ANN001
    def _raise(code: str, **_kwargs: Any):  # noqa: ANN202
        raise plaza_module.UnknownDatasetError(f"No dataset named {code!r} in the data plaza.")

    monkeypatch.setattr(plaza_module, "resolve_dataset_by_code", _raise)

    result = CliRunner().invoke(cli_main, ["dataset", "show", str(CATALOGUE_HANDLE)])

    assert result.exit_code == 12
    assert "catalogue code" in result.output
    assert "pixabay-81k" in result.output


def test_dataset_show_reports_an_unknown_name_without_a_handle_hint(monkeypatch) -> None:  # noqa: ANN001
    def _raise(code: str, **_kwargs: Any):  # noqa: ANN202
        raise plaza_module.UnknownDatasetError(f"No dataset named {code!r} in the data plaza.")

    monkeypatch.setattr(plaza_module, "resolve_dataset_by_code", _raise)

    result = CliRunner().invoke(cli_main, ["dataset", "show", "no-such-dataset"])

    assert result.exit_code == 12
    assert "no-such-dataset" in result.output
    assert "catalogue code" not in result.output


# ---------------------------------------------------------------------------
# validate
# ---------------------------------------------------------------------------


def _patch_validate(monkeypatch, verdicts: list[DatasetValidation]) -> list[dict[str, Any]]:  # noqa: ANN001
    calls: list[dict[str, Any]] = []

    def fake_validate(mounts, *, workspace_id, session=None):  # noqa: ANN001
        calls.append({"mounts": list(mounts), "workspace_id": workspace_id})
        return verdicts

    monkeypatch.setattr(dataset_commands_module, "validate_dataset_mounts", fake_validate)
    monkeypatch.setattr(
        dataset_commands_module,
        "resolve_workspace_operation_scope",
        lambda **_kwargs: "ws-test-workspace",
    )
    return calls


def test_dataset_validate_reports_each_verdict(monkeypatch) -> None:  # noqa: ANN001
    calls = _patch_validate(
        monkeypatch,
        [
            DatasetValidation(
                dataset="pixabay-81k",
                version="v0",
                ok=True,
                path="sftpgo/pixabay-81k/v0",
            )
        ],
    )

    result = CliRunner().invoke(
        cli_main,
        ["dataset", "validate", "pixabay-81k:v0", "--workspace", "CPU资源空间"],
    )

    assert result.exit_code == 0, result.output
    assert [(m.dataset, m.version) for m in calls[0]["mounts"]] == [("pixabay-81k", "v0")]
    assert "/inspire/dataset/pixabay-81k/v0" in result.output
    # The platform's own storage location is not the caller's business.
    assert "sftpgo" not in result.output


def test_dataset_validate_exits_non_zero_on_a_rejected_mount(monkeypatch) -> None:  # noqa: ANN001
    _patch_validate(
        monkeypatch,
        [
            DatasetValidation(dataset="pixabay-81k", version="v0", ok=True, path="p"),
            DatasetValidation(
                dataset="pexels-245k",
                version="v1",
                ok=False,
                error="code: 2005, message: 无访问权限",
            ),
        ],
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "dataset",
            "validate",
            "pixabay-81k:v0",
            "pexels-245k:v1",
            "--workspace",
            "CPU资源空间",
        ],
    )

    assert result.exit_code == 12
    assert "rejected" in result.output
    assert "无访问权限" in result.output


def test_dataset_validate_json_states_the_overall_verdict(monkeypatch) -> None:  # noqa: ANN001
    _patch_validate(
        monkeypatch,
        [
            DatasetValidation(
                dataset="videoufo",
                version="v9",
                ok=False,
                error="code: 2001, message: 版本不存在",
            )
        ],
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "dataset", "validate", "videoufo:v9", "--workspace", "CPU资源空间"],
    )

    assert result.exit_code == 12
    payload = _json_data(result.output)
    assert payload["mountable"] is False
    assert payload["items"][0] == {
        "name": "videoufo",
        "version": "v9",
        "mountable": False,
        "reason": "code: 2001, message: 版本不存在",
    }


def test_dataset_validate_rejects_a_malformed_spec(monkeypatch) -> None:  # noqa: ANN001
    calls = _patch_validate(monkeypatch, [])

    result = CliRunner().invoke(
        cli_main,
        ["dataset", "validate", "pixabay-81k", "--workspace", "CPU资源空间"],
    )

    assert result.exit_code == 12
    assert "<dataset>:<version>" in result.output
    assert calls == []


def test_mounted_dataset_views_keeps_the_codes_and_drops_the_storage_path() -> None:
    """The stored `path` is a platform handle; only the container path is the user's."""
    from inspire.platform.web.browser_api.datasets import mounted_dataset_views

    views = mounted_dataset_views(
        [
            {
                "dataset_id": "pixabay-81k",
                "version_id": "v0",
                "path": "sftpgo/pixabay-81k/v0",
                "access_mode": "",
            }
        ]
    )

    assert views == [
        {
            "name": "pixabay-81k",
            "version": "v0",
            "path": "/inspire/dataset/pixabay-81k/v0",
        }
    ]
    assert "sftpgo" not in repr(views)


def test_mounted_dataset_views_tolerates_every_empty_shape() -> None:
    from inspire.platform.web.browser_api.datasets import mounted_dataset_views

    for payload in (None, [], {}, "", [{}], [{"dataset_id": "x"}], ["nope"]):
        assert mounted_dataset_views(payload) == []


def test_notebook_status_projection_reports_mounted_datasets() -> None:
    from inspire.cli.commands.notebook.public_output import public_notebook

    view = public_notebook(
        {
            "name": "nb",
            "status": "RUNNING",
            "dataset_info": [
                {"dataset_id": "videoufo", "version_id": "v1", "path": "downloader-1/x"}
            ],
        }
    )

    assert view["datasets"] == [
        {"name": "videoufo", "version": "v1", "path": "/inspire/dataset/videoufo/v1"}
    ]


def test_workload_status_projections_omit_datasets_when_none_are_mounted() -> None:
    from inspire.cli.commands.hpc.public_output import public_hpc_status
    from inspire.cli.commands.job.public_output import public_job_status
    from inspire.cli.commands.notebook.public_output import public_notebook

    assert "datasets" not in public_notebook({"name": "nb", "status": "RUNNING"})
    assert "datasets" not in public_job_status({"name": "j", "status": "RUNNING"})
    assert "datasets" not in public_hpc_status({"job_name": "h", "status": "RUNNING"})


def test_job_and_hpc_status_projections_report_mounted_datasets() -> None:
    from inspire.cli.commands.hpc.public_output import public_hpc_status
    from inspire.cli.commands.job.public_output import public_job_status

    payload = {
        "dataset_info": [
            {"dataset_id": "pixabay-81k", "version_id": "v0", "path": "sftpgo/x"}
        ]
    }
    expected = [
        {"name": "pixabay-81k", "version": "v0", "path": "/inspire/dataset/pixabay-81k/v0"}
    ]

    assert public_job_status({"name": "j", "status": "RUNNING", **payload})["datasets"] == expected
    assert (
        public_hpc_status({"job_name": "h", "status": "RUNNING", **payload})["datasets"]
        == expected
    )
