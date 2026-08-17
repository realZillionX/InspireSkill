"""Project management commands.

Usage:
    inspire project list
    inspire project detail <project-name>
    inspire project owners
"""

from __future__ import annotations

import click

from .project_commands import detail_project_cmd, list_projects_cmd, owners_project_cmd


@click.group()
def project():
    """View project-level metadata, owners, budget, and priority fields.

    `project` is mostly for group-level project context: ownership, displayed
    budget / points, and platform priority fields. For ordinary personal
    compute decisions, start with `<workload> quota` and live availability;
    project budget is usually not the first constraint.

    \b
    Examples:
        inspire project list                # project metadata table
        inspire --json project list          # 同一张表的结构化输出
        inspire project detail <project-name> # single-project detail
        inspire project owners              # "负责人" dropdown contents
    """
    pass


project.add_command(list_projects_cmd)
project.add_command(detail_project_cmd)
project.add_command(owners_project_cmd)
