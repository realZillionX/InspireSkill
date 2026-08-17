"""A cached quota row belongs to one compute group, not to a spec id.

The platform reuses one ``quota_id`` across every compute group that offers
that shape — measured on 分布式训练空间: 9 groups, 11 distinct ids, 7 of them
shared by 4 to 7 groups. The identity cache's primary key does not include
``owner_id``, so keying a row by the bare ``quota_id`` made each group
overwrite the previous one's row. The stored catalog collapsed from 32 rows
across 8 groups to 11 across 3, and the groups that lost the race disappeared
from ``<workload> quota`` and became unreachable by ``--group`` — while the
platform was still answering for all of them.
"""

from __future__ import annotations

from inspire.cli.utils.quota_cache import quota_records


_SHARED_QUOTA_ID = "7166bd2e-6cbe-4bd9-b000-000000000000"

_PRICE = {
    "quota_id": _SHARED_QUOTA_ID,
    "gpu_count": 8,
    "cpu_count": 160,
    "memory_size_gib": 1800,
}


def _handles(*groups: tuple[str, str]) -> list[str]:
    return [
        record.resource_id
        for group_id, group_name in groups
        for record in quota_records(
            [_PRICE], logic_compute_group_id=group_id, compute_group_name=group_name
        )
    ]


def test_one_spec_shared_by_two_groups_yields_two_cache_keys() -> None:
    handles = _handles(("lcg-a", "训练区-H200-1号机房"), ("lcg-b", "开发区-H200-3号机房"))

    assert len(set(handles)) == 2, (
        "both groups collapsed onto one cache key, so whichever is written "
        "second erases the first"
    )


def test_the_group_leads_the_cache_key() -> None:
    (handle,) = _handles(("lcg-a", "训练区-H200-1号机房"))

    assert handle.startswith("lcg-a:")
    assert _SHARED_QUOTA_ID in handle


def test_rows_without_a_quota_id_stay_distinct_per_group() -> None:
    price = {"gpu_count": 1, "cpu_count": 20, "memory_size_gib": 200}
    handles = [
        record.resource_id
        for group_id in ("lcg-a", "lcg-b")
        for record in quota_records(
            [price], logic_compute_group_id=group_id, compute_group_name="g"
        )
    ]

    assert len(set(handles)) == 2


def test_the_record_still_carries_the_real_quota_id_in_its_payload() -> None:
    """The create call echoes `quota_id`; it must survive the key change."""
    import json

    (record,) = quota_records(
        [_PRICE], logic_compute_group_id="lcg-a", compute_group_name="g"
    )

    assert json.loads(record.payload)["quota_id"] == _SHARED_QUOTA_ID
    assert record.owner_id == "lcg-a"


def test_distinct_specs_in_one_group_stay_distinct() -> None:
    prices = [
        {"quota_id": "q-1", "gpu_count": 1, "cpu_count": 20, "memory_size_gib": 200},
        {"quota_id": "q-2", "gpu_count": 8, "cpu_count": 160, "memory_size_gib": 1800},
    ]

    records = quota_records(
        prices, logic_compute_group_id="lcg-a", compute_group_name="g"
    )

    assert len({record.resource_id for record in records}) == 2
