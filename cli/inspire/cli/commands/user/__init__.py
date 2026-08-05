"""User-scoped commands for Inspire CLI."""

from __future__ import annotations

import click

from .user_commands import (
    api_keys_user,
    permissions_user,
    quota_user,
    ssh_keys_user,
    whoami_user,
)


@click.group()
def user() -> None:
    """Inspect the current user's identity, permissions, quota, and keys.

    Use `whoami` to verify the active user, `permissions --workspace` to
    check whether the account can create a workload in a workspace,
    `api-keys` to list key metadata, and `ssh-keys` to manage public keys
    used by notebook SSH access.
    """


user.add_command(whoami_user)
user.add_command(quota_user)
user.add_command(api_keys_user)
user.add_command(ssh_keys_user)
user.add_command(permissions_user)


__all__ = ["user"]
