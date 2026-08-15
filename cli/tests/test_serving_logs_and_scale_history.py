"""Tests for `inspire serving logs` and `inspire serving scale-history`.

Both commands wrap Browser API Actions that had wrappers but no caller. The
wire facts pinned here were checked against the live gateway:
`ListServingScaleHistory` answers under `scale_history_items` with a string
`total` (discovery declares `Items` / `TotalCount`, and neither exists), and
`GetServingLog` is pod-scoped — it takes `filter.podNames` and a window, never
a serving handle, so the instance list is what connects a name to its logs.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from click.testing import CliRunner

from inspire import config as config_module
from inspire.cli.commands.serving import serving_commands as serving_commands_module
from inspire.cli.commands.serving import serving_logs as serving_logs_module
from inspire.cli.commands.serving.serving_commands import (
    _format_scale_history,
    _public_scale_history_entry,
)
from inspire.cli.context import EXIT_LOG_NOT_FOUND, EXIT_VALIDATION_ERROR
from inspire.cli.main import main as cli_main
from inspire.cli.utils.collection_output import DEFAULT_COLLECTION_LIMIT
from inspire.platform.web import browser_api as browser_api_module
from inspire.platform.web.browser_api.servings import list_serving_scale_history


class _FakeSession:
    storage_state: dict[str, Any] = {}
    workspace_id = "ws-1"
    all_workspace_names = {"ws-1": "Serving空间"}
    all_workspace_ids = ["ws-1"]


def _patch_cli_deps(
    monkeypatch: pytest.MonkeyPatch,
    *,
    resolved_id: str = "sv-internal",
) -> list[dict[str, Any]]:
    """Stub config, session, and name resolution for both command modules."""
    config = config_module.Config(username="user", password="pass")
    resolutions: list[dict[str, Any]] = []
    monkeypatch.setattr(
        config_module.Config,
        "from_files_and_env",
        classmethod(lambda cls, require_credentials=True: (config, {})),
    )

    def _resolve(
        _ctx,  # noqa: ANN001
        name,  # noqa: ANN001
        *,
        workspace_id=None,  # noqa: ANN001
        pick=None,  # noqa: ANN001
        require_live=False,  # noqa: ANN001
    ) -> str:
        resolutions.append(
            {
                "name": name,
                "workspace_id": workspace_id,
                "pick": pick,
                "require_live": require_live,
            }
        )
        return resolved_id

    for module in (serving_commands_module, serving_logs_module):
        monkeypatch.setattr(module, "get_web_session", lambda: _FakeSession())
        monkeypatch.setattr(
            module, "_resolve_workspace_id", lambda _workspace: "ws-internal"
        )
    # `serving_logs` reuses `_run_readonly_serving_operation`, which resolves
    # through its own module globals -- patching it once is what both commands
    # actually go through.
    monkeypatch.setattr(serving_commands_module, "_resolve_serving_name", _resolve)
    return resolutions


# --------------------------------------------------------------------------
# Wrapper: the live list key
# --------------------------------------------------------------------------


def test_scale_history_reads_the_live_list_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """`scale_history_items` is what the gateway answers, not `Items`."""
    body: dict[str, Any] = {}

    def _fake_request(_session, _method, url, *, referer, body: dict, timeout):  # noqa: ANN001, ANN202
        body_capture.update({"url": url, "body": body, "referer": referer})
        return {
            "Result": {
                "scale_history_items": [
                    {
                        "id": 41,
                        "status": 2,
                        "replicas_before_scale": 3,
                        "replicas_after_scale": 5,
                        "created_at": "1786780070000",
                    }
                ],
                "total": "7",
            }
        }

    body_capture = body
    monkeypatch.setattr(
        "inspire.platform.web.browser_api.servings._request_json",
        _fake_request,
    )

    items, total = list_serving_scale_history(
        "sv-1", page=1, page_size=20, session=_FakeSession()
    )

    assert total == 7, "a string total must still coerce to an int"
    assert items == [
        {
            "id": 41,
            "status": 2,
            "replicas_before_scale": 3,
            "replicas_after_scale": 5,
            "created_at": "1786780070000",
        }
    ]
    assert body_capture["url"].endswith(
        "/api/v2/inference_serving?Action=ListServingScaleHistory"
    )
    assert body_capture["body"] == {
        "inference_serving_id": "sv-1",
        "page": 1,
        "page_size": 20,
    }


# --------------------------------------------------------------------------
# scale-history projection and rendering
# --------------------------------------------------------------------------


def test_scale_history_projection_drops_internal_id_and_formats_epoch() -> None:
    view = _public_scale_history_entry(
        {
            "id": 41,
            "status": "SUCCEEDED",
            "replicas_before_scale": 3,
            "replicas_after_scale": 5,
            "created_at": "1786780070000",
        }
    )

    assert set(view) == {"replicas_from", "replicas_to", "status", "created_at"}
    assert view["replicas_from"] == 3
    assert view["replicas_to"] == 5
    assert view["created_at"].startswith("2026-")
    assert "41" not in json.dumps(view)


def test_scale_history_projection_keeps_partial_rows() -> None:
    assert _public_scale_history_entry({"replicas_after_scale": 2}) == {"replicas_to": 2}
    assert _public_scale_history_entry({}) == {}


def test_scale_history_keeps_a_scale_to_zero() -> None:
    """Scaling to zero is the case the command exists for; 0 is not "missing"."""
    view = _public_scale_history_entry(
        {"replicas_before_scale": 2, "replicas_after_scale": 0}
    )

    assert view == {"replicas_from": 2, "replicas_to": 0}
    assert "2 -> 0" in _format_scale_history([view])


def test_format_scale_history_renders_the_replica_delta() -> None:
    out = _format_scale_history(
        [
            {
                "replicas_from": 3,
                "replicas_to": 5,
                "status": "SUCCEEDED",
                "created_at": "2026-08-15 15:47:38",
            }
        ]
    )

    assert out.splitlines()[0].startswith("Created")
    assert "3 -> 5" in out
    assert "SUCCEEDED" in out


def test_format_scale_history_empty_state() -> None:
    assert _format_scale_history([]) == "No serving scale history found."


def test_scale_history_omits_status_column_when_absent() -> None:
    out = _format_scale_history([{"replicas_from": 1, "replicas_to": 2}])

    assert "Status" not in out
    assert "1 -> 2" in out


# --------------------------------------------------------------------------
# scale-history command
# --------------------------------------------------------------------------


def test_serving_scale_history_is_name_only_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolutions = _patch_cli_deps(monkeypatch)
    calls: list[tuple[str, int]] = []

    def _history(serving_id: str, *, page: int, page_size: int, session):  # noqa: ANN001, ANN202
        assert page == 1
        calls.append((serving_id, page_size))
        return (
            [
                {
                    "id": 41,
                    "status": "SUCCEEDED",
                    "replicas_before_scale": 3,
                    "replicas_after_scale": 5,
                    "created_at": "1786780070000",
                }
            ],
            4,
        )

    monkeypatch.setattr(browser_api_module, "list_serving_scale_history", _history)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "serving",
            "scale-history",
            "demo",
            "--workspace",
            "Test Workspace",
            "--pick",
            "3",
            "--limit",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert calls == [("sv-internal", 1)]
    assert resolutions == [
        {
            "name": "demo",
            "workspace_id": "ws-internal",
            "pick": 3,
            "require_live": False,
        }
    ]
    payload = json.loads(result.output)["data"]
    assert payload["name"] == "demo"
    assert payload["items"] == [
        {
            "replicas_from": 3,
            "replicas_to": 5,
            "status": "SUCCEEDED",
            "created_at": payload["items"][0]["created_at"],
        }
    ]
    assert payload["shown"] == 1
    assert payload["total"] == 4
    assert payload["truncated"] is True
    assert "sv-internal" not in result.output

    human = CliRunner().invoke(
        cli_main,
        [
            "serving",
            "scale-history",
            "demo",
            "--workspace",
            "Test Workspace",
            "--limit",
            "1",
        ],
    )
    assert human.exit_code == 0, human.output
    assert "3 -> 5" in human.output
    assert "Showing 1 of 4. Use --all for the full list." in human.output
    assert "sv-internal" not in human.output


def test_serving_scale_history_all_refetches_full_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cli_deps(monkeypatch)
    page_sizes: list[int] = []

    def _history(_serving_id: str, *, page: int, page_size: int, session):  # noqa: ANN001, ANN202
        page_sizes.append(page_size)
        count = 1 if page_size == DEFAULT_COLLECTION_LIMIT else 3
        return (
            [
                {"replicas_before_scale": index, "replicas_after_scale": index + 1}
                for index in range(count)
            ],
            3,
        )

    monkeypatch.setattr(browser_api_module, "list_serving_scale_history", _history)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "serving",
            "scale-history",
            "demo",
            "--workspace",
            "Test Workspace",
            "--all",
        ],
    )

    assert result.exit_code == 0, result.output
    assert page_sizes == [DEFAULT_COLLECTION_LIMIT, 3]
    payload = json.loads(result.output)["data"]
    assert set(payload) == {"name", "items"}
    assert len(payload["items"]) == 3


def test_serving_scale_history_rejects_limit_with_all(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cli_deps(monkeypatch)
    monkeypatch.setattr(
        browser_api_module,
        "list_serving_scale_history",
        lambda *_a, **_k: pytest.fail("conflicting options must be caught pre-API"),
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "serving",
            "scale-history",
            "demo",
            "--workspace",
            "Test Workspace",
            "--limit",
            "2",
            "--all",
        ],
    )

    assert result.exit_code == EXIT_VALIDATION_ERROR
    assert "Use either --limit or --all, not both." in result.output


def test_serving_scale_history_rejects_raw_handle_before_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser_api_module,
        "list_serving_scale_history",
        lambda *_a, **_k: pytest.fail("raw handle must be rejected before the API call"),
    )

    result = CliRunner().invoke(
        cli_main,
        ["serving", "scale-history", "sv-12345678", "--workspace", "Test Workspace"],
    )

    assert result.exit_code != 0
    assert "only accept serving names" in result.output
    assert "sv-12345678" not in result.output


# --------------------------------------------------------------------------
# logs
# --------------------------------------------------------------------------


def _log_record(index: int, *, pod: str = "sv-internal-worker-0-0") -> dict[str, Any]:
    return {
        "log_id": f"log-{index}",
        "message": f"line {index}",
        "node": "gpu-node-1",
        "pod_name": pod,
        "time": f"2026-08-15T15:47:{index:02d}+08:00",
        "timestamp_ms": str(1786780000000 + index * 1000),
        "timestamp_str": f"2026-08-15T07:47:{index:02d}.000Z",
    }


_POD = "sv-ed52f184-b66b-478a-8620-379033c6dbf3"


def _patch_log_api(
    monkeypatch: pytest.MonkeyPatch,
    *,
    instances: list[dict[str, Any]] | None = None,
    records: list[dict[str, Any]] | None = None,
    total: int | None = None,
) -> dict[str, Any]:
    seen: dict[str, Any] = {"instances": [], "logs": []}
    rows = [{"name": "sv-internal-worker-0-0"}] if instances is None else instances
    logs = [_log_record(i) for i in range(3)] if records is None else records

    def _list_instances(serving_id: str, *, page: int, page_size: int, session):  # noqa: ANN001, ANN202
        seen["instances"].append((serving_id, page, page_size))
        return rows, len(rows)

    def _list_logs(
        *,
        pod_names: list[str],
        start_timestamp_ms: int | str,
        end_timestamp_ms: int | str,
        page_size: int,
        inference_serving_id: str | None = None,
        session=None,  # noqa: ANN001
    ):  # noqa: ANN202
        seen["logs"].append(
            {
                "pod_names": list(pod_names),
                "start": int(start_timestamp_ms),
                "end": int(end_timestamp_ms),
                "page_size": page_size,
                "inference_serving_id": inference_serving_id,
            }
        )
        return logs, len(logs) if total is None else total

    monkeypatch.setattr(browser_api_module, "list_serving_instances", _list_instances)
    monkeypatch.setattr(browser_api_module, "list_serving_logs", _list_logs)
    return seen


def test_serving_logs_uses_instance_pods_and_a_default_day_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolutions = _patch_cli_deps(monkeypatch)
    seen = _patch_log_api(monkeypatch)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "serving",
            "logs",
            "demo",
            "--workspace",
            "Test Workspace",
            "--pick",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen["instances"] == [("sv-internal", 1, 200)]
    assert resolutions == [
        {
            "name": "demo",
            "workspace_id": "ws-internal",
            "pick": 2,
            "require_live": False,
        }
    ]
    call = seen["logs"][0]
    assert call["pod_names"] == ["sv-internal-worker-0-0"]
    assert call["page_size"] == 100
    assert call["inference_serving_id"] == "sv-internal"
    assert call["end"] - call["start"] == 24 * 60 * 60 * 1000

    payload = json.loads(result.output)["data"]
    assert payload["name"] == "demo"
    assert payload["shown"] == 3
    assert payload["total"] == 3
    assert payload["truncated"] is False
    assert payload["limit"] == 100
    assert payload["character_limit"] == 16_000
    assert payload["shown_chars"] > 0
    assert [item["message"] for item in payload["logs"]] == ["line 0", "line 1", "line 2"]


def test_serving_logs_window_option_narrows_the_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cli_deps(monkeypatch)
    seen = _patch_log_api(monkeypatch)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "serving",
            "logs",
            "demo",
            "--workspace",
            "Test Workspace",
            "--window",
            "30m",
        ],
    )

    assert result.exit_code == 0, result.output
    call = seen["logs"][0]
    assert call["end"] - call["start"] == 30 * 60 * 1000


def test_serving_logs_rejects_a_malformed_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cli_deps(monkeypatch)
    monkeypatch.setattr(
        browser_api_module,
        "list_serving_logs",
        lambda **_k: pytest.fail("a bad window must be caught before the API call"),
    )

    result = CliRunner().invoke(
        cli_main,
        ["serving", "logs", "demo", "--workspace", "Test Workspace", "--window", "5x"],
    )

    assert result.exit_code == EXIT_VALIDATION_ERROR
    assert "window" in result.output.lower()


def test_serving_logs_instance_option_resolves_the_rank_to_its_pod(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The selector names the Rank; only the instance list knows the pod."""
    _patch_cli_deps(monkeypatch)
    seen = _patch_log_api(
        monkeypatch,
        instances=[
            {"name": f"frontiers/{_POD}-0", "rank": 0},
            {"name": f"frontiers/{_POD}-1", "rank": 1},
        ],
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "serving",
            "logs",
            "demo",
            "--workspace",
            "Test Workspace",
            "--instance",
            "1",
        ],
    )

    assert result.exit_code == 0, result.output
    assert seen["instances"]
    assert seen["logs"][0]["pod_names"] == [f"frontiers/{_POD}-1"]


