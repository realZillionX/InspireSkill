"""Tests for notebook rtunnel verification helpers."""

from __future__ import annotations

import pytest

from inspire.platform.web.browser_api.rtunnel import (
    _is_rtunnel_proxy_ready,
)


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, "SSH-2.0-OpenSSH_9.0", True),
        (200, "", True),
        (500, "upstream error", False),
        (200, "ECONNREFUSED", False),
        (200, "404 page not found", False),
        (200, "<html><title>Jupyter Server</title></html>", False),
        # Plain-text 404 must NOT be treated as ready (could be a platform
        # gateway returning "route not found" for a non-existent proxy path).
        (404, "404 page not found", False),
        (404, "page not found\n", False),
    ],
)
def test_is_rtunnel_proxy_ready(status: int, body: str, expected: bool) -> None:
    assert _is_rtunnel_proxy_ready(status=status, body=body) is expected
