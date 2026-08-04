from __future__ import annotations

import pytest

from inspire.cli.utils.collection_output import (
    DEFAULT_COLLECTION_LIMIT,
    bound_collection,
    resolve_collection_limit,
    truncation_notice,
)


def test_resolve_collection_limit_defaults_to_compact_budget() -> None:
    assert (
        resolve_collection_limit(limit=None, show_all=False)
        == DEFAULT_COLLECTION_LIMIT
    )


def test_resolve_collection_limit_all_is_unbounded() -> None:
    assert resolve_collection_limit(limit=None, show_all=True) is None


def test_resolve_collection_limit_rejects_limit_with_all() -> None:
    with pytest.raises(ValueError, match="either --limit or --all"):
        resolve_collection_limit(limit=3, show_all=True)


def test_bound_collection_reports_truncation_with_known_total() -> None:
    page = bound_collection(["a", "b"], limit=2, total=9)

    assert page.items == ["a", "b"]
    assert page.metadata() == {
        "shown": 2,
        "total": 9,
        "truncated": True,
    }
    assert truncation_notice(page) == "Showing 2 of 9. Use --all for the full list."


def test_bound_collection_uses_materialized_length_without_server_total() -> None:
    page = bound_collection(["a", "b", "c"], limit=2)

    assert page.items == ["a", "b"]
    assert page.total == 3
    assert page.truncated is True


def test_unbounded_collection_has_no_notice() -> None:
    page = bound_collection(["a", "b"], limit=None)

    assert page.items == ["a", "b"]
    assert page.metadata() == {}
    assert truncation_notice(page) is None
