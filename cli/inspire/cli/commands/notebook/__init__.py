"""Notebook / Interactive instance commands.

Usage:
    inspire notebook list --workspace <workspace>
    inspire notebook status <name> --workspace <workspace>
    inspire notebook create --workspace <workspace> --project <project> \
        --group <full-group-name> --quota 1,20,200
    inspire notebook stop <name> --workspace <workspace>
    inspire notebook save-image <name> --workspace <workspace> -n "name" \
        [--visibility public|private] [--dry-run]
    inspire notebook cancel-save-image <name> --workspace <workspace>
    inspire notebook ssh <notebook> --workspace <workspace>
    inspire notebook exec <notebook> "<cmd>"
    inspire notebook scp <notebook> <src> <dst>
"""

from __future__ import annotations

import click

from inspire.cli.commands.batch import notebook_batch
from inspire.cli.commands.workload_quota import make_quota_command

from .notebook_commands import (
    create_notebook_cmd,
    delete_notebook_cmd,
    list_notebooks,
    notebook_status,
    start_notebook_cmd,
    stop_notebook_cmd,
)
from .connection import notebook_connection
from .ssh import notebook_ssh
from .ssh_config_cmd import ssh_config_cmd
from .ssh_proxy_cmd import ssh_proxy_cmd
from .notebook_events import events as notebook_events
from .notebook_lifecycle import lifecycle as notebook_lifecycle
from .notebook_metrics import notebook_metrics
from .notebook_save_image import cancel_save_image_cmd, save_image_cmd
from .url_cmd import notebook_proxy_url

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
    probes, open command access by notebook name, and expose temporary HTTP
    services when network policy allows. For 分布式训练空间 or another offline GPU area,
    prepare public downloads in an internet-enabled CPU资源空间 notebook first;
    restricted notebooks use JupyterTerminal for exec/shell and shared
    /inspire/... paths for file movement. Once an environment is prepared,
    `save-image` commits the running container into a reusable custom image:
    the notebook cannot be operated while that runs, is not stopped once it
    finishes, and `cancel-save-image` hands it straight back.

    \b
    Examples:
        inspire notebook create --workspace CPU资源空间 --group CPU资源-2 -q 0,20,256 --project <project> --image <image> --name prep-box --wait
        inspire notebook ssh prep-box --workspace CPU资源空间
        inspire notebook ssh prep-box -- hostname
        inspire notebook ssh-config prep-box >> ~/.ssh/config
        inspire notebook exec prep-box --cwd /inspire/ssd/project/topic/user/repo "git pull && pip install -r requirements.txt"
        inspire notebook scp prep-box ./config.yaml /inspire/ssd/project/topic/user/repo/config.yaml
        inspire notebook metrics <notebook> --workspace CPU资源空间 --window 30m
        inspire notebook save-image prep-box --workspace CPU资源空间 -n my-img --dry-run
        inspire notebook save-image prep-box --workspace CPU资源空间 -n my-img
        inspire notebook cancel-save-image prep-box --workspace CPU资源空间
    """
    pass


# Core lifecycle (existing).
notebook.add_command(list_notebooks)            # list
notebook.add_command(notebook_status)           # status
notebook.add_command(notebook_proxy_url)        # proxy-url (container HTTP service URL)
notebook.add_command(create_notebook_cmd)       # create
notebook.add_command(make_quota_command("notebook"))  # quota
notebook.add_command(notebook_batch)            # batch
notebook.add_command(stop_notebook_cmd)         # stop
notebook.add_command(start_notebook_cmd)        # start
notebook.add_command(save_image_cmd)            # save-image (commit the running container)
notebook.add_command(cancel_save_image_cmd)     # cancel-save-image
notebook.add_command(delete_notebook_cmd)       # delete
notebook.add_command(notebook_ssh)              # ssh
notebook.add_command(notebook_connection)       # connection
notebook.add_command(ssh_config_cmd)            # ssh-config
notebook.add_command(ssh_proxy_cmd)             # ssh-proxy
notebook.add_command(notebook_events)           # events (K8s scheduling / pod lifecycle)
notebook.add_command(notebook_lifecycle)        # lifecycle (run-cycle timeline; /run_index/list)
notebook.add_command(notebook_metrics)          # metrics (资源视图 time-series, no SSH needed)

# Remote operations on a cached notebook connection.
notebook.add_command(_remote_exec,  name="exec")
notebook.add_command(_remote_scp,   name="scp")
notebook.add_command(_remote_shell, name="shell")
notebook.add_command(install_deps_cmd, name="install-deps")
