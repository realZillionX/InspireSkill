"""Safe serialization of platform catalog rows, without frontend dependencies."""

from dataclasses import fields
from typing import Any
from inspire.platform.web.browser_api.images import CustomImageInfo
from inspire.platform.web.browser_api.projects import ProjectInfo

_TYPES = {cls.__name__: cls for cls in (ProjectInfo, CustomImageInfo)}


def encode_catalog(value: Any) -> Any:
    # Tagged containers preserve tuple fields and integer dictionary keys.
    # Only these two known directory models can be reconstructed; no pickle
    # or imports selected by disk contents.
    if type(value) in (str, int, float, bool, type(None)):
        return value
    if type(value) in (list, tuple):
        return [type(value).__name__, [encode_catalog(item) for item in value]]
    if type(value) is dict:
        return ["dict", [[encode_catalog(k), encode_catalog(v)] for k, v in value.items()]]
    if type(value) in _TYPES.values():
        return [
            type(value).__name__,
            {f.name: encode_catalog(getattr(value, f.name)) for f in fields(value)},
        ]
    raise TypeError("Unsupported catalog value")


def decode_catalog(value: Any) -> Any:
    if type(value) in (str, int, float, bool, type(None)):
        return value
    tag, data = value
    if tag == "list":
        return [decode_catalog(item) for item in data]
    if tag == "tuple":
        return tuple(decode_catalog(item) for item in data)
    if tag == "dict":
        return {decode_catalog(k): decode_catalog(v) for k, v in data}
    if tag in _TYPES:
        return _TYPES[tag](**{k: decode_catalog(v) for k, v in data.items()})
    raise ValueError("Unknown catalog value")


def validate_catalog(kind: str, value: Any) -> None:
    if kind in {"workspaces", "projects", "compute_groups", "images", "prices"}:
        if not isinstance(value, list):
            raise ValueError("Invalid catalog rows")
        for row in value:
            if kind == "projects":
                valid = isinstance(row, ProjectInfo) and bool(row.project_id)
            elif kind == "images":
                valid = isinstance(row, CustomImageInfo) and bool(row.image_id)
            else:
                identity = {
                    "workspaces": ("id",),
                    "compute_groups": ("id", "logic_compute_group_id"),
                    "prices": ("quota_id", "spec_id"),
                }[kind]
                valid = isinstance(row, dict) and any(row.get(field) for field in identity)
            if not valid:
                raise ValueError("Catalog omitted a resource identity")
    elif kind == "fair_scheduling":
        if type(value) is not bool:
            raise ValueError("Invalid scheduling flag")
    elif not isinstance(value, dict) or (
        kind == "current_user" and not (value.get("id") or value.get("user_id"))
    ):
        raise ValueError("Invalid catalog mapping")

