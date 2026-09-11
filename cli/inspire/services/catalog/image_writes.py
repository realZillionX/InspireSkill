from __future__ import annotations

from typing import Optional
from inspire.services.catalog.images import VISIBILITY_PUBLIC, VISIBILITY_PROJECT, VISIBILITY_PRIVATE

_VISIBILITY_BY_NAME = {
    "public": VISIBILITY_PUBLIC,
    "project": VISIBILITY_PROJECT,
    "private": VISIBILITY_PRIVATE,
}
IMAGE_ADD_METHOD_LOCAL_PUSH = 2


def parse_visibility_value(visibility: Optional[str]) -> Optional[str]:
    if visibility is None:
        return None
    return _VISIBILITY_BY_NAME.get(visibility.lower(), VISIBILITY_PRIVATE)
