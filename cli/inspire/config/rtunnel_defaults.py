"""Platform-aware defaults for rtunnel client binaries."""

from __future__ import annotations

import platform

RTUNNEL_RELEASE_BASE_URL = "https://github.com/Sarfflow/rtunnel/releases/download/nightly"


def _normalize_arch(machine: str) -> str:
    machine_norm = str(machine or "").strip().lower()
    if machine_norm in {"arm64", "aarch64"}:
        return "arm64"
    return "amd64"


def _normalize_os(system: str) -> str:
    system_norm = str(system or "").strip().lower()
    if system_norm.startswith("darwin"):
        return "darwin"
    if system_norm.startswith("linux"):
        return "linux"
    if system_norm.startswith(("windows", "mingw", "msys", "cygwin")):
        return "windows"
    # Fallback keeps existing Linux behavior for unknown hosts.
    return "linux"


def default_rtunnel_download_url(*, system: str | None = None, machine: str | None = None) -> str:
    """Return the default nightly rtunnel archive URL for the local platform."""
    os_part = _normalize_os(system or platform.system())
    arch_part = _normalize_arch(machine or platform.machine())
    ext = "zip" if os_part == "windows" else "tar.gz"
    return f"{RTUNNEL_RELEASE_BASE_URL}/rtunnel-{os_part}-{arch_part}.{ext}"


def rtunnel_download_url_shell_snippet() -> str:
    """Return a shell expression that computes the correct rtunnel download URL
    at runtime on the **remote** machine (container), using ``uname`` instead of
    the local Python ``platform`` module.

    This is critical when the local machine (e.g. macOS ARM64) differs from the
    remote container (e.g. Linux x86_64).  The returned string is a shell
    expression suitable for embedding in ``$( ... )`` or direct variable
    assignment.
    """
    # The shell snippet normalises uname output the same way the Python helpers
    # do, then constructs the URL from RTUNNEL_RELEASE_BASE_URL.
    return (
        f'_RT_OS=$(uname -s 2>/dev/null | tr "[:upper:]" "[:lower:]"); '
        f"_RT_ARCH=$(uname -m 2>/dev/null); "
        f'case "$_RT_OS" in darwin*) _RT_OS=darwin;; linux*) _RT_OS=linux;; *) _RT_OS=linux;; esac; '
        f'case "$_RT_ARCH" in arm64|aarch64) _RT_ARCH=arm64;; *) _RT_ARCH=amd64;; esac; '
        f'_RT_EXT=tar.gz; [ "$_RT_OS" = "windows" ] && _RT_EXT=zip; '
        f'RTUNNEL_DOWNLOAD_URL="{RTUNNEL_RELEASE_BASE_URL}/rtunnel-${{_RT_OS}}-${{_RT_ARCH}}.${{_RT_EXT}}"'
    )


__all__ = [
    "RTUNNEL_RELEASE_BASE_URL",
    "default_rtunnel_download_url",
    "rtunnel_download_url_shell_snippet",
]
