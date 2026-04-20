"""Project management commands.

Usage:
    inspire project list
    inspire project detail <project-id>
    inspire project owners
"""

from __future__ import annotations

import click

from .project_commands import detail_project_cmd, list_projects_cmd, owners_project_cmd


@click.group()
def project():
    """View project information, quota, members, and owners.

    \b
    Examples:
        inspire project list                # quota table
        inspire project list --json         # JSON with all fields
        inspire project detail <project-id> # single-project detail
        inspire project owners              # "负责人" dropdown contents
    """
    pass


project.add_command(list_projects_cmd)
project.add_command(detail_project_cmd)
project.add_command(owners_project_cmd)
