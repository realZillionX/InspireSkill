from __future__ import annotations

import pytest

from inspire.cli.utils import quota_resolver as quota_resolver_module
from inspire.cli.utils.quota_resolver import (
    QuotaCatalogUnavailable,
    QuotaMatchError,
    QuotaParseError,
    QuotaSpec,
    ResolvedQuota,
    SCHEDULE_TYPE_HPC,
    SCHEDULE_TYPE_TRAIN,
    allowed_priority_levels_for,
    build_resource_spec_price,
    describe_priority_levels,
    ensure_priority_allowed,
    load_quota_priority_levels,
    parse_quota,
    resolve_quota,
)
from inspire.cli.utils.resource_index import (
    ResourceIdentity,
    ResourceIndex,
    ResourceScope,
    scope_for_session,
)
from inspire.platform.web.session import WebSession


def test_parse_quota_basic() -> None:
    assert parse_quota("1,20,200") == QuotaSpec(gpu_count=1, cpu_count=20, memory_gib=200)


def test_parse_quota_allows_spaces() -> None:
    assert parse_quota("  4 , 80 , 800 ") == QuotaSpec(
        gpu_count=4, cpu_count=80, memory_gib=800
    )


def test_parse_quota_cpu_only_allowed() -> None:
    assert parse_quota("0,4,32") == QuotaSpec(gpu_count=0, cpu_count=4, memory_gib=32)


def test_parse_quota_rejects_wrong_arity() -> None:
    with pytest.raises(QuotaParseError):
        parse_quota("1,20")
    with pytest.raises(QuotaParseError):
        parse_quota("1,20,200,400")


def test_parse_quota_rejects_non_integer() -> None:
    with pytest.raises(QuotaParseError):
        parse_quota("1,cpu,200")


def test_parse_quota_rejects_negative_or_zero() -> None:
    with pytest.raises(QuotaParseError):
        parse_quota("-1,20,200")
    with pytest.raises(QuotaParseError):
        parse_quota("1,0,200")
    with pytest.raises(QuotaParseError):
        parse_quota("1,20,0")


def _make_group(lcg_id: str, name: str) -> dict:
    return {"logic_compute_group_id": lcg_id, "name": name}


def _make_price(
    *,
    quota_id: str,
    gpu: int,
    cpu: int,
    mem: int,
    gpu_type: str = "",
    cpu_type: str = "Intel",
) -> dict:
    gpu_info = {"gpu_type": gpu_type, "gpu_type_display": gpu_type} if gpu_type else {}
    return {
        "quota_id": quota_id,
        "gpu_count": gpu,
        "cpu_count": cpu,
        "memory_size_gib": mem,
        "gpu_info": gpu_info,
        "cpu_info": {"cpu_type": cpu_type},
    }


def _cached_group_session() -> WebSession:
    return WebSession(
        storage_state={},
        created_at=0,
        base_url="https://inspire.example",
        user_detail={"id": "user-1"},
    )


def _seed_group_cache(
    tmp_path,
    session: WebSession,
    *,
    workspace_id: str = "ws-1",
    group_id: str = "lcg-old",
    group_name: str = "H200 Group",
    ttl_seconds: int = 300,
) -> tuple[ResourceIndex, ResourceScope]:
    index = ResourceIndex(tmp_path / "resource-index.sqlite3")
    scope = scope_for_session(
        session,
        resource_type="compute-group",
        workspace_id=workspace_id,
    )
    assert scope is not None
    index.upsert(
        scope,
        [ResourceIdentity(resource_id=group_id, name=group_name)],
        ttl_seconds=ttl_seconds,
    )
    return index, scope


def test_resolve_quota_unique_match() -> None:
    groups = [_make_group("lcg-a", "H200 Group A")]
    prices = {
        "lcg-a": [
            _make_price(quota_id="q-1", gpu=1, cpu=20, mem=200, gpu_type="H200"),
            _make_price(quota_id="q-2", gpu=4, cpu=80, mem=800, gpu_type="H200"),
        ]
    }
    result = resolve_quota(
        spec=QuotaSpec(1, 20, 200),
        workspace_id="ws-1",
        groups=groups,
        prices_loader=lambda lcg: prices.get(lcg, []),
    )
    assert result.quota_id == "q-1"
    assert result.logic_compute_group_id == "lcg-a"
    assert result.gpu_type == "H200"
    assert result.compute_group_name == "H200 Group A"
    assert result.cpu_count == 20
    assert result.memory_gib == 200


