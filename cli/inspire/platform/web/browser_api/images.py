"""Browser (web-session) image management APIs (list, detail, create, save, delete)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

from inspire.platform.web.browser_api.notebooks import (
    _get_session_and_workspace_id,
    _request_notebooks_data,
)
from inspire.platform.web.session import WebSession, get_web_session


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclass
class CustomImageInfo:
    """Custom Docker image information."""

    image_id: str
    url: str
    name: str
    framework: str
    version: str
    source: str  # SOURCE_PRIVATE / SOURCE_PUBLIC / SOURCE_OFFICIAL
    status: str  # READY / BUILDING / FAILED
    description: str
    created_at: str


def _image_from_api(item: dict[str, Any]) -> CustomImageInfo:
    """Convert an API image dict to a CustomImageInfo."""
    url = item.get("address", "")
    name = item.get("name", url.split("/")[-1] if url else "")
    return CustomImageInfo(
        image_id=item.get("image_id", ""),
        url=url,
        name=name,
        framework=item.get("framework", ""),
        version=item.get("version", ""),
        source=item.get("source", ""),
        status=item.get("status", ""),
        description=item.get("description", ""),
        created_at=item.get("created_at", ""),
    )


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def list_images_by_source(
    source: str = "official",
    session: Optional[WebSession] = None,
) -> list[CustomImageInfo]:
    """List Docker images for any source, returning full metadata.

    Unlike :func:`~inspire.platform.web.browser_api.notebooks.list_images`
    (which returns the limited ``ImageInfo``), this function always returns
    ``CustomImageInfo`` objects with ``source``, ``status``, ``description``,
    and ``created_at`` populated from the raw API response.

    Args:
        source: One of ``"official"`` / ``"public"`` / ``"private"``, matching the
            three categories shown in the web UI (官方镜像 / 公开镜像 / 个人可见镜像).
            ``"private"`` applies ``visibility=VISIBILITY_PRIVATE`` across both
            private and public source lists.
        session: Existing web session.
    """
    source_map = {
        "official": "SOURCE_OFFICIAL",
        "public": "SOURCE_PUBLIC",
        # UI "个人可见镜像": private visibility across both private/public sources.
        "private": "SOURCE_PERSONAL_VISIBLE",
    }
    api_source = source_map.get(source.lower(), source)

    session, workspace_id = _get_session_and_workspace_id(workspace_id=None, session=session)

    if api_source == "SOURCE_PUBLIC":
        body: dict[str, Any] = {
            "page": 0,
            "page_size": -1,
            "filter": {
                "source_list": ["SOURCE_PRIVATE", "SOURCE_PUBLIC"],
                "visibility": "VISIBILITY_PUBLIC",
                "registry_hint": {"workspace_id": workspace_id},
            },
        }
    elif api_source == "SOURCE_PERSONAL_VISIBLE":
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
                "source": api_source,
                "source_list": [],
                "registry_hint": {"workspace_id": workspace_id},
            },
        }

    data = _request_notebooks_data(
        session,
        "POST",
        "/image/list",
        body=body,
        timeout=30,
        default_data={},
    )
    items = data.get("images", [])
    return [_image_from_api(item) for item in items]


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def get_image_detail(
    image_id: str,
    session: Optional[WebSession] = None,
) -> CustomImageInfo:
    """Get detailed image information.

    Args:
        image_id: The image ID to look up.
        session: Existing web session.
    """
    session, _ = _get_session_and_workspace_id(workspace_id=None, session=session)

    data = _request_notebooks_data(
        session,
        "GET",
        f"/image/{image_id}",
        timeout=30,
        default_data={},
    )
    return _image_from_api(data)


def create_image(
    name: str,
    version: str,
    workspace_id: Optional[str] = None,
    description: str = "",
    visibility: str = "VISIBILITY_PRIVATE",
    add_method: int = 0,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Register a custom Docker image.

    The platform supports two add methods:
      - 0: LOCAL_PUSH (user pushes via ``docker push``)
      - 2: IMAGE_ADDRESS (register an existing image address)

    Args:
        name: Image name (lowercase, digits, dashes, dots, underscores).
        version: Image version tag (max 64 chars).
        workspace_id: Workspace ID (determines registry).
        description: Optional description.
        visibility: ``"VISIBILITY_PRIVATE"`` or ``"VISIBILITY_PUBLIC"``.
        add_method: 0 for local push, 2 for image address.
        session: Existing web session.

    Returns:
        API response data (contains ``image`` dict with ``image_id``).
    """
    session, workspace_id = _get_session_and_workspace_id(
        workspace_id=workspace_id, session=session
    )

    body: dict[str, Any] = {
        "name": name,
        "version": version,
        "registry_hint": {"workspace_id": workspace_id},
        "visibility": visibility,
        "add_method": add_method,
        "description": description,
    }

    return _request_notebooks_data(
        session,
        "POST",
        "/image/create",
        body=body,
        timeout=30,
        default_data={},
    )


