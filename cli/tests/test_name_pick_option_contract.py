from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from inspire.cli.main import main as cli_main
from inspire.cli.utils.id_resolver import NAME_PICK_HELP


def _find_name_pick_commands(
    command: click.Command,
    path: tuple[str, ...] = (),
) -> tuple[tuple[str, ...], ...]:
    if isinstance(command, click.Group):
        return tuple(
            child_path
            for name, child in command.commands.items()
            for child_path in _find_name_pick_commands(child, (*path, name))
        )
    if any(
        isinstance(param, click.Option) and param.name == "pick"
        for param in command.params
    ):
        return (path,)
    return ()


_NAME_PICK_COMMANDS = _find_name_pick_commands(cli_main)


def _resolve_command(path: tuple[str, ...]) -> click.Command:
    command = cli_main
    for name in path:
        assert isinstance(command, click.Group)
        command = command.commands[name]
    return command


@pytest.mark.parametrize("path", _NAME_PICK_COMMANDS)
def test_name_pick_options_share_one_contract(path: tuple[str, ...]) -> None:
    command = _resolve_command(path)
    option = next(
        param
        for param in command.params
        if isinstance(param, click.Option) and param.name == "pick"
    )

    assert option.help == NAME_PICK_HELP
    assert isinstance(option.type, click.IntRange)
    assert option.type.min == 1
    assert option.type.max is None
    assert option.default is None
    assert option.metavar is None
    assert not option.required

    result = CliRunner().invoke(cli_main, [*path, "--help"])
    usage = result.output.splitlines()[0]
    assert result.exit_code == 0, result.output
    assert " NAME" in usage
    assert "--pick INTEGER RANGE" in result.output
    assert NAME_PICK_HELP in " ".join(result.output.split())
    assert "ID" not in usage
