"""Shared `--dataset` handling for the workload create commands.

`notebook`, `job` and `hpc` all accept official-dataset mounts through the same
`dataset_info` payload field, so they share one spec grammar and one resolver.
`ray` and `serving` do not: the platform rejects `dataset_info` on both, and
their console forms have no 官方数据集 section either.

Spec grammar is `<dataset>:<version>`, both being the codes shown by
`inspire dataset list` — never the numeric ids the plaza uses internally.
"""

from __future__ import annotations

from typing import Callable, Iterable, Optional, Sequence

import click

from inspire.services.catalog.datasets import (
    DatasetSpecError as DatasetSpecError,
    dataset_mount_views as dataset_mount_views,
    parse_dataset_spec as parse_dataset_spec,
    parse_dataset_specs as parse_dataset_specs,
    resolve_dataset_info as resolve_dataset_info,
)

from inspire.platform.web.browser_api.datasets import (
    DatasetMount,
    container_mount_path,
)

__all__ = [
    "DATASET_OPTION_HELP",
    "DatasetSpecError",
    "dataset_mount_views",
    "dataset_option",
    "describe_dataset_mounts",
    "parse_dataset_spec",
    "parse_dataset_specs",
    "parse_dataset_specs_or_usage_error",
    "resolve_dataset_info",
]

DATASET_OPTION_HELP = (
    "Mount an official dataset as '<dataset>:<version>' (repeatable), using the "
    "codes from 'inspire dataset list' — not the numeric ids. Each mount appears "
    "inside the container at /inspire/dataset/<dataset>/<version>. The platform "
    "resolves and checks every entry before the workload is submitted."
)


def describe_dataset_mounts(mounts: Sequence[DatasetMount]) -> list[str]:
    """Human lines for dry-run and post-create output."""
    return [
        f"{m.dataset}:{m.version} -> {container_mount_path(m.dataset, m.version)}" for m in mounts
    ]


def dataset_option() -> Callable:
    """Return the shared `--dataset` option used by the create commands."""
    return click.option(
        "--dataset",
        "datasets",
        multiple=True,
        metavar="NAME:VERSION",
        help=DATASET_OPTION_HELP,
    )


def parse_dataset_specs_or_usage_error(
    values: Optional[Iterable[str]],
) -> list[DatasetMount]:
    """Parse `--dataset` values, reporting a bad spec as a Click usage error.

    A malformed spec is a typo in the command line, not a platform verdict, so
    it is reported before any workspace, quota or project is resolved.
    """
    try:
        return parse_dataset_specs(values)
    except DatasetSpecError as e:
        raise click.UsageError(str(e)) from e
