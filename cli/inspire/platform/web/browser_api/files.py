"""Browser (web-session) APIs for the web UI file browser."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from inspire.platform.web.browser_api.core import _browser_api_path, _get_base_url, _request_json
from inspire.platform.web.session import WebSession, get_web_session

__all__ = [
    "FileDirectoryInfo",
    "SystemStorageInfo",
    "list_file_directories",
    "list_project_file_directories",
    "list_system_storage_types",
]


@dataclass
class SystemStorageInfo:
    """Storage entry exposed by the file browser."""

    name: str
    cluster_id: str = ""

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> "SystemStorageInfo":
        return cls(
            name=str(data.get("name") or "").strip(),
            cluster_id=str(data.get("cluster_id") or "").strip(),
        )


@dataclass
class FileDirectoryInfo:
    """Top-level directory entry returned by the file browser."""

    directory: str

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> "FileDirectoryInfo":
        return cls(
            directory=str(data.get("directory") or "").strip(),
        )


def _files_referer(workspace_id: str | None = None) -> str:
    suffix = f"?spaceId={workspace_id}" if workspace_id else ""
    return f"{_get_base_url()}/jobs/files{suffix}"


def list_system_storage_types(
    *,
    workspace_id: str,
    session: Optional[WebSession] = None,
) -> list[SystemStorageInfo]:
    """List storage tiers shown by the web UI file browser."""
    workspace_id = str(workspace_id or "").strip()
    if not workspace_id:
        raise ValueError("Workspace selection is required.")
    if session is None:
        session = get_web_session()

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/file/get_system_storage_type_list"),
        referer=_files_referer(workspace_id),
        body={"filter": {"workspace_id": workspace_id}},
        timeout=30,
    )
    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    items = data.get("data", {}).get("system_storages", [])
    if not isinstance(items, list):
        return []
    return [
        storage
        for item in items
        if isinstance(item, dict)
        if (storage := SystemStorageInfo.from_api_response(item)).name
    ]


def list_file_directories(
    *,
    workspace_id: str,
    storage_type: str,
    name: str,
    cluster_id: str | None = None,
    session: Optional[WebSession] = None,
) -> list[FileDirectoryInfo]:
    """List top-level directories of a file-browser category.

    ``name`` is the category key used by the frontend, e.g. ``project``,
    ``global_public`` or ``global_user``.
    """
    workspace_id = str(workspace_id or "").strip()
    storage_type = str(storage_type or "").strip()
    name = str(name or "").strip()
    cluster_id = str(cluster_id or "").strip()
    if not workspace_id or not storage_type or not name:
        raise ValueError("Workspace, storage type, and file name are required.")
    if session is None:
        session = get_web_session()

    filter_body: dict[str, Any] = {
        "workspace_id": workspace_id,
        "system_storage_type": storage_type,
        "name": name,
    }
    if cluster_id:
        filter_body["cluster_id"] = cluster_id

    data = _request_json(
        session,
        "POST",
        _browser_api_path("/file/dir/list"),
        referer=_files_referer(workspace_id),
        body={"filter": filter_body},
        timeout=30,
    )
    if data.get("code") != 0:
        raise ValueError(f"API error: {data.get('message')}")

    items = data.get("data", {}).get("files", [])
    if not isinstance(items, list):
        return []
    return [
        entry
        for item in items
        if isinstance(item, dict)
        if (entry := FileDirectoryInfo.from_api_response(item)).directory
    ]


def list_project_file_directories(
    *,
    workspace_id: str,
    session: Optional[WebSession] = None,
    storage_names: Optional[set[str]] = None,
) -> list[FileDirectoryInfo]:
    """List project storage directories across non-share storage tiers."""
    workspace_id = str(workspace_id or "").strip()
    if not workspace_id:
        raise ValueError("Workspace selection is required.")
    if session is None:
        session = get_web_session()

    requested = {name.strip() for name in (storage_names or set()) if name.strip()}
    storages = list_system_storage_types(workspace_id=workspace_id, session=session)
    entries: list[FileDirectoryInfo] = []
    for storage in storages:
        storage_name = storage.name
        if not storage_name or storage_name.startswith("share-"):
            continue
        if requested and storage_name not in requested:
            continue
        try:
            entries.extend(
                list_file_directories(
                    workspace_id=workspace_id,
                    storage_type=storage_name,
                    cluster_id=storage.cluster_id,
                    name="project",
                    session=session,
                )
            )
        except Exception:
            continue
    return entries
