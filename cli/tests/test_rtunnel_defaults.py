"""Tests for platform-aware rtunnel download URL defaults."""

from __future__ import annotations

from pathlib import Path

import pytest

from inspire.config.rtunnel_defaults import (
    RTUNNEL_RELEASE_BASE_URL,
    _normalize_arch,
    _normalize_os,
    default_rtunnel_download_url,
    rtunnel_download_url_shell_snippet,
)


class TestNormalizeArch:
    def test_arm64(self) -> None:
        assert _normalize_arch("arm64") == "arm64"

    def test_aarch64(self) -> None:
        assert _normalize_arch("aarch64") == "arm64"

    def test_x86_64(self) -> None:
        assert _normalize_arch("x86_64") == "amd64"

    def test_amd64(self) -> None:
        assert _normalize_arch("AMD64") == "amd64"


class TestNormalizeOs:
    def test_darwin(self) -> None:
        assert _normalize_os("Darwin") == "darwin"

    def test_linux(self) -> None:
        assert _normalize_os("Linux") == "linux"

    def test_windows(self) -> None:
        assert _normalize_os("Windows") == "windows"


class TestDefaultRtunnelDownloadUrl:
    def test_linux_amd64(self) -> None:
        assert default_rtunnel_download_url(system="Linux", machine="x86_64") == (
            f"{RTUNNEL_RELEASE_BASE_URL}/rtunnel-linux-amd64.tar.gz"
        )

    def test_darwin_arm64(self) -> None:
        assert default_rtunnel_download_url(system="Darwin", machine="arm64") == (
            f"{RTUNNEL_RELEASE_BASE_URL}/rtunnel-darwin-arm64.tar.gz"
        )

    def test_linux_arm64(self) -> None:
        assert default_rtunnel_download_url(system="Linux", machine="aarch64") == (
            f"{RTUNNEL_RELEASE_BASE_URL}/rtunnel-linux-arm64.tar.gz"
        )

    def test_windows_arm64(self) -> None:
        assert default_rtunnel_download_url(system="Windows", machine="aarch64") == (
            f"{RTUNNEL_RELEASE_BASE_URL}/rtunnel-windows-arm64.zip"
        )


class TestShellSnippet:
    def test_contains_uname(self) -> None:
        snippet = rtunnel_download_url_shell_snippet()
        assert "uname -s" in snippet
        assert "uname -m" in snippet

    def test_sets_rtunnel_download_url(self) -> None:
        snippet = rtunnel_download_url_shell_snippet()
        assert "RTUNNEL_DOWNLOAD_URL=" in snippet

    def test_contains_base_url(self) -> None:
        snippet = rtunnel_download_url_shell_snippet()
        assert RTUNNEL_RELEASE_BASE_URL in snippet

    def test_handles_arch_mapping(self) -> None:
        snippet = rtunnel_download_url_shell_snippet()
        assert "arm64" in snippet
        assert "aarch64" in snippet
        assert "amd64" in snippet


class TestIsRtunnelBinaryUsable:
    def test_nonexistent_path(self, tmp_path: Path) -> None:
        from inspire.bridge.tunnel.rtunnel import _is_rtunnel_binary_usable

        assert _is_rtunnel_binary_usable(tmp_path / "no-such-file") is False

    def test_not_executable(self, tmp_path: Path) -> None:
        from inspire.bridge.tunnel.rtunnel import _is_rtunnel_binary_usable

        fake = tmp_path / "rtunnel"
        fake.write_text("not a binary")
        fake.chmod(0o644)
        assert _is_rtunnel_binary_usable(fake) is False

    def test_invalid_binary(self, tmp_path: Path) -> None:
        from inspire.bridge.tunnel.rtunnel import _is_rtunnel_binary_usable

        fake = tmp_path / "rtunnel"
        fake.write_text("#!/bin/sh\nexit 1\n")
        fake.chmod(0o755)
        assert _is_rtunnel_binary_usable(fake) is False


@pytest.mark.integration
def test_rtunnel_download_url_is_reachable() -> None:
    """Verify the default download URL returns HTTP 200 for the current platform."""
    import urllib.request

    url = default_rtunnel_download_url()
    req = urllib.request.Request(url, method="HEAD")
    resp = urllib.request.urlopen(req, timeout=10)
    assert resp.status == 200
