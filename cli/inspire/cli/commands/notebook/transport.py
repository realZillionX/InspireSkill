from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from inspire.cli.context import EXIT_GENERAL_ERROR, Context
from inspire.cli.utils.errors import emit_error, exit_with_error
from inspire.cli.utils.notebook_cli import (
    WEB_AUTH_HINT,
    get_base_url,
    require_web_session,
)
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.platform.web import browser_api as browser_api_module

from .gpu_model import notebook_gpu_model
from .notebook_lookup import _notebook_compute_group, _resolve_notebook_target

if TYPE_CHECKING:
    from inspire.platform.web.session import WebSession

logger = logging.getLogger(__name__)

NotebookExecTransport = Literal["ssh", "jupyter"]

# Machines built on these GPU models are SSH-restricted on the platform.
SSH_RESTRICTED_GPU_MODELS: tuple[str, ...] = ("H100", "H200")

_RUNNING_NOTEBOOK_STATUS = "RUNNING"


def gpu_model_supports_ssh(gpu_model: str) -> bool:
    """Whether a machine reporting this GPU model can be reached over SSH/rtunnel."""
    upper = str(gpu_model or "").upper()
    return not any(model in upper for model in SSH_RESTRICTED_GPU_MODELS)


def require_notebook_gpu_model(
    ctx: Context,
    *,
    notebook: str,
    notebook_id: str,
    compute_group: str,
    session: WebSession | None,
) -> str:
    """Read the machine's GPU model, or exit explaining why it stayed silent.

    The probe rides JupyterTerminal, which is also the restricted transport, so
    a machine that cannot answer cannot run a command either way. Rather than
    guess a transport for it, say what is actually wrong -- almost always a
    notebook that is not running -- and stop.
    """
    gpu_model = notebook_gpu_model(
        notebook_id=notebook_id,
        compute_group=compute_group,
        session=session,
    )
    if gpu_model is not None:
        return gpu_model

    name = scrub_raw_ids(notebook)
    status = _notebook_status(notebook_id=notebook_id, session=session)
    if status and status != _RUNNING_NOTEBOOK_STATUS:
        exit_with_error(
            ctx,
            "NotebookNotRunning",
            f"Notebook {name} is {status}.",
            EXIT_GENERAL_ERROR,
            hint=(
                "`ssh`, `exec`, `shell` and `scp` all need a running notebook. Start it with "
                f"`inspire notebook start {name} --workspace <workspace>`, then retry."
            ),
        )
    exit_with_error(
        ctx,
        "NotebookUnreachable",
        f"Cannot reach notebook {name}: its JupyterTerminal did not respond.",
        EXIT_GENERAL_ERROR,
        hint=(
            "The GPU model decides SSH vs JupyterTerminal, and it is read over that "
            "terminal. Retry with `inspire --debug notebook exec ...` to record whether "
            "access URL, REST, proxy, or WebSocket setup failed. If the web terminal works, "
            "check HTTP(S)_PROXY and NO_PROXY before restarting the notebook."
        ),
    )
    raise RuntimeError("unreachable")


def _notebook_status(*, notebook_id: str, session: WebSession | None) -> str:
    try:
        detail = browser_api_module.get_notebook_detail(
            notebook_id=notebook_id,
            session=session,
        )
    except Exception:  # noqa: BLE001 - only used to sharpen an error message
        logger.debug("Notebook status lookup failed", exc_info=True)
        return ""
    return str((detail or {}).get("status") or "").strip().upper()


@dataclass(frozen=True)
class NotebookTransportPolicy:
    notebook: str
    notebook_id: str
    gpu_model: str
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


def restricted_gpu_label(gpu_model: str) -> str:
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
    notebook_id, _workspace_id, compute_group = _resolve_notebook_target(
        ctx,
        session=session,
        base_url=get_base_url(account=account),
        identifier=notebook,
        json_output=ctx.json_output,
        workspace_ids=workspace_ids,
        pick=pick,
    )
    if not compute_group:
        # The group is the probe's cache key, and name resolution normally hands
        # it back for free from the identity cache or the list response it
        # already fetched. Only a platform payload that omitted it costs a
        # detail request; without one, every notebook in the group probes again.
        detail = browser_api_module.get_notebook_detail(
            notebook_id=notebook_id,
            session=session,
        )
        compute_group = _notebook_compute_group(detail)
    return NotebookTransportPolicy(
        notebook=notebook,
        notebook_id=notebook_id,
        gpu_model=require_notebook_gpu_model(
            ctx,
            notebook=notebook,
            notebook_id=notebook_id,
            compute_group=compute_group,
            session=session,
        ),
        session=session,
    )
