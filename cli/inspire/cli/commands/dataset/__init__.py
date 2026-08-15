"""Official dataset commands.

Usage:
    inspire dataset list [--keyword TEXT] [--tag NAME]
    inspire dataset tags
    inspire dataset show <name>
    inspire dataset validate <name>:<version> ... --workspace <workspace>
    inspire dataset applications [<name>] [--to-approve]
"""

from __future__ import annotations

import click

from .dataset_commands import (
    dataset_applications_cmd,
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
    does — access to a dataset is decided per workspace. Part of the catalogue
    is closed to any given account; `applications` shows where a request for
    access stands, though making one is a web-only flow.

    \b
    Examples:
        inspire dataset list --keyword pixabay
        inspire dataset tags
        inspire dataset list --tag 视频生成 --limit 5
        inspire dataset show pixabay-81k
        inspire dataset validate pixabay-81k:v0 --workspace CPU资源空间
        inspire dataset applications
        inspire dataset applications --to-approve
    """
    pass


dataset.add_command(list_datasets_cmd)
dataset.add_command(list_dataset_tags_cmd)
dataset.add_command(show_dataset_cmd)
dataset.add_command(validate_datasets_cmd)
dataset.add_command(dataset_applications_cmd)
