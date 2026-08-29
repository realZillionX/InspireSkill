"""Early loading for an explicitly supplied dotenv file."""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
import click

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LOADED_ENV_FILE_KEYS: set[str] = set()
_LOADED_ENV_FILE_VALUES: dict[str, str] = {}
_LOADED_ENV_FILE_PATH: Path | None = None


def reset_loaded_env_file_state() -> None:
    for key, value in list(_LOADED_ENV_FILE_VALUES.items()):
        if os.environ.get(key) == value:
            os.environ.pop(key, None)
    _LOADED_ENV_FILE_KEYS.clear()
    _LOADED_ENV_FILE_VALUES.clear()
    global _LOADED_ENV_FILE_PATH
    _LOADED_ENV_FILE_PATH = None


def is_env_file_key(key: str) -> bool:
    return key in _LOADED_ENV_FILE_KEYS


def loaded_env_file_path() -> Path | None:
    return _LOADED_ENV_FILE_PATH


def _strip_inline_comment(text: str) -> str:
    for index, char in enumerate(text):
        if char == "#" and (index == 0 or text[index - 1].isspace()):
            return text[:index].rstrip()
    return text.rstrip()


def _find_quote_end(text: str, quote: str) -> int | None:
    escaped = False
    for index in range(1, len(text)):
        char = text[index]
        if quote == '"' and char == "\\" and not escaped:
            escaped = True
            continue
        if char == quote and not escaped:
            return index
        escaped = False
    return None


def _parse_env_value(raw_value: str, *, path: Path, line_no: int) -> str:
    value = raw_value.strip()
    if not value:
        return ""

    if value[0] in ("'", '"'):
        quote = value[0]
        end = _find_quote_end(value, quote)
        if end is None:
            raise click.ClickException(f"{path}:{line_no}: unterminated quoted value")
        literal = value[: end + 1]
        tail = value[end + 1 :].strip()
        if tail and not tail.startswith("#"):
            raise click.ClickException(f"{path}:{line_no}: unexpected text after quoted value")
        try:
            parsed = ast.literal_eval(literal)
        except (SyntaxError, ValueError) as exc:
            raise click.ClickException(f"{path}:{line_no}: invalid quoted value") from exc
        return str(parsed)

    return _strip_inline_comment(value)


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError as exc:
        raise click.ClickException(f"Env file not found: {path}") from exc
    except OSError as exc:
        raise click.ClickException(f"Failed to read env file {path}: {exc}") from exc

    for line_no, raw_line in enumerate(lines, start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            raise click.ClickException(f"{path}:{line_no}: expected KEY=value")
        raw_key, raw_value = line.split("=", 1)
        key = raw_key.strip()
        if not _ENV_KEY_RE.match(key):
            raise click.ClickException(f"{path}:{line_no}: invalid environment variable name")
        values[key] = _parse_env_value(raw_value, path=path, line_no=line_no)
    return values


def _apply_env_values(path: Path, values: dict[str, str]) -> None:
    global _LOADED_ENV_FILE_PATH
    _LOADED_ENV_FILE_PATH = path
    for key, value in values.items():
        if key in os.environ:
            continue
        os.environ[key] = value
        _LOADED_ENV_FILE_KEYS.add(key)
        _LOADED_ENV_FILE_VALUES[key] = value


def bootstrap_env_file(
    *,
    env_file: Path | None,
    disabled: bool = False,
) -> Path | None:
    """Load an explicitly selected dotenv file into ``os.environ``."""
    reset_loaded_env_file_state()
    if disabled and env_file is not None:
        raise click.ClickException("--env-file cannot be combined with --no-env-file")
    if disabled:
        return None
    if env_file is None:
        return None

    selected_path = env_file.expanduser()
    if not selected_path.is_absolute():
        selected_path = Path.cwd() / selected_path
    selected_path = selected_path.resolve()
    if not selected_path.exists():
        raise click.ClickException(f"Env file not found: {selected_path}")
    values = _parse_env_file(selected_path)
    _apply_env_values(selected_path, values)
    return selected_path


__all__ = [
    "bootstrap_env_file",
    "is_env_file_key",
    "loaded_env_file_path",
    "reset_loaded_env_file_state",
]
