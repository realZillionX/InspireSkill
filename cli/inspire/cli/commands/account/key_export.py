"""Credential serialization and private file publication across platforms."""

from __future__ import annotations

import os
import re
import shlex
import shutil as shutil
import subprocess as subprocess
import sys
import tempfile
from pathlib import Path

# Preserve the CLI helper names for existing callers and test seams.
from inspire import local_files
from inspire.local_files import _WINDOWS_ACL_SCRIPT as _WINDOWS_ACL_SCRIPT


def render_key(value: str, output_format: str, env_name: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", env_name):
        raise ValueError("Use a valid environment variable name.")
    if output_format == "raw":
        return value + "\n"
    if output_format == "dotenv":
        # Dotenv parsers disagree on quote escaping and ${...} expansion.
        # Plain safe tokens round-trip in shell, python-dotenv and Docker.
        if not re.fullmatch(r"[A-Za-z0-9_./+=:@%-]+", value):
            raise ValueError(
                "This key needs parser-specific dotenv escaping; use raw or a shell format."
            )
        return f"{env_name}={value}\n"
    if output_format == "sh":
        return f"export {env_name}={shlex.quote(value)}\n"
    if output_format == "powershell":
        return f"$env:{env_name} = '" + value.replace("'", "''") + "'\n"
    raise ValueError("Unknown key export format.")


def windows_acl_tool() -> str:
    try:
        return local_files.windows_acl_tool()
    except ValueError:
        raise ValueError(
            "Private file export requires PowerShell on Windows. Alternatively use --stdout or api-key run."
        ) from None


def restrict_windows_file(path: str) -> None:
    local_files.restrict_windows_file(path, tool=windows_acl_tool())


def export_private_key(content: str, output: Path) -> None:
    """Publish a complete private file atomically, never replacing a path."""
    fd, temporary = tempfile.mkstemp(prefix=".inspire-key-", dir=output.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            if sys.platform == "win32":
                restrict_windows_file(temporary)
            elif os.fstat(stream.fileno()).st_mode & 0o777 != 0o600:
                raise ValueError(
                    "The destination filesystem did not enforce private 0600 permissions."
                )
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, output)
    finally:
        os.unlink(temporary)