def test_resolve_quota_no_match_raises_with_catalog() -> None:
    groups = [_make_group("lcg-a", "H200 Group")]
    prices = {"lcg-a": [_make_price(quota_id="q-1", gpu=1, cpu=20, mem=200, gpu_type="H200")]}

    with pytest.raises(QuotaMatchError) as exc:
        resolve_quota(
            spec=QuotaSpec(8, 160, 1800),
            workspace_id="ws-1",
            groups=groups,
            prices_loader=lambda lcg: prices.get(lcg, []),
        )

    message = str(exc.value)
    assert "matches no quota row" in message
    assert "1,20,200" in message
    assert "H200 Group" in message


def test_resolve_quota_multi_match_requires_group() -> None:
    groups = [
        _make_group("lcg-a", "H100 Group"),
        _make_group("lcg-b", "H200 Group"),
    ]
    prices = {
        "lcg-a": [_make_price(quota_id="q-100", gpu=1, cpu=20, mem=200, gpu_type="H100")],
        "lcg-b": [_make_price(quota_id="q-200", gpu=1, cpu=20, mem=200, gpu_type="H200")],
    }

    with pytest.raises(QuotaMatchError) as exc:
        resolve_quota(
            spec=QuotaSpec(1, 20, 200),
            workspace_id="ws-1",
            groups=groups,
            prices_loader=lambda lcg: prices.get(lcg, []),
        )
    assert "pass --group <full compute group name>" in str(exc.value)
    assert "quota query --group <keyword>" in str(exc.value)
    assert "H100 Group" in str(exc.value)
    assert "H200 Group" in str(exc.value)


def test_resolve_quota_group_override_disambiguates() -> None:
    groups = [
        _make_group("lcg-a", "H100 Group"),
        _make_group("lcg-b", "H200 Group"),
    ]
    prices = {
        "lcg-a": [_make_price(quota_id="q-100", gpu=1, cpu=20, mem=200, gpu_type="H100")],
        "lcg-b": [_make_price(quota_id="q-200", gpu=1, cpu=20, mem=200, gpu_type="H200")],
    }

    result = resolve_quota(
        spec=QuotaSpec(1, 20, 200),
        workspace_id="ws-1",
        groups=groups,
        prices_loader=lambda lcg: prices.get(lcg, []),
        group_override="H200 Group",
    )
    assert result.logic_compute_group_id == "lcg-b"
    assert result.gpu_type == "H200"


def test_resolve_quota_group_override_rejects_partial_match() -> None:
    groups = [
        _make_group("lcg-a", "H100 Group"),
        _make_group("lcg-b", "H200 Group 2"),
    ]
    prices = {
        "lcg-b": [_make_price(quota_id="q-200", gpu=1, cpu=20, mem=200, gpu_type="H200")],
    }
    with pytest.raises(QuotaMatchError) as exc:
        resolve_quota(
            spec=QuotaSpec(1, 20, 200),
            workspace_id="ws-1",
            groups=groups,
            prices_loader=lambda lcg: prices.get(lcg, []),
            group_override="H200",
        )
    message = str(exc.value)
    assert "exactly matches --group" in message
    assert "full compute group name" in message
    assert "H200 Group 2" in message


def test_resolve_quota_group_override_no_match() -> None:
    groups = [_make_group("lcg-a", "H100 Group")]
    with pytest.raises(QuotaMatchError) as exc:
        resolve_quota(
            spec=QuotaSpec(1, 20, 200),
            workspace_id="ws-1",
            groups=groups,
            prices_loader=lambda lcg: [],
            group_override="nonsense",
        )
    assert "No compute group name exactly matches --group" in str(exc.value)


