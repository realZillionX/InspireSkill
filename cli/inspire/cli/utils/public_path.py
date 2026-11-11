"""CLI adapter for the public-path mount contract."""

import click

from inspire.services.public_path import resolve_public_path_mount_mode


def public_path_mode(mode: int | None, readonly: bool | None) -> int | None:
    if readonly is not None:
        click.echo(
            "Deprecated: public_path_readonly / --public-path-readonly; "
            "use public_path_mount_mode / --public-path-mount-mode instead.",
            err=True,
        )
    try:
        return resolve_public_path_mount_mode(mode, readonly)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="public_path_mount_mode") from exc
