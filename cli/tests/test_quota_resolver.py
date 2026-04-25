from __future__ import annotations

import pytest

from inspire.cli.utils.quota_resolver import (
    QuotaMatchError,
    QuotaParseError,
    QuotaSpec,
    ResolvedQuota,
    build_resource_spec_price,
    parse_quota,
    resolve_quota,
)


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
    assert "matches no spec" in message
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
    assert "pass --group" in str(exc.value)
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


def test_resolve_quota_group_override_partial_match() -> None:
    groups = [
        _make_group("lcg-a", "H100 Group"),
        _make_group("lcg-b", "H200 Group 2"),
    ]
    prices = {
        "lcg-b": [_make_price(quota_id="q-200", gpu=1, cpu=20, mem=200, gpu_type="H200")],
    }
    result = resolve_quota(
        spec=QuotaSpec(1, 20, 200),
        workspace_id="ws-1",
        groups=groups,
        prices_loader=lambda lcg: prices.get(lcg, []),
        group_override="h200",
    )
    assert result.quota_id == "q-200"


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
    assert "No compute group name matches" in str(exc.value)


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


def test_resolve_quota_swallows_price_loader_errors() -> None:
    groups = [
        _make_group("lcg-broken", "Broken"),
        _make_group("lcg-ok", "OK"),
    ]

    def loader(lcg: str) -> list[dict]:
        if lcg == "lcg-broken":
            raise RuntimeError("transient")
        return [_make_price(quota_id="q-ok", gpu=1, cpu=20, mem=200, gpu_type="H200")]

    result = resolve_quota(
        spec=QuotaSpec(1, 20, 200),
        workspace_id="ws-1",
        groups=groups,
        prices_loader=loader,
    )
    assert result.quota_id == "q-ok"


def test_build_resource_spec_price_shape() -> None:
    quota = ResolvedQuota(
        quota_id="q-1",
        logic_compute_group_id="lcg-1",
        compute_group_name="H200 Group",
        gpu_count=1,
        cpu_count=20,
        memory_gib=200,
        gpu_type="H200",
        raw_price={"cpu_info": {"cpu_type": "Intel Xeon"}},
    )
    payload = build_resource_spec_price(quota=quota)
    assert payload == {
        "cpu_type": "Intel Xeon",
        "cpu_count": 20,
        "gpu_type": "H200",
        "gpu_count": 1,
        "memory_size_gib": 200,
        "logic_compute_group_id": "lcg-1",
        "quota_id": "q-1",
    }