def test_serving_logs_reject_an_unknown_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cli_deps(monkeypatch)
    _patch_log_api(
        monkeypatch,
        instances=[{"name": f"frontiers/{_POD}-0", "rank": 0}],
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "serving",
            "logs",
            "demo",
            "--workspace",
            "Test Workspace",
            "--instance",
            "rank=9",
        ],
    )

    assert result.exit_code == 12
    payload = json.loads(result.output)
    assert payload["error"]["type"] == "ValidationError"
    assert "rank=0" in payload["error"]["message"]


def test_serving_logs_instance_option_is_name_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cli_deps(monkeypatch)
    monkeypatch.setattr(
        browser_api_module,
        "list_serving_logs",
        lambda **_k: pytest.fail("a handle-shaped instance must be rejected pre-API"),
    )

    result = CliRunner().invoke(
        cli_main,
        [
            "serving",
            "logs",
            "demo",
            "--workspace",
            "Test Workspace",
            "--instance",
            "sv-12345678",
        ],
    )

    assert result.exit_code == EXIT_VALIDATION_ERROR
    assert "only accept serving instance names" in result.output
    assert "sv-12345678" not in result.output


def test_serving_logs_reports_a_deployment_with_no_replicas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cli_deps(monkeypatch)
    seen = _patch_log_api(monkeypatch, instances=[])

    result = CliRunner().invoke(
        cli_main,
        ["serving", "logs", "demo", "--workspace", "Test Workspace"],
    )

    assert result.exit_code == EXIT_LOG_NOT_FOUND
    assert seen["logs"] == []
    assert "No instances found for serving demo." in result.output


