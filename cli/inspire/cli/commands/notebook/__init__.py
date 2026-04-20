"""Notebook / Interactive instance commands (unified entry for all
remote-access operations — what used to live under `bridge` and `tunnel`
is now reachable here under the same notebook group).

Usage:
    inspire notebook list
    inspire notebook status <instance-id>
    inspire notebook top
    inspire notebook create --resource 1xH200
    inspire notebook stop <instance-id>
    inspire notebook ssh <instance-id>          # first-time bootstrap
    inspire notebook ssh <alias>                # reconnect to a saved alias
    inspire notebook exec <alias> "<cmd>"
    inspire notebook scp <src> <dst>
    inspire notebook connections                # list saved aliases
    inspire notebook refresh <alias>
    inspire notebook forget <alias>
"""

from __future__ import annotations

import click

from .notebook_commands import (
    create_notebook_cmd,
    list_notebooks,
    notebook_status,
    ssh_notebook_cmd,
    start_notebook_cmd,
    stop_notebook_cmd,
)
from .top import notebook_top
from .notebook_events import events as notebook_events
from .notebook_lifecycle import lifecycle as notebook_lifecycle

# Remote operations on a saved alias (formerly `inspire bridge *`).
from .remote_exec import exec_command as _remote_exec
from .remote_scp import bridge_scp as _remote_scp
from .remote_shell import bridge_ssh as _remote_shell

# Local alias management (formerly `inspire tunnel *`).
from .connections_cmd import tunnel_list as _connections
from .forget_cmd import tunnel_remove as _forget
from .refresh_cmd import tunnel_update as _refresh
from .set_default_cmd import tunnel_set_default as _set_default
from .ssh_config_cmd import tunnel_ssh_config as _ssh_config
from .connection_test_cmd import tunnel_test as _connection_test


@click.group()
def notebook():
    """Manage notebook/interactive instances.

    \b
    Examples:
        inspire notebook list                       # List all instances
        inspire notebook ssh <id>                   # Bootstrap SSH (saves an alias)
        inspire notebook exec <alias> "nvidia-smi"  # Run a command on a saved alias
    """
    pass


# Core lifecycle (existing).
notebook.add_command(list_notebooks)            # list
notebook.add_command(notebook_status)           # status
notebook.add_command(create_notebook_cmd)       # create
notebook.add_command(stop_notebook_cmd)         # stop
notebook.add_command(start_notebook_cmd)        # start
notebook.add_command(ssh_notebook_cmd)          # ssh  (bootstrap; alias-aware dispatch in the cmd body)
notebook.add_command(notebook_top)              # top
notebook.add_command(notebook_events)           # events (K8s scheduling / pod lifecycle)
notebook.add_command(notebook_lifecycle)        # lifecycle (run-cycle timeline; /run_index/list)

# Remote operations on a saved alias.
notebook.add_command(_remote_exec,  name="exec")
notebook.add_command(_remote_scp,   name="scp")
notebook.add_command(_remote_shell, name="shell")

# Local alias management.
notebook.add_command(_connections,     name="connections")
notebook.add_command(_refresh,         name="refresh")
notebook.add_command(_forget,          name="forget")
notebook.add_command(_set_default,     name="set-default")
notebook.add_command(_connection_test, name="test")
notebook.add_command(_ssh_config,      name="ssh-config")
