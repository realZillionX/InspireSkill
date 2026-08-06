from __future__ import annotations

from types import SimpleNamespace

import pytest

from inspire.cli.utils import quota_cache as quota_cache_module
from inspire.cli.utils.quota_cache import (
    SCHEDULE_TYPE_BY_WORKLOAD,
    CachedPricesLoader,
    workload_for_schedule_type,
)
from inspire.cli.utils.quota_resolver import QuotaSpec, resolve_quota
from inspire.cli.utils.resource_index import ResourceIndex


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


@pytest.mark.parametrize("workload", sorted(SCHEDULE_TYPE_BY_WORKLOAD))
def test_schedule_type_round_trips_to_workload(workload: str) -> None:
    schedule_type = SCHEDULE_TYPE_BY_WORKLOAD[workload]
    assert workload_for_schedule_type(schedule_type) == workload


def test_loader_serves_second_call_from_cache(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    calls: list[dict] = []

    def _fake_prices(**kwargs):  # noqa: ANN202
        calls.append(kwargs)
        return [_price("q-1", 8, 160, 1800)]

    monkeypatch.setattr(
        quota_cache_module.browser_api_module, "get_resource_prices", _fake_prices
    )
    index = ResourceIndex(tmp_path / "index.sqlite3")

    def _loader() -> CachedPricesLoader:
        return CachedPricesLoader(
            session=_session(),
            workspace_id="workspace-one",
            schedule_config_type=SCHEDULE_TYPE_BY_WORKLOAD["notebook"],
            cache_index=index,
        )

    first = _loader()
    assert first("lcg-a") == [_price("q-1", 8, 160, 1800)]
    assert first.served_from_cache == set()
    assert len(calls) == 1

    # A brand-new loader (i.e. a separate CLI invocation) hits the cache.
    second = _loader()
    assert second("lcg-a") == [_price("q-1", 8, 160, 1800)]
    assert second.served_from_cache == {"lcg-a"}
    assert len(calls) == 1


def test_loader_partitions_cache_by_workload(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    by_schedule_type = {
        SCHEDULE_TYPE_BY_WORKLOAD["notebook"]: [_price("dsw", 1, 20, 200)],
        SCHEDULE_TYPE_BY_WORKLOAD["hpc"]: [_price("hpc", 8, 160, 1800)],
    }
    monkeypatch.setattr(
        quota_cache_module.browser_api_module,
        "get_resource_prices",
        lambda **kwargs: by_schedule_type[kwargs["schedule_config_type"]],
    )
    index = ResourceIndex(tmp_path / "index.sqlite3")

    for workload in ("notebook", "hpc"):
        loader = CachedPricesLoader(
            session=_session(),
            workspace_id="workspace-one",
            schedule_config_type=SCHEDULE_TYPE_BY_WORKLOAD[workload],
            cache_index=index,
        )
        loader("lcg-a")

    notebook_rows = CachedPricesLoader(
        session=_session(),
        workspace_id="workspace-one",
        schedule_config_type=SCHEDULE_TYPE_BY_WORKLOAD["notebook"],
        cache_index=index,
    )("lcg-a")
    hpc_rows = CachedPricesLoader(
        session=_session(),
        workspace_id="workspace-one",
        schedule_config_type=SCHEDULE_TYPE_BY_WORKLOAD["hpc"],
        cache_index=index,
    )("lcg-a")

    assert notebook_rows[0]["quota_id"] == "dsw"
    assert hpc_rows[0]["quota_id"] == "hpc"


def test_loader_caches_empty_response(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    calls: list[dict] = []

    def _fake_prices(**kwargs):  # noqa: ANN202
        calls.append(kwargs)
        return []

    monkeypatch.setattr(
        quota_cache_module.browser_api_module, "get_resource_prices", _fake_prices
    )
    index = ResourceIndex(tmp_path / "index.sqlite3")

    for _ in range(2):
        loader = CachedPricesLoader(
            session=_session(),
            workspace_id="workspace-one",
            schedule_config_type=SCHEDULE_TYPE_BY_WORKLOAD["ray"],
            cache_index=index,
        )
        assert loader("lcg-cpu") == []

    # A compute group with no quotas for this workload is not re-fetched.
    assert len(calls) == 1


def test_loader_falls_through_when_cache_lookup_fails(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    monkeypatch.setattr(
        quota_cache_module.browser_api_module,
        "get_resource_prices",
        lambda **_kwargs: [_price("q-live", 1, 20, 200)],
    )

    class _BrokenIndex:
        def lookup_quota_prices(self, *_args, **_kwargs):  # noqa: ANN202
            raise RuntimeError("cache is on fire")

        def store_quota_prices(self, *_args, **_kwargs):  # noqa: ANN202
            raise RuntimeError("cache is still on fire")

    loader = CachedPricesLoader(
        session=_session(),
        workspace_id="workspace-one",
        schedule_config_type=SCHEDULE_TYPE_BY_WORKLOAD["job"],
        cache_index=_BrokenIndex(),  # type: ignore[arg-type]
    )

    assert loader("lcg-a") == [_price("q-live", 1, 20, 200)]
    assert loader.served_from_cache == set()


def test_resolve_quota_reuses_cached_prices_across_invocations(
    monkeypatch,  # noqa: ANN001
    tmp_path,  # noqa: ANN001
) -> None:
    groups = [{"logic_compute_group_id": "lcg-a", "name": "训练区-H200-1号机房"}]
    price_calls: list[str] = []

    monkeypatch.setattr(
        quota_cache_module.browser_api_module,
        "get_resource_prices",
        lambda **kwargs: (
            price_calls.append(kwargs["logic_compute_group_id"]),
            [_price("q-8", 8, 160, 1800)],
        )[1],
    )
    index = ResourceIndex(tmp_path / "index.sqlite3")

    def _resolve():  # noqa: ANN202
        return resolve_quota(
            spec=QuotaSpec(gpu_count=8, cpu_count=160, memory_gib=1800),
            workspace_id="workspace-one",
            session=_session(),  # type: ignore[arg-type]
            groups=groups,
            cache_index=index,
        )

    first = _resolve()
    assert first.quota_id == "q-8"
    assert first.compute_group_name == "训练区-H200-1号机房"
    assert price_calls == ["lcg-a"]

    second = _resolve()
    assert second.quota_id == "q-8"
    # The catalog came from the cache; no second price request.
    assert price_calls == ["lcg-a"]


def test_cached_empty_group_does_not_trigger_stale_group_retry(
    monkeypatch,  # noqa: ANN001
    tmp_path,  # noqa: ANN001
) -> None:
    """An empty cached catalog is authoritative, not a dead-handle signal.

    A compute group with no quotas for this workload (a CPU group asked for
    Ray specs, say) returns an empty list. From the live API that is one of
    the symptoms of a compute group handle that died, so the resolver re-lists
    groups and retries. Served from the cache it means exactly what it says,
    and must cost nothing.
    """
    index = ResourceIndex(tmp_path / "index.sqlite3")
    price_calls: list[str] = []
    monkeypatch.setattr(
        quota_cache_module.browser_api_module,
        "get_resource_prices",
        lambda **kwargs: (price_calls.append(kwargs["logic_compute_group_id"]), [])[1],
    )

    groups_loader_calls = {"n": 0}

    def _groups_loader():  # noqa: ANN202
        groups_loader_calls["n"] += 1
        return [{"logic_compute_group_id": "lcg-a", "name": "CPU资源-2"}]

    from inspire.cli.utils.quota_resolver import QuotaMatchError

    def _resolve():  # noqa: ANN202
        return resolve_quota(
            spec=QuotaSpec(gpu_count=1, cpu_count=20, memory_gib=200),
            workspace_id="workspace-one",
            session=_session(),  # type: ignore[arg-type]
            group_override="CPU资源-2",
            groups_loader=_groups_loader,
            cache_index=index,
        )

    with pytest.raises(QuotaMatchError):
        _resolve()
    assert groups_loader_calls["n"] == 1
    assert price_calls == ["lcg-a"]

    with pytest.raises(QuotaMatchError):
        _resolve()

    # Group name and empty catalog both came from the cache: no new requests.
    assert groups_loader_calls["n"] == 1
    assert price_calls == ["lcg-a"]
