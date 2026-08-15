"""Client façade for 数据广场 (上海创智学院数据广场) on ``aip.sii.edu.cn``.

The dataset catalogue lives in its own application, off the qz console and
behind its own session cookie, so it gets its own package rather than another
module under :mod:`inspire.platform.web.browser_api` — everything there is the
qz host, one CAS session and the ``/api/v1`` … ``/api/v2`` envelopes. What the
two share is the platform web session, which is why this sits beside them
under :mod:`inspire.platform.web`.

Mounting is the other half and belongs to qz:
:mod:`inspire.platform.web.browser_api.datasets` resolves a
``(dataset code, version code)`` pair to the path the platform will mount.
"""

from __future__ import annotations

from .applications import (
    APPLICATION_STATES,
    DatasetApplication,
    UnknownDatasetApplicationError,
    find_dataset_applications,
    get_dataset_application,
    list_dataset_applications,
    list_dataset_approvals,
)
from .core import (
    CAS_BASE_URL,
    PLAZA_BASE_URL,
    PlazaError,
    plaza_request,
    reset_plaza_client,
)
from .datasets import (
    DEFAULT_PAGE_SIZE,
    TAG_CATEGORIES,
    DatasetDetail,
    DatasetSummary,
    DatasetTag,
    DatasetVersion,
    UnknownDatasetError,
    UnknownDatasetTagError,
    get_dataset_detail,
    list_dataset_tags,
    list_datasets,
    resolve_dataset_by_code,
    resolve_tag_ids,
)

__all__ = [
    # Session / transport
    "CAS_BASE_URL",
    "PLAZA_BASE_URL",
    "PlazaError",
    "plaza_request",
    "reset_plaza_client",
    # Dataset catalogue
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
    # Access applications (read-only; applying and approving are web-only)
    "APPLICATION_STATES",
    "DatasetApplication",
    "UnknownDatasetApplicationError",
    "find_dataset_applications",
    "get_dataset_application",
    "list_dataset_applications",
    "list_dataset_approvals",
]
