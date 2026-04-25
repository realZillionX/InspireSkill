"""`inspire notebook install-deps`: bake hpc/ray runtime deps into a notebook.

Each step does its own *in-shell* probing before running anything destructive,
so:

  * Hitting `install-deps` twice is safe (second run is a no-op).
  * The unified-base:v2 image — which already ships slurm + ray — short-circuits
    everything; nothing is reinstalled.
  * If the container's distro doesn't match what we know how to drive
    (jammy / noble), we bail with a clear message instead of letting apt fall
    over half-way through.

Run inside a notebook you already have an SSH alias to, then ``image save`` to
derive a project base image with all the runtimes you need.

Scope:

* ``--slurm`` — apt-installs ``slurm-wlm slurm-client munge hwloc libpmix2``,
  matching unified-base:v2. Slurm config and munge key are *not* touched; the
  platform injects ``/etc/slurm/slurm.conf`` at ``hpc create`` time.
* ``--ray`` — pip-installs a version-pinned ``ray`` (default ``2.55.1`` to
  match unified-base:v2). Override with ``--ray-version``.

Distributed-training stacks (deepspeed/accelerate/torch/transformers) are
project-specific; install those with ``inspire notebook exec`` directly.
"""

from __future__ import annotations

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
PYPI_FALLBACK_INDEX_URL = "https://pypi.org/simple"
SUPPORTED_DISTROS = ("noble", "jammy")

_SLURM_APT_PACKAGES = (
    "slurm-wlm",
    "slurm-client",
    "munge",
    "hwloc",
    "libpmix2",
)


def _distro_preflight() -> str:
    """Bash snippet that exits 2 with a clear message on unsupported distros."""
    supported = "|".join(SUPPORTED_DISTROS)
    return (
        "_codename=$(. /etc/os-release && echo \"$VERSION_CODENAME\"); "
        f'case "$_codename" in {supported}) ;; '
        "*) echo \"[install-deps] ERROR: unsupported distro '$_codename'; "
        f'supported: {", ".join(SUPPORTED_DISTROS)}\" >&2; exit 2 ;; esac; '
        'echo "[install-deps] distro=$_codename"'
    )


def _build_slurm_step() -> str:
    """Detect-then-install slurm with an apt-graph preflight.

    ``apt-get install --simulate`` is run first; if it reports unmet
    dependencies (typical on inspire-base / NGC-style images that have
    been built with cross-distro libs pinned), we abort with a clear
    error instead of letting the real install crash mid-way and leave
    apt in a broken state. ``srun`` already on PATH short-circuits the
    whole step so unified-base:v2 / vtb-* / videothinkbench-hpc-slurm-*
    are all no-ops.
    """
    pkgs = " ".join(_SLURM_APT_PACKAGES)
    return (
        "set -e; "
        f"{_distro_preflight()}; "
        'if command -v srun >/dev/null 2>&1 && command -v sbatch >/dev/null 2>&1; then '
        '  echo "[install-deps] slurm already installed (srun=$(command -v srun)); skipping"; '
        '  exit 0; '
        "fi; "
        "export DEBIAN_FRONTEND=noninteractive; "
        'echo "[install-deps] apt-get update"; '
        "apt-get update -qq; "
        # Simulate first — apt prints "Unmet dependencies" on stderr but
        # still exits 0 sometimes; we grep both streams + exit codes.
        'echo "[install-deps] apt-get install --simulate (preflight)"; '
        f"if ! _sim_out=$(apt-get install -y --no-install-recommends -s {pkgs} 2>&1); "
        'then _sim_failed=1; else _sim_failed=0; fi; '
        'if [ "$_sim_failed" != 0 ] || echo "$_sim_out" | grep -q "Unmet dependencies"; '
        "then "
        '  echo "[install-deps] ERROR: apt graph inconsistent; cannot install slurm cleanly." >&2; '
        '  echo "[install-deps] last 12 lines of dry-run output:" >&2; '
        '  echo "$_sim_out" | tail -12 >&2; '
        '  echo "" >&2; '
        '  echo "[install-deps] This image has been built with mixed-distro libs and apt cannot reconcile slurm-wlm without downgrades." >&2; '
        '  echo "[install-deps] Recommended workarounds:" >&2; '
        "  echo \"  - derive your project image from 'docker.sii.shaipower.online/inspire-studio/unified-base:v2' (slurm preinstalled, no apt step needed)\" >&2; "
        '  echo "  - or check if your image vendor (e.g. inspire-base / ngc-*) ships a slurm variant already" >&2; '
        "  exit 3; "
        "fi; "
        # Real install only after a clean simulate.
        'echo "[install-deps] apt-get install (real)"; '
        f"apt-get install -y --no-install-recommends {pkgs}"
    )