def test_explicit_group_uses_fresh_cache_without_listing_groups(tmp_path) -> None:
    session = _cached_group_session()
    index, _ = _seed_group_cache(tmp_path, session)
    group_list_calls = 0

    def groups_loader() -> list[dict]:
        nonlocal group_list_calls
        group_list_calls += 1
        raise AssertionError("fresh compute-group cache should avoid list API")

    result = resolve_quota(
        spec=QuotaSpec(1, 20, 200),
        workspace_id="ws-1",
        session=session,
        cache_index=index,
        group_override="H200 Group",
        groups_loader=groups_loader,
        prices_loader=lambda group_id: [
            _make_price(
                quota_id="q-1",
                gpu=1,
                cpu=20,
                mem=200,
                gpu_type="H200",
            )
        ]
        if group_id == "lcg-old"
        else [],
    )

    assert result.logic_compute_group_id == "lcg-old"
    assert group_list_calls == 0


def test_explicit_group_cache_lookup_is_case_insensitive(tmp_path) -> None:
    session = _cached_group_session()
    index, _ = _seed_group_cache(
        tmp_path,
        session,
        group_name="h200 group",
    )

    result = resolve_quota(
        spec=QuotaSpec(1, 20, 200),
        workspace_id="ws-1",
        session=session,
        cache_index=index,
        group_override="H200 GROUP",
        prices_loader=lambda _group_id: [
            _make_price(
                quota_id="q-1",
                gpu=1,
                cpu=20,
                mem=200,
                gpu_type="H200",
            )
        ],
    )

    assert result.logic_compute_group_id == "lcg-old"


def test_explicit_group_cache_miss_live_fallback_replaces_old_same_name(
    tmp_path,
) -> None:
    session = _cached_group_session()
    index, scope = _seed_group_cache(
        tmp_path,
        session,
        group_id="lcg-old",
        group_name="H200 Group",
        ttl_seconds=0,
    )
    group_list_calls = 0

    def groups_loader() -> list[dict]:
        nonlocal group_list_calls
        group_list_calls += 1
        return [_make_group("lcg-new", "H200 Group")]

    result = resolve_quota(
        spec=QuotaSpec(1, 20, 200),
        workspace_id="ws-1",
        session=session,
        cache_index=index,
        group_override="H200 Group",
        groups_loader=groups_loader,
        prices_loader=lambda group_id: [
            _make_price(
                quota_id="q-new",
                gpu=1,
                cpu=20,
                mem=200,
                gpu_type="H200",
            )
        ]
        if group_id == "lcg-new"
        else [],
    )

    assert result.logic_compute_group_id == "lcg-new"
    assert group_list_calls == 1
    cached = index.lookup(scope, "H200 Group")
    assert len(cached) == 1
    assert cached[0].resource_id == "lcg-new"
    old = index.lookup_id(scope, "lcg-old", include_tombstoned=True)
    assert old is not None and old.tombstoned_at is not None


def test_live_group_snapshot_cannot_overwrite_newer_write_through(
    tmp_path,
) -> None:
    session = _cached_group_session()
    index, scope = _seed_group_cache(
        tmp_path,
        session,
        group_id="lcg-old",
        group_name="H200 Group",
        ttl_seconds=0,
    )

    def groups_loader() -> list[dict]:
        index.mark_deleted(scope, resource_id="lcg-old")
        index.upsert(
            scope,
            [ResourceIdentity(resource_id="lcg-new", name="H200 Group")],
        )
        return [_make_group("lcg-old", "H200 Group")]

    result = resolve_quota(
        spec=QuotaSpec(1, 20, 200),
        workspace_id="ws-1",
        session=session,
        cache_index=index,
        group_override="H200 Group",
        groups_loader=groups_loader,
        prices_loader=lambda group_id: [
            _make_price(
                quota_id="q-new",
                gpu=1,
                cpu=20,
                mem=200,
                gpu_type="H200",
            )
        ]
        if group_id == "lcg-new"
        else [],
    )

    assert result.logic_compute_group_id == "lcg-new"
    assert [
        item.resource_id
        for item in index.lookup(scope, "H200 Group", fresh_only=False)
    ] == ["lcg-new"]


