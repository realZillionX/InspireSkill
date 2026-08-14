from __future__ import annotations

from types import SimpleNamespace

import pytest

from inspire.cli.utils import quota_cache as quota_cache_module
from inspire.cli.utils.quota_cache import (
    SCHEDULE_TYPE_BY_WORKLOAD,
    CachedPricesLoader,
    fetch_quota_catalog,
    quota_scope_for_session,
    quota_triple,
    workload_for_schedule_type,
)
from inspire.cli.utils.quota_resolver import (
    QuotaMatchError,
    QuotaSpec,
    ResolvedQuota,
    resolve_quota,
    validate_quota_priority,
)
from inspire.cli.utils.resource_index import QUOTA_WORKLOADS, ResourceIndex


def warm_quota_catalog(*, session, index, workspace_id, workload) -> int:  # noqa: ANN001
    """Fetch and reconcile one workspace/workload catalog, as `cache refresh` does."""
    scope = quota_scope_for_session(
        session, workspace_id=workspace_id, workload=workload
    )
    assert scope is not None
    records = fetch_quota_catalog(
        session, workspace_id=workspace_id, workload=workload
    )
    index.reconcile(scope, records)
    return len(records)


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
    # Notebook / job / serving read from ``GetScheduleConfig``; stub it (and
    # gpu_type back-fill from group nodes) at the same platform boundary as
    # the prices stub so no unit test ever hits the network.
    def _schedule_config_specs(**kwargs):  # noqa: ANN202
        spec_field = kwargs["spec_field"]
        workload = {
            "quota": "notebook",
            "predef_train_spec": "job",
            "serving_quota": "serving",
        }[spec_field]
        # Reflect which (group, price) pairs each workload owns by
        # materialising a spec row per price keyed to the workload ID space.
        specs = []
        for group_id, prices in prices_by_group.items():
            for price in prices:
                specs.append({
                    "id": price["quota_id"],
                    "cpu_count": price["cpu_count"],
                    "memory_size": price.get("memory_size_gib", 0),
                    "gpu_count": price["gpu_count"],
                    "gpu_type": (price.get("gpu_info") or {}).get("gpu_type", ""),
                    "logic_compute_group_ids": [group_id],
                    "allowed_priority_levels": [
                        level for level in (price.get("allowed_priority_levels") or [])
                    ],
                })
        return specs

    monkeypatch.setattr(
        quota_cache_module.browser_api_module,
        "get_schedule_config_specs",
        _schedule_config_specs,
    )
    monkeypatch.setattr(
        quota_cache_module.browser_api_module,
        "get_group_node_gpu_type",
        lambda *args, **_kwargs: "",
    )
    return price_calls


def test_every_quota_workload_has_a_schedule_type() -> None:
    assert set(SCHEDULE_TYPE_BY_WORKLOAD) == set(QUOTA_WORKLOADS)


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

    loader = CachedPricesLoader(
        session=_session(),  # type: ignore[arg-type]
        workspace_id="workspace-one",
        schedule_config_type=SCHEDULE_TYPE_BY_WORKLOAD["notebook"],
        cache_index=index,
    )
    assert loader("lcg-a")[0]["quota_id"] == "q-8"
    assert loader("lcg-b")[0]["quota_id"] == "q-cpu"
    assert loader.served_from_cache == {"lcg-a", "lcg-b"}
    # The warm pass answered from the newly populated catalog without any
    # additional per-group v1 price call.
    assert price_calls == []