def _build_ray_step(version: str, *, pip_index_url: str) -> str:
    """Detect-then-install ray with python3/pip availability + DNS probes.

    pre-checks:
      - ``command -v python3 && command -v pip`` — pytorch-inspire-base
        ships only conda Python (no system python3 on PATH) and pip-install
        without that exits cryptically.
      - ``getent hosts <pip-index-host>`` — some images (videothinkbench-*
        observed) silently fail DNS to pypi.tuna.tsinghua.edu.cn even on
        an internet-bearing compute group; report that up-front instead
        of hanging in pip's connect-timeout retry loop.
    """
    spec = f"ray=={version}" if version else "ray"
    target = version or "(any)"

    # Reachability + fallback. Some images (videothinkbench-* observed)
    # are reachable to pypi.org but not to the tsinghua mirror — auto-fallback
    # so the user gets ray installed instead of a network error.
    from urllib.parse import urlparse

    candidate_indexes: list[str] = []
    if pip_index_url:
        candidate_indexes.append(pip_index_url)
    if PYPI_FALLBACK_INDEX_URL not in candidate_indexes:
        candidate_indexes.append(PYPI_FALLBACK_INDEX_URL)

    reach_lines: list[str] = ['_chosen_index=""']
    for url in candidate_indexes:
        parsed = urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        reach_lines.append(
            f'if [ -z "$_chosen_index" ]; then '
            f'  echo "[install-deps] reachability check: {host}:{port}"; '
            f"  if timeout 5 bash -c '</dev/tcp/{host}/{port}' >/dev/null 2>&1; then "
            f'    _chosen_index="{url}"; '
            f'    echo "[install-deps] picked index: $_chosen_index"; '
            "  else "
            f'    echo "[install-deps] WARN: {host}:{port} unreachable; trying next candidate"; '
            "  fi; "
            "fi"
        )
    reach_lines.append(
        'if [ -z "$_chosen_index" ]; then '
        '  echo "[install-deps] ERROR: no reachable PyPI index from this notebook." >&2; '
        f'  echo "[install-deps] tried: {", ".join(candidate_indexes)}" >&2; '
        '  echo "[install-deps] hint: this compute group probably has no internet egress; run on HPC-可上网区资源-2 / CPU资源-2 etc." >&2; '
        "  exit 4; "
        "fi"
    )
    reachability_block = "; ".join(reach_lines) + "; "

    return (
        "set -e; "
        f"{_distro_preflight()}; "
        # python3 + pip in PATH?
        'if ! command -v python3 >/dev/null 2>&1; then '
        '  echo "[install-deps] ERROR: python3 not on PATH; this image likely uses conda/venv only." >&2; '
        "  echo \"[install-deps] hint: activate the project's env first, then run \"\"pip install --break-system-packages "
        f'ray=={version}\\"\\" by hand; or derive from a system-python image." >&2; '
        "  exit 5; "
        "fi; "
        'if ! command -v pip >/dev/null 2>&1 && ! command -v pip3 >/dev/null 2>&1; then '
        '  echo "[install-deps] ERROR: pip not on PATH." >&2; '
        "  exit 5; "
        "fi; "
        '_pip=$(command -v pip || command -v pip3); '
        # already-installed?
        f'_have=$($_pip show ray 2>/dev/null | awk \'/^Version:/ {{print $2}}\'); '
        f'if [ -n "$_have" ] && [ "$_have" = "{version}" ]; then '
        f'  echo "[install-deps] ray=={version} already installed; skipping"; '
        "  exit 0; "
        "fi; "
        f'if [ -n "$_have" ]; then '
        f'  echo "[install-deps] upgrading ray $_have -> {target}"; '
        "fi; "
        # network preflight + auto-fallback to next candidate if the configured
        # mirror is unreachable. Sets $_chosen_index for the install line below.
        f"{reachability_block}"
        # actual install
        f'echo "[install-deps] pip install ray=={version}"; '
        '$_pip install --upgrade --no-input --break-system-packages '
        '--index-url "$_chosen_index" '
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
        "saved image. Skipped automatically when srun + sbatch already exist."
    ),
)
@click.option(
    "--ray/--no-ray",
    default=False,
    help=(
        "pip-install ray (version pinned via --ray-version). Skipped "
        "automatically when the requested version is already installed."
    ),
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
        "PyPI index for pip steps. Default mirrors unified-base:v2. "
        "Pass an empty string to skip the explicit --index-url flag and "
        "let pip pick up whatever the image already configures (/etc/pip.conf etc); "
        "pass 'https://pypi.org/simple' to force upstream PyPI."
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
    `inspire ray create` without further setup. Each step probes the
    container first and skips itself if the requested runtime is already
    in place — hitting this command twice is safe.
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


__all__ = ["install_deps_cmd", "DEFAULT_RAY_VERSION", "SUPPORTED_DISTROS"]