def test_clear_during_live_group_lookup_does_not_repopulate_cache(
    tmp_path,
) -> None:
    session = _cached_group_session()
    index = ResourceIndex(tmp_path / "resource-index.sqlite3")
    scope = scope_for_session(
        session,
        resource_type="compute-group",
        workspace_id="ws-1",
    )
    assert scope is not None

    def groups_loader() -> list[dict]:
        index.clear()
        return [_make_group("lcg-live", "H200 Group")]

    result = resolve_quota(
        spec=QuotaSpec(1, 20, 200),
        workspace_id="ws-1",
        session=session,
        cache_index=index,
        group_override="H200 Group",
        groups_loader=groups_loader,
        prices_loader=lambda group_id: [
            _make_price(
                quota_id="q-live",
                gpu=1,
                cpu=20,
                mem=200,
                gpu_type="H200",
            )
        ]
        if group_id == "lcg-live"
        else [],
    )

    assert result.logic_compute_group_id == "lcg-live"
    assert index.list_identities(scope, fresh_only=False) == []


def test_clear_during_implicit_group_refresh_does_not_repopulate_cache(
    tmp_path,
) -> None:
    session = _cached_group_session()
    index = ResourceIndex(tmp_path / "resource-index.sqlite3")
    scope = scope_for_session(
        session,
        resource_type="compute-group",
        workspace_id="ws-1",
    )
    assert scope is not None

    def groups_loader() -> list[dict]:
        index.clear()
        return [_make_group("lcg-live", "H200 Group")]

    result = resolve_quota(
        spec=QuotaSpec(1, 20, 200),
        workspace_id="ws-1",
        session=session,
        cache_index=index,
        groups_loader=groups_loader,
        prices_loader=lambda group_id: [
            _make_price(
                quota_id="q-live",
                gpu=1,
                cpu=20,
                mem=200,
                gpu_type="H200",
            )
        ]
        if group_id == "lcg-live"
        else [],
    )

    assert result.logic_compute_group_id == "lcg-live"
    assert index.list_identities(scope, fresh_only=False) == []


def test_stale_group_preclean_guard_preserves_clear_recreated_group(
    tmp_path,
) -> None:
    session = _cached_group_session()
    index, scope = _seed_group_cache(tmp_path, session)
    price_calls: list[str] = []

    def groups_loader() -> list[dict]:
        return [_make_group("lcg-new", "H200 Group")]

    def prices_loader(group_id: str) -> list[dict]:
        price_calls.append(group_id)
        if group_id == "lcg-old":
            index.clear()
            index.upsert(
                scope,
                [ResourceIdentity(resource_id="lcg-new", name="H200 Group")],
            )
            raise ValueError("API returned 404: compute group not found")
        return [
            _make_price(
                quota_id="q-new",
                gpu=1,
                cpu=20,
                mem=200,
                gpu_type="H200",
            )
        ]

    result = resolve_quota(
        spec=QuotaSpec(1, 20, 200),
        workspace_id="ws-1",
        session=session,
        cache_index=index,
        group_override="H200 Group",
        groups_loader=groups_loader,
        prices_loader=prices_loader,
    )

    assert result.logic_compute_group_id == "lcg-new"
    assert price_calls == ["lcg-old", "lcg-new"]
    replacement = index.lookup_id(scope, "lcg-new")
    assert replacement is not None
    assert replacement.tombstoned_at is None


def test_group_snapshot_failure_skips_live_cache_write(
    tmp_path,
    monkeypatch,
) -> None:
    session = _cached_group_session()
    index = ResourceIndex(tmp_path / "resource-index.sqlite3")
    scope = scope_for_session(
        session,
        resource_type="compute-group",
        workspace_id="ws-1",
    )
    assert scope is not None
    monkeypatch.setattr(
        index,
        "snapshot_token",
        lambda _scope: (_ for _ in ()).throw(OSError("cache unavailable")),
    )

    result = resolve_quota(
        spec=QuotaSpec(1, 20, 200),
        workspace_id="ws-1",
        session=session,
        cache_index=index,
        group_override="H200 Group",
        groups_loader=lambda: [_make_group("lcg-live", "H200 Group")],
        prices_loader=lambda _group_id: [
            _make_price(
                quota_id="q-live",
                gpu=1,
                cpu=20,
                mem=200,
                gpu_type="H200",
            )
        ],
    )

    assert result.logic_compute_group_id == "lcg-live"
    assert index.list_identities(scope, fresh_only=False) == []


