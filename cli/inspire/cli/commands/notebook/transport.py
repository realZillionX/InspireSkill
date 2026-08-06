from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from inspire.cli.context import EXIT_GENERAL_ERROR, Context
from inspire.cli.utils.errors import emit_error
from inspire.cli.utils.notebook_cli import (
    WEB_AUTH_HINT,
    get_base_url,
    require_web_session,
)
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.platform.web import browser_api as browser_api_module

from .notebook_lookup import _notebook_compute_group, _resolve_notebook_id

if TYPE_CHECKING:
    from inspire.platform.web.session import WebSession

NotebookExecTransport = Literal["ssh", "jupyter"]

# Compute groups built on these GPU models are SSH-restricted on the platform.
SSH_RESTRICTED_GPU_MODELS: tuple[str, ...] = ("H100", "H200")


def group_supports_ssh(compute_group: str) -> bool:
    """Whether notebooks in this compute group can be reached over SSH/rtunnel.

    Group names carry their GPU model (``训练区-H200-1号机房``,
    ``开发区-H100-cuda12.8版本-119核``), so the name alone decides the
    transport. Deciding from group metadata the notebook detail already
    carries keeps the preflight to a single cheap API call.
    """
    upper = str(compute_group or "").upper()
    return not any(model in upper for model in SSH_RESTRICTED_GPU_MODELS)


@dataclass(frozen=True)
class NotebookTransportPolicy:
    notebook: str
    notebook_id: str
    compute_group: str
    session: WebSession | None = field(default=None, repr=False, compare=False)

    @property
    def allow_ssh(self) -> bool:
        return group_supports_ssh(self.compute_group)

    @property
    def allow_proxy_url(self) -> bool:
        return group_supports_ssh(self.compute_group)

    @property
    def exec_transport(self) -> NotebookExecTransport:
        return "ssh" if self.allow_ssh else "jupyter"

    @property
    def block_hint(self) -> str:
        return (
            "Use `inspire notebook exec` or `inspire notebook shell`; "
            "restricted notebooks use JupyterTerminal instead of SSH/rtunnel."
        )


def restricted_group_label(compute_group: str) -> str:
    group = scrub_raw_ids(str(compute_group or "").strip())
    return f"compute group {group}" if group else "an H100/H200 compute group"


def emit_ssh_policy_error(ctx: Context, policy: NotebookTransportPolicy) -> int:
    return emit_error(
        ctx,
        "PolicyBlocked",
        (
            "SSH/rtunnel access is blocked on H100/H200 notebooks: "
            f"{scrub_raw_ids(policy.notebook)} runs in "
            f"{restricted_group_label(policy.compute_group)}"
        ),
        EXIT_GENERAL_ERROR,
        hint=policy.block_hint,
    )


def preflight_notebook_transport_policy(
    ctx: Context,
    *,
    notebook: str,
    workspace: str | None,
    account: str | None = None,
    pick: int | None = None,
) -> NotebookTransportPolicy:
    from inspire.config.workspaces import resolve_workspace_query_scope

    session = (
        require_web_session(ctx, hint=WEB_AUTH_HINT, account=account)
        if account
        else require_web_session(ctx, hint=WEB_AUTH_HINT)
    )
    if workspace:
        workspace_ids, _ = resolve_workspace_query_scope(
            workspace=workspace,
            session=session,
        )
    else:
        workspace_ids = None
    notebook_id, _workspace_id = _resolve_notebook_id(
        ctx,
        session=session,
        base_url=get_base_url(account=account),
        identifier=notebook,
        json_output=ctx.json_output,
        workspace_ids=workspace_ids,
        pick=pick,
    )
    detail = browser_api_module.get_notebook_detail(notebook_id=notebook_id, session=session)
    return NotebookTransportPolicy(
        notebook=notebook,
        notebook_id=notebook_id,
        compute_group=_notebook_compute_group(detail),
        session=session,
    )
