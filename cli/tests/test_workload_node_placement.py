"""Node placement contracts for workload status and instance output.

Which nodes a workload landed on is the fact infrastructure work is planned
against, and every one of these payloads already carried it — the projections
simply dropped it. These tests pin the reads to the shapes the platform
actually answers with, including the "not placed yet" shapes that must not be
mistaken for a node.
"""

from __future__ import annotations

import json

import pytest

from inspire.cli.commands.hpc.hpc_commands import _public_hpc_instances
from inspire.cli.commands.hpc.public_output import format_hpc_status, public_hpc_status
from inspire.cli.commands.job.job_commands import _public_job_instances
from inspire.cli.commands.job.public_output import format_job_status, public_job_status
from inspire.cli.commands.notebook.public_output import public_notebook
from inspire.cli.commands.ray.ray_commands import _public_ray_instances
from inspire.cli.commands.serving.public_output import public_serving
from inspire.cli.commands.serving.serving_commands import _public_serving_instances
from inspire.cli.utils.raw_ids import scrub_raw_ids


@pytest.mark.parametrize(
    "node_name",
    ("qb-prod-4090-gpu105", "hpc-compute003", "cpu-nat-351", "node-001"),
)
def test_node_names_survive_handle_scrubbing(node_name: str) -> None:
    """Node names are infrastructure identity, not platform handles."""
    assert scrub_raw_ids(node_name) == node_name


def test_job_status_reports_live_placement_and_request_side_pins() -> None:
    view = public_job_status(
        {
            "name": "train-a",
            "status": "job_running",
            "node_count": 2,
            "node_infos": [
                {"node_name": "qb-prod-4090-gpu105"},
                {"node_name": "qb-prod-4090-gpu021"},
            ],
            "specified_nodes": ["qb-prod-4090-gpu105"],
            "exclude_nodes": ["qb-prod-4090-gpu007"],
        }
    )

    assert view["nodes"] == ["qb-prod-4090-gpu105", "qb-prod-4090-gpu021"]
    assert view["pinned_nodes"] == ["qb-prod-4090-gpu105"]
    assert view["excluded_nodes"] == ["qb-prod-4090-gpu007"]

    rendered = format_job_status(view)
    assert "Nodes: qb-prod-4090-gpu105, qb-prod-4090-gpu021" in rendered
    assert "Pinned Nodes: qb-prod-4090-gpu105" in rendered
    assert "Excluded Nodes: qb-prod-4090-gpu007" in rendered


def test_job_status_omits_placement_before_the_scheduler_places_it() -> None:
    """An unplaced job answers empty lists; that must not print as a node."""
    view = public_job_status(
        {
            "name": "train-a",
            "status": "job_stopped",
            "node_infos": [],
            "specified_nodes": [],
            "exclude_nodes": [],
        }
    )

    assert "nodes" not in view
    assert "pinned_nodes" not in view
    assert "excluded_nodes" not in view
    assert "Nodes" not in format_job_status(view)


def test_job_status_deduplicates_nodes_shared_by_several_workers() -> None:
    view = public_job_status(
        {
            "name": "train-a",
            "status": "job_running",
            "node_infos": [
                {"node_name": "qb-prod-4090-gpu105"},
                {"node_name": "qb-prod-4090-gpu105"},
            ],
        }
    )

    assert view["nodes"] == ["qb-prod-4090-gpu105"]


def test_hpc_status_reports_the_slurm_placement() -> None:
    """``hpc.GetJob`` files the placement as a bare string array."""
    view = public_hpc_status(
        {
            "job_name": "prep",
            "status": "RUNNING",
            "nodes": ["hpc-compute003", "hpc-compute067"],
        }
    )

    assert view["nodes"] == ["hpc-compute003", "hpc-compute067"]
    assert "Nodes: hpc-compute003, hpc-compute067" in format_hpc_status(view)


def test_hpc_status_omits_placement_when_the_cluster_is_down() -> None:
    view = public_hpc_status({"job_name": "prep", "status": "STOPPED", "nodes": []})

    assert "nodes" not in view
    assert "Nodes" not in format_hpc_status(view)


def test_notebook_status_reports_its_node_and_health() -> None:
    view = public_notebook(
        {
            "name": "prep",
            "status": "RUNNING",
            "node": {
                "name": "cpu-nat-351",
                "status": "READY",
                "cordon_type": "drain",
                "is_maint": True,
            },
        }
    )

    assert view["node"] == {
        "name": "cpu-nat-351",
        "status": "READY",
        "cordoned": "drain",
        "maintenance": True,
    }


def test_notebook_status_drops_the_placeholder_node_of_a_stopped_instance() -> None:
    """A STOPPED notebook answers an empty name and a proto zero-value status."""
    view = public_notebook(
        {
            "name": "prep",
            "status": "STOPPED",
            "node": {"name": "", "status": "UNKNOWN_NODE_STATUS"},
        }
    )

    assert "node" not in view


def test_notebook_status_reports_a_node_without_a_known_status() -> None:
    view = public_notebook(
        {
            "name": "prep",
            "status": "RUNNING",
            "node": {"name": "cpu-nat-351", "status": "UNKNOWN_NODE_STATUS"},
        }
    )

    assert view["node"] == {"name": "cpu-nat-351"}


def test_serving_status_reads_placement_from_extra_info() -> None:
    """``GetServing`` files ``node_names`` one level down, under ``extra_info``."""
    view = public_serving(
        {
            "name": "chat",
            "status": "RUNNING",
            "extra_info": {"node_names": ["qb-prod-4090-gpu105"]},
        }
    )

    assert view["nodes"] == ["qb-prod-4090-gpu105"]


def test_serving_status_omits_placement_when_nothing_is_running() -> None:
    view = public_serving(
        {"name": "chat", "status": "STOPPED", "extra_info": {"node_names": []}}
    )

    assert "nodes" not in view


@pytest.mark.parametrize(
    ("projector", "row"),
    (
        pytest.param(
            _public_job_instances,
            {
                "name": "worker-0",
                "instance_status": "instance_running",
                "node": "qb-prod-4090-gpu105",
            },
            id="job",
        ),
        pytest.param(
            _public_hpc_instances,
            {"name": "slurmd", "status": "Running", "node": "hpc-compute003"},
            id="hpc",
        ),
        pytest.param(
            _public_ray_instances,
            {"name": "head-0", "status": "running", "node_name": "ray-node-a"},
            id="ray",
        ),
        pytest.param(
            _public_serving_instances,
            {"name": "replica-0", "status": "Running", "node": "gpu-node-1"},
            id="serving",
        ),
    ),
)
def test_instance_rows_report_the_node_each_pod_landed_on(
    projector, row: dict[str, object]
) -> None:  # noqa: ANN001
    (item,) = projector([row])

    assert item["node"] == (row.get("node") or row.get("node_name"))


@pytest.mark.parametrize(
    "projector",
    (
        _public_job_instances,
        _public_hpc_instances,
        _public_ray_instances,
        _public_serving_instances,
    ),
)
def test_instance_rows_stay_free_of_platform_handles(projector) -> None:  # noqa: ANN001
    (item,) = projector(
        [
            {
                "instance_id": "job-abc-worker-deadbeef",
                "name": "worker-0",
                "status": "Running",
                "node": "qb-prod-4090-gpu105",
                "backend": "browser",
            }
        ]
    )

    rendered = json.dumps(item)
    assert "qb-prod-4090-gpu105" in rendered
    assert "deadbeef" not in rendered
    assert "backend" not in rendered
