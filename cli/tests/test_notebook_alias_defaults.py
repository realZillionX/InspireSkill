"""Tests for name-based default alias derivation in notebook ssh bootstrap.

Covers the helpers that turn a notebook's display name into an alias-safe
token and arbitrate collisions against pre-existing cached bridges.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from inspire.cli.commands.notebook.notebook_ssh_flow import (
    _default_alias_for_notebook,
    _find_alias_for_notebook_id,
    _sanitize_alias_from_name,
    _unique_alias_for_notebook,
)


# ---------------------------------------------------------------------------
# _sanitize_alias_from_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Already alias-safe
        ("nlp-preprocess", "nlp-preprocess"),
        ("train_v1", "train_v1"),
        ("exp.2026.04", "exp.2026.04"),
        # Case folded so ssh_config lookups are forgiving
        ("Train-V1", "train-v1"),
        # Spaces → dashes, collapsed
        ("model preprocessing run", "model-preprocessing-run"),
        ("  too   many   spaces  ", "too-many-spaces"),
        # CJK and emoji fully replaced; remaining dashes collapsed/trimmed
        ("测试 notebook 🚀", "notebook"),
        # Other punctuation becomes dashes
        ("exp/v1#prod", "exp-v1-prod"),
        # Empty / whitespace / purely non-ASCII → "" so callers fall back
        ("", ""),
        ("   ", ""),
        ("🔥🔥🔥", ""),
        # Too short after sanitising → ""
        ("a", ""),
        # Leading/trailing punctuation trimmed
        ("--train--", "train"),
        (".hidden.", "hidden"),
    ],
)
def test_sanitize_alias_from_name(raw: str, expected: str) -> None:
    assert _sanitize_alias_from_name(raw) == expected


# ---------------------------------------------------------------------------
# _find_alias_for_notebook_id
# ---------------------------------------------------------------------------


@dataclass
class _FakeBridge:
    notebook_id: str = ""
    notebook_name: str | None = None


@dataclass
class _FakeTunnelConfig:
    bridges: dict


def test_find_alias_for_notebook_id_returns_existing_alias() -> None:
    cfg = _FakeTunnelConfig(
        bridges={
            "nb-abcd1234": _FakeBridge(notebook_id="notebook-abcd1234ffff"),
            "unrelated": _FakeBridge(notebook_id="notebook-ffff0000aaaa"),
        }
    )
    assert (
        _find_alias_for_notebook_id(cfg, "notebook-abcd1234ffff") == "nb-abcd1234"
    )


def test_find_alias_for_notebook_id_returns_none_when_no_match() -> None:
    cfg = _FakeTunnelConfig(
        bridges={"other": _FakeBridge(notebook_id="notebook-other")}
    )
    assert _find_alias_for_notebook_id(cfg, "notebook-missing") is None


def test_find_alias_for_notebook_id_handles_empty_input() -> None:
    cfg = _FakeTunnelConfig(bridges={})
    assert _find_alias_for_notebook_id(cfg, "") is None


# ---------------------------------------------------------------------------
# _unique_alias_for_notebook
# ---------------------------------------------------------------------------


def test_unique_alias_returns_base_when_not_cached() -> None:
    cfg = _FakeTunnelConfig(bridges={})
    assert (
        _unique_alias_for_notebook(
            cfg, base="train-v1", notebook_id="notebook-aaaa1111"
        )
        == "train-v1"
    )


def test_unique_alias_returns_base_when_existing_bridge_is_same_notebook() -> None:
    # Re-running `inspire notebook ssh <id>` after an alias was already cached
    # must not spuriously rename it.
    cfg = _FakeTunnelConfig(
        bridges={
            "train-v1": _FakeBridge(notebook_id="notebook-aaaa1111"),
        }
    )
    assert (
        _unique_alias_for_notebook(
            cfg, base="train-v1", notebook_id="notebook-aaaa1111"
        )
        == "train-v1"
    )


def test_unique_alias_adds_suffix_on_cross_notebook_collision() -> None:
    # Two notebooks sharing the same display name must not clobber each other.
    cfg = _FakeTunnelConfig(
        bridges={
            "train-v1": _FakeBridge(notebook_id="notebook-aaaa1111"),
        }
    )
    result = _unique_alias_for_notebook(
        cfg, base="train-v1", notebook_id="notebook-bbbb2222"
    )
    assert result.startswith("train-v1-")
    assert result != "train-v1"
    # Suffix comes from the notebook id and is compact enough to be readable.
    assert len(result) <= len("train-v1-") + 4


# ---------------------------------------------------------------------------
# _default_alias_for_notebook (end-to-end)
# ---------------------------------------------------------------------------


def test_default_alias_prefers_sanitised_display_name() -> None:
    cfg = _FakeTunnelConfig(bridges={})
    assert (
        _default_alias_for_notebook(
            cfg, notebook_id="notebook-aaaa1111", notebook_name="Train V1 run"
        )
        == "train-v1-run"
    )


def test_default_alias_falls_back_to_nb_prefix_when_name_unusable() -> None:
    cfg = _FakeTunnelConfig(bridges={})
    assert (
        _default_alias_for_notebook(
            cfg, notebook_id="notebook-aaaa1111ffff", notebook_name="🚀"
        )
        == "nb-notebook"
    )


def test_default_alias_falls_back_to_nb_prefix_when_name_missing() -> None:
    cfg = _FakeTunnelConfig(bridges={})
    assert (
        _default_alias_for_notebook(
            cfg, notebook_id="notebook-aaaa1111ffff", notebook_name=""
        )
        == "nb-notebook"
    )
    assert (
        _default_alias_for_notebook(
            cfg, notebook_id="abcd1234", notebook_name=None
        )
        == "nb-abcd1234"
    )


def test_default_alias_preserves_when_cached_for_same_notebook() -> None:
    cfg = _FakeTunnelConfig(
        bridges={"train-v1": _FakeBridge(notebook_id="notebook-aaaa1111")}
    )
    assert (
        _default_alias_for_notebook(
            cfg, notebook_id="notebook-aaaa1111", notebook_name="train v1"
        )
        == "train-v1"
    )


def test_default_alias_deconflicts_against_unrelated_bridge() -> None:
    cfg = _FakeTunnelConfig(
        bridges={"train-v1": _FakeBridge(notebook_id="notebook-aaaa1111")}
    )
    result = _default_alias_for_notebook(
        cfg, notebook_id="notebook-bbbb2222", notebook_name="Train V1"
    )
    assert result != "train-v1"
    assert result.startswith("train-v1-")
