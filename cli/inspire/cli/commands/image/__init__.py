"""Image management commands.

Usage:
    inspire image list [--source official|public|private|all]
    inspire image detail <name>
    inspire image register -n "name" -v v1.0
    inspire image save <notebook-name> --workspace <workspace> -n "name" [--visibility public|private] [--dry-run]
    inspire image cancel-save <notebook-name> --workspace <workspace>
    inspire image set-visibility <name> --visibility public|private
    inspire image delete <name>
"""

from __future__ import annotations

import click

from .image_commands import (
    cancel_save_image_cmd,
    delete_image_cmd,
    image_detail,
    list_images_cmd,
    register_image_cmd,
    save_image_cmd,
    set_image_visibility_cmd,
)


@click.group()
def image():
    """Manage Docker images for notebook, job, HPC, Ray, and serving.

    Use `image list/detail` to choose a ready image, `image save` after
    preparing a notebook environment, `image register` for images built
    outside the platform, `set-visibility` to share or privatize a custom
    image, and `delete` only after confirming no active workload depends on
    that image. `image save` starts a medium-length saving process; the
    notebook cannot be operated while saving is in progress, is not stopped
    after saving completes, and can then be used again. It prints the platform's
    estimate of how much data it has to snapshot before it starts; `--dry-run`
    stops after that estimate. `image cancel-save` aborts a save that is still
    running and gives the notebook straight back.

    \b
    Examples:
        inspire image list                              # List all visible images
        inspire image list --source public              # List public images
        inspire image list --source private             # List personal-visible images
        inspire image save <notebook-name> --workspace CPU资源空间 -n my-img
        inspire image save <notebook-name> --workspace CPU资源空间 -n my-img --dry-run
        inspire image save <notebook-name> --workspace CPU资源空间 -n shared --visibility public
        inspire image cancel-save <notebook-name> --workspace CPU资源空间
        inspire image set-visibility <name> --visibility public
        inspire image register -n my-img -v v1.0        # Register external image
    """
    pass


image.add_command(list_images_cmd)
image.add_command(image_detail)
image.add_command(register_image_cmd)
image.add_command(save_image_cmd)
image.add_command(cancel_save_image_cmd)
image.add_command(set_image_visibility_cmd)
image.add_command(delete_image_cmd)
