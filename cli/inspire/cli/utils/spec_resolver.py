"""Live spec_id resolvers — replaces the hardcoded fallback list.

Both HPC and train-job submissions target a ``spec_id`` (``predef_quota_id``)
that is a platform-generated UUID. The (compute_group, cpu, memory,
gpu_type, gpu_count) tuple identifies the same spec uniquely, so the CLI
can ask the platform rather than ship static tables that rot when the
platform rotates IDs.

The HPC-side helper lives in ``cli/commands/hpc/hpc_commands.py``
(``_resolve_hpc_spec_id``); the train-side one is here because
``job_submit.py`` is the only caller and both need to stay small.
"""

from __future__ import annotations

from typing import Optional

from inspire.config import ConfigError
from inspire.platform.web import browser_api as browser_api_module


def _extract_memory_gib(price: dict) -> int:
    value = (
        price.get("memory_size_gib")
        or price.get("memory_size")
        or price.get("memory_size_gb")
        or 0
    )
    try:
        return int(value)
    except Exception:
        return 0


def _extract_gpu_type(price: dict) -> str:
    gpu_info = price.get("gpu_info") if isinstance(price.get("gpu_info"), dict) else {}
    return str(
        gpu_info.get("gpu_type_display")
        or gpu_info.get("gpu_type")
        or gpu_info.get("brand_name")
        or price.get("gpu_type")
        or ""
    ).strip()


def resolve_train_spec(
    *,
    session,
    workspace_id: str,
    compute_group_id: str,
    gpu_type: str,
    gpu_count: int,
) -> tuple[str, int, int]:
    """Return ``(spec_id, cpu_count, memory_size_gib)`` for a train-job spec.

    The Inspire platform returns one or more TRAIN specs per compute group,
    keyed by ``(gpu_type, gpu_count)`` — cpu and memory are derived. Query
    live prices, pick the row whose ``gpu_count`` and ``gpu_type`` match
    the requested resource.
    """
    try:
        prices = browser_api_module.get_resource_prices(
            workspace_id=workspace_id,
            logic_compute_group_id=compute_group_id,
            schedule_config_type="SCHEDULE_CONFIG_TYPE_TRAIN",
            session=session,
        )
    except Exception as err:
        raise ConfigError(
            f"Could not query train specs for compute group {compute_group_id}: {err}. "
            "Run 'inspire resources specs --usage all --json' to check the group."
        ) from err

    gpu_type_norm = (gpu_type or "").upper()
    matches: list[dict] = []
    for p in prices:
        if int(p.get("gpu_count") or 0) != int(gpu_count):
            continue
        candidate = _extract_gpu_type(p).upper()
        if not candidate or gpu_type_norm not in candidate:
            continue
        matches.append(p)

    if not matches:
        available = [
            f"{int(p.get('gpu_count') or 0)}x{_extract_gpu_type(p) or '?'}"
            for p in prices
        ]
        hint = ", ".join(sorted(set(available))) if available else "(no TRAIN specs)"
        raise ConfigError(
            f"No train spec matches {gpu_count}x{gpu_type} in compute group "
            f"{compute_group_id}. Available: {hint}"
        )

    # Multiple matches (same gpu count+type but different cpu/memory) — pick
    # the one with the highest cpu_count, which mirrors the previous
    # hardcoded ordering (biggest spec wins).
    matches.sort(key=lambda p: int(p.get("cpu_count") or 0), reverse=True)
    p = matches[0]
    spec_id = str(p.get("quota_id") or p.get("spec_id") or "").strip()
    if not spec_id:
        raise ConfigError(
            "Platform returned a train spec without a quota_id — cannot submit."
        )
    return spec_id, int(p.get("cpu_count") or 0), _extract_memory_gib(p)


__all__ = ["resolve_train_spec"]
