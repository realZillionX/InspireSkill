"""Shared pytest fixtures for the v2 CLI.

The CLI's resolvers reject platform ids at the user boundary (v2.0.0
breaking change — only names cross the user / agent boundary). The test
suite still exercises internal code paths by pre-resolving to a full
platform id; this autouse fixture short-circuits the name→id lookup so
existing tests keep passing against id-shaped fixtures without network
calls while production code still enforces the no-id contract.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _short_circuit_platform_resolvers(monkeypatch):  # noqa: ANN001
    """Pass any id-like argument through resolvers untouched for tests.

    Production `resolve_job_id` etc. reject platform ids and force a
    name lookup; here we let ids through unchanged so id-seeded fixtures
    keep working. Real name→id resolution is covered by unit tests of
    `resolve_by_name` / `resolve_job_id` that mock the list API directly.
    """
    def _passthrough(ctx, arg):  # noqa: ANN001
        return arg

    import importlib

    for mod_name in (
        "inspire.cli.utils.job_cli",
        "inspire.cli.commands.job.job_commands",
        "inspire.cli.commands.job.job_events",
        "inspire.cli.commands.job.job_logs",
    ):
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:  # pragma: no cover
            continue
        if hasattr(mod, "resolve_job_id"):
            monkeypatch.setattr(mod, "resolve_job_id", _passthrough)
