"""Name-only identity projection contracts shared by workload outputs."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from inspire.cli.commands.hpc.public_output import public_hpc_list_item
from inspire.cli.commands.job.public_output import public_job_list_item
from inspire.cli.commands.notebook.public_output import public_notebook_list_item
from inspire.cli.commands.ray.public_output import public_ray_list_item
from inspire.cli.commands.serving.public_output import public_serving_list_item

Projector = Callable[[dict[str, Any]], dict[str, Any]]

_PROJECTORS = (
    pytest.param(public_job_list_item, id="job"),
    pytest.param(public_hpc_list_item, id="hpc"),
    pytest.param(public_ray_list_item, id="ray"),
    pytest.param(public_notebook_list_item, id="notebook"),
    pytest.param(public_serving_list_item, id="serving"),
)
_HIDDEN_IDENTITIES = (
    "user-hidden",
    "usr_391",
    "student-42",
    "253108120116",
)


@pytest.mark.parametrize("projector", _PROJECTORS)
def test_identity_projection_rejects_ambiguous_scalar_fields(
    projector: Projector,
) -> None:
    item = {
        "name": "demo",
        "created_by": "user-hidden",
        "creator": "usr_391",
        "owner": "student-42",
        "username": "253108120116",
        "login_name": "253108120116",
    }

    view = projector(item)

    assert view["created_by"] == ""
    rendered = json.dumps(view)
    for hidden in _HIDDEN_IDENTITIES:
        assert hidden not in rendered


@pytest.mark.parametrize("projector", _PROJECTORS)
def test_identity_projection_rejects_nested_handles_without_names(
    projector: Projector,
) -> None:
    item = {
        "name": "demo",
        "created_by": {
            "id": "user-hidden",
            "username": "usr_391",
            "login_name": "253108120116",
        },
        "creator": {"id": "student-42"},
        "owner": {"id": "253108120116"},
    }

    view = projector(item)

    assert view["created_by"] == ""
    rendered = json.dumps(view)
    for hidden in _HIDDEN_IDENTITIES:
        assert hidden not in rendered


@pytest.mark.parametrize("projector", _PROJECTORS)
@pytest.mark.parametrize("identity_key", ("created_by", "creator", "owner"))
@pytest.mark.parametrize("name_key", ("name", "display_name"))
def test_identity_projection_accepts_nested_display_names(
    projector: Projector,
    identity_key: str,
    name_key: str,
) -> None:
    view = projector(
        {
            "name": "demo",
            identity_key: {
                "id": "user-hidden",
                name_key: "Ada Lovelace",
            },
        }
    )

    assert view["created_by"] == "Ada Lovelace"
    assert "user-hidden" not in json.dumps(view)


@pytest.mark.parametrize("projector", _PROJECTORS)
@pytest.mark.parametrize(
    "name_field",
    ("created_by_name", "creator_name", "owner_name"),
)
def test_identity_projection_accepts_explicit_name_fields(
    projector: Projector,
    name_field: str,
) -> None:
    view = projector({"name": "demo", name_field: "Ada Lovelace"})

    assert view["created_by"] == "Ada Lovelace"
