"""TensorBoard commands for Inspire CLI.

TensorBoard is a first-class platform object: compute groups advertise it as
the job type `tensorboard`, it has its own console tab, and it can stand alone
on any summary directory rather than belonging to a training job. The command
group covers that whole lifecycle, and then reads the running board so the
training curves come back as numbers.
"""

from __future__ import annotations

import click

from .tensorboard_commands import (
    create_tensorboard_cmd,
    delete_tensorboard_cmd,
    list_tensorboards_cmd,
    start_tensorboard_cmd,
    status_tensorboard,
    stop_tensorboard_cmd,
)
from .tensorboard_data import tensorboard_scalars, tensorboard_tags


@click.group()
def tensorboard() -> None:
    """Create, run and read TensorBoards.

    A board is a fixed 1 CPU / 2 GiB workload that serves the event files under
    one shared-disk directory, so there is no quota to choose and no image to
    pick — only a compute group that advertises the tensorboard job type. It
    stops itself after `--auto-stop-hours` (platform maximum 72).

    `tags` and `scalars` read the running board directly: an Agent gets the
    training curves as numbers instead of a web address it cannot open.

    \b
    Examples:
        inspire tensorboard create -n glm-sft --workspace 分布式训练空间 --project 前沿课题探索 --group 训练区-H200-1号机房 --summary-path /inspire/hdd/project/<project>/<user>/runs/glm-sft
        inspire tensorboard tags glm-sft --workspace 分布式训练空间
        inspire tensorboard scalars glm-sft --workspace 分布式训练空间 --tag train/loss --points 20
        inspire tensorboard stop glm-sft --workspace 分布式训练空间
        inspire tensorboard delete glm-sft --workspace 分布式训练空间 --yes
    """


tensorboard.add_command(list_tensorboards_cmd)
tensorboard.add_command(status_tensorboard)
tensorboard.add_command(create_tensorboard_cmd)
tensorboard.add_command(start_tensorboard_cmd)
tensorboard.add_command(stop_tensorboard_cmd)
tensorboard.add_command(delete_tensorboard_cmd)
tensorboard.add_command(tensorboard_tags)
tensorboard.add_command(tensorboard_scalars)


__all__ = ["tensorboard"]
