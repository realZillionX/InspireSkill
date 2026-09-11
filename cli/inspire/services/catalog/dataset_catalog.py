"""Dataset catalog, version and application views."""

from __future__ import annotations

from typing import Any

from inspire.services.utils.raw_ids import scrub_raw_ids
from inspire.platform.web import plaza as plaza_module
from inspire.platform.web.browser_api.datasets import container_mount_path
from inspire.services.utils.text import clip_display


DESCRIPTION_BUDGET = 400


def summarize(text: str, *, budget: int = DESCRIPTION_BUDGET) -> str:
    """Collapse a markdown description into one clipped, readable line."""
    collapsed = " ".join(str(text or "").split())
    return clip_display(scrub_raw_ids(collapsed), budget) if collapsed else ""


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _format_size(files_size_mib: int) -> str:
    """Render a version's size, which the catalogue reports in MiB."""
    size = float(files_size_mib or 0)
    if size <= 0:
        return ""
    for unit in ("MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return ""  # pragma: no cover - the loop always returns


def dataset_row(dataset: plaza_module.DatasetSummary) -> dict[str, Any]:
    """The compact, name-only projection of one catalogue row."""
    view: dict[str, Any] = {
        "name": scrub_raw_ids(dataset.code),
        "project": scrub_raw_ids(dataset.project),
        "grade": scrub_raw_ids(dataset.grade),
        "state": scrub_raw_ids(dataset.state),
        "access": _yes_no(dataset.accessible),
        "tags": [scrub_raw_ids(tag) for tag in dataset.tags],
        "updated_at": scrub_raw_ids(dataset.updated_at),
    }
    return {key: value for key, value in view.items() if value not in ("", [], None)}


def dataset_detail_view(detail: plaza_module.DatasetDetail) -> dict[str, Any]:
    view: dict[str, Any] = {
        "name": scrub_raw_ids(detail.code),
        "project": scrub_raw_ids(detail.project),
        "grade": scrub_raw_ids(detail.grade),
        "state": scrub_raw_ids(detail.state),
        "access": _yes_no(detail.accessible),
        "owner": scrub_raw_ids(detail.owner),
        "maintainer": scrub_raw_ids(detail.maintainer),
        "tags": [scrub_raw_ids(tag) for tag in detail.tags],
        "data_type": scrub_raw_ids(detail.data_type),
        "source_type": scrub_raw_ids(detail.source_type),
        "license": scrub_raw_ids(detail.license_name),
        "license_url": scrub_raw_ids(detail.license_url),
        "updated_at": scrub_raw_ids(detail.updated_at),
        "description": summarize(detail.description),
    }
    return {key: value for key, value in view.items() if value not in ("", [], None)}


def version_views(detail: plaza_module.DatasetDetail) -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = []
    for version in detail.versions:
        view: dict[str, Any] = {
            "version": scrub_raw_ids(version.code),
            "state": scrub_raw_ids(version.state),
            "size": _format_size(version.files_size_mib),
            "files": version.files_count or "",
            "formats": [scrub_raw_ids(fmt) for fmt in version.data_formats],
            "updated_at": scrub_raw_ids(version.updated_at),
            "mount": f"--dataset {detail.code}:{version.code}",
            "path": container_mount_path(detail.code, version.code),
        }
        views.append({key: value for key, value in view.items() if value not in ("", [], None)})
    return views


def application_row(
    application: plaza_module.DatasetApplication,
    *,
    incoming: bool,
) -> dict[str, Any]:
    """Project one application down to what identifies and qualifies it."""
    view: dict[str, Any] = {
        "name": scrub_raw_ids(application.dataset),
        "state": scrub_raw_ids(application.state),
        "authority": scrub_raw_ids(application.authority),
        "applied_at": scrub_raw_ids(application.applied_at),
    }
    if incoming:
        view["applicant"] = scrub_raw_ids(application.applicant)
        view["project"] = scrub_raw_ids(application.project)
    else:
        view["decided_at"] = scrub_raw_ids(application.decided_at)
        view["approver"] = scrub_raw_ids(application.approver)
    return {key: value for key, value in view.items() if value not in ("", None)}


def application_detail_view(
    application: plaza_module.DatasetApplication,
) -> dict[str, Any]:
    view = {
        "name": scrub_raw_ids(application.dataset),
        "state": scrub_raw_ids(application.state),
        "authority": scrub_raw_ids(application.authority),
        "applicant": scrub_raw_ids(application.applicant),
        "project": scrub_raw_ids(application.project),
        "reason": summarize(application.reason),
        "approver": scrub_raw_ids(application.approver),
        "applied_at": scrub_raw_ids(application.applied_at),
        "decided_at": scrub_raw_ids(application.decided_at),
    }
    return {key: value for key, value in view.items() if value not in ("", None)}
