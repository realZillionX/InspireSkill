"""Browser (web-session) APIs for TensorBoard.

TensorBoard is its own platform object, not a field on a training job: the
console gives it a tab beside the job list, compute groups advertise it in
`support_job_type_list` as the job type `tensorboard`, and it can be created
either against a job or standing alone on any summary directory.

Two things make the object worth wrapping past `list`. `tb_summary_path` is
the shared-disk directory the event files are read from, so a board can be
pointed at any run that already wrote events. And `url` is a live TensorBoard
HTTP app reachable with the same session cookie — which means the scalar
series behind the web view are readable as JSON, without a browser.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urljoin

from inspire.platform.web.browser_api.core import (
    _coerce_total,
    _get_base_url,
    _request_json,
    _v2_result,
)
from inspire.platform.web.session import WebSession, get_web_session
from inspire.platform.web.session.requests import build_requests_session

__all__ = [
    "MAX_AUTO_STOP_MS",
    "TensorboardInfo",
    "create_tensorboard",
    "delete_tensorboard",
    "get_tensorboard",
    "list_tensorboards",
    "read_tensorboard_runs",
    "read_tensorboard_scalar_series",
    "read_tensorboard_scalar_tags",
    "start_tensorboard",
    "stop_tensorboard",
    "tensorboard_app_url",
]

# `CreateTensorboard` answers `tensorboard_auto_stop_time_ms must less than
# 72h0m0s` above this. Rejecting client-side keeps a plain user error out of
# the transient-retry path.
MAX_AUTO_STOP_MS = 72 * 60 * 60 * 1000

_REFERER_PAGE = "/jobs/distributedTraining"
_STATUS_PREFIX = "tb_status_"


def _referer() -> str:
    return f"{_get_base_url()}{_REFERER_PAGE}"


def _train_action(
    action: str,
    body: dict,
    session: Optional[WebSession] = None,
    timeout: int = 30,
) -> dict:
    if session is None:
        session = get_web_session()
    return _v2_result(
        _request_json(
            session,
            "POST",
            f"/api/v2/train?Action={action}",
            referer=_referer(),
            body=body,
            timeout=timeout,
        )
    )


@dataclass
class TensorboardInfo:
    """One TensorBoard the platform runs, whether or not a job owns it.

    `summary_path` is the shared-disk directory the event files are read from;
    `url` is the live TensorBoard app, which :func:`read_tensorboard_runs` and
    friends query directly.
    """

    tb_id: str
    name: str
    status: str
    job_id: str
    job_name: str
    summary_path: str
    url: str
    project_name: str
    compute_group_name: str
    auto_stop_ms: str
    running_time_ms: str
    created_at: str

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> "TensorboardInfo":
        return cls(
            tb_id=str(data.get("tb_id") or "").strip(),
            name=str(data.get("name") or "").strip(),
            # `tb_status_running` / `tb_status_stopped` / `tb_status_creating`;
            # the prefix is noise in a command whose every row is a TensorBoard.
            status=str(data.get("status") or "").strip().removeprefix(_STATUS_PREFIX),
            job_id=str(data.get("job_id") or "").strip(),
            job_name=str(data.get("job_name") or "").strip(),
            summary_path=str(data.get("tb_summary_path") or "").strip(),
            url=str(data.get("url") or "").strip(),
            project_name=str(data.get("project_name") or "").strip(),
            compute_group_name=str(data.get("logic_compute_group_name") or "").strip(),
            auto_stop_ms=str(data.get("auto_stop_time_ms") or "").strip(),
            running_time_ms=str(data.get("running_time_ms") or "").strip(),
            created_at=str(data.get("created_at") or "").strip(),
        )


def list_tensorboards(
    workspace_id: Optional[str] = None,
    created_by: Optional[str] = None,
    status: Optional[str] = None,
    keyword: Optional[str] = None,
    page_num: int = 1,
    page_size: int = 50,
    session: Optional[WebSession] = None,
) -> tuple[list[TensorboardInfo], int]:
    """List the current user's TensorBoards in one workspace.

    Two things about this Action are not guessable. Its page parameter is the
    PascalCase `PageNumber` -- `page` and `page_num` are both ignored -- and
    without `created_by` it answers `total` for the whole workspace while
    `items` still holds only the rows the caller may read, so the two numbers
    cannot be reconciled. `created_by` is therefore always sent.

    `status` takes the same prefix-free value the rows are projected with
    (`running`), and is re-prefixed here so the `tb_status_` convention lives
    in one place; the field is a bare string, and a list is rejected by the
    unmarshaller. `keyword` matches on name.
    """
    if session is None:
        session = get_web_session()
    if workspace_id is None:
        raise ValueError("Workspace selection is required.")
    if created_by is None:
        from inspire.platform.web.browser_api.jobs import get_current_user

        current_user = get_current_user(session=session)
        created_by = str(current_user.get("id") or current_user.get("user_id") or "").strip()
        if not created_by:
            raise ValueError("Current user could not be resolved for TensorBoard listing.")

    body: dict[str, Any] = {
        "workspace_id": workspace_id,
        "created_by": created_by,
        "PageNumber": page_num,
        "page_size": page_size,
    }
    if status:
        status = str(status).strip()
        body["status"] = (
            status if status.startswith(_STATUS_PREFIX) else f"{_STATUS_PREFIX}{status}"
        )
    if keyword:
        body["keyword"] = keyword

    payload = _train_action("ListTensorboards", body, session)
    items = payload.get("items")
    items = items if isinstance(items, list) else []
    boards = [
        TensorboardInfo.from_api_response(item) for item in items if isinstance(item, dict)
    ]
    return boards, _coerce_total(payload.get("total"), len(boards))


def get_tensorboard(
    tb_id: str,
    session: Optional[WebSession] = None,
) -> TensorboardInfo:
    """Read one TensorBoard by handle.

    An id that does not resolve for the caller comes back as
    `InvalidParameter: 用户不存在。` — a message about the user, not the
    board. Callers must not read it as an account problem.
    """
    if not str(tb_id or "").strip():
        raise ValueError("TensorBoard id is required.")
    return TensorboardInfo.from_api_response(
        _train_action("GetTensorboard", {"tb_id": tb_id}, session)
    )


def create_tensorboard(
    *,
    name: str,
    workspace_id: str,
    project_id: str,
    logic_compute_group_id: str,
    summary_path: str,
    auto_stop_ms: int,
    job_id: str = "",
    session: Optional[WebSession] = None,
) -> dict:
    """Start a TensorBoard on one summary directory.

    The instance spec is fixed by the platform at 1 CPU / 2 GiB, so there is
    no quota to choose: a compute group is the only placement input, and it
    must advertise `tensorboard` in `support_job_type_list`.

    Three fields the platform will accept while leaving the board useless are
    required here instead. `name` may be omitted on the wire, which creates a
    nameless row that a Name-only CLI can never address again.
    `tb_summary_path` may be omitted, which creates a board with nothing to
    read. `job_id` is genuinely optional — with it the board is listed against
    that training job, without it the board stands alone.

    The Action returns an empty `Result`: **there is no id in the response**,
    so a caller that needs the handle has to find the new row through
    :func:`list_tensorboards`.
    """
    name = str(name or "").strip()
    summary_path = str(summary_path or "").strip()
    if not name:
        raise ValueError("TensorBoard name is required.")
    if not workspace_id:
        raise ValueError("Workspace selection is required.")
    if not project_id:
        raise ValueError("Project selection is required.")
    if not logic_compute_group_id:
        raise ValueError("Compute group selection is required.")
    if not summary_path:
        raise ValueError("Summary path is required.")
    if auto_stop_ms <= 0 or auto_stop_ms > MAX_AUTO_STOP_MS:
        raise ValueError(
            f"Auto-stop must be between 1ms and {MAX_AUTO_STOP_MS}ms (72h)."
        )

    body: dict[str, Any] = {
        "name": name,
        "workspace_id": workspace_id,
        "project_id": project_id,
        "logic_compute_group_id": logic_compute_group_id,
        "tb_summary_path": summary_path,
        # Declared as a string field; an int is rejected by the unmarshaller.
        "auto_stop_time_ms": str(auto_stop_ms),
    }
    if job_id:
        body["job_id"] = job_id
    return _train_action("CreateTensorboard", body, session)


def start_tensorboard(tb_id: str, session: Optional[WebSession] = None) -> dict:
    """Restart a stopped TensorBoard; it keeps its summary path and auto-stop."""
    if not str(tb_id or "").strip():
        raise ValueError("TensorBoard id is required.")
    return _train_action("StartTensorboard", {"tb_id": tb_id}, session)


def stop_tensorboard(tb_id: str, session: Optional[WebSession] = None) -> dict:
    """Stop a running TensorBoard. Stopping an already-stopped board succeeds."""
    if not str(tb_id or "").strip():
        raise ValueError("TensorBoard id is required.")
    return _train_action("StopTensorboard", {"tb_id": tb_id}, session)


def delete_tensorboard(tb_id: str, session: Optional[WebSession] = None) -> dict:
    """Delete a stopped TensorBoard record.

    A running board answers `Conflict: 当前状态（运行中）无法删除，请先停止后
    再删除` — stop first. The event files on the shared disk are untouched.
    """
    if not str(tb_id or "").strip():
        raise ValueError("TensorBoard id is required.")
    return _train_action("DeleteTensorboard", {"tb_id": tb_id}, session)


# ---------------------------------------------------------------------------
# The TensorBoard app itself
# ---------------------------------------------------------------------------
#
# `url` is a real TensorBoard server behind the same session cookie, so its
# HTTP data plane answers JSON directly. Older rows carry a site-relative
# path, newer ones an absolute address on a different host; both resolve and
# both authenticate the same way.


def tensorboard_app_url(url: str) -> str:
    """Normalize a board's `url` into an absolute base ending in a slash."""
    url = str(url or "").strip()
    if not url:
        raise ValueError("This TensorBoard has no address yet.")
    if url.startswith("/"):
        url = f"{_get_base_url()}{url}"
    return url if url.endswith("/") else f"{url}/"


