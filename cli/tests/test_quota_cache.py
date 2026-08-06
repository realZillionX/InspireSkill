from __future__ import annotations

from types import SimpleNamespace

import pytest

from inspire.cli.utils import quota_cache as quota_cache_module
from inspire.cli.utils.quota_cache import (
    SCHEDULE_TYPE_BY_WORKLOAD,
    CachedPricesLoader,
    quota_triple,
    warm_quota_catalog,
    workload_for_schedule_type,
)
from inspire.cli.utils.quota_resolver import QuotaMatchError, QuotaSpec, resolve_quota
from inspire.cli.utils.resource_index import ResourceIndex

GROUPS = [
    {"logic_compute_group_id": "lcg-a", "name": "训练区-H200-1号机房"},
    {"logic_compute_group_id": "lcg-b", "name": "CPU资源-2"},
]


def _session() -> SimpleNamespace:
    return SimpleNamespace(
        base_url="https://inspire.example",
        user_detail={"id": "user-one"},
        account="primary",
    )


def _price(quota_id: str, gpu: int, cpu: int, mem: int, gpu_type: str = "H200") -> dict:
    return {
        "quota_id": quota_id,
        "gpu_count": gpu,
        "cpu_count": cpu,
        "memory_size_gib": mem,
        "gpu_info": {"gpu_type": f"NVIDIA_{gpu_type}", "gpu_type_display": gpu_type},
        "cpu_info": {"cpu_type": "intel"},
    }


def _patch_platform(monkeypatch, prices_by_group: dict[str, list[dict]]) -> list[str]:  # noqa: ANN001
    """Stub the two platform calls; return the log of price requests."""
    price_calls: list[str] = []

    def _prices(**kwargs):  # noqa: ANN202
        price_calls.append(kwargs["logic_compute_group_id"])
        return prices_by_group.get(kwargs["logic_compute_group_id"], [])

    monkeypatch.setattr(
        quota_cache_module.browser_api_module, "get_resource_prices", _prices
    )
    monkeypatch.setattr(
        quota_cache_module.browser_api_module,
        "list_notebook_compute_groups",
        lambda **_kwargs: GROUPS,
    )
    return price_calls


@pytest.mark.parametrize("workload", sorted(SCHEDULE_TYPE_BY_WORKLOAD))
def test_schedule_type_round_trips_to_workload(workload: str) -> None:
    assert workload_for_schedule_type(SCHEDULE_TYPE_BY_WORKLOAD[workload]) == workload


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        ({"gpu_count": 8, "cpu_count": 160, "memory_size_gib": 1800}, "8,160,1800"),
        ({"gpu_count": 0, "cpu_count": 20, "memory_size": 256}, "0,20,256"),
        ({}, "0,0,0"),
    ],
)
def test_quota_triple_is_the_user_facing_name(price: dict, expected: str) -> None:
    assert quota_triple(price) == expected


