"""User-scoped commands for Inspire CLI."""

from __future__ import annotations

import click

from .user_commands import api_keys_user, permissions_user, quota_user, whoami_user


@click.group()
def user() -> None:
    """Inspect the current user's identity, quota, permissions, and API keys."""


user.add_command(whoami_user)
user.add_command(quota_user)
user.add_command(api_keys_user)
user.add_command(permissions_user)


__all__ = ["user"]
