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

from .gpu_model import notebook_gpu_model
from .notebook_lookup import _resolve_notebook_id

if TYPE_CHECKING:
    from inspire.platform.web.session import WebSession

NotebookExecTransport = Literal["ssh", "jupyter"]

# Machines built on these GPU models are SSH-restricted on the platform.
SSH_RESTRICTED_GPU_MODELS: tuple[str, ...] = ("H100", "H200")


def gpu_model_supports_ssh(gpu_model: str | None) -> bool:
    """Whether a machine reporting this GPU model can be reached over SSH/rtunnel.

    ``None`` is the probe's "the machine did not answer", and it allows SSH: the
    probe runs over JupyterTerminal, so a machine that cannot answer cannot
    serve the restricted transport either. SSH is then the only transport with a
    chance of working, and it reports its own failure clearly.
    """
    upper = str(gpu_model or "").upper()
    return not any(model in upper for model in SSH_RESTRICTED_GPU_MODELS)


@dataclass(frozen=True)
class NotebookTransportPolicy:
    notebook: str
    notebook_id: str
    gpu_model: str | None
    session: WebSession | None = field(default=None, repr=False, compare=False)

    @property
    def allow_ssh(self) -> bool:
        return gpu_model_supports_ssh(self.gpu_model)

    @property
    def allow_proxy_url(self) -> bool:
        return gpu_model_supports_ssh(self.gpu_model)

    @property
    def exec_transport(self) -> NotebookExecTransport:
        return "ssh" if self.allow_ssh else "jupyter"

    @property
    def block_hint(self) -> str:
        return (
            "Use `inspire notebook exec` or `inspire notebook shell`; "
            "restricted notebooks use JupyterTerminal instead of SSH/rtunnel."
        )


def restricted_gpu_label(gpu_model: str | None) -> str:
    model = str(gpu_model or "").strip()
    return f"on {model} GPUs" if model else "on H100/H200 GPUs"


def emit_ssh_policy_error(ctx: Context, policy: NotebookTransportPolicy) -> int:
    return emit_error(
        ctx,
        "PolicyBlocked",
        (
            "SSH/rtunnel access is blocked on H100/H200 notebooks: "
            f"{scrub_raw_ids(policy.notebook)} runs "
            f"{restricted_gpu_label(policy.gpu_model)}"
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
    return NotebookTransportPolicy(
        notebook=notebook,
        notebook_id=notebook_id,
        gpu_model=notebook_gpu_model(notebook_id=notebook_id, session=session),
        session=session,
    )
