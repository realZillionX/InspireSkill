"""Browser (web-session) notebook HTTP APIs (images, schedule, create, stop, start, detail, wait)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from inspire.config import Config
from inspire.platform.web.browser_api.core import (
    _browser_api_path,
    _get_base_url,
    _request_json,
    _v2_result,
)
from inspire.platform.web.session import DEFAULT_WORKSPACE_ID, WebSession, get_web_session

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class NotebookFailedError(Exception):
    """Raised when a notebook reaches a terminal failure state."""

    def __init__(self, notebook_id: str, status: str, detail: dict, events: str = ""):
        self.notebook_id = notebook_id
        self.status = status
        self.detail = detail
        self.events = events
        parts = [f"Notebook '{notebook_id}' reached terminal status: {status}"]
        sub = detail.get("sub_status")
        if sub:
            parts.append(f"Sub-status: {sub}")
        super().__init__(". ".join(parts))


_NOTEBOOK_TERMINAL_STATUSES = frozenset({"FAILED", "ERROR", "STOPPED", "DELETED"})


@dataclass
class ImageInfo:
    """Docker image information."""

    image_id: str
    url: str
    name: str
    framework: str
    version: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _notebooks_referer() -> str:
    return f"{_get_base_url()}/jobs/interactiveModeling"


def _get_session_and_workspace_id(
    *,
    workspace_id: Optional[str],
    session: Optional[WebSession],
) -> tuple[WebSession, str]:
    if session is None:
        session = get_web_session()

    if workspace_id is None:
        workspace_id = session.workspace_id or DEFAULT_WORKSPACE_ID

    return session, workspace_id


def _request_notebooks_data(
    session: WebSession,
    method: str,
    endpoint_path: str,
    *,
    body: Optional[dict] = None,
    timeout: int = 30,
    default_data: Any = None,
) -> Any:
    """Call a `/api/v1` endpoint that has no v2 counterpart.

    Resource pricing is the only family left on it: no Action on any v2 route
    answers `/resource_prices/logic_compute_groups/`. The notebook family is on
    v2 via :func:`_notebook_v2`, the image family via :func:`_image_v2`.
    """
    data = _request_json(
        session,
        method,
        _browser_api_path(endpoint_path),
        referer=_notebooks_referer(),
        body=body,
        timeout=timeout,
    )

    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    return data.get("data", default_data)


def _image_v2(
    session: WebSession,
    action: str,
    body: Optional[dict] = None,
    *,
    timeout: int = 30,
) -> dict[str, Any]:
    """Call one `/api/v2/image` Action and return its unwrapped ``Result``.

    Only `DeleteImage`, `GetImageById`, `ListImageBrands` and `ListImages` are
    published in discovery; `CreateImage` and `UpdateImage` are live but
    undocumented, and all six take the v1 request bodies unchanged.
    """
    data = _request_json(
        session,
        "POST",
        f"/api/v2/image?Action={action}",
        referer=_notebooks_referer(),
        body=body or {},
        timeout=timeout,
    )
    return _v2_result(data)


def _notebook_v2(
    session: WebSession,
    action: str,
    body: Optional[dict] = None,
    *,
    timeout: int = 30,
) -> dict[str, Any]:
    """Call one `/api/v2/notebook` Action and return its unwrapped ``Result``.

    Keeps the ``API error: ...`` message shape the v1 helper raised so the
    command layer's error handling is unchanged.
    """
    data = _request_json(
        session,
        "POST",
        f"/api/v2/notebook?Action={action}",
        referer=_notebooks_referer(),
        body=body or {},
        timeout=timeout,
    )
    return _v2_result(data)


def _notebook_page(payload: dict[str, Any]) -> tuple[list[dict], int]:
    """Split a paged notebook Result into its item list and total.

    The list key here is ``list``, not the ``items`` that ``ray`` uses — v2 has
    no cross-Action convention for it, so it is spelled out per domain.
    """
    items = payload.get("list")
    if not isinstance(items, list):
        items = []
    items = [item for item in items if isinstance(item, dict)]

    total_raw = payload.get("total")
    try:
        total = int(str(total_raw)) if total_raw is not None else len(items)
    except (TypeError, ValueError):
        total = len(items)
    return items, total


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------


def list_images(
    workspace_id: Optional[str] = None,
    source: str = "SOURCE_OFFICIAL",
    session: Optional[WebSession] = None,
) -> list[ImageInfo]:
    """List available Docker images.

    Args:
        workspace_id: Workspace ID for filtering.
        source: Image source filter. Use "SOURCE_OFFICIAL" for official images,
            "SOURCE_PUBLIC" for public images (uses visibility filter as
            required by the platform API).
        session: Existing web session.
    """
    session, workspace_id = _get_session_and_workspace_id(
        workspace_id=workspace_id, session=session
    )

    if source == "SOURCE_PUBLIC":
        # Public images require source_list + visibility (not a simple source field).
        # Discovered via Playwright network capture of the platform UI.
        body: dict = {
            "page": 0,
            "page_size": -1,
            "filter": {
                "source_list": ["SOURCE_PRIVATE", "SOURCE_PUBLIC"],
                "visibility": "VISIBILITY_PUBLIC",
                "registry_hint": {"workspace_id": workspace_id},
            },
        }
    elif source == "SOURCE_PRIVATE":
        # Personal-visible images require private visibility across private/public sources.
        body = {
            "page": 0,
            "page_size": -1,
            "filter": {
                "source_list": ["SOURCE_PRIVATE", "SOURCE_PUBLIC"],
                "visibility": "VISIBILITY_PRIVATE",
                "registry_hint": {"workspace_id": workspace_id},
            },
        }
    else:
        body = {
            "page": 0,
            "page_size": -1,
            "filter": {
                "source": source,
                "source_list": [],
                "registry_hint": {"workspace_id": workspace_id},
            },
        }

    data = _image_v2(session, "ListImages", body)
    items = data.get("images", [])
    results = []
    for item in items:
        url = item.get("address", "")
        name = item.get("name", url.split("/")[-1] if url else "")
        framework = item.get("framework", "")
        version = item.get("version", "")

        results.append(
            ImageInfo(
                image_id=item.get("image_id", ""),
                url=url,
                name=name,
                framework=framework,
                version=version,
            )
        )
    return results


# ---------------------------------------------------------------------------
# Schedule / Prices / Compute groups
# ---------------------------------------------------------------------------


def get_resource_prices(
    workspace_id: Optional[str] = None,
    logic_compute_group_id: str = "",
    schedule_config_type: str = "SCHEDULE_CONFIG_TYPE_DSW",
    session: Optional[WebSession] = None,
) -> list[dict]:
    """Fetch resource spec prices for a compute group and schedule type.

    The UI calls this endpoint when the user opens the resource spec dialog.
    Returns a list of price entries, each containing quota_id, cpu_count,
    memory_size_gib, gpu_count, gpu_info, and price.

    Known schedule types:
    - SCHEDULE_CONFIG_TYPE_DSW: notebook/DSW quotas
    - SCHEDULE_CONFIG_TYPE_HPC: HPC/Slurm predef_node_specs
    - SCHEDULE_CONFIG_TYPE_TRAIN: training-job framework specs
    - SCHEDULE_CONFIG_TYPE_RAY_JOB: Ray head / worker quotas
      (consumed by `inspire ray create --group/--quota` and worker specs)
    """
    session, workspace_id = _get_session_and_workspace_id(
        workspace_id=workspace_id, session=session
    )

    body = {
        "workspace_id": workspace_id,
        "schedule_config_type": schedule_config_type,
        "logic_compute_group_id": logic_compute_group_id,
    }

    try:
        data = _request_notebooks_data(
            session,
            "POST",
            "/resource_prices/logic_compute_groups/",
            body=body,
            timeout=30,
            default_data=[],
        )
    except ValueError:
        return []

    if isinstance(data, list):
        return data
    # The API nests results under 'lcg_resource_spec_prices'
    return data.get(
        "lcg_resource_spec_prices", data.get("resource_spec_prices", data.get("list", []))
    )


def list_notebook_compute_groups(
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """List compute groups available for notebook creation.

    Use the workspace-wide ``logic_compute_groups/list`` endpoint as the
    source of truth, with an InspireSkill-config-based fallback for offline
    or misconfigured environments.
    """
    session, workspace_id = _get_session_and_workspace_id(
        workspace_id=workspace_id, session=session
    )

    api_error: Exception | None = None
    try:
        from inspire.platform.web.browser_api.availability.api import (
            list_compute_groups as _list_groups,
        )

        data = _list_groups(workspace_id=workspace_id, session=session)
        if isinstance(data, list) and data:
            return data
    except Exception as exc:  # noqa: BLE001 — fallback path must remain available
        api_error = exc

    fallback = _config_compute_groups_fallback(workspace_id=workspace_id)
    if fallback:
        reason = f"API error: {api_error!r}" if api_error else "API returned empty list"
        _log.warning(
            "list_notebook_compute_groups: %s for workspace %s — "
            "falling back to %d compute_groups defined in config.toml. "
            "The returned list may be stale; prefer a live refresh when available.",
            reason,
            workspace_id,
            len(fallback),
        )
        return fallback

    if api_error is not None:
        _log.warning(
            "list_notebook_compute_groups: %r and no config.toml fallback "
            "available; returning empty list.",
            api_error,
        )
    return []


def list_notebook_users(
    workspace_id: Optional[str] = None,
    *,
    session: Optional[WebSession] = None,
) -> tuple[list[dict[str, Any]], int]:
    """List users for the notebook page filter (``ListNotebookCreators``)."""
    session, workspace_id = _get_session_and_workspace_id(
        workspace_id=workspace_id, session=session
    )
    payload = _notebook_v2(
        session,
        "ListNotebookCreators",
        {"workspace_id": workspace_id},
        timeout=15,
    )
    return _notebook_page(payload)


def list_notebooks(
    workspace_id: str,
    *,
    user_ids: list[str],
    keyword: str = "",
    status: Optional[list[str]] = None,
    page: int = 1,
    page_size: int = 100,
    session: Optional[WebSession] = None,
) -> tuple[list[dict], Optional[int]]:
    """Fetch one page of notebooks for a workspace via ``ListNotebooks``.

    Returns ``(items, total)``; ``total`` is ``None`` when the platform omits
    it. Pagination policy stays with the caller — this is a 1:1 wrapper around
    the platform call.

    The filter envelope is the same one v1 took, and v2 accepts it verbatim:
    ``filter_by`` carries the user / keyword / status selection while
    ``workspace_id`` stays top-level. The nested ``filter`` object that
    ``workspace.*`` Actions require is rejected here.
    """
    if not user_ids:
        raise ValueError("Cannot list notebooks without a current-user filter.")
    if session is None:
        session = get_web_session()

    body: dict[str, Any] = {
        "workspace_id": workspace_id,
        "page": max(1, int(page)),
        "page_size": max(1, int(page_size)),
        "filter_by": {
            "keyword": keyword,
            "user_id": list(user_ids),
            "logic_compute_group_id": [],
            "status": list(status or []),
            "mirror_url": [],
        },
        "order_by": [{"field": "created_at", "order": "desc"}],
    }

    payload = _notebook_v2(session, "ListNotebooks", body)
    items = payload.get("list")
    if not isinstance(items, list):
        return [], None
    items = [item for item in items if isinstance(item, dict)]
    try:
        total = int(str(payload["total"]))
    except (KeyError, TypeError, ValueError):
        total = None
    return items, total


def _config_compute_groups_fallback(workspace_id: str | None = None) -> list[dict]:
    """Build synthetic compute group list from InspireSkill config."""
    try:
        cfg, _ = Config.from_files_and_env(require_credentials=False)
    except Exception:
        return []

    groups = cfg.compute_groups
    result = []
    for g in groups:
        group_ws_ids = g.get("workspace_ids") or []
        if workspace_id and group_ws_ids and workspace_id not in group_ws_ids:
            continue
        gpu_type = g.get("gpu_type", "")
        is_real_gpu = gpu_type and gpu_type.upper() != "CPU"
        result.append(
            {
                "logic_compute_group_id": g.get("id", ""),
                "name": g.get("name", ""),
                "gpu_type_stats": (
                    [
                        {
                            "gpu_info": {
                                "gpu_type": gpu_type,
                                "gpu_type_display": gpu_type,
                                "brand_name": gpu_type,
                            },
                        }
                    ]
                    if is_real_gpu
                    else []
                ),
            }
        )
    return result


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def create_notebook(
    name: str,
    project_id: str,
    project_name: str,
    image_id: str,
    image_url: str,
    logic_compute_group_id: str,
    quota_id: str,
    gpu_count: int,
    cpu_count: int,
    memory_size: int,
    shared_memory_size: int,
    auto_stop: bool,
    workspace_id: Optional[str] = None,
    session: Optional[WebSession] = None,
    task_priority: Optional[int] = None,
    resource_spec_price: Optional[dict] = None,
    node_id: Optional[str] = None,
) -> dict:
    """Create a new notebook instance.

    The request body must match the exact structure the platform UI sends.
    Captured via Playwright network interception — the proto rejects unknown
    fields, so only send fields the backend expects.
    """
    session, workspace_id = _get_session_and_workspace_id(
        workspace_id=workspace_id, session=session
    )

    # Match the exact field set the platform UI sends (captured via Playwright).
    # Proto-compatible names: mirror_id/mirror_url (not image_id/image_url).
    # The UI does NOT send: gpu_type (top-level).
    # ``allow_ssh: true`` is required so the platform exposes the in-container
    # rtunnel server (port 31337) on the proxy URL the CLI tunnels through;
    # without it the proxy returns 404 and notebook SSH bootstrap cannot
    # complete its preflight. Empirically the field defaults to false when
    # omitted, regardless of image SSH tooling — verified 2026-04-25 against
    # pytorch-inspire-base on CPU资源-2.
    body: dict[str, Any] = {
        "workspace_id": workspace_id,
        "name": name,
        "project_id": project_id,
        "project_name": project_name,
        "auto_stop": auto_stop,
        "allow_ssh": True,
        "mirror_id": image_id,
        "mirror_url": image_url,
        "logic_compute_group_id": logic_compute_group_id,
        "quota_id": quota_id,
        "cpu_count": cpu_count,
        "gpu_count": gpu_count,
        "memory_size": memory_size,
        "shared_memory_size": shared_memory_size,
    }

    # resource_spec_price is required for GPU notebooks.
    # Structure: {cpu_type, cpu_count, gpu_type, gpu_count, memory_size_gib,
    #             logic_compute_group_id, quota_id}
    if resource_spec_price is not None:
        body["resource_spec_price"] = resource_spec_price

    if task_priority is not None:
        body["task_priority"] = task_priority

    if node_id:
        body["node_id"] = node_id

    return _notebook_v2(session, "CreateNotebook", body)


def stop_notebook(
    notebook_id: str,
    session: Optional[WebSession] = None,
) -> dict:
    """Stop a running notebook instance.

    v1 multiplexed start/stop through ``/notebook/operate`` with an
    ``operation`` enum; v2 splits them into their own Actions and takes only
    the id.
    """
    session, _ = _get_session_and_workspace_id(workspace_id=None, session=session)

    return _notebook_v2(session, "StopNotebook", {"notebook_id": notebook_id})


def start_notebook(
    notebook_id: str,
    session: Optional[WebSession] = None,
) -> dict:
    """Start a stopped notebook instance."""
    session, _ = _get_session_and_workspace_id(workspace_id=None, session=session)

    return _notebook_v2(session, "StartNotebook", {"notebook_id": notebook_id})


def delete_notebook(
    notebook_id: str,
    session: Optional[WebSession] = None,
) -> dict:
    """Permanently delete a notebook instance.

    v1 needed the REST-style ``DELETE /api/v1/notebook/{id}`` because
    ``/notebook/operate`` only accepted ``START`` / ``STOP``; v2 has a first
    class ``DeleteNotebook`` Action, so the special case is gone.

    Destructive — the entry disappears from the platform UI and cannot be
    recovered. If the notebook is running, stop it first.
    """
    session, _ = _get_session_and_workspace_id(workspace_id=None, session=session)

    return _notebook_v2(session, "DeleteNotebook", {"notebook_id": notebook_id})


def get_notebook_detail(
    notebook_id: str,
    session: Optional[WebSession] = None,
) -> dict:
    """Get detailed notebook information."""
    session, _ = _get_session_and_workspace_id(workspace_id=None, session=session)

    return _notebook_v2(session, "GetNotebook", {"notebook_id": notebook_id})


# ---------------------------------------------------------------------------
# Wait
# ---------------------------------------------------------------------------


def list_notebook_events(
    notebook_id: str,
    *,
    page: int = 1,
    page_size: int = 500,
    fetch_all: bool = True,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """Fetch lifecycle events for a notebook.

    The request body is ``{notebook_id, page, page_size}``; response data uses
    ``{list, total}``.

    Events are returned oldest-first. The platform caps per-page size; by
    default this function auto-paginates from `page` until all `total`
    events are collected, matching what the web UI shows (long-running
    notebooks can accumulate 200+ events and the platform default cap is
    low). Pass ``fetch_all=False`` to stop after the first page.

    Each raw event carries `content` (free-form message), `created_at`
    (epoch-ms string), `event_id`, `id`, and `notebook_id`. The shared
    renderer in `cli.utils.events` consumes the synthesized common fields:

    - ``message`` ← ``content``
    - ``last_timestamp`` ← ``created_at`` (same for ``first_timestamp``)

    Raw fields remain available to internal callers. Public CLI renderers
    project them onto the compact event schema before emitting text or JSON.

    Raises on transport / business errors. Diagnostic callers that need
    silent degradation should wrap this with their own try/except — see
    `_try_fetch_events` for the inline wait-loop preview pattern.
    """
    if session is None:
        session = get_web_session()

    def _fetch_page(p: int, ps: int) -> tuple[list[dict], Optional[int]]:
        data = _notebook_v2(
            session,
            "ListNotebookEvents",
            {"notebook_id": notebook_id, "page": p, "page_size": ps},
            timeout=10,
        )
        items_raw = data.get("list") or data.get("events") or []
        items_list = items_raw if isinstance(items_raw, list) else []
        total_raw = data.get("total")
        if total_raw is None:
            return items_list, None
        try:
            return items_list, int(total_raw)
        except (TypeError, ValueError):
            return items_list, None

    raw_items: list[dict] = []
    current_page = page
    while True:
        items, total = _fetch_page(current_page, page_size)
        raw_items.extend(items)
        if not fetch_all:
            break
        # Stop on empty page (natural end of stream regardless of `total`).
        if not items:
            break
        # Only use the server-reported total as a terminator when it is
        # actually present. Missing / unparseable `total` → keep paginating
        # until a page comes back empty, otherwise we'd silently truncate
        # at page 1 (the bug Codex flagged).
        if total is not None and len(raw_items) >= total:
            break
        # Short page → last page (page_size is the server-side cap; a page
        # smaller than it can only mean the stream is exhausted).
        if len(items) < page_size:
            break
        current_page += 1
        if current_page > 100:  # safety cap: 50k events at page_size=500
            break

    out: list[dict] = []
    for ev in raw_items:
        if not isinstance(ev, dict):
            continue
        norm = dict(ev)
        if "message" not in norm and ev.get("content"):
            norm["message"] = ev["content"]
        ts = ev.get("created_at") or ev.get("timestamp")
        if ts is not None:
            norm.setdefault("last_timestamp", ts)
            norm.setdefault("first_timestamp", ts)
        out.append(norm)
    return out


def _try_fetch_events(notebook_id: str, session: WebSession) -> str:
    """Best-effort inline-friendly event preview used by `wait_for_notebook_running`.

    Returns a short `\n`-joined string summary of the last ~10 events, or ""
    when none are available. Swallows errors so the wait-loop's terminal-error
    message can render even when the events endpoint itself is unreachable.
    For full programmatic access use :func:`list_notebook_events`.
    """
    try:
        events = list_notebook_events(notebook_id, session=session)
    except Exception:
        return ""
    if not events:
        return ""

    lines = []
    for ev in events[-10:]:
        reason = ev.get("reason") or ""
        message = ev.get("message") or ""
        ev_type = ev.get("type") or ""
        if not (reason or message or ev_type):
            continue
        prefix = f"[{ev_type}] " if ev_type else ""
        label = f"{reason}: " if reason else ""
        lines.append(f"{prefix}{label}{message}")
    return "\n".join(lines) if lines else ""


def list_notebook_runs(
    notebook_id: str,
    *,
    session: Optional[WebSession] = None,
) -> list[dict]:
    """List all run cycles of a notebook via ``ListRunIndex``.

    Each run corresponds to one start→stop cycle (notebooks can be
    re-started after being auto-recycled or manually stopped). Returns the
    raw `list`: each entry is `{index: int, start_time: str, end_time: str}`.
    The current run has `end_time = ""`. Sorted oldest-first by `index`.

    Takes the notebook id alone — this Action is unpaginated and rejects
    ``PageNumber`` outright.

    Backs `inspire notebook lifecycle`, which uses the run boundaries as a
    coarse counterpart to the fine-grained `inspire notebook events`.
    """
    if session is None:
        session = get_web_session()
    data = _notebook_v2(
        session, "ListRunIndex", {"notebook_id": notebook_id}, timeout=15
    )
    runs = data.get("list")
    if isinstance(runs, list):
        return [r for r in runs if isinstance(r, dict)]
    return []


def list_notebook_lifecycle(
    notebook_id: str,
    *,
    page: int = 1,
    page_size: int = 200,
    start_time: str = "",
    end_time: str = "",
    session: Optional[WebSession] = None,
) -> list[dict]:
    """Fetch notebook lifecycle state-transition records (``ListNotebookLifecycles``).

    Returns the raw `list`. In practice this Action returns an empty list
    for most notebooks on qz.sii.edu.cn (2026-04) — the web 生命周期 tab
    is rendered from :func:`list_notebook_runs` instead. Kept as a thin
    wrapper because the Action is present on the platform and may come back
    with data as it evolves.
    """
    if session is None:
        session = get_web_session()
    body: dict[str, Any] = {
        "notebook_id": notebook_id,
        "page": page,
        "page_size": page_size,
    }
    if start_time != "":
        body["start_time"] = start_time
    if end_time != "":
        body["end_time"] = end_time
    data = _notebook_v2(session, "ListNotebookLifecycles", body, timeout=15)
    items = data.get("list")
    if isinstance(items, list):
        return [r for r in items if isinstance(r, dict)]
    return []


def wait_for_notebook_running(
    notebook_id: str,
    session: Optional[WebSession] = None,
    timeout: int = 600,
    poll_interval: int = 5,
    progress_callback: Optional[Callable[[dict, str, str], None]] = None,
    progress_interval: int = 30,
) -> dict:
    """Wait for a notebook instance to reach RUNNING status."""
    if session is None:
        session = get_web_session()

    start = time.time()
    last_status = None
    last_progress_at = 0.0
    last_reported_status = None

    while True:
        notebook = get_notebook_detail(notebook_id=notebook_id, session=session)
        status = (notebook.get("status") or "").upper()
        if status:
            last_status = status

        if status == "RUNNING":
            return notebook

        if status in _NOTEBOOK_TERMINAL_STATUSES:
            events = _try_fetch_events(notebook_id, session)
            raise NotebookFailedError(notebook_id, status, notebook, events=events)

        now = time.time()
        if progress_callback is not None and (
            status != last_reported_status or now - last_progress_at >= progress_interval
        ):
            events = _try_fetch_events(notebook_id, session)
            progress_callback(notebook, status, events)
            last_reported_status = status
            last_progress_at = now

        if now - start >= timeout:
            raise TimeoutError(
                f"Notebook '{notebook_id}' did not reach RUNNING within {timeout}s "
                f"(last status: {last_status or 'unknown'})"
            )

        time.sleep(poll_interval)


__all__ = [
    "ImageInfo",
    "NotebookFailedError",
    "create_notebook",
    "get_notebook_detail",
    "get_resource_prices",
    "list_images",
    "list_notebook_compute_groups",
    "list_notebook_events",
    "list_notebook_lifecycle",
    "list_notebook_runs",
    "list_notebook_users",
    "list_notebooks",
    "start_notebook",
    "stop_notebook",
    "wait_for_notebook_running",
    "_config_compute_groups_fallback",
]
