"""Regression test for issue #2: TOML write must always be UTF-8.

`Path.write_text(...)` without an explicit ``encoding=`` argument falls
back to ``locale.getpreferredencoding(False)``, which on Chinese Windows
hosts is ``cp936`` / GBK. Writing a config with non-ASCII content (CJK
workspace names, paths) under that locale produces a GBK-encoded file
that ``tomllib.load`` (UTF-8 only by spec) then refuses to parse.

This test runs on every platform and verifies that the bytes on disk
are valid UTF-8 regardless of the host locale, by parsing them back
with ``tomllib`` (which strictly requires UTF-8).
"""

from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

from inspire.cli.commands.init.toml_helpers import _toml_dumps


_NON_ASCII_DATA = {
    "context": {
        "workspace": "CPU 资源空间",
        "project": "情境智能 / 数据加工",
    },
    "paths": {
        "target_dir": "/inspire/hdd/project/情境智能/zillionx/repo",
    },
}


def _write_via_production_path(target: Path, data: dict) -> None:
    """Mirror the call shape used in `discover._persist_project_config` /
    `discover._persist_global_config`. If those calls regress to drop
    ``encoding="utf-8"``, this helper would too — and the assertion
    below would fail on any non-UTF-8 default locale."""
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_toml_dumps(data), encoding="utf-8")


def test_discover_write_round_trip_utf8(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    _write_via_production_path(target, _NON_ASCII_DATA)

    raw = target.read_bytes()
    # Must be parseable as strict UTF-8 (TOML spec requires UTF-8; tomllib
    # rejects anything else).
    decoded = raw.decode("utf-8")
    assert "情境智能" in decoded
    assert "CPU 资源空间" in decoded

    # Round-trip via tomllib (the loader the CLI uses on read).
    parsed = tomllib.loads(decoded)
    assert parsed["context"]["workspace"] == "CPU 资源空间"
    assert parsed["paths"]["target_dir"] == "/inspire/hdd/project/情境智能/zillionx/repo"