def test_serving_logs_tail_selects_the_latest_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cli_deps(monkeypatch)
    _patch_log_api(monkeypatch, records=[_log_record(i) for i in range(5)], total=5)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "serving",
            "logs",
            "demo",
            "--workspace",
            "Test Workspace",
            "--tail",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert [item["message"] for item in payload["logs"]] == ["line 3", "line 4"]
    assert payload["shown"] == 2
    assert payload["total"] == 5
    assert payload["truncated"] is True
    assert payload["limit"] == 2


def test_serving_logs_head_selects_the_earliest_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cli_deps(monkeypatch)
    _patch_log_api(monkeypatch, records=[_log_record(i) for i in range(5)], total=5)

    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "serving",
            "logs",
            "demo",
            "--workspace",
            "Test Workspace",
            "--head",
            "2",
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)["data"]
    assert [item["message"] for item in payload["logs"]] == ["line 0", "line 1"]


def test_serving_logs_all_refetches_the_reported_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cli_deps(monkeypatch)
    seen = _patch_log_api(
        monkeypatch,
        records=[_log_record(i) for i in range(2)],
        total=9,
    )

    result = CliRunner().invoke(
        cli_main,
        ["--json", "serving", "logs", "demo", "--workspace", "Test Workspace", "--all"],
    )

    assert result.exit_code == 0, result.output
    assert [call["page_size"] for call in seen["logs"]] == [100, 9]
    payload = json.loads(result.output)["data"]
    assert payload["character_limit"] is None
    assert payload["limit"] is None


