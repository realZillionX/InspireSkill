from __future__ import annotations

from typing import Optional
from inspire.services.catalog.images import VISIBILITY_PUBLIC, VISIBILITY_PROJECT, VISIBILITY_PRIVATE

_VISIBILITY_BY_NAME = {
    "public": VISIBILITY_PUBLIC,
    "project": VISIBILITY_PROJECT,
    "private": VISIBILITY_PRIVATE,
}
IMAGE_ADD_METHOD_LOCAL_PUSH = "DIRECT_PUSH"


def image_name_version(image) -> tuple[str, str]:
    """Use the catalog's canonical name and tag for SyncImage."""
    name, version = image.name, image.version
    if version and name.endswith(":" + version):
        name = name[: -(len(version) + 1)]
    if not name or not version:
        raise ValueError("Image catalog entry has no name or version.")
    return name, version


def parse_visibility_value(visibility: Optional[str]) -> Optional[str]:
    if visibility is None:
        return None
    return _VISIBILITY_BY_NAME.get(visibility.lower(), VISIBILITY_PRIVATE)
