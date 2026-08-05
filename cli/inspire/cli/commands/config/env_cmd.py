"""Generate and register dotenv files."""

from __future__ import annotations

from pathlib import Path

import click

from inspire.cli.context import (
    EXIT_CONFIG_ERROR,
    EXIT_GENERAL_ERROR,
    Context,
    pass_context,
)
from inspire.cli.env_bootstrap import write_shared_project_env_file
from inspire.cli.utils.errors import exit_with_error
from inspire.cli.utils.output import emit_success
from inspire.config import get_categories, get_options_by_category


def _render_env_template(template: str) -> str:
    lines: list[str] = []
    lines.append("# Inspire CLI environment variables")
    lines.append("")

    essential_categories = {"Authentication", "API"}

    categories = get_categories()
    for category in categories:
        if template == "minimal" and category not in essential_categories:
            continue

        options = get_options_by_category(category)
        if not options:
            continue

        lines.append(f"# {category}")

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
    help="Template type: full (all options) or minimal (essential only).",
)
@click.option(
    "--output",
    "-o",
    "output_file",
    type=click.Path(),
    metavar="PATH",
    help="Write to a file instead of stdout.",
)
@click.pass_context
def generate_env(click_ctx: click.Context, template: str, output_file: str | None) -> None:
    """Generate a dotenv template or register a project dotenv file.

    \b
    Examples:
        inspire config env
        inspire config env --template full
        inspire config env --output .env.example
        inspire config env use .env
    """
    if click_ctx.invoked_subcommand is not None:
        return

    ctx = click_ctx.find_object(Context) or Context()
    content = _render_env_template(template)

    if output_file:
        output_path = Path(output_file)
        try:
            output_path.write_text(content, encoding="utf-8")
        except OSError as exc:
            exit_with_error(
                ctx,
                "FileError",
                f"Could not write dotenv template: {exc}",
                EXIT_GENERAL_ERROR,
                hint="Choose a writable --output path.",
            )
        emit_success(
            ctx,
            payload={"status": "created"},
            text=click.style("Created dotenv template.", fg="green"),
        )
    else:
        emit_success(
            ctx,
            payload={"template": content},
            text=content,
        )


@generate_env.command("use")
@click.argument("env_file", metavar="PATH")
@pass_context
def use_env_file(ctx: Context, env_file: str) -> None:
    """Register a project dotenv file."""
    try:
        write_shared_project_env_file(env_file)
    except click.ClickException as exc:
        exit_with_error(
            ctx,
            "ConfigError",
            str(exc),
            EXIT_CONFIG_ERROR,
        )
    except (OSError, ValueError) as exc:
        exit_with_error(
            ctx,
            "ConfigError",
            f"Could not register project env file: {exc}",
            EXIT_CONFIG_ERROR,
            hint="Check the project configuration and retry.",
        )
    emit_success(
        ctx,
        payload={"status": "registered"},
        text=click.style("Registered project env file.", fg="green"),
    )
