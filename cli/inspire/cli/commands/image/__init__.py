"""Image management commands.

Usage:
    inspire image list [--source official|public|private|all]
    inspire image detail <image-id>
    inspire image register -n "name" -v v1.0
    inspire image save <notebook-id> -n "name"
    inspire image delete <image-id>
    inspire image set-default --job <name> --notebook <name>
"""

from __future__ import annotations

import click

from .image_commands import (
    delete_image_cmd,
    image_detail,
    list_images_cmd,
    register_image_cmd,
    save_image_cmd,
    set_default_image_cmd,
)


@click.group()
def image():
    """Manage Docker images for notebooks and jobs.

    \b
    Examples:
        inspire image list                           # List official images
        inspire image list --source private          # List personal-visible images
        inspire image save <notebook-id> -n my-img   # Save notebook as image
        inspire image register -n my-img -v v1.0     # Register external image
        inspire image set-default --job my-pytorch   # Set default image
    """
    pass


image.add_command(list_images_cmd)
image.add_command(image_detail)
image.add_command(register_image_cmd)
image.add_command(save_image_cmd)
image.add_command(delete_image_cmd)
image.add_command(set_default_image_cmd)