def save_notebook_as_image(
    notebook_id: str,
    name: str,
    version: str = "v1",
    description: str = "",
    visibility: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Save a running notebook's state as a custom Docker image.

    Uses the ``/mirror/save`` endpoint.

    Args:
        notebook_id: ID of the running notebook.
        name: Name for the saved image.
        version: Version tag (default ``"v1"``).
        description: Optional description.
        visibility: ``"VISIBILITY_PRIVATE"`` / ``"VISIBILITY_PUBLIC"``. When
            ``None``, the field is omitted and the platform picks the default
            (currently private). Passing it through lets callers request a
            public image in a single request; if the save endpoint ignores the
            field, follow up with :func:`update_image` to force visibility.
        session: Existing web session.

    Returns:
        API response data.
    """
    session, _ = _get_session_and_workspace_id(workspace_id=None, session=session)

    body: dict[str, Any] = {
        "notebook_id": notebook_id,
        "name": name,
        "version": version,
        "description": description,
    }
    if visibility is not None:
        body["visibility"] = visibility

    return _request_notebooks_data(
        session,
        "POST",
        "/mirror/save",
        body=body,
        timeout=60,
        default_data={},
    )


def update_image(
    image_id: str,
    *,
    visibility: Optional[str] = None,
    description: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Update a custom image's metadata via ``/image/update``.

    Only fields that are not ``None`` are sent. Use this to flip an image's
    visibility (``VISIBILITY_PRIVATE`` ↔ ``VISIBILITY_PUBLIC``) after the fact
    when ``/mirror/save`` didn't honor the request, or to edit description.

    Args:
        image_id: The image ID to update.
        visibility: ``"VISIBILITY_PRIVATE"`` or ``"VISIBILITY_PUBLIC"``.
        description: New description text.
        session: Existing web session.

    Returns:
        API response data.
    """
    session, _ = _get_session_and_workspace_id(workspace_id=None, session=session)

    body: dict[str, Any] = {"image_id": image_id}
    if visibility is not None:
        body["visibility"] = visibility
    if description is not None:
        body["description"] = description

    return _request_notebooks_data(
        session,
        "POST",
        "/image/update",
        body=body,
        timeout=30,
        default_data={},
    )


def delete_image(
    image_id: str,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Delete a custom Docker image.

    Args:
        image_id: ID of the image to delete.
        session: Existing web session.

    Returns:
        API response data.
    """
    session, _ = _get_session_and_workspace_id(workspace_id=None, session=session)

    return _request_notebooks_data(
        session,
        "DELETE",
        f"/image/{image_id}",
        timeout=30,
        default_data={},
    )


def wait_for_image_ready(
    image_id: str,
    session: Optional[WebSession] = None,
    timeout: int = 300,
    poll_interval: int = 5,
) -> CustomImageInfo:
    """Wait for a custom image to reach READY status.

    Args:
        image_id: The image ID to poll.
        session: Existing web session.
        timeout: Maximum seconds to wait.
        poll_interval: Seconds between polls.

    Raises:
        TimeoutError: If the image does not become ready in time.
        ValueError: If the image build fails.
    """
    if session is None:
        session = get_web_session()

    start = time.time()
    last_status = None

    while True:
        image = get_image_detail(image_id=image_id, session=session)
        status = (image.status or "").upper()
        if status:
            last_status = status

        if status == "READY":
            return image

        if status == "FAILED":
            raise ValueError(f"Image '{image_id}' build failed (status: {status})")

        if time.time() - start >= timeout:
            raise TimeoutError(
                f"Image '{image_id}' did not reach READY within {timeout}s "
                f"(last status: {last_status or 'unknown'})"
            )

        time.sleep(poll_interval)


__all__ = [
    "CustomImageInfo",
    "create_image",
    "delete_image",
    "get_image_detail",
    "list_images_by_source",
    "save_notebook_as_image",
    "update_image",
    "wait_for_image_ready",
]
