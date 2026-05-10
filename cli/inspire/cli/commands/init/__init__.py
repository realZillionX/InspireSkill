"""Init command package.

This module remains the stable import surface for `inspire init` and selected helpers
used by tests.
"""

from __future__ import annotations

from .env_detect import _detect_env_vars, _generate_toml_content
from .init_cmd import init
from .templates import CONFIG_TEMPLATE

__all__ = [
    "CONFIG_TEMPLATE",
    "_detect_env_vars",
    "_generate_toml_content",
    "init",
]