def test_explicit_group_stale_cached_handle_re_resolves_after_not_found(
    tmp_path,
) -> None:
    session = _cached_group_session()
    index, scope = _seed_group_cache(tmp_path, session)
    group_list_calls = 0
    price_calls: list[str] = []

    def groups_loader() -> list[dict]:
        nonlocal group_list_calls
        group_list_calls += 1
        return [_make_group("lcg-new", "H200 Group")]

    def prices_loader(group_id: str) -> list[dict]:
        price_calls.append(group_id)
        if group_id == "lcg-old":
            raise ValueError("API returned 404: compute group not found")
        return [
            _make_price(
                quota_id="q-new",
                gpu=1,
                cpu=20,
                mem=200,
                gpu_type="H200",
            )
        ]

    result = resolve_quota(
        spec=QuotaSpec(1, 20, 200),
        workspace_id="ws-1",
        session=session,
        cache_index=index,
        group_override="H200 Group",
        groups_loader=groups_loader,
        prices_loader=prices_loader,
    )

    assert result.logic_compute_group_id == "lcg-new"
    assert price_calls == ["lcg-old", "lcg-new"]
    assert group_list_calls == 1
    old = index.lookup_id(scope, "lcg-old", include_tombstoned=True)
    assert old is not None and old.tombstoned_at is not None


def test_cached_group_network_error_does_not_trigger_live_retry(tmp_path) -> None:
    session = _cached_group_session()
    index, _ = _seed_group_cache(tmp_path, session)
    group_list_calls = 0

    def groups_loader() -> list[dict]:
        nonlocal group_list_calls
        group_list_calls += 1
        raise AssertionError("network errors must not trigger a blind group retry")

    # A timeout is the platform not answering, so it is reported as such —
    # never as "that quota does not exist".
    with pytest.raises(QuotaCatalogUnavailable):
        resolve_quota(
            spec=QuotaSpec(1, 20, 200),
            workspace_id="ws-1",
            session=session,
            cache_index=index,
            group_override="H200 Group",
            groups_loader=groups_loader,
            prices_loader=lambda _group_id: (_ for _ in ()).throw(
                TimeoutError("temporary network failure")
            ),
        )

    assert group_list_calls == 0


def test_cached_group_auth_error_does_not_trigger_live_retry(tmp_path) -> None:
    session = _cached_group_session()
    index, _ = _seed_group_cache(tmp_path, session)
    group_list_calls = 0

    class ForbiddenResponseError(Exception):
        status_code = 403

    def groups_loader() -> list[dict]:
        nonlocal group_list_calls
        group_list_calls += 1
        raise AssertionError("auth errors must not trigger a blind group retry")

    # 403 says the caller may not read this group, not that the group is gone.
    with pytest.raises(QuotaCatalogUnavailable):
        resolve_quota(
            spec=QuotaSpec(1, 20, 200),
            workspace_id="ws-1",
            session=session,
            cache_index=index,
            group_override="H200 Group",
            groups_loader=groups_loader,
            prices_loader=lambda _group_id: (_ for _ in ()).throw(
                ForbiddenResponseError("compute group not found")
            ),
        )

    assert group_list_calls == 0


def test_cache_failure_does_not_block_live_group_resolution(tmp_path) -> None:
    session = _cached_group_session()
    group_list_calls = 0

    class BrokenCache:
        def lookup(self, *_args, **_kwargs):
            raise OSError("cache unavailable")

        def replace_name(self, *_args, **_kwargs):
            raise OSError("cache unavailable")

    def groups_loader() -> list[dict]:
        nonlocal group_list_calls
        group_list_calls += 1
        return [_make_group("lcg-live", "H200 Group")]

    result = resolve_quota(
        spec=QuotaSpec(1, 20, 200),
        workspace_id="ws-1",
        session=session,
        cache_index=BrokenCache(),  # type: ignore[arg-type]
        group_override="H200 Group",
        groups_loader=groups_loader,
        prices_loader=lambda _group_id: [
            _make_price(
                quota_id="q-live",
                gpu=1,
                cpu=20,
                mem=200,
                gpu_type="H200",
            )
        ],
    )

    assert result.logic_compute_group_id == "lcg-live"
    assert group_list_calls == 1


