from __future__ import annotations

import pytest

from inspire.config import ConfigError
from inspire.config.workspaces import validate_workspace_operation_name


@pytest.mark.parametrize("workspace", ["all", "current"])
def test_operation_workspace_rejects_query_sentinels(workspace: str) -> None:
    with pytest.raises(ConfigError, match="workspace name"):
        validate_workspace_operation_name(workspace)


def test_operation_workspace_requires_a_visible_name() -> None:
    with pytest.raises(ConfigError, match="Workspace selection is invalid\\."):
        validate_workspace_operation_name("ws-12345678-1234-1234-1234-123456789abc")


def test_operation_workspace_accepts_visible_name() -> None:
    assert validate_workspace_operation_name("CPU资源空间") == "CPU资源空间"
