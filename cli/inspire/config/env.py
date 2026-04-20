"""Environment parsing helpers for Inspire CLI config."""

from __future__ import annotations

import os
import re
import shlex
from typing import Optional

from inspire.config.models import ConfigError


def _parse_remote_timeout(value: str) -> int:
    """Parse INSP_REMOTE_TIMEOUT environment variable."""
    try:
        timeout = int(value)
        if timeout < 5:
            # Warn but allow small values for testing
            pass
        return timeout
    except ValueError as e:
        raise ConfigError(
            "Invalid INSP_REMOTE_TIMEOUT value. It must be an integer number of seconds."
        ) from e


def _parse_denylist(value: Optional[str]) -> list[str]:
    """Parse denylist from env (comma or newline separated)."""
    if not value:
        return []
    parts: list[str] = []
    for raw in value.replace("\r", "").split("\n"):
        for chunk in raw.split(","):
            item = chunk.strip()
            if item:
                parts.append(item)
    return parts


def build_env_exports(env_dict: dict[str, str]) -> str:
    """Build shell export commands for remote environment variables."""
    if not env_dict:
        return ""

    var_name_re = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    env_ref_re = re.compile(
        r"^\$(?:\{(?P<braced>[A-Za-z_][A-Za-z0-9_]*)\}|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))$"
    )

    exports: list[str] = []
    for key, raw_value in env_dict.items():
        if not var_name_re.match(key):
            raise ConfigError(f"Invalid remote_env key: {key!r} (must match {var_name_re.pattern})")

        value = raw_value
        if value == "":
            env_var = key
            if env_var not in os.environ:
                raise ConfigError(
                    f"remote_env[{key}] is empty but {env_var} is not set in the local environment"
                )
            value = os.environ[env_var]
        else:
            match = env_ref_re.match(value)
            if match is not None:
                env_var = match.group("braced") or match.group("bare")
                if env_var not in os.environ:
                    raise ConfigError(
                        f"remote_env[{key}] references {env_var} but it is not set in the local environment"
                    )
                value = os.environ[env_var]

        exports.append(f"export {key}={shlex.quote(value)}")

    return " && ".join(exports) + " && "