def test_warm_catalog_partitions_by_workload(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    # hpc and ray still walk the per-group v1 ``schedule_config_type``
    # endpoint; notebook / job / serving read the workspace-level
    # GetScheduleConfig menus (``quota`` / ``predef_train_spec`` /
    # ``serving_quota``). The cache partitions by workload either way.
    per_schedule_type = {
        SCHEDULE_TYPE_BY_WORKLOAD["hpc"]: [_price("hpc", 8, 160, 1800)],
        SCHEDULE_TYPE_BY_WORKLOAD["ray"]: [_price("ray", 0, 4, 16)],
    }
    per_spec_field = {
        "quota": [
            {
                "id": "dsw",
                "cpu_count": 20,
                "memory_size": 200,
                "gpu_count": 1,
                "gpu_type": "H200",
                "logic_compute_group_ids": ["lcg-a"],
                "allowed_priority_levels": [],
            }
        ]
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
    monkeypatch.setattr(
        quota_cache_module.browser_api_module,
        "get_schedule_config_specs",
        lambda **kwargs: per_spec_field.get(kwargs["spec_field"], []),
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
    _patch_platform(monkeypatch, {"lcg-a": [_price("q-8", 8, 160, 1800)]})
    index = ResourceIndex(tmp_path / "index.sqlite3")

    loader = CachedPricesLoader(
        session=_session(),  # type: ignore[arg-type]
        workspace_id="workspace-one",
        schedule_config_type=SCHEDULE_TYPE_BY_WORKLOAD["notebook"],
        cache_index=index,
    )

    assert loader("lcg-a")[0]["quota_id"] == "q-8"
    assert loader.served_from_cache == set()


def test_loader_falls_through_when_cache_lookup_fails(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    """A broken cache must never block a live answer.

    Job quota reads live from the workspace's ``GetScheduleConfig`` spec menu
    (not the per-group v1 price endpoint); other workloads still fall through
    to their per-group v1 path. Both answers must surface even when the cache
    itself is on fire.
    """
    _patch_platform(
        monkeypatch,
        {"lcg-a": [_price("q-live", 1, 20, 200)]},
    )

    train_specs = [
        {
            "id": "q-live-train",
            "cpu_count": 20,
            "memory_size": 200,
            "gpu_count": 1,
            "gpu_type": "NVIDIA_H200_SXM_141G",
            "logic_compute_group_ids": ["lcg-a"],
        }
    ]
    monkeypatch.setattr(
        quota_cache_module.browser_api_module,
        "get_schedule_config_specs",
        lambda **_kwargs: train_specs,
    )
    monkeypatch.setattr(
        quota_cache_module.browser_api_module,
        "get_group_node_gpu_type",
        lambda *args, **_kwargs: "",
    )

    class _BrokenIndex:
        def scope_due(self, *_args, **_kwargs):  # noqa: ANN202
            raise RuntimeError("cache is on fire")

    job_loader = CachedPricesLoader(
        session=_session(),  # type: ignore[arg-type]
        workspace_id="workspace-one",
        schedule_config_type=SCHEDULE_TYPE_BY_WORKLOAD["job"],
        cache_index=_BrokenIndex(),  # type: ignore[arg-type]
    )
    assert job_loader("lcg-a")[0]["quota_id"] == "q-live-train"
    assert job_loader.served_from_cache == set()

    # Notebook / job / serving all read the workspace-level GetScheduleConfig
    # menus; the cache-on-fire walk just lands on the same live answer.
    notebook_loader = CachedPricesLoader(
        session=_session(),  # type: ignore[arg-type]
        workspace_id="workspace-one",
        schedule_config_type=SCHEDULE_TYPE_BY_WORKLOAD["notebook"],
        cache_index=_BrokenIndex(),  # type: ignore[arg-type]
    )
    assert notebook_loader("lcg-a")[0]["quota_id"] == "q-live-train"
    assert notebook_loader.served_from_cache == set()


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
    # The GetScheduleConfig spec row carries the platform-machine gpu_type
    # (``NVIDIA_H200`` from the ``_price`` fixture), not the v1 endpoint's
    # separate ``gpu_type_display`` short name, so ``_extract_gpu_type``
    # surfaces it verbatim.
    assert resolved.gpu_type == "NVIDIA_H200"
    # The raw payload survives the cache round-trip; ``cpu_info.cpu_type``
    # is empty because the GetScheduleConfig spec row does not carry it and
    # the create payload does not depend on it.
    assert resolved.raw_price["cpu_info"]["cpu_type"] == ""
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


def _resolved(*, allowed: tuple[str, ...]) -> ResolvedQuota:
    return ResolvedQuota(
        quota_id="q",
        logic_compute_group_id="lcg-x",
        compute_group_name="训练区-H200-1号机房",
        gpu_count=1,
        cpu_count=20,
        memory_gib=200,
        gpu_type="H200",
        raw_price={},
        allowed_priority_levels=allowed,
    )


def test_validate_quota_priority_blocks_high_for_low_only_quota() -> None:
    quota = _resolved(allowed=("low",))
    with pytest.raises(QuotaMatchError, match=r"--priority 4"):
        validate_quota_priority(quota, 4)


def test_validate_quota_priority_passes_low_for_low_only_quota() -> None:
    validate_quota_priority(_resolved(allowed=("low",)), 1)


def test_validate_quota_priority_passes_anything_for_unrestricted_quota() -> None:
    quota = _resolved(allowed=())
    validate_quota_priority(quota, 1)
    validate_quota_priority(quota, 4)
    validate_quota_priority(quota, 10)
