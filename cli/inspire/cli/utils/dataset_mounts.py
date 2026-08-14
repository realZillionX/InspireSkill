"""Shared `--dataset` handling for the workload create commands.

`notebook`, `job` and `hpc` all accept official-dataset mounts through the same
`dataset_info` payload field, so they share one spec grammar and one resolver.
`ray` and `serving` do not: the platform rejects `dataset_info` on both, and
their console forms have no 官方数据集 section either.

Spec grammar is `<dataset>:<version>`, both being the codes shown by
`inspire dataset list` — never the numeric ids the plaza uses internally.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional, Sequence

from inspire.platform.web.browser_api.datasets import (
    DatasetMount,
    DatasetValidation,
    container_mount_path,
    validate_dataset_mounts,
)

__all__ = [
    "DATASET_OPTION_HELP",
    "DatasetSpecError",
    "describe_dataset_mounts",
    "parse_dataset_spec",
    "parse_dataset_specs",
    "resolve_dataset_info",
]

DATASET_OPTION_HELP = (
    "Mount an official dataset as '<dataset>:<version>' (repeatable), using the "
    "codes from 'inspire dataset list' — not the numeric ids. Each mount appears "
    "inside the container at /inspire/dataset/<dataset>/<version>. The platform "
    "resolves and checks every entry before the workload is submitted."
)


class DatasetSpecError(ValueError):
    """Raised when a `--dataset` value cannot be parsed or resolved."""


def parse_dataset_spec(text: str) -> DatasetMount:
    """Parse one `<dataset>:<version>` value."""
    raw = str(text or "").strip()
    if not raw:
        raise DatasetSpecError("--dataset requires '<dataset>:<version>'")
    dataset, separator, version = raw.partition(":")
    dataset = dataset.strip()
    version = version.strip()
    if not separator or not dataset or not version:
        raise DatasetSpecError(
            f"--dataset expects '<dataset>:<version>' (for example 'pixabay-81k:v0'); got {raw!r}"
        )
    return DatasetMount(dataset=dataset, version=version)


def parse_dataset_specs(values: Optional[Iterable[str]]) -> list[DatasetMount]:
    """Parse repeated `--dataset` values, rejecting duplicates."""
    mounts: list[DatasetMount] = []
    seen: set[tuple[str, str]] = set()
    for value in values or ():
        mount = parse_dataset_spec(value)
        key = (mount.dataset, mount.version)
        if key in seen:
            raise DatasetSpecError(
                f"--dataset {mount.dataset}:{mount.version} was given more than once"
            )
        seen.add(key)
        mounts.append(mount)
    return mounts


def _describe_failures(failed: Sequence[DatasetValidation]) -> str:
    lines = [
        f"  {v.dataset}:{v.version} — {v.error or 'rejected by the platform'}" for v in failed
    ]
    return "The platform rejected these dataset mounts:\n" + "\n".join(lines)


def resolve_dataset_info(
    mounts: Sequence[DatasetMount],
    *,
    workspace_id: str,
    session: Any = None,
) -> list[dict[str, str]]:
    """Validate the requested mounts and build the `dataset_info` payload.

    Resolution is not optional: the create Actions take a `path` alongside the
    two codes, and the platform fills that in through `ValidateDataset` — the
    same round trip the console makes when 校验数据 is pressed. Validating first
    also turns a typo into a clear error before a workload is submitted rather
    than after it fails to start.
    """
    if not mounts:
        return []

    verdicts = validate_dataset_mounts(mounts, workspace_id=workspace_id, session=session)
    failed = [v for v in verdicts if not v.ok]
    if failed:
        raise DatasetSpecError(_describe_failures(failed))
    return [
        DatasetMount(dataset=v.dataset, version=v.version).as_payload(v.path) for v in verdicts
    ]


def describe_dataset_mounts(mounts: Sequence[DatasetMount]) -> list[str]:
    """Human lines for dry-run and post-create output."""
    return [f"{m.dataset}:{m.version} -> {container_mount_path(m.dataset, m.version)}" for m in mounts]
