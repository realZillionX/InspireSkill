"""Dataset access applications on 数据广场: who asked for what, and how it went.

Roughly a fifth of the catalogue answers ``hasPermission: false`` for any given
account, and a mount of one of those is refused outright. The way back in is an
access application — and **submitting one is a web-only flow**. What the CLI can
do is see the applications: whether the one already filed is still pending,
approved, rejected, or withdrawn, and whether anything is waiting on this
account's own approval.

That read-only boundary is deliberate and is the whole reason this module holds
three GETs and nothing else. The plaza also exposes ``datasetApply``,
``datasetApprove`` and the ``datasetUserRole`` writes; the first two reach a
human reviewer under the caller's name, so they are left to the web UI where a
person is the one clicking.

Identifiers behave as they do everywhere else in the plaza: ``datasetCode`` is
the dataset's user-facing identity and the value a mount takes, while the
application's own ``id`` is a plaza-internal handle that addresses nothing else
and stays inside these resolvers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Sequence

from inspire.platform.web.plaza.core import plaza_request
from inspire.platform.web.session import WebSession

__all__ = [
    "APPLICATION_STATES",
    "DatasetApplication",
    "UnknownDatasetApplicationError",
    "find_dataset_applications",
    "get_dataset_application",
    "list_dataset_applications",
    "list_dataset_approvals",
]

# The catalogue's own default page is 12; the CLI's collection budget is 20.
DEFAULT_PAGE_SIZE = 20

# One request is enough to find every application on one dataset: the search is
# narrow and an account's application history is short.
_RESOLVE_PAGE_SIZE = 100

_APPLY_LIST_PATH = "/api/datasetApplyApprove/getDatasetApplyList"
_APPROVE_LIST_PATH = "/api/datasetApplyApprove/getDatasetApproveList"
_APPROVE_DETAIL_PATH = "/api/datasetApplyApprove/intoApproveById"

# The plaza reports a decision as a small int. ``0`` is a real state, so an
# absent or unreadable value must not fall through to it.
APPLICATION_STATES = {
    0: "pending",
    1: "approved",
    2: "rejected",
    -1: "withdrawn",
}


class UnknownDatasetApplicationError(LookupError):
    """No visible application matches what was asked for."""


def _text(value: Any) -> str:
    return str(value or "").strip()


def _count(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _state(value: Any) -> str:
    """Render the decision as a word, and never invent one.

    ``0`` is a real state and a falsy one, so this cannot go through
    :func:`_text` -- doing that turns every pending application into a blank.
    """
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        code = int(value)
        return APPLICATION_STATES.get(code, str(code))
    text = str(value).strip()
    if not text:
        return ""
    try:
        code = int(float(text))
    except ValueError:
        return text
    # An unmapped code is reported as the plaza sent it rather than guessed at.
    return APPLICATION_STATES.get(code, text)


@dataclass
class DatasetApplication:
    """One request for access to one dataset, from either side of it."""

    dataset: str
    state: str = ""
    # What was asked for, as the plaza labels the permission.
    authority: str = ""
    applicant: str = ""
    project: str = ""
    reason: str = ""
    approver: str = ""
    applied_at: str = ""
    decided_at: str = ""
    # Plaza-internal handle. Needed to load one record's detail; never shown.
    application_id: int = 0

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> "DatasetApplication":
        return cls(
            # The detail view is the one that carries the mountable code; a
            # list row identifies its dataset by name.
            dataset=_text(data.get("datasetCode")) or _text(data.get("datasetName")),
            state=_state(data.get("state")),
            authority=_text(data.get("authorityName")),
            applicant=_text(data.get("applyUser")),
            project=_text(data.get("projectName")),
            reason=_text(data.get("applyDescr")),
            approver=_text(data.get("approveUser")),
            applied_at=_text(data.get("applyTime")),
            decided_at=_text(data.get("approveTime")),
            application_id=_count(data.get("id")),
        )


def _page(
    path: str,
    *,
    keyword: Optional[str],
    page: int,
    page_size: int,
    session: Optional[WebSession],
) -> tuple[list[DatasetApplication], int]:
    params: dict[str, Any] = {
        "page": max(1, _count(page) or 1),
        "pageSize": max(1, _count(page_size) or DEFAULT_PAGE_SIZE),
    }
    if keyword := _text(keyword):
        params["keyword"] = keyword

    data = plaza_request("GET", path, params=params, session=session)
    payload = data if isinstance(data, dict) else {}
    rows = payload.get("list")
    items = [
        DatasetApplication.from_api_response(row)
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict)
    ]
    return items, max(_count(payload.get("total")), len(items))


def list_dataset_applications(
    *,
    keyword: Optional[str] = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    session: Optional[WebSession] = None,
) -> tuple[list[DatasetApplication], int]:
    """Return one page of the applications this account submitted.

    An empty page with ``total: 0`` is the ordinary answer for an account that
    has never applied for anything; it is the plaza saying so, not a failure.
    """
    return _page(
        _APPLY_LIST_PATH,
        keyword=keyword,
        page=page,
        page_size=page_size,
        session=session,
    )


def list_dataset_approvals(
    *,
    keyword: Optional[str] = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    session: Optional[WebSession] = None,
) -> tuple[list[DatasetApplication], int]:
    """Return one page of the applications waiting on this account's approval.

    Same envelope as :func:`list_dataset_applications`, and the rows carry the
    applicant and their project on top of it. The plaza's own front end also
    sends a ``role`` narrowing here; its semantics are not established, so it is
    not sent and not exposed.
    """
    return _page(
        _APPROVE_LIST_PATH,
        keyword=keyword,
        page=page,
        page_size=page_size,
        session=session,
    )


def get_dataset_application(
    application_id: int,
    *,
    session: Optional[WebSession] = None,
) -> DatasetApplication:
    """Load one application's full record by its plaza handle.

    The handle comes from one of the two listings; no CLI surface accepts or
    emits it. A handle nothing answers to is refused with 申请记录不存在, which
    surfaces as a :class:`~inspire.platform.web.plaza.core.PlazaError`.
    """
    handle = _count(application_id)
    if handle <= 0:
        raise UnknownDatasetApplicationError(
            "An application must be found in a listing before it can be loaded."
        )
    data = plaza_request(
        "GET",
        _APPROVE_DETAIL_PATH,
        params={"id": handle},
        session=session,
    )
    if not isinstance(data, dict):
        raise UnknownDatasetApplicationError("The data plaza returned no such application.")
    return DatasetApplication.from_api_response(data)


def _matches(application: DatasetApplication, dataset: str) -> bool:
    return application.dataset.casefold() == dataset.casefold()


def find_dataset_applications(
    dataset: str,
    *,
    incoming: bool = False,
    session: Optional[WebSession] = None,
    limit: int = DEFAULT_PAGE_SIZE,
) -> list[DatasetApplication]:
    """Load the full record of every visible application on one dataset.

    The listing is only the way in: its ``keyword`` search is the plaza's own
    and matches more than the dataset identity, so the answer is the rows whose
    dataset matches exactly, never simply the first hit. Each match is then
    re-read through the detail endpoint, which is where the mountable
    ``datasetCode`` lives — a listing row only names its dataset.
    """
    wanted = _text(dataset)
    if not wanted:
        raise UnknownDatasetApplicationError("A dataset name is required.")

    lister = list_dataset_approvals if incoming else list_dataset_applications
    items, _total = lister(
        keyword=wanted,
        page=1,
        page_size=_RESOLVE_PAGE_SIZE,
        session=session,
    )
    matches: Sequence[DatasetApplication] = [
        item for item in items if _matches(item, wanted) and item.application_id > 0
    ]
    if not matches:
        raise UnknownDatasetApplicationError(
            f"No dataset access application for {wanted!r}."
        )
    # Bounded on purpose: this is one request per record, and the caller is
    # only ever going to print the first page of them.
    return [
        get_dataset_application(item.application_id, session=session)
        for item in matches[: max(1, limit)]
    ]
