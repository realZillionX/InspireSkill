"""rtunnel binary helpers for SSH ProxyCommand access."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

from inspire.config.ssh_runtime import (
    DEFAULT_RTUNNEL_DOWNLOAD_URL as CONFIG_DEFAULT_RTUNNEL_DOWNLOAD_URL,
    resolve_ssh_runtime_config,
)

from .config import load_tunnel_config
from .models import TunnelConfig, TunnelError

# nightly release includes stdio:// mode for SSH ProxyCommand support
DEFAULT_RTUNNEL_DOWNLOAD_URL = CONFIG_DEFAULT_RTUNNEL_DOWNLOAD_URL


def _is_rtunnel_binary_usable(path: Path) -> bool:
    """Return True when *path* exists, is executable, and can be launched."""
    if not path.exists() or not os.access(path, os.X_OK):
        return False

    try:
        # `--help` is a lightweight health check and catches exec-format errors
        # while rejecting trivial executable stubs that merely exit non-zero.
        result = subprocess.run(
            [str(path), "--help"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return False

    return result.returncode == 0


def _get_rtunnel_download_url() -> str:
    """Get the rtunnel download URL from resolved SSH runtime config.

    Returns:
        Download URL for rtunnel binary
    """
    try:
        return resolve_ssh_runtime_config().rtunnel_download_url
    except Exception:
        pass

    # Use default
    return DEFAULT_RTUNNEL_DOWNLOAD_URL


def _ensure_rtunnel_binary(config: TunnelConfig) -> Path:
    """Ensure rtunnel binary exists, download if needed."""
    if _is_rtunnel_binary_usable(config.rtunnel_bin):
        return config.rtunnel_bin

    # Delete stale binary before re-download
    if config.rtunnel_bin.exists():
        config.rtunnel_bin.unlink(missing_ok=True)

    # Download rtunnel
    config.rtunnel_bin.parent.mkdir(parents=True, exist_ok=True)

    try:
        import tarfile
        import tempfile
        import urllib.request
        import zipfile

        download_url = _get_rtunnel_download_url()
        archive_suffix = ".zip" if download_url.lower().endswith(".zip") else ".tar.gz"
        # Download tar.gz and extract
        with tempfile.NamedTemporaryFile(suffix=archive_suffix, delete=False) as tmp:
            urllib.request.urlretrieve(download_url, tmp.name)

            if archive_suffix == ".zip":
                with zipfile.ZipFile(tmp.name) as archive:
                    for member in archive.namelist():
                        if member.endswith("/") or "rtunnel" not in member:
                            continue
                        config.rtunnel_bin.write_bytes(archive.read(member))
                        config.rtunnel_bin.chmod(0o755)
                        break
            else:
                with tarfile.open(tmp.name, "r:gz") as tar:
                    # Extract the rtunnel binary (should be the only file or named rtunnel*)
                    for member in tar.getmembers():
                        if member.isfile() and "rtunnel" in member.name:
                            # Extract to a temp location first
                            extracted = tar.extractfile(member)
                            if extracted:
                                config.rtunnel_bin.write_bytes(extracted.read())
                                config.rtunnel_bin.chmod(0o755)
                                break
            # Clean up temp file
            Path(tmp.name).unlink(missing_ok=True)

        if not _is_rtunnel_binary_usable(config.rtunnel_bin):
            raise TunnelError("rtunnel binary missing or unusable after download")

        return config.rtunnel_bin
    except Exception as e:
        raise TunnelError(f"Failed to download rtunnel: {e}")


def get_rtunnel_path(config: Optional[TunnelConfig] = None) -> Path:
    """Get rtunnel binary path, downloading if needed.

    Args:
        config: Tunnel configuration

    Returns:
        Path to rtunnel binary

    Raises:
        TunnelError: If rtunnel cannot be found or downloaded
    """
    if config is None:
        config = load_tunnel_config()
    return _ensure_rtunnel_binary(config)
