"""Project public-path mount contract shared by all workload creators."""

from enum import IntEnum
from typing import Any
import warnings

from inspire.platform.errors import ValidationError


class PublicPathMountMode(IntEnum):
    READ_WRITE = 0
    READ_ONLY = 1
    NOT_MOUNTED = 2


def resolve_public_path_mount_mode(
    mode: int | None = None,
    readonly: bool | None = None,
) -> PublicPathMountMode | None:
    if mode is not None and (
        isinstance(mode, bool) or not isinstance(mode, int) or mode not in (0, 1, 2)
    ):
        raise ValidationError("public_path_mount_mode must be 0, 1, or 2.")
    if readonly is not None:
        if not isinstance(readonly, bool):
            raise ValidationError("public_path_readonly must be a boolean.")
        warnings.warn(
            "public_path_readonly is deprecated; use public_path_mount_mode "
            "(0=read-write, 1=read-only, 2=not mounted).",
            DeprecationWarning,
            stacklevel=2,
        )
        legacy = int(readonly)
        if mode is not None and mode != legacy:
            raise ValidationError("public_path_mount_mode conflicts with public_path_readonly.")
        mode = legacy
    return PublicPathMountMode(mode) if mode is not None else None


def public_path_mount_fields(
    mode: int | None = None,
    readonly: bool | None = None,
) -> dict[str, int]:
    resolved = resolve_public_path_mount_mode(mode, readonly)
    return {} if resolved is None else {"publicpath_mount_mode": int(resolved)}


def public_path_mount_mode_from_detail(item: dict[str, Any]) -> int:
    mode = item.get("publicpath_mount_mode")
    return int(bool(item.get("is_publicpath_readonly"))) if mode is None else int(mode)
