from __future__ import annotations
from inspire.services.tensorboard.tensorboard_status import normalize_status
import time
from inspire.platform.web.flow import call, perform_sync
from typing import Any
from inspire.config import ConfigError
from inspire.platform.web import browser_api as browser_api_module
from inspire.services.catalog.compute_groups import group_supports_workload
from inspire.services.catalog.quotas import validate_compute_group_name

RESOURCE_TYPE = "tensorboard"
_NAME_SCAN_LIMIT = 2000
_CREATE_LOOKUP_ATTEMPTS = 10
_CREATE_LOOKUP_INTERVAL_SECONDS = 2.0
_STOP_CONFIRM_ATTEMPTS = 20
_STOP_CONFIRM_INTERVAL_SECONDS = 3.0


def current_user_id(session) -> str:  # noqa: ANN001
    me = browser_api_module.get_current_user(session=session)
    user_id = str(me.get("id") or me.get("user_id") or "").strip()
    if not user_id:
        raise ValueError("Cannot determine the current user from the live web session.")
    return user_id


def fetch_boards(
    session,  # noqa: ANN001
    *,
    workspace_id: str,
    limit: int,
    status: str = "",
    keyword: str = "",
) -> list[Any]:
    """Read one page of this account's boards, following `total` if short."""
    boards, total = browser_api_module.list_tensorboards(
        workspace_id=workspace_id,
        status=status or None,
        keyword=keyword or None,
        page_num=1,
        page_size=limit,
        session=session,
    )
    if total > len(boards) and len(boards) >= limit:
        boards, _ = browser_api_module.list_tensorboards(
            workspace_id=workspace_id,
            status=status or None,
            keyword=keyword or None,
            page_num=1,
            page_size=total,
            session=session,
        )
    return boards


def resolve_group_id(
    session,  # noqa: ANN001
    *,
    workspace_id: str,
    group: str,
    select=None,
    groups_loader=None,
) -> str:
    """Resolve a compute group name that can actually run a TensorBoard.

    Group support is uneven — in `分布式训练空间` several training groups do
    not advertise `tensorboard` — and quoting one of those reaches the
    platform as `已选择的计算类型组不支持此类型任务` at create time.
    """
    group = validate_compute_group_name(group)
    groups = (
        groups_loader()
        if groups_loader
        else browser_api_module.list_compute_groups(
            workspace_id=workspace_id,
            session=session,
        )
    )
    named = [
        candidate
        for candidate in groups
        if str(candidate.get("name") or "").strip().casefold() == group.casefold()
    ]
    if not named:
        raise ConfigError(
            f"No compute group named {group!r} in this workspace. "
            "List them with `inspire resources availability --workspace <name>`."
        )
    usable = [candidate for candidate in named if group_supports_workload(candidate, RESOURCE_TYPE)]
    if not usable:
        raise ConfigError(
            f"Compute group {group!r} does not run TensorBoards. "
            "Pick a group that advertises the tensorboard job type."
        )
    if select is not None:
        return str(select(usable))
    group_id = str(usable[0].get("logic_compute_group_id") or usable[0].get("id") or "")
    if not group_id:
        raise ConfigError(f"Compute group {group!r} has no usable handle.")
    return group_id


def find_created_board(
    session,  # noqa: ANN001
    *,
    workspace_id: str,
    name: str,
) -> Any:
    """Find the row a `CreateTensorboard` just made; it returns no id."""
    board = None
    for attempt in range(_CREATE_LOOKUP_ATTEMPTS):
        if attempt:
            perform_sync(call(time.sleep, _CREATE_LOOKUP_INTERVAL_SECONDS))
        matches = [
            candidate
            for candidate in fetch_boards(
                session,
                workspace_id=workspace_id,
                limit=_NAME_SCAN_LIMIT,
                keyword=name,
            )
            if candidate.name == name
        ]
        if matches:
            # Newest first is the platform's own list order.
            board = matches[0]
            if normalize_status(board.status) != "CREATING":
                return board
    return board


def await_status(
    session,  # noqa: ANN001
    tb_id: str,
    *,
    leaving: str = "",
    reaching: str = "",
    attempts: int = _STOP_CONFIRM_ATTEMPTS,
    interval: float = _STOP_CONFIRM_INTERVAL_SECONDS,
) -> str:
    """Poll until the board leaves *leaving* or reaches *reaching*."""
    status = ""
    for attempt in range(attempts):
        if attempt:
            perform_sync(call(time.sleep, interval))
        status = browser_api_module.get_tensorboard(tb_id, session=session).status
        if reaching and status == reaching:
            return status
        if leaving and status != leaving:
            return status
    return status


def job_candidates(
    *, session, name: str, workspace_id: str, user_id_loader=None
) -> list[dict[str, Any]]:
    user_id = user_id_loader() if user_id_loader else current_user_id(session)
    jobs, _ = browser_api_module.list_jobs(
        workspace_id=workspace_id,
        created_by=user_id,
        keyword=name,
        page_num=1,
        page_size=200,
        session=session,
    )
    return [
        {
            "name": job.name,
            "id": job.job_id,
            "status": job.status,
            "created_at": job.created_at,
        }
        for job in jobs
    ]
