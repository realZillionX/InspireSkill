from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from inspire.bridge.tunnel import has_internet_for_gpu_type
from inspire.cli.context import EXIT_GENERAL_ERROR, Context
from inspire.cli.utils.errors import emit_error
from inspire.cli.utils.notebook_cli import (
    WEB_AUTH_HINT,
    get_base_url,
    require_web_session,
)
from inspire.platform.web import browser_api as browser_api_module

from .notebook_lookup import _notebook_gpu_type, _resolve_notebook_id

if TYPE_CHECKING:
    from inspire.platform.web.session import WebSession

NotebookExecTransport = Literal["ssh", "jupyter"]


@dataclass(frozen=True)
class NotebookTransportPolicy:
    notebook: str
    notebook_id: str
    public_internet: bool | None
    reason: str
    session: WebSession | None = field(default=None, repr=False, compare=False)

    @property
    def allow_ssh(self) -> bool:
        return self.public_internet is True

    @property
    def allow_proxy_url(self) -> bool:
        return self.public_internet is True

    @property
    def exec_transport(self) -> NotebookExecTransport:
        return "ssh" if self.allow_ssh else "jupyter"

    @property
    def block_hint(self) -> str:
        return (
            "Use `inspire notebook exec` or `inspire notebook shell`; "
            "restricted notebooks use JupyterTerminal instead of SSH/rtunnel."
        )


def emit_ssh_policy_error(ctx: Context, policy: NotebookTransportPolicy) -> int:
    return emit_error(
        ctx,
        "PolicyBlocked",
        (
            "SSH/rtunnel access is blocked on notebooks without public internet: "
            f"{policy.notebook}"
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
    timeout: int = 30,
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
    gpu_type = _notebook_gpu_type(detail)
    static_public = has_internet_for_gpu_type(gpu_type)
    public_internet: bool | None
    if static_public is False:
        # H100/H200 and other statically restricted notebook types must never
        # use SSH.  Avoid an expensive JupyterTerminal network probe here: it
        # would open a complete remote terminal only to select the same
        # restricted transport, then `notebook exec` would open a second one
        # for the user's command.
        public_internet = False
        reason = "static_gpu_policy"
    else:
        try:
            probe = browser_api_module.probe_notebook_network(
                notebook_id=notebook_id,
                session=session,
                timeout=timeout,
            )
            public_internet = probe.public_internet
            reason = "live_probe"
        except Exception:
            public_internet = None
            reason = "static_gpu_fallback"
    return NotebookTransportPolicy(
        notebook=notebook,
        notebook_id=notebook_id,
        public_internet=public_internet,
        reason=reason,
        session=session,
    )