def test_warm_catalog_then_serve_every_group_from_cache(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    price_calls = _patch_platform(
        monkeypatch,
        {
            "lcg-a": [_price("q-8", 8, 160, 1800)],
            "lcg-b": [_price("q-cpu", 0, 20, 256, gpu_type="")],
        },
    )
    index = ResourceIndex(tmp_path / "index.sqlite3")

    assert (
        warm_quota_catalog(
            session=_session(),
            index=index,
            workspace_id="workspace-one",
            workload="notebook",
        )
        == 2
    )
    assert price_calls == ["lcg-a", "lcg-b"]

    loader = CachedPricesLoader(
        session=_session(),  # type: ignore[arg-type]
        workspace_id="workspace-one",
        schedule_config_type=SCHEDULE_TYPE_BY_WORKLOAD["notebook"],
        cache_index=index,
    )
    assert loader("lcg-a")[0]["quota_id"] == "q-8"
    assert loader("lcg-b")[0]["quota_id"] == "q-cpu"
    assert loader.served_from_cache == {"lcg-a", "lcg-b"}
    # Nothing new hit the platform.
    assert price_calls == ["lcg-a", "lcg-b"]


def test_warm_catalog_partitions_by_workload(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    per_schedule_type = {
        SCHEDULE_TYPE_BY_WORKLOAD["notebook"]: [_price("dsw", 1, 20, 200)],
        SCHEDULE_TYPE_BY_WORKLOAD["hpc"]: [_price("hpc", 8, 160, 1800)],
    }
    monkeypatch.setattr(
        quota_cache_module.browser_api_module,
        "get_resource_prices",
        lambda **kwargs: (
            per_schedule_type[kwargs["schedule_config_type"]]
            if kwargs["logic_compute_group_id"] == "lcg-a"
            else []
        ),
    )
    monkeypatch.setattr(
        quota_cache_module.browser_api_module,
        "list_notebook_compute_groups",
        lambda **_kwargs: GROUPS,
    )
    index = ResourceIndex(tmp_path / "index.sqlite3")

    for workload in ("notebook", "hpc"):
        warm_quota_catalog(
            session=_session(),
            index=index,
            workspace_id="workspace-one",
            workload=workload,
        )

    def _cached(workload: str) -> list[dict]:
        return CachedPricesLoader(
            session=_session(),  # type: ignore[arg-type]
            workspace_id="workspace-one",
            schedule_config_type=SCHEDULE_TYPE_BY_WORKLOAD[workload],
            cache_index=index,
        )("lcg-a")

    assert _cached("notebook")[0]["quota_id"] == "dsw"
    assert _cached("hpc")[0]["quota_id"] == "hpc"


def test_group_with_no_quotas_is_authoritatively_empty(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    price_calls = _patch_platform(monkeypatch, {"lcg-a": [_price("q-8", 8, 160, 1800)]})
    index = ResourceIndex(tmp_path / "index.sqlite3")
    warm_quota_catalog(
        session=_session(),
        index=index,
        workspace_id="workspace-one",
        workload="ray",
    )
    before = list(price_calls)

    loader = CachedPricesLoader(
        session=_session(),  # type: ignore[arg-type]
        workspace_id="workspace-one",
        schedule_config_type=SCHEDULE_TYPE_BY_WORKLOAD["ray"],
        cache_index=index,
    )

    # lcg-b returned nothing during the warm pass; the empty answer is cached,
    # not re-fetched.
    assert loader("lcg-b") == []
    assert "lcg-b" in loader.served_from_cache
    assert price_calls == before


def test_cold_scope_falls_through_to_live_per_group(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    price_calls = _patch_platform(monkeypatch, {"lcg-a": [_price("q-8", 8, 160, 1800)]})
    index = ResourceIndex(tmp_path / "index.sqlite3")

    loader = CachedPricesLoader(
        session=_session(),  # type: ignore[arg-type]
        workspace_id="workspace-one",
        schedule_config_type=SCHEDULE_TYPE_BY_WORKLOAD["notebook"],
        cache_index=index,
    )

    assert loader("lcg-a")[0]["quota_id"] == "q-8"
    assert loader.served_from_cache == set()
    assert price_calls == ["lcg-a"]


def test_loader_falls_through_when_cache_lookup_fails(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    _patch_platform(monkeypatch, {"lcg-a": [_price("q-live", 1, 20, 200)]})

    class _BrokenIndex:
        def scope_due(self, *_args, **_kwargs):  # noqa: ANN202
            raise RuntimeError("cache is on fire")

    loader = CachedPricesLoader(
        session=_session(),  # type: ignore[arg-type]
        workspace_id="workspace-one",
        schedule_config_type=SCHEDULE_TYPE_BY_WORKLOAD["job"],
        cache_index=_BrokenIndex(),  # type: ignore[arg-type]
    )

    assert loader("lcg-a")[0]["quota_id"] == "q-live"
    assert loader.served_from_cache == set()


def test_resolve_quota_reuses_the_warm_catalog(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    price_calls = _patch_platform(
        monkeypatch, {"lcg-a": [_price("q-8", 8, 160, 1800)]}
    )
    index = ResourceIndex(tmp_path / "index.sqlite3")
    warm_quota_catalog(
        session=_session(),
        index=index,
        workspace_id="workspace-one",
        workload="notebook",
    )
    before = list(price_calls)

    resolved = resolve_quota(
        spec=QuotaSpec(gpu_count=8, cpu_count=160, memory_gib=1800),
        workspace_id="workspace-one",
        session=_session(),  # type: ignore[arg-type]
        groups=GROUPS,
        cache_index=index,
    )

    assert resolved.quota_id == "q-8"
    assert resolved.compute_group_name == "训练区-H200-1号机房"
    assert resolved.gpu_type == "H200"
    # build_resource_spec_price needs the raw payload, which survived the cache.
    assert resolved.raw_price["cpu_info"]["cpu_type"] == "intel"
    assert price_calls == before


def test_cached_empty_group_does_not_trigger_stale_group_retry(
    monkeypatch,  # noqa: ANN001
    tmp_path,  # noqa: ANN001
) -> None:
    """An empty cached catalog is authoritative, not a dead-handle signal.

    A compute group with no quotas for this workload returns an empty list.
    From the live API that is one of the symptoms of a compute group handle
    that died, so the resolver re-lists groups and retries. Served from the
    cache it means exactly what it says, and must cost nothing.
    """
    price_calls = _patch_platform(monkeypatch, {})
    index = ResourceIndex(tmp_path / "index.sqlite3")
    warm_quota_catalog(
        session=_session(),
        index=index,
        workspace_id="workspace-one",
        workload="notebook",
    )
    before = list(price_calls)

    groups_loader_calls = {"n": 0}

    def _groups_loader():  # noqa: ANN202
        groups_loader_calls["n"] += 1
        return [GROUPS[1]]

    with pytest.raises(QuotaMatchError):
        resolve_quota(
            spec=QuotaSpec(gpu_count=1, cpu_count=20, memory_gib=200),
            workspace_id="workspace-one",
            session=_session(),  # type: ignore[arg-type]
            group_override="CPU资源-2",
            groups_loader=_groups_loader,
            cache_index=index,
        )

    assert groups_loader_calls["n"] == 1
    assert price_calls == before
