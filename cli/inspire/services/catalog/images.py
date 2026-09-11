"""Image source loading, identity deduplication and display views."""

from __future__ import annotations

from typing import Any
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context

from inspire.services.utils.raw_ids import scrub_raw_ids
from inspire.platform.web import browser_api as browser_api_module


VISIBILITY_PUBLIC = "VISIBILITY_PUBLIC"


VISIBILITY_PROJECT = "VISIBILITY_PROJECT"


VISIBILITY_PRIVATE = "VISIBILITY_PRIVATE"


def load_image_sources(
    *,
    source_keys: tuple[str, ...],
    session: Any,
    workspace_id: str,
    concurrent: bool = True,
) -> tuple[list[browser_api_module.CustomImageInfo], list[str]]:
    """Read independent image catalogues concurrently in stable tab order.

    Each source is one complete ``ListImages`` request. Starting the requests
    together changes neither their wire contracts nor partial-failure
    semantics; results and failures are still folded in the caller-visible
    official/public/project/private order.
    """
    if not source_keys:
        return [], []

    def _fetch(source: str) -> list[browser_api_module.CustomImageInfo]:
        return browser_api_module.list_images_by_source(
            source=source,
            session=session,
            workspace_id=workspace_id,
        )

    if not concurrent:
        images = []
        failed = []
        for source in source_keys:
            try:
                images.extend(_fetch(source))
            except Exception:
                failed.append(source)
        return images, failed

    with ThreadPoolExecutor(max_workers=len(source_keys)) as executor:
        futures = {
            source: executor.submit(copy_context().run, _fetch, source) for source in source_keys
        }
        images = []
        failed = []
        for source in source_keys:
            try:
                images.extend(futures[source].result())
            except Exception:
                failed.append(source)
    return images, failed


def image_label(img: browser_api_module.CustomImageInfo) -> str:
    name = str(img.name or "").strip()
    version = str(img.version or "").strip()
    if version and ":" not in name:
        return f"{name}:{version}"
    return name


def image_visibility(img: browser_api_module.CustomImageInfo) -> str:
    """Return who can see the image: official / public / private.

    `visibility` is the field ``set-visibility`` writes and the field the
    ``--source public`` / ``--source private`` filters select on; `source` is
    only the registry namespace and reads SOURCE_PUBLIC for personal images
    too, so it cannot answer this. Official images carry no `visibility`, so
    they are still recognised by `source`.
    """
    source = str(img.source or "").strip()
    if source == "SOURCE_OFFICIAL":
        return "official"
    return {
        VISIBILITY_PUBLIC: "public",
        VISIBILITY_PROJECT: "project",
        VISIBILITY_PRIVATE: "private",
    }.get(str(img.visibility or "").strip(), "")


def image_summary(img: browser_api_module.CustomImageInfo) -> dict[str, str]:
    """Return the compact, name-only image representation exposed by the CLI."""
    return {
        "name": scrub_raw_ids(image_label(img)),
        "status": scrub_raw_ids(img.status),
        "framework": scrub_raw_ids(img.framework),
        "visibility": image_visibility(img),
    }


def dedupe_images_by_id(
    images: list[browser_api_module.CustomImageInfo],
) -> list[browser_api_module.CustomImageInfo]:
    """Deduplicate internal image records while preserving platform order."""
    deduped: list[browser_api_module.CustomImageInfo] = []
    seen_ids: set[str] = set()
    for image in images:
        image_id = str(image.image_id or "").strip()
        if image_id:
            if image_id in seen_ids:
                continue
            seen_ids.add(image_id)
        deduped.append(image)
    return deduped
