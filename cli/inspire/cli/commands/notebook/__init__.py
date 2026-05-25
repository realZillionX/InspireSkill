"""Notebook / Interactive instance commands.

Usage:
    inspire notebook list --workspace <workspace>
    inspire notebook status <name> --workspace <workspace>
    inspire notebook create --workspace <workspace> --project <project> \
        --group <full-group-name> --quota 1,20,200
    inspire notebook stop <name> --workspace <workspace>
    inspire notebook ssh connect <notebook> --workspace <workspace>
    inspire notebook exec <notebook> "<cmd>"
    inspire notebook scp <notebook> <src> <dst>
"""

from __future__ import annotations

import click

from inspire.cli.commands.batch import notebook_batch
from inspire.cli.commands.workload_quota import make_quota_command
from inspire.cli.commands.workload_profile import make_profile_command

from .notebook_commands import (
    create_notebook_cmd,
    delete_notebook_cmd,
    list_notebooks,
    notebook_id_cmd,
    notebook_status,
    start_notebook_cmd,
    stop_notebook_cmd,
)
from .connection import notebook_connection
from .path_aliases import path_aliases_cmd
from .ssh import notebook_ssh
from .ssh_config_cmd import ssh_config_cmd
from .ssh_proxy_cmd import ssh_proxy_cmd
from .notebook_events import events as notebook_events
from .notebook_lifecycle import lifecycle as notebook_lifecycle
from .notebook_metrics import notebook_metrics

# Remote operations on a cached notebook connection.
from .install_deps import install_deps_cmd
from .remote_exec import exec_command as _remote_exec
from .remote_scp import bridge_scp as _remote_scp
from .remote_shell import bridge_ssh as _remote_shell


@click.group()
def notebook():
    """Manage notebook/interactive instances.

    Notebooks are the interactive workbench: use them to prepare project
    environments, download data or weights into shared storage, run quick
    probes, expose temporary HTTP services, and open SSH / exec / scp access
    by notebook name. For 分布式训练空间 or another offline GPU area,
    prepare public downloads in an internet-enabled CPU资源空间 notebook first;
    for package installs, check the SII internal mirrors before falling back.

    \b
    Examples:
        inspire notebook create --workspace CPU资源空间 --group CPU资源-2 -q 0,20,256 --project CI-情境智能 --image unified-base:v2 --name prep-box --wait
        inspire notebook ssh prep-box --workspace CPU资源空间
        inspire notebook ssh prep-box -- hostname
        inspire notebook ssh-config prep-box >> ~/.ssh/config
        inspire notebook exec prep-box --cwd me:repo "git pull && pip install -r requirements.txt"
        inspire notebook scp prep-box ./config.yaml me:repo/config.yaml
        inspire notebook metrics <notebook> --workspace CPU资源空间 --window 30m
    """
    pass


# Core lifecycle (existing).
notebook.add_command(list_notebooks)            # list
notebook.add_command(notebook_status)           # status
notebook.add_command(notebook_id_cmd)           # id
notebook.add_command(create_notebook_cmd)       # create
notebook.add_command(make_quota_command("notebook"))  # quota
notebook.add_command(make_profile_command("notebook"))  # profile
notebook.add_command(notebook_batch)            # batch
notebook.add_command(stop_notebook_cmd)         # stop
notebook.add_command(start_notebook_cmd)        # start
notebook.add_command(delete_notebook_cmd)       # delete
notebook.add_command(notebook_ssh)              # ssh
notebook.add_command(notebook_connection)       # connection
notebook.add_command(ssh_config_cmd)            # ssh-config
notebook.add_command(ssh_proxy_cmd)             # ssh-proxy
notebook.add_command(notebook_events)           # events (K8s scheduling / pod lifecycle)
notebook.add_command(notebook_lifecycle)        # lifecycle (run-cycle timeline; /run_index/list)
notebook.add_command(notebook_metrics)          # metrics (资源视图 time-series, no SSH needed)
notebook.add_command(path_aliases_cmd)          # path (project remote path aliases)

# Remote operations on a cached notebook connection.
notebook.add_command(_remote_exec,  name="exec")
notebook.add_command(_remote_scp,   name="scp")
notebook.add_command(_remote_shell, name="shell")
notebook.add_command(install_deps_cmd, name="install-deps")
