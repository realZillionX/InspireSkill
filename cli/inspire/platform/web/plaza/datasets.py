"""The 数据广场 dataset catalogue: browse, search, and resolve dataset codes.

Two identifier systems meet here and must not be confused. ``datasetCode`` and
``versionCode`` are the dataset's user-facing identity: they are what the mount
API accepts, what the container path is built from, and what this CLI shows.
``datasetId`` and ``versionId`` are plaza-internal handles — ``findDatasets``
needs one, and mounting with one is rejected outright with 数据集不存在 — so they
stay inside these resolvers and never reach CLI output.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional, Sequence

from inspire.platform.web.plaza.core import plaza_request
from inspire.platform.web.session import WebSession

__all__ = [
    "DEFAULT_PAGE_SIZE",
    "TAG_CATEGORIES",
    "DatasetDetail",
    "DatasetSummary",
    "DatasetTag",
    "DatasetVersion",
    "UnknownDatasetError",
    "UnknownDatasetTagError",
    "get_dataset_detail",
    "list_dataset_tags",
    "list_datasets",
    "resolve_dataset_by_code",
    "resolve_tag_ids",
]

# The catalogue's own default page is 12; the CLI's collection budget is 20.
DEFAULT_PAGE_SIZE = 20

# One request is enough to resolve a code: the search is narrow, and the plaza
# honours any page size (it returned all 531 rows for pageSize=1000).
_RESOLVE_PAGE_SIZE = 100

# The tag catalogue is a small fixed vocabulary (52 tags as of 2026-08); the
# SPA asks for 999 and so do we, rather than paging something that never pages.
_TAG_PAGE_SIZE = 999

# Tag groups, as the 数据广场 sidebar labels them. Category 0 is folded into 文本
# by the front end, so it is folded here too.
TAG_CATEGORIES = {
    0: "文本",
    1: "文本",
    2: "图像",
    3: "音频",
    4: "视频",
    5: "多模态",
}


class UnknownDatasetError(LookupError):
    """No catalogue entry carries the requested dataset code."""


class UnknownDatasetTagError(LookupError):
    """A requested tag name is not in the catalogue's tag vocabulary."""

    def __init__(self, unknown: Sequence[str], available: Sequence[str]) -> None:
        self.unknown = tuple(unknown)
        self.available = tuple(available)
        super().__init__(f"Unknown dataset tag: {', '.join(self.unknown)}")


def _text(value: Any) -> str:
    return str(value or "").strip()


