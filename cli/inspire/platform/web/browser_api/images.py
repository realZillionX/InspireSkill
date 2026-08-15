"""Browser (web-session) image management APIs (list, detail, create, delete).

Saving a notebook as an image is **not** here: all three Actions behind that
flow live on the ``notebook`` route, so they sit in :mod:`.notebooks` next to
the rest of the notebook lifecycle.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Optional

from inspire.platform.web.browser_api.notebooks import (
    _get_session_and_workspace_id,
    _image_v2,
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
    # Who can see the image, which is not what `source` says: `source` is the
    # registry namespace it was built into and never changes, while
    # `visibility` is the field `UpdateImage` flips. A personal image saved
    # from a notebook reads SOURCE_PUBLIC + VISIBILITY_PRIVATE.
    visibility: str = ""  # VISIBILITY_PRIVATE / VISIBILITY_PUBLIC


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
        visibility=item.get("visibility", ""),
    )


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def list_images_by_source(
    source: str = "official",
    session: Optional[WebSession] = None,
    *,
    workspace_id: Optional[str] = None,
) -> list[CustomImageInfo]:
    """List Docker images for any source, returning full metadata.

    Unlike :func:`~inspire.platform.web.browser_api.notebooks.list_images`
    (which returns the limited ``ImageInfo``), this function always returns
    ``CustomImageInfo`` objects with ``source``, ``status``, ``description``,
    and ``created_at`` populated from the raw API response.

    Args:
        source: One of ``"official"`` / ``"public"`` / ``"project"`` /
            ``"private"``, matching the four tabs the web image picker shows
            (官方镜像 / 公开可见镜像 / 项目可见镜像 / 个人可见镜像). Only
            ``"official"`` is a real source; the other three are
            ``visibility`` values applied across both source lists.
        session: Existing web session.
        workspace_id: Which workspace's image registry to read. Images are
            stored per workspace and every request carries
            ``registry_hint: {workspace_id}``, so a caller that means one
            workspace must say so — falling back to the session's active
            workspace silently reads a different registry. Defaults to the
            session's workspace when omitted.
    """
    # Three of the four categories are visibility filters over the same two
    # source lists; only 官方镜像 selects on `source`.
    visibility_map = {
        "public": "VISIBILITY_PUBLIC",
        "project": "VISIBILITY_PROJECT",
        "private": "VISIBILITY_PRIVATE",
    }
    normalized = source.lower()
    visibility = visibility_map.get(normalized)

    session, workspace_id = _get_session_and_workspace_id(
        workspace_id=workspace_id, session=session
    )

    if visibility is not None:
        body: dict[str, Any] = {
            "page": 0,
            "page_size": -1,
            "filter": {
                "source_list": ["SOURCE_PRIVATE", "SOURCE_PUBLIC"],
                "visibility": visibility,
                "registry_hint": {"workspace_id": workspace_id},
            },
        }
    else:
        body = {
            "page": 0,
            "page_size": -1,
            "filter": {
                "source": "SOURCE_OFFICIAL" if normalized == "official" else source,
                "source_list": [],
                "registry_hint": {"workspace_id": workspace_id},
            },
        }

    data = _image_v2(session, "ListImages", body)
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

    # `GetImageById` spells the target `ImageId`; `DeleteImage` spells the same
    # thing `image_id`. The two are not interchangeable.
    data = _image_v2(session, "GetImageById", {"ImageId": image_id})
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

    return _image_v2(session, "CreateImage", body)


def update_image(
    image_id: str,
    *,
    visibility: Optional[str] = None,
    description: Optional[str] = None,
    session: Optional[WebSession] = None,
) -> dict[str, Any]:
    """Update a custom image's metadata via ``/image/update``.

    Only fields that are not ``None`` are sent. Use this to flip an image's
    visibility (``VISIBILITY_PRIVATE`` ↔ ``VISIBILITY_PUBLIC``) after a save,
    or to edit the description.

    The platform's body schema uses ``id`` (not ``image_id``) for the target.
    v2 kept the quirk but degraded the error: ``{"image_id": ...}`` no longer
    says "unknown field", it says ``InternalError: 数据库错误`` — the field is
    simply ignored, so the update runs against an empty id. Confirmed
    2026-08-08 by a create → update → delete round trip.

    Args:
        image_id: The image ID to update. (Wired into the body as ``id``.)
        visibility: ``"VISIBILITY_PRIVATE"`` or ``"VISIBILITY_PUBLIC"``.
        description: New description text.
        session: Existing web session.

    Returns:
        API response data.
    """
    session, _ = _get_session_and_workspace_id(workspace_id=None, session=session)

    body: dict[str, Any] = {"id": image_id}
    if visibility is not None:
        body["visibility"] = visibility
    if description is not None:
        body["description"] = description

    return _image_v2(session, "UpdateImage", body)


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

    # `image_id` here, but `ImageId` on `GetImageById` — the image service is
    # inconsistent about the same identifier.
    return _image_v2(session, "DeleteImage", {"image_id": image_id})


_IMAGE_READY_STATES = {"READY", "SUCCESS", "SUCCEED", "SUCCEEDED"}
# Platform-observed terminal-failure states. Missing any of these makes
# ``wait_for_image_ready`` hang to the timeout instead of failing fast.
_IMAGE_FAILED_STATES = {
    "FAILED",
    "FAILURE",
    "ERROR",
    "CANCELLED",
    "CANCELED",
    "TIMEOUT",
    "ABORTED",
    "INTERRUPTED",
}


def wait_for_image_ready(
    image_id: str,
    session: Optional[WebSession] = None,
    timeout: int = 600,
    poll_interval: int = 5,
) -> CustomImageInfo:
    """Wait for a custom image to reach a terminal success state.

    The platform uses ``SUCCESS`` for ``notebook save-image``-produced images
    (2026-04 observation — not ``READY`` like ``create_image`` does for
    externally-registered images). Both are accepted here, as are any
    ``SUCCEEDED`` variants, so the wait works for both flows.

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

        if status in _IMAGE_READY_STATES:
            return image

        if status in _IMAGE_FAILED_STATES:
            raise ValueError(f"Image '{image_id}' build failed (status: {status})")

        if time.time() - start >= timeout:
            raise TimeoutError(
                f"Image '{image_id}' did not reach a terminal success state "
                f"within {timeout}s (last status: {last_status or 'unknown'})"
            )

        time.sleep(poll_interval)


__all__ = [
    "CustomImageInfo",
    "create_image",
    "delete_image",
    "get_image_detail",
    "list_images_by_source",
    "update_image",
    "wait_for_image_ready",
]