def _tensorboard_get(
    url: str,
    path: str,
    session: Optional[WebSession] = None,
    params: Optional[dict[str, str]] = None,
    timeout: int = 60,
) -> Any:
    if session is None:
        session = get_web_session()
    base = tensorboard_app_url(url)
    http = build_requests_session(session, base)
    response = http.get(urljoin(base, path), params=params or None, timeout=timeout)
    if response.status_code >= 400:
        raise ValueError(
            f"TensorBoard returned {response.status_code} for {path}: "
            f"{response.text[:200]}"
        )
    return response.json()


def read_tensorboard_runs(url: str, session: Optional[WebSession] = None) -> list[str]:
    """List the run directories this board found under its summary path.

    A board pointed straight at a directory of event files reports the single
    run `"."`; one pointed at a parent reports a run per subdirectory.
    """
    runs = _tensorboard_get(url, "data/runs", session)
    return [str(run) for run in runs] if isinstance(runs, list) else []


def read_tensorboard_scalar_tags(
    url: str,
    session: Optional[WebSession] = None,
) -> dict[str, list[str]]:
    """Map each run to its scalar tags (`loss`, `eval/return`, …).

    An empty mapping means the board is running but found no scalar events —
    a wrong summary path looks exactly like a run that has not logged yet.
    """
    payload = _tensorboard_get(url, "data/plugin/scalars/tags", session)
    if not isinstance(payload, dict):
        return {}
    return {
        str(run): sorted(str(tag) for tag in tags)
        for run, tags in payload.items()
        if isinstance(tags, dict)
    }


def read_tensorboard_scalar_series(
    url: str,
    *,
    run: str,
    tag: str,
    session: Optional[WebSession] = None,
) -> list[tuple[float, int, float]]:
    """Read one scalar series as `(wall_time, step, value)` points.

    The platform returns them in event-file order, which is not step order for
    a run that was resumed or written by several workers; sorting is the
    caller's job because "the last point" and "the newest point" are different
    questions.
    """
    payload = _tensorboard_get(
        url,
        "data/plugin/scalars/scalars",
        session,
        params={"run": run, "tag": tag},
    )
    points: list[tuple[float, int, float]] = []
    if not isinstance(payload, list):
        return points
    for entry in payload:
        if not isinstance(entry, (list, tuple)) or len(entry) < 3:
            continue
        try:
            points.append((float(entry[0]), int(entry[1]), float(entry[2])))
        except (TypeError, ValueError):
            continue
    return points
