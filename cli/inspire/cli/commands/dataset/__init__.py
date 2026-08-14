"""Official dataset commands.

Usage:
    inspire dataset list [--keyword TEXT] [--tag NAME]
    inspire dataset tags
    inspire dataset show <name>
    inspire dataset validate <name>:<version> ... --workspace <workspace>
"""

from __future__ import annotations

import click

from .dataset_commands import (
    list_dataset_tags_cmd,
    list_datasets_cmd,
    show_dataset_cmd,
    validate_datasets_cmd,
)


@click.group()
def dataset():
    """Browse official datasets and check they can be mounted.

    A dataset's name is its catalogue code and a version's name is its version
    code; those two are what a workload mounts by. The catalogue is
    platform-wide, so `list` and `show` take no workspace, while `validate`
    does — access to a dataset is decided per workspace.

    \b
    Examples:
        inspire dataset list --keyword pixabay
        inspire dataset tags
        inspire dataset list --tag 视频生成 --limit 5
        inspire dataset show pixabay-81k
        inspire dataset validate pixabay-81k:v0 --workspace CPU资源空间
    """
    pass


dataset.add_command(list_datasets_cmd)
dataset.add_command(list_dataset_tags_cmd)
dataset.add_command(show_dataset_cmd)
dataset.add_command(validate_datasets_cmd)