def _count(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _tag_names(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    names = [_text(item.get("tagName")) for item in value if isinstance(item, dict)]
    return tuple(name for name in names if name)


def _data_formats(value: Any) -> tuple[str, ...]:
    """Unpack ``dataFormats``, which the plaza stores as a JSON string."""
    raw = _text(value)
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except ValueError:
        return (raw,)
    if not isinstance(parsed, list):
        return (raw,)
    return tuple(entry for item in parsed if (entry := _text(item)))


@dataclass
class DatasetTag:
    """One entry of the catalogue's tag vocabulary."""

    name: str
    category: str = ""
    tag_id: int = 0

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> "DatasetTag":
        category_id = _count(data.get("categoryId"))
        return cls(
            name=_text(data.get("tagName")),
            category=TAG_CATEGORIES.get(category_id, ""),
            tag_id=_count(data.get("tagId")),
        )


@dataclass
class DatasetVersion:
    """One version of a dataset, and therefore one mountable unit."""

    code: str
    state: str = ""
    description: str = ""
    files_count: int = 0
    # ``filesSize`` is MiB: the 2,816,752 reported for pixabay-81k is the
    # 2.95 TB its own description states.
    files_size_mib: int = 0
    data_formats: tuple[str, ...] = ()
    updated_at: str = ""
    version_id: int = 0

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> "DatasetVersion":
        return cls(
            code=_text(data.get("versionCode")),
            state=_text(data.get("versionState")),
            description=_text(data.get("description")),
            files_count=_count(data.get("filesCount")),
            files_size_mib=_count(data.get("filesSize")),
            data_formats=_data_formats(data.get("dataFormats")),
            updated_at=_text(data.get("updateTime")),
            version_id=_count(data.get("versionId")),
        )


@dataclass
class DatasetSummary:
    """A catalogue row, as the 数据广场 list returns it."""

    code: str
    project: str = ""
    owner: str = ""
    director: str = ""
    maintainer: str = ""
    grade: str = ""
    state: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    # ``hasPermission`` is the account's own access, and the reason a mount
    # comes back 无访问权限: roughly a fifth of the catalogue is closed.
    accessible: bool = False
    created_at: str = ""
    updated_at: str = ""
    dataset_id: int = 0

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> "DatasetSummary":
        return cls(
            code=_text(data.get("datasetCode")),
            project=_text(data.get("projectName")),
            owner=_text(data.get("ownerName")),
            director=_text(data.get("director")),
            maintainer=_text(data.get("maintenance")),
            grade=_text(data.get("super")),
            state=_text(data.get("state")),
            description=_text(data.get("description")),
            tags=_tag_names(data.get("tags")),
            accessible=bool(data.get("hasPermission")),
            created_at=_text(data.get("createdAt")),
            updated_at=_text(data.get("updatedAt")),
            dataset_id=_count(data.get("datasetId")),
        )


@dataclass
class DatasetDetail:
    """A dataset's full record, including the versions that can be mounted."""

    code: str
    project: str = ""
    owner: str = ""
    director: str = ""
    maintainer: str = ""
    grade: str = ""
    state: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    accessible: bool = False
    data_type: str = ""
    source_type: str = ""
    license_name: str = ""
    license_url: str = ""
    created_at: str = ""
    updated_at: str = ""
    versions: list[DatasetVersion] = field(default_factory=list)
    dataset_id: int = 0

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> "DatasetDetail":
        raw_versions = data.get("versions")
        versions = [
            version
            for item in (raw_versions if isinstance(raw_versions, list) else [])
            if isinstance(item, dict)
            if (version := DatasetVersion.from_api_response(item)).code
        ]
        return cls(
            code=_text(data.get("datasetCode")),
            project=_text(data.get("projectName")),
            owner=_text(data.get("ownerName")),
            director=_text(data.get("director")),
            maintainer=_text(data.get("maintenance")),
            grade=_text(data.get("super")),
            state=_text(data.get("state")),
            description=_text(data.get("description")),
            tags=_tag_names(data.get("tags")),
            accessible=bool(data.get("hasPermission")),
            data_type=_text(data.get("dataType")),
            source_type=_text(data.get("sourceType")),
            license_name=_text(data.get("licenseName")),
            license_url=_text(data.get("licenseUrl")),
            created_at=_text(data.get("createdAt")),
            updated_at=_text(data.get("updatedAt")),
            versions=versions,
            dataset_id=_count(data.get("datasetId")),
        )


def list_datasets(
    *,
    keyword: Optional[str] = None,
    tag_ids: Optional[Iterable[int]] = None,
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
    session: Optional[WebSession] = None,
) -> tuple[list[DatasetSummary], int]:
    """Return one page of the catalogue and the total number of matches.

    ``keyword`` is the plaza's own search: case-insensitive, and matched
    against the description as well as the code and project name, so a common
    word matches far more rows than it looks like it should.
    """
    params: dict[str, Any] = {
        "page": max(1, _count(page) or 1),
        "pageSize": max(1, _count(page_size) or DEFAULT_PAGE_SIZE),
    }
    if keyword := _text(keyword):
        params["keyword"] = keyword
    selected = [tag_id for tag_id in (tag_ids or ()) if _count(tag_id) > 0]
    if selected:
        # `tags` is a comma-joined list of tag handles, matched with OR
        # semantics. An empty value is not a wildcard — the plaza reads it as
        # "matches no tag" and answers with an empty page — so it is omitted
        # entirely when nothing is selected, exactly as the SPA does.
        params["tags"] = ",".join(str(_count(tag_id)) for tag_id in selected)

    data = plaza_request(
        "GET",
        "/api/datasets/getDatasetsList",
        params=params,
        session=session,
        timeout=60,
    )
    payload = data if isinstance(data, dict) else {}
    rows = payload.get("list")
    items = [
        summary
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict)
        if (summary := DatasetSummary.from_api_response(row)).code
    ]
    total = payload.get("total")
    return items, max(_count(total), len(items))


def get_dataset_detail(
    dataset_id: int,
    *,
    session: Optional[WebSession] = None,
) -> DatasetDetail:
    """Load one dataset's full record by its catalogue handle.

    The handle comes from :func:`resolve_dataset_by_code`; no CLI surface
    accepts or emits it.
    """
    handle = _count(dataset_id)
    if handle <= 0:
        raise UnknownDatasetError("A dataset must be resolved before it can be loaded.")
    data = plaza_request(
        "POST",
        "/api/datasets/findDatasets",
        body={"datasetId": handle},
        session=session,
    )
    return DatasetDetail.from_api_response(data if isinstance(data, dict) else {})


def _exact_code(items: Sequence[DatasetSummary], code: str) -> Optional[DatasetSummary]:
    wanted = code.casefold()
    for item in items:
        if item.code.casefold() == wanted:
            return item
    return None


def resolve_dataset_by_code(
    code: str,
    *,
    session: Optional[WebSession] = None,
) -> DatasetSummary:
    """Find the catalogue row whose ``datasetCode`` is exactly *code*.

    Codes are unique across the catalogue, so a code is an identity and not a
    query. The search endpoint is only a means to find it: it also matches
    project names and descriptions, so the answer is the exact-code row among
    the hits, never simply the first one.
    """
    wanted = _text(code)
    if not wanted:
        raise UnknownDatasetError("A dataset name is required.")

    items, total = list_datasets(
        keyword=wanted,
        page=1,
        page_size=_RESOLVE_PAGE_SIZE,
        session=session,
    )
    match = _exact_code(items, wanted)
    if match is None and total > len(items):
        # A short code can be buried behind prose hits from other datasets'
        # descriptions. The plaza honours any page size, so the rest costs one
        # more request rather than a page walk.
        items, _ = list_datasets(
            keyword=wanted,
            page=1,
            page_size=total,
            session=session,
        )
        match = _exact_code(items, wanted)
    if match is None:
        raise UnknownDatasetError(f"No dataset named {wanted!r} in the data plaza.")
    return match


def list_dataset_tags(*, session: Optional[WebSession] = None) -> list[DatasetTag]:
    """Return the catalogue's whole tag vocabulary."""
    data = plaza_request(
        "GET",
        "/api/datasetTags/getDatasetTagsList",
        params={"pageSize": _TAG_PAGE_SIZE},
        session=session,
    )
    payload = data if isinstance(data, dict) else {}
    rows = payload.get("list")
    return [
        tag
        for row in (rows if isinstance(rows, list) else [])
        if isinstance(row, dict)
        if (tag := DatasetTag.from_api_response(row)).name
    ]


def resolve_tag_ids(
    names: Iterable[str],
    *,
    session: Optional[WebSession] = None,
) -> list[int]:
    """Map tag names to the handles the catalogue filter accepts.

    Tag names are unique across every category, so a name identifies a tag on
    its own and the handle never has to be spoken by the caller.
    """
    wanted = [name for value in names if (name := _text(value))]
    if not wanted:
        return []

    catalogue = list_dataset_tags(session=session)
    by_name = {tag.name.casefold(): tag for tag in catalogue}
    resolved: list[int] = []
    unknown: list[str] = []
    for name in wanted:
        tag = by_name.get(name.casefold())
        if tag is None or tag.tag_id <= 0:
            unknown.append(name)
        elif tag.tag_id not in resolved:
            resolved.append(tag.tag_id)
    if unknown:
        raise UnknownDatasetTagError(unknown, [tag.name for tag in catalogue])
    return resolved
