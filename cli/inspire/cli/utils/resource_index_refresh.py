"""Live refresh engine for the per-account resource identity index.

The index is disposable acceleration state. Every refresh reads the platform;
normal list/status commands continue to use live APIs as their source of truth.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from inspire.accounts import account_dir, current_account
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.cli.utils.resource_index import (
    DEFAULT_TTL_SECONDS,
    ResourceIdentity,
    ResourceIndex,
    scope_for_session,
)

RESOURCE_TYPES = (
    "workspace",
    "project",
    "compute-group",
    "image",
    "model",
    "job",
    "hpc",
    "ray",
    "serving",
    "notebook",
    "ssh-key",
)
GLOBAL_RESOURCE_TYPES = frozenset({"workspace", "ssh-key"})
WORKSPACE_RESOURCE_TYPES = tuple(
    resource_type
    for resource_type in RESOURCE_TYPES
    if resource_type not in GLOBAL_RESOURCE_TYPES
)

PERIODIC_REFRESH_INTERVAL_SECONDS = 5 * 60
PERIODIC_REFRESH_STAMP = "resource-index-refresh.stamp"


@dataclass(frozen=True)
class FetchResult:
    records: list[ResourceIdentity]
    complete: bool = True


@dataclass(frozen=True)
class RefreshResult:
    resource_type: str
    workspace_name: str
    item_count: int
    outcome: str
    error: str = ""

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "resource": self.resource_type,
            "items": self.item_count,
            "outcome": self.outcome,
        }
        if self.workspace_name:
            payload["workspace"] = self.workspace_name
        if self.error:
            payload["error"] = scrub_raw_ids(self.error)
        return payload


@dataclass(frozen=True)
class RefreshSummary:
    results: list[RefreshResult]

    @property
    def refreshed_count(self) -> int:
        return sum(result.outcome == "refreshed" for result in self.results)

    @property
    def skipped_count(self) -> int:
        return sum(result.outcome == "fresh" for result in self.results)

    @property
    def busy_count(self) -> int:
        return sum(result.outcome == "busy" for result in self.results)

    @property
    def error_count(self) -> int:
        return sum(result.outcome == "error" for result in self.results)

    @property
    def item_count(self) -> int:
        return sum(
            result.item_count
            for result in self.results
            if result.outcome == "refreshed"
        )

    def to_payload(self) -> dict[str, object]:
        return {
            "refreshed": self.refreshed_count,
            "fresh": self.skipped_count,
            "busy": self.busy_count,
            "errors": self.error_count,
            "items": self.item_count,
            "scopes": [result.to_payload() for result in self.results],
        }


Fetcher = Callable[[object, str, str], FetchResult]


def _dedupe_records(records: Iterable[ResourceIdentity]) -> list[ResourceIdentity]:
    by_id: dict[str, ResourceIdentity] = {}
    for record in records:
        resource_id = str(record.resource_id or "").strip()
        name = str(record.name or "").strip()
        if not resource_id or not name:
            continue
        by_id[resource_id] = ResourceIdentity(
            resource_id=resource_id,
            name=name,
            owner_id=str(record.owner_id or "").strip(),
            status=str(record.status or "").strip(),
            created_at=str(record.created_at or "").strip(),
        )
    return list(by_id.values())


def _filter_exact(
    records: Iterable[ResourceIdentity],
    exact_name: str,
    *,
    case_sensitive: bool = True,
) -> list[ResourceIdentity]:
    if not exact_name:
        return list(records)
    if case_sensitive:
        return [record for record in records if record.name == exact_name]
    needle = exact_name.casefold()
    return [record for record in records if record.name.casefold() == needle]


def _workspace_fetch(session: object, _workspace_id: str, exact_name: str) -> FetchResult:
    from inspire.platform.web.browser_api.workspaces import try_enumerate_workspaces

    live_items = try_enumerate_workspaces(session)  # type: ignore[arg-type]
    records = [
        ResourceIdentity(
            resource_id=str(item.get("id") or "").strip(),
            name=str(item.get("name") or "").strip(),
        )
        for item in live_items
        if isinstance(item, dict)
    ]
    return FetchResult(
        _filter_exact(
            _dedupe_records(records),
            exact_name,
            case_sensitive=False,
        ),
        complete=True,
    )


def _project_fetch(session: object, workspace_id: str, exact_name: str) -> FetchResult:
    from inspire.platform.web.browser_api.projects import list_projects

    items = list_projects(workspace_id=workspace_id, session=session)  # type: ignore[arg-type]
    records = [
        ResourceIdentity(
            resource_id=item.project_id,
            name=item.name,
        )
        for item in items
    ]
    return FetchResult(
        _filter_exact(_dedupe_records(records), exact_name, case_sensitive=False)
    )


def _compute_group_fetch(
    session: object,
    workspace_id: str,
    exact_name: str,
) -> FetchResult:
    from inspire.platform.web.browser_api.availability.api import list_compute_groups

    items = list_compute_groups(workspace_id=workspace_id, session=session)  # type: ignore[arg-type]
    records = []
    for item in items:
        resource_id = str(
            item.get("logic_compute_group_id") or item.get("id") or ""
        ).strip()
        name = str(
            item.get("name")
            or item.get("logic_compute_group_name")
            or item.get("compute_group_name")
            or ""
        ).strip()
        records.append(ResourceIdentity(resource_id=resource_id, name=name))
    return FetchResult(
        _filter_exact(_dedupe_records(records), exact_name, case_sensitive=False)
    )


def _image_fetch(session: object, workspace_id: str, exact_name: str) -> FetchResult:
    from inspire.platform.web.browser_api.notebooks import list_images

    records: list[ResourceIdentity] = []
    for source in ("SOURCE_OFFICIAL", "SOURCE_PUBLIC", "SOURCE_PRIVATE"):
        items = list_images(
            workspace_id=workspace_id,
            source=source,
            session=session,  # type: ignore[arg-type]
        )
        for item in items:
            name = str(item.name or "").strip()
            version = str(item.version or "").strip()
            label = name if ":" in name or not version else f"{name}:{version}"
            records.append(
                ResourceIdentity(
                    resource_id=item.image_id,
                    name=label,
                )
            )
    return FetchResult(_filter_exact(_dedupe_records(records), exact_name))


def _model_fetch(session: object, workspace_id: str, exact_name: str) -> FetchResult:
    from inspire.platform.web.browser_api.models import list_models

    items, _ = list_models(
        workspace_id=workspace_id,
        keyword=exact_name or None,
        page_size=-1,
        session=session,  # type: ignore[arg-type]
    )
    records = [
        ResourceIdentity(
            resource_id=item.model_id,
            name=item.name,
            owner_id=item.user_id,
            status=item.status,
            created_at=item.created_at,
        )
        for item in items
    ]
    return FetchResult(_filter_exact(_dedupe_records(records), exact_name))


def _current_user_id(session: object) -> str:
    detail = getattr(session, "user_detail", None)
    if isinstance(detail, dict):
        value = detail.get("id") or detail.get("user_id")
        if value:
            return str(value).strip()

    from inspire.platform.web.browser_api.jobs import get_current_user

    detail = get_current_user(session=session)  # type: ignore[arg-type]
    value = detail.get("id") or detail.get("user_id")
    if not value:
        raise ValueError("Cannot determine the current account user.")
    try:
        setattr(session, "user_detail", detail)
        session.save()  # type: ignore[attr-defined]
    except Exception:
        pass
    return str(value).strip()


def _job_fetch(session: object, workspace_id: str, exact_name: str) -> FetchResult:
    from inspire.platform.web.browser_api.jobs import list_jobs

    user_id = _current_user_id(session)
    records: list[ResourceIdentity] = []
    page = 1
    page_size = 100
    while True:
        items, total = list_jobs(
            workspace_id=workspace_id,
            created_by=user_id,
            keyword=exact_name or None,
            page_num=page,
            page_size=page_size,
            session=session,  # type: ignore[arg-type]
        )
        records.extend(
            ResourceIdentity(
                resource_id=item.job_id,
                name=item.name,
                owner_id=item.created_by_id,
                status=item.status,
                created_at=item.created_at,
            )
            for item in items
        )
        if not items or len(records) >= total or len(items) < page_size:
            break
        page += 1
    return FetchResult(_filter_exact(_dedupe_records(records), exact_name))


def _hpc_fetch(session: object, workspace_id: str, exact_name: str) -> FetchResult:
    from inspire.platform.web.browser_api.hpc_jobs import list_hpc_jobs

    user_id = _current_user_id(session)
    records: list[ResourceIdentity] = []
    page = 1
    page_size = 100
    total_seen = 0
    while True:
        items, total = list_hpc_jobs(
            workspace_id=workspace_id,
            created_by=user_id,
            page_num=page,
            page_size=page_size,
            session=session,  # type: ignore[arg-type]
        )
        total_seen += len(items)
        records.extend(
            ResourceIdentity(
                resource_id=item.job_id,
                name=item.name,
                owner_id=item.created_by_id,
                status=item.status,
                created_at=item.created_at,
            )
            for item in items
        )
        if not items or total_seen >= total or len(items) < page_size:
            break
        page += 1
    return FetchResult(_filter_exact(_dedupe_records(records), exact_name))


def _ray_fetch(session: object, workspace_id: str, exact_name: str) -> FetchResult:
    from inspire.platform.web.browser_api.ray_jobs import list_ray_jobs

    user_id = _current_user_id(session)
    records: list[ResourceIdentity] = []
    page = 1
    page_size = 100
    total_seen = 0
    while True:
        items, total = list_ray_jobs(
            workspace_id=workspace_id,
            user_ids=[user_id],
            page_num=page,
            page_size=page_size,
            session=session,  # type: ignore[arg-type]
        )
        total_seen += len(items)
        records.extend(
            ResourceIdentity(
                resource_id=item.ray_job_id,
                name=item.name,
                owner_id=item.created_by_id,
                status=item.status,
                created_at=item.created_at,
            )
            for item in items
        )
        if not items or total_seen >= total or len(items) < page_size:
            break
        page += 1
    return FetchResult(_filter_exact(_dedupe_records(records), exact_name))


def _serving_fetch(session: object, workspace_id: str, exact_name: str) -> FetchResult:
    from inspire.platform.web.browser_api.servings import list_servings

    records: list[ResourceIdentity] = []
    page = 1
    page_size = 100
    total_seen = 0
    while True:
        items, total = list_servings(
            workspace_id=workspace_id,
            page=page,
            page_size=page_size,
            keyword=exact_name or None,
            session=session,  # type: ignore[arg-type]
        )
        total_seen += len(items)
        records.extend(
            ResourceIdentity(
                resource_id=item.inference_serving_id,
                name=item.name,
                status=item.status,
                created_at=item.created_at,
            )
            for item in items
        )
        if not items or total_seen >= total or len(items) < page_size:
            break
        page += 1
    return FetchResult(_filter_exact(_dedupe_records(records), exact_name))


def _notebook_fetch(session: object, workspace_id: str, exact_name: str) -> FetchResult:
    from inspire.cli.commands.notebook.notebook_lookup import (
        _list_notebooks_for_workspace,
        _notebook_id_from_item,
        _try_get_current_user_ids,
    )

    base_url = str(getattr(session, "base_url", None) or "").strip().rstrip("/")
    if not base_url:
        raise ValueError("The current account session has no platform URL.")
    user_ids = _try_get_current_user_ids(session, base_url=base_url)  # type: ignore[arg-type]
    if not user_ids:
        raise ValueError("Cannot determine the current account user.")
    items = _list_notebooks_for_workspace(
        session,  # type: ignore[arg-type]
        base_url=base_url,
        workspace_id=workspace_id,
        user_ids=user_ids,
        keyword=exact_name,
    )
    records = [
        ResourceIdentity(
            resource_id=str(_notebook_id_from_item(item) or ""),
            name=str(item.get("name") or ""),
            owner_id=str(
                item.get("user_id")
                or item.get("owner_id")
                or item.get("creator_id")
                or ""
            ),
            status=str(item.get("status") or ""),
            created_at=str(item.get("created_at") or ""),
        )
        for item in items
    ]
    return FetchResult(_filter_exact(_dedupe_records(records), exact_name))


def _ssh_key_fetch(session: object, _workspace_id: str, exact_name: str) -> FetchResult:
    from inspire.platform.web.browser_api.users import list_user_ssh_keys

    records: list[ResourceIdentity] = []
    page = 1
    page_size = 500
    total_seen = 0
    while True:
        items, total = list_user_ssh_keys(
            page=page,
            page_size=page_size,
            session=session,  # type: ignore[arg-type]
        )
        total_seen += len(items)
        for item in items:
            records.append(
                ResourceIdentity(
                    resource_id=str(item.get("ssh_id") or item.get("id") or ""),
                    name=str(item.get("name") or item.get("title") or ""),
                    created_at=str(item.get("created_at") or item.get("create_at") or ""),
                )
            )
        if not items or total_seen >= total or len(items) < page_size:
            break
        page += 1
    return FetchResult(_filter_exact(_dedupe_records(records), exact_name))


RESOURCE_FETCHERS: Mapping[str, Fetcher] = {
    "workspace": _workspace_fetch,
    "project": _project_fetch,
    "compute-group": _compute_group_fetch,
    "image": _image_fetch,
    "model": _model_fetch,
    "job": _job_fetch,
    "hpc": _hpc_fetch,
    "ray": _ray_fetch,
    "serving": _serving_fetch,
    "notebook": _notebook_fetch,
    "ssh-key": _ssh_key_fetch,
}


def _workspace_names(
    session: object,
    *,
    workspace_fetch: FetchResult,
) -> dict[str, str]:
    names = {
        record.resource_id: record.name
        for record in workspace_fetch.records
        if record.resource_id and record.name
    }
    cached = getattr(session, "all_workspace_names", None)
    if not workspace_fetch.complete and isinstance(cached, dict):
        for workspace_id, name in cached.items():
            if workspace_id and name:
                names.setdefault(str(workspace_id), str(name))
    return names


def _select_workspace_ids(
    workspace_names: Mapping[str, str],
    requested: Sequence[str] | None,
) -> list[str]:
    if not requested or any(value.strip().lower() == "all" for value in requested):
        return list(workspace_names)

    selected: list[str] = []
    for requested_name in requested:
        normalized = requested_name.strip().casefold()
        matches = [
            workspace_id
            for workspace_id, name in workspace_names.items()
            if name.strip().casefold() == normalized
        ]
        if not matches:
            raise ValueError(f"Unknown workspace name: {requested_name!r}.")
        if len(matches) > 1:
            raise ValueError(f"Workspace name is ambiguous: {requested_name!r}.")
        if matches[0] not in selected:
            selected.append(matches[0])
    return selected


def _refresh_one(
    *,
    index: ResourceIndex,
    session: object,
    resource_type: str,
    workspace_id: str,
    workspace_name: str,
    exact_name: str,
    force: bool,
    fetcher: Fetcher,
) -> RefreshResult:
    scope = scope_for_session(
        session,
        resource_type=resource_type,
        workspace_id=workspace_id,
        owner_scope="self" if resource_type not in {"workspace", "project", "compute-group"} else "",
    )
    if scope is None:
        return RefreshResult(
            resource_type=resource_type,
            workspace_name=workspace_name,
            item_count=0,
            outcome="error",
            error="The current account session has no stable identity.",
        )

    interval = DEFAULT_TTL_SECONDS.get(resource_type, 300)
    if (
        not force
        and not exact_name
        and not index.scope_due(
            scope,
            interval_seconds=interval,
            require_full=True,
        )
    ):
        return RefreshResult(resource_type, workspace_name, 0, "fresh")

    with index.refresh_lease(scope) as acquired:
        if not acquired:
            return RefreshResult(resource_type, workspace_name, 0, "busy")
        try:
            fetched = fetcher(session, workspace_id, exact_name)
            records = _dedupe_records(fetched.records)
            if exact_name:
                count = index.replace_name(
                    scope,
                    exact_name,
                    records,
                    ttl_seconds=interval,
                )
            elif fetched.complete:
                count = index.reconcile(
                    scope,
                    records,
                    ttl_seconds=interval,
                )
            else:
                count = index.upsert(
                    scope,
                    records,
                    ttl_seconds=interval,
                )
            return RefreshResult(resource_type, workspace_name, count, "refreshed")
        except Exception as exc:  # noqa: BLE001 - aggregate all scopes
            index.record_refresh_error(scope, str(exc))
            return RefreshResult(
                resource_type,
                workspace_name,
                0,
                "error",
                scrub_raw_ids(str(exc) or type(exc).__name__),
            )


def refresh_resource_index(
    *,
    session: object,
    index: ResourceIndex,
    resource_types: Sequence[str] | None = None,
    workspace_names: Sequence[str] | None = None,
    exact_name: str = "",
    force: bool = False,
    fetchers: Mapping[str, Fetcher] | None = None,
) -> RefreshSummary:
    """Refresh selected resource scopes and return a name-only summary."""
    selected_types = tuple(resource_types or RESOURCE_TYPES)
    unknown = sorted(set(selected_types) - set(RESOURCE_TYPES))
    if unknown:
        raise ValueError(f"Unknown resource type: {', '.join(unknown)}")
    if exact_name and len(selected_types) != 1:
        raise ValueError("--name requires exactly one --resource.")

    registry = fetchers or RESOURCE_FETCHERS
    workspace_fetcher = registry["workspace"]
    try:
        workspace_snapshot = workspace_fetcher(
            session,
            "",
            exact_name if selected_types == ("workspace",) else "",
        )
    except Exception as exc:
        workspace_snapshot = FetchResult([])
        workspace_error = str(exc)
    else:
        workspace_error = ""

    names_by_id = (
        {}
        if workspace_error
        else _workspace_names(session, workspace_fetch=workspace_snapshot)
    )
    selected_workspace_ids = (
        []
        if workspace_error
        else _select_workspace_ids(names_by_id, workspace_names)
    )

    results: list[RefreshResult] = []
    for resource_type in selected_types:
        fetcher = registry[resource_type]
        if resource_type == "workspace":
            if workspace_error:
                scope = scope_for_session(session, resource_type="workspace")
                if scope is not None:
                    index.record_refresh_error(scope, workspace_error)
                results.append(
                    RefreshResult(
                        "workspace",
                        "",
                        0,
                        "error",
                        scrub_raw_ids(workspace_error),
                    )
                )
                continue
            results.append(
                _refresh_one(
                    index=index,
                    session=session,
                    resource_type="workspace",
                    workspace_id="",
                    workspace_name="",
                    exact_name=exact_name,
                    force=force,
                    fetcher=lambda _session, _workspace, _name: workspace_snapshot,
                )
            )
            continue

        if resource_type == "ssh-key":
            results.append(
                _refresh_one(
                    index=index,
                    session=session,
                    resource_type=resource_type,
                    workspace_id="",
                    workspace_name="",
                    exact_name=exact_name,
                    force=force,
                    fetcher=fetcher,
                )
            )
            continue

        if not selected_workspace_ids:
            results.append(
                RefreshResult(
                    resource_type,
                    "",
                    0,
                    "error",
                    "No visible workspace names are available.",
                )
            )
            continue
        for workspace_id in selected_workspace_ids:
            results.append(
                _refresh_one(
                    index=index,
                    session=session,
                    resource_type=resource_type,
                    workspace_id=workspace_id,
                    workspace_name=names_by_id.get(workspace_id, ""),
                    exact_name=exact_name,
                    force=force,
                    fetcher=fetcher,
                )
            )

    index.purge_tombstones()
    return RefreshSummary(results)


def periodic_refresh_stamp_path(account: str | None = None) -> Path | None:
    selected = str(account or "").strip() or current_account()
    if not selected:
        return None
    return account_dir(selected) / PERIODIC_REFRESH_STAMP


def maybe_spawn_periodic_refresh(
    *,
    interval_seconds: int = PERIODIC_REFRESH_INTERVAL_SECONDS,
) -> bool:
    """Spawn a quiet due-only refresh when a valid cached session is available."""
    if os.environ.get("INSPIRE_RESOURCE_INDEX_REFRESH_CHILD") == "1":
        return False
    if os.environ.get("INSPIRE_DISABLE_RESOURCE_INDEX_REFRESH") == "1":
        return False
    if "PYTEST_CURRENT_TEST" in os.environ:
        return False

    account = current_account()
    stamp = periodic_refresh_stamp_path(account)
    if not account or stamp is None:
        return False

    from inspire.platform.web.session.models import WebSession

    if WebSession.load(account=account) is None:
        return False

    now = time.time()
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(
                    stamp,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                try:
                    stamp.chmod(0o600)
                    contents = stamp.read_text(encoding="ascii").strip()
                    pid = int(contents)
                except (OSError, ValueError):
                    pid = 0
                if pid:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        pass
                    except PermissionError:
                        return False
                    except OSError:
                        return False
                    else:
                        return False
                try:
                    if now - stamp.stat().st_mtime < max(30, interval_seconds):
                        return False
                    stamp.unlink()
                except FileNotFoundError:
                    continue
                except OSError:
                    return False
                continue
            else:
                with os.fdopen(fd, "w", encoding="ascii") as handle:
                    handle.write(str(os.getpid()))
                break
        else:
            return False
    except OSError:
        return False

    env = os.environ.copy()
    env["INSPIRE_RESOURCE_INDEX_REFRESH_CHILD"] = "1"
    env["INSPIRE_SKIP_UPDATE_CHECK"] = "1"
    try:
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "inspire.cli.main",
                "cache",
                "refresh",
                "--due",
                "--quiet",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        child_pid = getattr(process, "pid", None)
        if isinstance(child_pid, int) and child_pid > 0:
            try:
                stamp.write_text(str(child_pid), encoding="ascii")
                stamp.chmod(0o600)
            except OSError:
                pass
    except OSError:
        try:
            stamp.unlink()
        except OSError:
            pass
        return False
    return True


__all__ = [
    "GLOBAL_RESOURCE_TYPES",
    "PERIODIC_REFRESH_INTERVAL_SECONDS",
    "RESOURCE_FETCHERS",
    "RESOURCE_TYPES",
    "WORKSPACE_RESOURCE_TYPES",
    "FetchResult",
    "RefreshResult",
    "RefreshSummary",
    "maybe_spawn_periodic_refresh",
    "periodic_refresh_stamp_path",
    "refresh_resource_index",
]
