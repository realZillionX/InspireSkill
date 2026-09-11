"""Resolve a visible image name to the registry URL the platform expects.

`--image` is documented as accepting a name or a Docker URL, but the `train`
and `hpc` create payloads only match on the **registry URL**; a display name is
rejected with ``InvalidParameter: 无法找到对应镜像``. Names therefore have to be
looked up in the image catalogues before the payload is built.

Only the URL matters. Verified against a live HPC create: a correct URL paired
with a deliberately wrong ``image_type`` (``SOURCE_PRIVATE`` for an image whose
real source is ``SOURCE_PUBLIC``) is accepted, so ``image_type`` is not worth a
catalogue round-trip and the previous constant is kept.

Ray does not use this: its create body wants the internal mirror handle
(``mirror_id``), which ``ray_commands._resolve_image_id`` produces instead.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from inspire.config import ConfigError

logger = logging.getLogger(__name__)

_CATALOGUE_ORDER = ("official", "public", "private")

# One command-local catalogue snapshot. The bool says the source request
# failed, which must stay distinct from a successful empty catalogue.
ImageCatalogCache = dict[tuple[str, str], tuple[list[Any], bool]]

# Sent for every image; the platform ignores it as long as `image` is a URL.
IMAGE_TYPE = "SOURCE_PRIVATE"


def _looks_like_registry_url(value: str) -> bool:
    """Whether the value is already pullable rather than a visible name.

    A registry reference carries a path separator (``host/project/image:tag``);
    visible names never do.
    """
    return "/" in value


def _labels_for(image: Any) -> set[str]:
    name = str(getattr(image, "name", "") or "").strip()
    version = str(getattr(image, "version", "") or "").strip()
    labels = {name, str(getattr(image, "url", "") or "").strip()}
    if name and version:
        labels.add(f"{name}:{version}")
    return {label for label in labels if label}


def resolve_image_url(
    raw: Optional[str],
    *,
    session: Any,
    workspace_id: Optional[str] = None,
    debug: bool = False,
    catalog_cache: ImageCatalogCache | None = None,
) -> str:
    """Return the registry URL for a visible image name.

    A value that already looks like a registry URL is returned untouched — it
    may legitimately be absent from the catalogues (freshly pushed, or not
    visible in the listing) and must still reach the platform. Only names cost
    a catalogue lookup.

    Raises :class:`ConfigError` when a name matches nothing, so the caller can
    surface a validation error instead of the platform's opaque rejection.
    """
    value = str(raw or "").strip()
    if not value:
        raise ConfigError("Image is empty.")
    if _looks_like_registry_url(value):
        return value

    from inspire.platform.web.browser_api.images import list_images_by_source

    target = value.lower()
    for source in _CATALOGUE_ORDER:
        cache_key = (str(workspace_id or ""), source)
        if catalog_cache is not None and cache_key in catalog_cache:
            images, failed = catalog_cache[cache_key]
        else:
            try:
                images = list_images_by_source(
                    source=source, session=session, workspace_id=workspace_id
                )
                failed = False
            except Exception:  # noqa: BLE001 - one catalogue failing is not fatal
                if debug:
                    logger.debug("Image lookup via %s failed", source, exc_info=True)
                images, failed = [], True
            if catalog_cache is not None:
                catalog_cache[cache_key] = (images, failed)
        if failed:
            continue
        for image in images:
            if target in {label.lower() for label in _labels_for(image)}:
                url = str(getattr(image, "url", "") or "").strip()
                if url:
                    return url

    raise ConfigError(
        f"Image {value!r} not found in official/public/private catalogues. "
        "Pass a visible image name or Docker URL from `inspire image list`."
    )


__all__ = ["IMAGE_TYPE", "ImageCatalogCache", "resolve_image_url"]
