"""`inspire notebook install-deps`: bake hpc/ray runtime deps into a notebook.

Pre-canned, version-pinned installs that mirror what
``docker.sii.shaipower.online/inspire-studio/unified-base:v2`` ships with.
Run inside a notebook you already have an SSH alias to, then ``image save``
to derive a project base image with all the runtimes you need.

Scope (intentionally narrow):

* ``--slurm`` — apt-installs ``slurm-wlm slurm-client munge hwloc libpmix2``
  so ``inspire hpc create`` can land on this image. Slurm config and munge
  key are *not* touched; the platform injects ``/etc/slurm/slurm.conf`` at
  ``hpc create`` time.
* ``--ray`` — pip-installs a version-pinned ``ray`` (default ``2.55.1`` to
  match unified-base:v2). Override with ``--ray-version``.

Distributed-training stacks (deepspeed/accelerate/torch/transformers) are
project-specific; install those with ``inspire notebook exec`` directly.
"""

from __future__ import annotations

from typing import Iterable

import click

from inspire.bridge.tunnel import (
    TunnelConfig,
    load_tunnel_config,
    run_ssh_command_streaming,
)
from inspire.cli.context import Context, EXIT_CONFIG_ERROR, EXIT_GENERAL_ERROR, pass_context
from inspire.cli.utils.errors import exit_with_error as _handle_error

DEFAULT_RAY_VERSION = "2.55.1"
DEFAULT_PIP_INDEX_URL = "https://pypi.tuna.tsinghua.edu.cn/simple"

_SLURM_APT_PACKAGES = (
    "slurm-wlm",
    "slurm-client",
    "munge",
    "hwloc",
    "libpmix2",
)


def _build_slurm_step() -> str:
    pkgs = " ".join(_SLURM_APT_PACKAGES)
    return (
        "set -e; "
        "export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update; "
        f"apt-get install -y --no-install-recommends {pkgs}"
    )


def _build_ray_step(version: str, *, pip_index_url: str) -> str:
    spec = f"ray=={version}" if version else "ray"
    # `--break-system-packages` is required on Ubuntu 24.04+ (PEP 668), which
    # marks the system Python as externally-managed. Our intent is exactly to
    # populate the system site-packages so `image save` bakes ray into a
    # project base image — there's no venv in play.
    index_arg = f' --index-url "{pip_index_url}"' if pip_index_url else ""
    return (
        f"set -e; pip install --upgrade --no-input --break-system-packages{index_arg} "
        f'"{spec}"'
    )


def _resolve_alias(alias: str, tunnel_config: TunnelConfig):
    bridge = tunnel_config.get_bridge(alias)
    if bridge is None:
        raise click.UsageError(
            f"No saved bridge for alias {alias!r}. "
            "Run 'inspire notebook ssh <name> --save-as <alias>' first."
        )
    return bridge


def _run_step(label: str, command: str, *, alias: str, timeout: int) -> int:
    click.echo(f"=== install-deps: {label} ===")
    return run_ssh_command_streaming(
        command=command,
        bridge_name=alias,
        timeout=timeout,
    )


@click.command("install-deps")
@click.argument("alias")
@click.option(
    "--slurm/--no-slurm",
    default=False,
    help=(
        "apt-install the Slurm client + dependencies that match "
        "unified-base:v2. Required for `inspire hpc create` to use the "
        "saved image."
    ),
)
@click.option(
    "--ray/--no-ray",
    default=False,
    help="pip-install ray (version pinned via --ray-version).",
)
@click.option(
    "--ray-version",
    default=DEFAULT_RAY_VERSION,
    show_default=True,
    help="Ray version to install when --ray is set.",
)
@click.option(
    "--pip-index-url",
    default=DEFAULT_PIP_INDEX_URL,
    show_default=True,
    help=(
        "PyPI index for pip steps. Default mirrors what unified-base:v2 ships "
        "with; pass '' to fall back to upstream pypi.org."
    ),
)
@click.option(
    "--timeout",
    type=int,
    default=1800,
    show_default=True,
    help=(
        "Per-step timeout in seconds. ray brings in pyarrow/grpcio/numpy etc. "
        "which can take several minutes even on a fast mirror."
    ),
)
@pass_context
def install_deps_cmd(
    ctx: Context,
    alias: str,
    slurm: bool,
    ray: bool,
    ray_version: str,
    pip_index_url: str,
    timeout: int,
) -> None:
    """One-shot install of hpc/ray runtime deps via an existing SSH alias.

    \b
    Examples:
        inspire notebook install-deps cpu-box --slurm
        inspire notebook install-deps cpu-box --slurm --ray
        inspire notebook install-deps cpu-box --ray --ray-version 2.40.0

    Designed to run once on a fresh notebook before `inspire image save`,
    so the resulting image is ready for `inspire hpc create` /
    `inspire ray create` without further setup.
    """
    if not (slurm or ray):
        raise click.UsageError("Pass at least one of --slurm / --ray.")

    try:
        tunnel_config = load_tunnel_config()
        _resolve_alias(alias, tunnel_config)
    except click.UsageError:
        raise
    except Exception as e:
        _handle_error(ctx, "ConfigError", str(e), EXIT_CONFIG_ERROR)
        return

    steps: list[tuple[str, str]] = []
    if slurm:
        steps.append(("slurm", _build_slurm_step()))
    if ray:
        steps.append(
            (
                f"ray=={ray_version}",
                _build_ray_step(ray_version, pip_index_url=pip_index_url),
            )
        )

    for label, command in steps:
        exit_code = _run_step(label, command, alias=alias, timeout=timeout)
        if exit_code != 0:
            _handle_error(
                ctx,
                "InstallStepFailed",
                f"Step {label!r} failed with exit code {exit_code}.",
                EXIT_GENERAL_ERROR,
                hint=(
                    "Re-run with `--debug` to see the full SSH transcript. "
                    "If the alias dropped, run `inspire notebook test "
                    f"-a {alias}` or `inspire notebook refresh {alias}` first."
                ),
            )
            return

    click.echo("install-deps complete.")


__all__ = ["install_deps_cmd", "DEFAULT_RAY_VERSION"]
