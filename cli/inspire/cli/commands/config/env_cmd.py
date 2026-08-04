"""Config env command – generate .env template file."""

from __future__ import annotations

from pathlib import Path

import click

from inspire.cli.env_bootstrap import write_shared_project_env_file
from inspire.config import (
    get_categories,
    get_options_by_category,
)

# ---------------------------------------------------------------------------
# Command
# ---------------------------------------------------------------------------


def _render_env_template(template: str) -> str:
    lines: list[str] = []
    lines.append("# Inspire CLI Environment Variables")
    lines.append("# Generated template - customize values as needed")
    lines.append("")

    essential_categories = {"Authentication", "API", "Paths", "GitHub"}

    categories = get_categories()
    for category in categories:
        if template == "minimal" and category not in essential_categories:
            continue

        options = get_options_by_category(category)
        if not options:
            continue

        lines.append(f"# === {category} ===")

        for option in options:
            lines.append(f"# {option.description}")

            if option.secret:
                lines.append(f"# {option.env_var}=<your-secret-here>")
            elif option.default is not None:
                default_str = str(option.default)
                if isinstance(option.default, list):
                    default_str = ",".join(option.default) if option.default else ""
                if " " in default_str or "," in default_str:
                    lines.append(f'# {option.env_var}="{default_str}"')
                else:
                    lines.append(f"# {option.env_var}={default_str}")
            else:
                lines.append(f"# {option.env_var}=")

        lines.append("")

    return "\n".join(lines)


@click.group("env", invoke_without_command=True)
@click.option(
    "--template",
    "-t",
    type=click.Choice(["full", "minimal"]),
    default="minimal",
    help="Template type: full (all options) or minimal (essential only)",
)
@click.option(
    "--output",
    "-o",
    "output_file",
    type=click.Path(),
    help="Write to file instead of stdout",
)
@click.pass_context
def generate_env(click_ctx: click.Context, template: str, output_file: str | None) -> None:
    """Generate or register dotenv files.

    Without a subcommand, creates a template with all configuration options as
    environment variables.

    \b
    Examples:
        inspire config env
        inspire config env --template full
        inspire config env --output .env.example
        inspire config env use .env
    """
    if click_ctx.invoked_subcommand is not None:
        return

    content = _render_env_template(template)

    if output_file:
        output_path = Path(output_file)
        output_path.write_text(content, encoding="utf-8")
        click.echo(click.style("Created dotenv template.", fg="green"))
    else:
        click.echo(content)


@generate_env.command("use")
@click.argument("env_file")
def use_env_file(env_file: str) -> None:
    """Register a repo-wide dotenv file in shared project config."""
    write_shared_project_env_file(env_file)
    click.echo(click.style("Registered project env file.", fg="green"))