def test_implicit_group_resolution_stays_live_and_reconciles_full_scope(
    tmp_path,
) -> None:
    session = _cached_group_session()
    index, scope = _seed_group_cache(
        tmp_path,
        session,
        group_id="lcg-old",
        group_name="H200 Group",
    )
    index.upsert(
        scope,
        [ResourceIdentity(resource_id="lcg-removed", name="Removed Group")],
    )
    group_list_calls = 0

    def groups_loader() -> list[dict]:
        nonlocal group_list_calls
        group_list_calls += 1
        return [_make_group("lcg-new", "H200 Group")]

    result = resolve_quota(
        spec=QuotaSpec(1, 20, 200),
        workspace_id="ws-1",
        session=session,
        cache_index=index,
        groups_loader=groups_loader,
        prices_loader=lambda group_id: [
            _make_price(
                quota_id="q-new",
                gpu=1,
                cpu=20,
                mem=200,
                gpu_type="H200",
            )
        ]
        if group_id == "lcg-new"
        else [],
    )

    assert result.logic_compute_group_id == "lcg-new"
    assert group_list_calls == 1
    assert index.lookup(scope, "H200 Group")[0].resource_id == "lcg-new"
    removed = index.lookup_id(scope, "lcg-removed", include_tombstoned=True)
    assert removed is not None and removed.tombstoned_at is not None


def test_resolve_quota_cpu_only() -> None:
    groups = [_make_group("lcg-cpu", "CPU Pool")]
    prices = {
        "lcg-cpu": [_make_price(quota_id="q-cpu", gpu=0, cpu=4, mem=32, gpu_type="")],
    }
    result = resolve_quota(
        spec=QuotaSpec(0, 4, 32),
        workspace_id="ws-cpu",
        groups=groups,
        prices_loader=lambda lcg: prices.get(lcg, []),
    )
    assert result.gpu_count == 0
    assert result.gpu_type == ""
    assert result.quota_id == "q-cpu"


def test_resolve_quota_empty_workspace_raises() -> None:
    with pytest.raises(QuotaMatchError):
        resolve_quota(
            spec=QuotaSpec(1, 20, 200),
            workspace_id="ws-empty",
            groups=[],
            prices_loader=lambda lcg: [],
        )


def test_resolve_quota_reports_an_unreadable_group_instead_of_guessing() -> None:
    """One unreadable group invalidates the whole match, not just its rows.

    The unread group could have held the same triple, which is the case that
    requires `--group` to disambiguate. Answering from the groups that did
    reply would send the workload somewhere the user never chose.
    """
    groups = [
        _make_group("lcg-broken", "Broken"),
        _make_group("lcg-ok", "OK"),
    ]

    def loader(lcg: str) -> list[dict]:
        if lcg == "lcg-broken":
            raise RuntimeError("API returned 429: Too Many Requests")
        return [_make_price(quota_id="q-ok", gpu=1, cpu=20, mem=200, gpu_type="H200")]

    with pytest.raises(QuotaCatalogUnavailable) as excinfo:
        resolve_quota(
            spec=QuotaSpec(1, 20, 200),
            workspace_id="ws-1",
            groups=groups,
            prices_loader=loader,
        )

    message = str(excinfo.value)
    assert "'Broken'" in message
    assert "not a workspace without quotas" in message


def test_resolve_quota_errors_no_longer_guess_from_compute_group_names() -> None:
    """Scheduling behaviour is read from the platform, never from a group name.

    The zone hint these messages used to carry inferred "this quota needs LOW
    priority" from the characters 训练区 in a compute group name. The platform
    publishes the same fact per quota row, so the guess is gone and must not
    come back through an error string.
    """
    groups = [
        _make_group("lcg-dev", "开发区-H100"),
        _make_group("lcg-train", "训练区-H200"),
    ]
    prices = {
        "lcg-dev": [
            _make_price(quota_id="q-dev", gpu=8, cpu=160, mem=1800, gpu_type="H100")
        ],
        "lcg-train": [
            _make_price(quota_id="q-train", gpu=8, cpu=160, mem=1800, gpu_type="H200")
        ],
    }

    with pytest.raises(QuotaMatchError) as exact_group_miss:
        resolve_quota(
            spec=QuotaSpec(4, 55, 900),
            workspace_id="ws-qz",
            groups=groups,
            prices_loader=lambda lcg: [],
            group_override="训练区",
        )
    with pytest.raises(QuotaMatchError) as no_match:
        resolve_quota(
            spec=QuotaSpec(4, 55, 900),
            workspace_id="ws-qz",
            groups=groups,
            prices_loader=lambda lcg: prices.get(lcg, []),
            group_override="训练区-H200",
        )
    with pytest.raises(QuotaMatchError) as multi_match:
        resolve_quota(
            spec=QuotaSpec(8, 160, 1800),
            workspace_id="ws-qz",
            groups=groups,
            prices_loader=lambda lcg: prices.get(lcg, []),
        )

    assert "No compute group name exactly matches --group" in str(exact_group_miss.value)
    assert "matches no quota row" in str(no_match.value)
    assert "matches multiple quota rows" in str(multi_match.value)
    for excinfo in (exact_group_miss, no_match, multi_match):
        assert "QZ scheduling zones" not in str(excinfo.value)
        assert "LOW priority" not in str(excinfo.value)


