"""Image management commands.

Usage:
    inspire image list --workspace <name> [--source official|public|private|all]
    inspire image detail <name> --workspace <name>
    inspire image register -n "name" --workspace <name> -v v1.0
    inspire image set-visibility <name> --workspace <name> --visibility public|private
    inspire image delete <name> --workspace <name>
"""

from __future__ import annotations

import click

from .image_commands import (
    delete_image_cmd,
    image_detail,
    list_images_cmd,
    register_image_cmd,
    set_image_visibility_cmd,
)


@click.group()
def image():
    """Manage Docker images for notebook, job, HPC, Ray, and serving.

    Use `image list/detail` to choose a ready image, `image register` for
    images built outside the platform, `set-visibility` to share or privatize
    a custom image, and `delete` only after confirming no active workload
    depends on that image. This group manages images that already exist;
    producing one from a prepared environment is a notebook lifecycle
    operation, so use `inspire notebook save-image` for that, and
    `inspire notebook cancel-save-image` to abort a save still running.

    Images live in a registry, not in a workspace, and several workspaces
    normally share one -- so `--workspace` here names which registry to read,
    and an image saved under one workspace is visible from every workspace on
    the same registry. What does not carry across is a registry boundary: 国产卡
    workspaces read a different registry from the NVIDIA ones, and a name that
    exists in one may not exist in the other. `image list` is the ground truth
    for which workspace names reach which catalog.

    \b
    Examples:
        inspire image list --workspace 分布式训练空间
        inspire image list --workspace 分布式训练空间 --source public
        inspire image list --workspace CPU资源空间 --source private
        inspire image detail <name> --workspace 分布式训练空间
        inspire image set-visibility <name> --workspace 分布式训练空间 --visibility public
        inspire image register -n my-img --workspace 分布式训练空间 -v v1.0
    """
    pass


image.add_command(list_images_cmd)
image.add_command(image_detail)
image.add_command(register_image_cmd)
image.add_command(set_image_visibility_cmd)
image.add_command(delete_image_cmd)
