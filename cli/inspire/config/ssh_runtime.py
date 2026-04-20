"""Runtime SSH option resolution for notebook and tunnel flows."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

from inspire.config.models import Config, ConfigError
from inspire.config.rtunnel_defaults import default_rtunnel_download_url
from inspire.config.schema_models import _parse_upload_policy

DEFAULT_RTUNNEL_DOWNLOAD_URL = default_rtunnel_download_url()


@dataclass(frozen=True)
class SshRuntimeConfig:
    """Resolved SSH runtime options used by notebook and tunnel workflows."""

    rtunnel_bin: Optional[str] = None
    sshd_deb_dir: Optional[str] = None
    dropbear_deb_dir: Optional[str] = None
    setup_script: Optional[str] = None
    rtunnel_download_url: str = DEFAULT_RTUNNEL_DOWNLOAD_URL
    apt_mirror_url: Optional[str] = None
    rtunnel_upload_policy: str = "auto"


def resolve_ssh_runtime_config(
    *,
    cli_overrides: Optional[Mapping[str, Optional[str]]] = None,
) -> SshRuntimeConfig:
    """Resolve SSH runtime configuration with layered precedence.

    The base resolution is delegated to Config.from_files_and_env(), which already applies
    cli.prefer_source behavior between project TOML and environment variables.
    CLI overrides are then applied as the highest-priority layer.
    """
    config, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)

    values: dict[str, Optional[str]] = {
        "rtunnel_bin": config.rtunnel_bin,
        "sshd_deb_dir": config.sshd_deb_dir,
        "dropbear_deb_dir": config.dropbear_deb_dir,
        "setup_script": config.setup_script,
        "rtunnel_download_url": config.rtunnel_download_url or DEFAULT_RTUNNEL_DOWNLOAD_URL,
        "apt_mirror_url": config.apt_mirror_url,
        "rtunnel_upload_policy": config.rtunnel_upload_policy,
    }

    if cli_overrides:
        for key in values:
            override = cli_overrides.get(key)
            if override is not None:
                values[key] = override

    download_url = values["rtunnel_download_url"] or DEFAULT_RTUNNEL_DOWNLOAD_URL
    try:
        upload_policy = _parse_upload_policy(str(values["rtunnel_upload_policy"] or "auto"))
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc

    return SshRuntimeConfig(
        rtunnel_bin=values["rtunnel_bin"],
        sshd_deb_dir=values["sshd_deb_dir"],
        dropbear_deb_dir=values["dropbear_deb_dir"],
        setup_script=values["setup_script"],
        rtunnel_download_url=download_url,
        apt_mirror_url=values["apt_mirror_url"],
        rtunnel_upload_policy=upload_policy,
    )


__all__ = [
    "DEFAULT_RTUNNEL_DOWNLOAD_URL",
    "SshRuntimeConfig",
    "resolve_ssh_runtime_config",
]