def _resolve_with_menu(
    menu: dict[str, tuple[str, ...]] | None,
    *,
    schedule_config_type: str = SCHEDULE_TYPE_TRAIN,
) -> ResolvedQuota:
    return resolve_quota(
        spec=QuotaSpec(1, 20, 200),
        workspace_id="ws-1",
        schedule_config_type=schedule_config_type,
        groups=[_make_group("lcg-a", "训练区-H200-1号机房")],
        prices_loader=lambda lcg: [
            _make_price(quota_id="q-1", gpu=1, cpu=20, mem=200, gpu_type="H200")
        ],
        priority_levels_loader=lambda: menu,
    )


def test_resolve_quota_carries_the_published_priority_restriction() -> None:
    assert _resolve_with_menu({"q-1": ("low",)}).allowed_priority_levels == ("low",)


def test_resolve_quota_reads_an_empty_menu_entry_as_unrestricted() -> None:
    assert _resolve_with_menu({"q-1": ()}).allowed_priority_levels == ()


def test_resolve_quota_reads_an_unreadable_menu_as_unknown() -> None:
    # `None` is the loader saying the platform did not answer. Rendering that
    # as `()` would turn one failed request into "no restriction".
    assert _resolve_with_menu(None).allowed_priority_levels is None


def test_resolve_quota_reads_a_spec_missing_from_the_menu_as_unknown() -> None:
    assert _resolve_with_menu({"q-other": ("low",)}).allowed_priority_levels is None


def test_resolve_quota_treats_menuless_workloads_as_unrestricted() -> None:
    # HPC has no spec menu anywhere in the scheduling record, so there is
    # nothing that could restrict it -- that is a platform fact, not a failure.
    resolved = _resolve_with_menu(None, schedule_config_type=SCHEDULE_TYPE_HPC)
    assert resolved.allowed_priority_levels == ()


def test_load_quota_priority_levels_answers_unknown_when_the_platform_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _boom(**_kwargs: object) -> dict[str, tuple[str, ...]]:
        raise ValueError("API error: RateLimit")

    monkeypatch.setattr(
        quota_resolver_module.browser_api_module,
        "get_quota_priority_levels",
        _boom,
        raising=False,
    )

    assert (
        load_quota_priority_levels(
            workspace_id="ws-1",
            session=_cached_group_session(),
            workload="job",
        )
        is None
    )


def test_load_quota_priority_levels_skips_workloads_without_a_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _never(**_kwargs: object) -> dict[str, tuple[str, ...]]:
        raise AssertionError("HPC has no spec menu, so it must not be requested")

    monkeypatch.setattr(
        quota_resolver_module.browser_api_module,
        "get_quota_priority_levels",
        _never,
        raising=False,
    )

    assert (
        load_quota_priority_levels(
            workspace_id="ws-1",
            session=_cached_group_session(),
            workload="hpc",
        )
        is None
    )


def test_allowed_priority_levels_for_separates_silence_from_permission() -> None:
    assert allowed_priority_levels_for({"q-1": ("low",)}, "q-1", workload="job") == ("low",)
    assert allowed_priority_levels_for({"q-1": ()}, "q-1", workload="job") == ()
    assert allowed_priority_levels_for(None, "q-1", workload="job") is None
    assert allowed_priority_levels_for({}, "q-1", workload="job") is None
    assert allowed_priority_levels_for(None, "q-1", workload="hpc") == ()