@pytest.mark.parametrize(
    ("extra", "message"),
    (
        (["--tail", "2", "--head", "2"], "--tail and --head cannot be used together."),
        (["--all", "--tail", "2"], "--all cannot be combined with --tail."),
        (["--all", "--limit", "2"], "--all cannot be combined with --limit."),
    ),
)
def test_serving_logs_rejects_conflicting_budget_options(
    monkeypatch: pytest.MonkeyPatch,
    extra: list[str],
    message: str,
) -> None:
    _patch_cli_deps(monkeypatch)
    monkeypatch.setattr(
        browser_api_module,
        "list_serving_logs",
        lambda **_k: pytest.fail("conflicting options must be caught pre-API"),
    )

    result = CliRunner().invoke(
        cli_main,
        ["serving", "logs", "demo", "--workspace", "Test Workspace", *extra],
    )

    assert result.exit_code == EXIT_VALIDATION_ERROR
    assert message in result.output


def test_serving_logs_human_output_scrubs_pod_handles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_cli_deps(monkeypatch)
    _patch_log_api(
        monkeypatch,
        records=[
            _log_record(0, pod="sv-1f0c2d34-5678-4abc-9def-0123456789ab-worker-0-0")
        ],
    )

    result = CliRunner().invoke(
        cli_main,
        ["serving", "logs", "demo", "--workspace", "Test Workspace"],
    )

    assert result.exit_code == 0, result.output
    assert "line 0" in result.output
    assert "1f0c2d34-5678-4abc-9def-0123456789ab" not in result.output


def test_serving_logs_rejects_raw_handle_before_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        browser_api_module,
        "list_serving_logs",
        lambda **_k: pytest.fail("raw handle must be rejected before the API call"),
    )

    result = CliRunner().invoke(
        cli_main,
        ["serving", "logs", "sv-12345678", "--workspace", "Test Workspace"],
    )

    assert result.exit_code != 0
    assert "only accept serving names" in result.output
    assert "sv-12345678" not in result.output


def test_instance_views_keep_the_namespaced_handle_off_the_label() -> None:
    """`GetServingLog` needs `<project>/<pod>`; output only ever shows the Rank."""
    from inspire.cli.commands.serving.serving_instances import serving_instance_views

    views = serving_instance_views(
        [
            {"name": f" frontiers/{_POD}-0 ", "rank": 0},
            {"name": f"frontiers/{_POD}-1", "rank": 1},
            {"rank": 3},
        ]
    )

    assert [view.handle for view in views] == [f"frontiers/{_POD}-0", f"frontiers/{_POD}-1"]
    assert [view.label for view in views] == ["rank=0", "rank=1"]
