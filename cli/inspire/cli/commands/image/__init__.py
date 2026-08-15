"""Image management commands.

Usage:
    inspire image list [--source official|public|private|all]
    inspire image detail <name>
    inspire image register -n "name" -v v1.0
    inspire image set-visibility <name> --visibility public|private
    inspire image delete <name>
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

    \b
    Examples:
        inspire image list                              # List all visible images
        inspire image list --source public              # List public images
        inspire image list --source private             # List personal-visible images
        inspire image detail <name>                     # Check status before scheduling
        inspire image set-visibility <name> --visibility public
        inspire image register -n my-img -v v1.0        # Register external image
    """
    pass


image.add_command(list_images_cmd)
image.add_command(image_detail)
image.add_command(register_image_cmd)
image.add_command(set_image_visibility_cmd)
image.add_command(delete_image_cmd)