def test_describe_priority_levels_never_calls_unknown_unrestricted() -> None:
    assert describe_priority_levels(None) == "unknown"
    assert describe_priority_levels(()) == "any"
    assert describe_priority_levels(("low",)) == "low"
    assert describe_priority_levels(("high", "low")) == "high/low"


def _quota_with_levels(levels: tuple[str, ...] | None) -> ResolvedQuota:
    return ResolvedQuota(
        quota_id="q-1",
        logic_compute_group_id="lcg-a",
        compute_group_name="训练区-H200-1号机房",
        gpu_count=1,
        cpu_count=20,
        memory_gib=200,
        gpu_type="H200",
        raw_price={},
        allowed_priority_levels=levels,
    )


def test_ensure_priority_allowed_blocks_a_create_the_platform_would_reject() -> None:
    with pytest.raises(QuotaMatchError) as excinfo:
        ensure_priority_allowed(
            _quota_with_levels(("low",)), 4, quota_command="inspire job quota"
        )

    message = str(excinfo.value)
    assert "1,20,200" in message
    assert "LOW-priority only" in message
    assert "--priority 4 is HIGH" in message
    assert "--priority 1" in message
    assert "inspire job quota" in message


def test_ensure_priority_allowed_accepts_the_published_level() -> None:
    ensure_priority_allowed(_quota_with_levels(("low",)), 1)


@pytest.mark.parametrize("levels", [None, (), ("emergency",)])
def test_ensure_priority_allowed_never_blocks_on_silence_or_novelty(
    levels: tuple[str, ...] | None,
) -> None:
    # Unknown, unrestricted, and a vocabulary this CLI has never seen all pass:
    # a create must not fail because one read failed or the platform grew a
    # level we cannot interpret.
    ensure_priority_allowed(_quota_with_levels(levels), 4)


def test_build_resource_spec_price_shape() -> None:
    quota = ResolvedQuota(
        quota_id="q-1",
        logic_compute_group_id="lcg-1",
        compute_group_name="H200 Group",
        gpu_count=1,
        cpu_count=20,
        memory_gib=200,
        gpu_type="NVIDIA H200 (141GB)",
        raw_price={
            "cpu_info": {"cpu_type": "Intel Xeon"},
            "cpu_price_id": "rpc-cpu",
            "cpu_price_version_id": 1,
            "gpu_info": {
                "gpu_type": "NVIDIA_H200_SXM_141G",
                "gpu_type_display": "NVIDIA H200 (141GB)",
            },
            "gpu_price_id": "rpc-gpu",
            "gpu_price_version_id": 1,
            "memory_price_id": "rpc-memory",
            "memory_price_version_id": 1,
            "total_price_per_hour": 1,
        },
    )
    payload = build_resource_spec_price(quota=quota)
    assert payload == {
        "cpu_type": "Intel Xeon",
        "cpu_count": 20,
        "gpu_type": "NVIDIA_H200_SXM_141G",
        "gpu_count": 1,
        "memory_size_gib": 200,
        "logic_compute_group_id": "lcg-1",
        "quota_id": "q-1",
    }


def test_build_resource_spec_price_requires_machine_gpu_type() -> None:
    quota = ResolvedQuota(
        quota_id="q-1",
        logic_compute_group_id="lcg-1",
        compute_group_name="H200 Group",
        gpu_count=1,
        cpu_count=20,
        memory_gib=200,
        gpu_type="NVIDIA H200 (141GB)",
        raw_price={"cpu_info": {"cpu_type": "Intel Xeon"}},
    )

    with pytest.raises(QuotaMatchError, match="machine-readable gpu_info.gpu_type"):
        build_resource_spec_price(quota=quota)


def test_build_resource_spec_price_cpu_only_omits_gpu_type() -> None:
    quota = ResolvedQuota(
        quota_id="q-cpu",
        logic_compute_group_id="lcg-cpu",
        compute_group_name="CPU Group",
        gpu_count=0,
        cpu_count=4,
        memory_gib=32,
        gpu_type="",
        raw_price={"cpu_info": {"cpu_type": "Intel Xeon"}},
    )

    payload = build_resource_spec_price(quota=quota)
    assert "gpu_type" not in payload
    assert payload["cpu_count"] == 4
    assert payload["quota_id"] == "q-cpu"
