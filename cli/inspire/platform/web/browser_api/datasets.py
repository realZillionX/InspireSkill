"""Browser (web-session) API for official dataset mounts.

The `dataset` route is absent from discovery — like `file` — but it is live and
carries exactly one Action: ``ValidateDataset``. It resolves a
``(dataset_id, version_id)`` pair to the storage path the platform will mount,
which is what the console's 校验数据 button does before it submits a create form.

The identifiers are **not** the numeric primary keys shown by the data plaza.
``dataset_id`` is the dataset's code (``pixabay-81k``) and ``version_id`` is the
version's code (``v0``); passing the numeric ids returns 数据集不存在. Browsing
the catalogue is not part of this service at all — the plaza lives on another
host, see :mod:`inspire.platform.web.plaza`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Optional

from inspire.platform.web.browser_api.core import _get_base_url, _request_json, _v2_result
from inspire.platform.web.session import WebSession, get_web_session

__all__ = [
    "DatasetMount",
    "DatasetValidation",
    "CONTAINER_DATASET_ROOT",
    "container_mount_path",
    "validate_dataset_mounts",
]

# The path the platform mounts a validated dataset at, as stated by the create
# form: /inspire/dataset/<dataset code>/<version code>.
CONTAINER_DATASET_ROOT = "/inspire/dataset"


@dataclass(frozen=True)
class DatasetMount:
    """One requested official-dataset mount."""

    dataset: str
    version: str

    def as_payload(self, path: str = "") -> dict[str, str]:
        return {"dataset_id": self.dataset, "version_id": self.version, "path": path}


@dataclass
class DatasetValidation:
    """Platform verdict for one requested mount."""

    dataset: str
    version: str
    ok: bool
    path: str = ""
    error: str = ""

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> "DatasetValidation":
        return cls(
            dataset=str(data.get("dataset_id") or "").strip(),
            version=str(data.get("version_id") or "").strip(),
            ok=bool(data.get("success")),
            path=str(data.get("path") or "").strip(),
            error=str(data.get("error_message") or "").strip(),
        )

    @property
    def mount_path(self) -> str:
        return container_mount_path(self.dataset, self.version)


def container_mount_path(dataset: str, version: str) -> str:
    """Where a mounted dataset shows up inside the container."""
    return f"{CONTAINER_DATASET_ROOT}/{dataset}/{version}"


def _dataset_referer(workspace_id: str | None = None) -> str:
    suffix = f"?spaceId={workspace_id}" if workspace_id else ""
    return f"{_get_base_url()}/jobs/interactiveModeling{suffix}"


def validate_dataset_mounts(
    mounts: Iterable[DatasetMount],
    *,
    workspace_id: str,
    session: Optional[WebSession] = None,
) -> list[DatasetValidation]:
    """Resolve each requested mount, preserving the requested order.

    A rejected entry comes back with ``ok=False`` and the platform's own reason
    (数据集不存在 / 版本不存在 / 无访问权限); the caller decides whether that is
    fatal. The whole batch goes in one request, the way the console sends it.
    """
    requested = list(mounts)
    if not requested:
        return []
    workspace_id = str(workspace_id or "").strip()
    if not workspace_id:
        raise ValueError("Workspace selection is required.")
    if session is None:
        session = get_web_session()

    data = _request_json(
        session,
        "POST",
        "/api/v2/dataset?Action=ValidateDataset",
        referer=_dataset_referer(workspace_id),
        body={
            "datasets": [{"dataset_id": m.dataset, "version_id": m.version} for m in requested],
            "workspace_id": workspace_id,
        },
    )
    result = _v2_result(data)

    rows = result.get("datasets_result")
    if not isinstance(rows, list):
        rows = []
    verdicts = {
        (v.dataset, v.version): v
        for item in rows
        if isinstance(item, dict)
        if (v := DatasetValidation.from_api_response(item)).dataset
    }

    # The platform echoes what it was asked about, but it is not contractually
    # ordered; key the answer back onto the request instead of zipping.
    return [
        verdicts.get(
            (m.dataset, m.version),
            DatasetValidation(
                dataset=m.dataset,
                version=m.version,
                ok=False,
                error="platform returned no verdict for this dataset",
            ),
        )
        for m in requested
    ]
