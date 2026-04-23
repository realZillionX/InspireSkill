"""``inspire account migrate`` — one-shot legacy → per-account migration."""

from __future__ import annotations

import click

from inspire.accounts import migration


@click.command("migrate")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Show the plan without touching any files.",
)
@click.option(
    "--yes",
    "-y",
    "assume_yes",
    is_flag=True,
    help="Skip the confirmation prompt before applying the plan.",
)
def migrate(dry_run: bool, assume_yes: bool) -> None:
    """Move legacy config / bridges / session files into per-account dirs.

    Before: ``~/.config/inspire/config.toml`` with ``[accounts."<user>"]`` sections
    + ``~/.inspire/bridges-<user>.json`` + ``~/.cache/inspire-skill/web_session-<user>.json``.

    After: ``~/.inspire/accounts/<name>/`` with ``config.toml`` + ``bridges.json``
    + ``web_session.json`` all colocated. Originals are copied to
    ``~/.inspire/legacy-<timestamp>/`` first and then removed.

    Safe to run multiple times — a second run with no legacy artefacts left
    does nothing. Refuses to overwrite an account directory that already exists.
    """
    plan = migration.build_plan()

    for line in migration.describe_plan(plan):
        click.echo(line)

    if plan.is_empty:
        return

    if dry_run:
        click.echo("\n--dry-run: no changes made.")
        return

    if not assume_yes:
        click.confirm("\nProceed with migration?", abort=True)

    try:
        backup_dir = migration.execute_plan(plan)
    except migration.MigrationConflictError as err:
        raise click.ClickException(str(err)) from err

    click.echo(f"\nMigration complete.")
    click.echo(f"  Backup: {backup_dir}")
    if plan.active_account:
        click.echo(f"  Active account: {plan.active_account}")
    else:
        click.echo(
            "  No active account set automatically — pick one with "
            "'inspire account use <name>'."
        )
