"""SDK operation helpers for browser-free remote execution."""

from __future__ import annotations
from dataclasses import replace
from inspire.platform.web.flow import call, perform_sync
from inspire.exec_output import DEFAULT_MAX_OUTPUT_BYTES, OutputTarget, validate_capture
from typing import Callable, Any
from inspire.config.env import build_env_exports
from inspire.platform.web.pty_socket import JobShellError, build_remote_cmd_ws_url
from inspire.platform.web.session.models import SessionExpiredError
from inspire.services.execution import remote_exec as core
from .compute_jobs import duration
from .exceptions import AuthenticationError, ValidationError
from .resources import Service


def shaped_command(
    service: Service,
    command: str,
    *,
    cwd: str | None,
    env: dict[str, str] | None,
    timeout: float,
    on_output: Callable[[str], None] | None,
    max_output_bytes: int | None = DEFAULT_MAX_OUTPUT_BYTES,
    output_to: OutputTarget = None,
    capture: bool = True,
) -> str:
    validate_capture(max_output_bytes, capture, output_to)
    duration(timeout, "timeout")
    if on_output is not None and not callable(on_output):
        raise ValidationError("on_output must be callable.")
    return core.build_remote_command(
        command,
        cwd=cwd,
        env=env,
        remote_env_exports=build_env_exports(service.client._config.remote_env),
    )


def authenticated_exec(
    service: Service, run: Callable[..., core.ExecResult], *, timeout: float, **kwargs: Any
) -> core.ExecResult:
    transport = service.client._transport
    output_received = False
    callback = kwargs.get("on_output")

    def observe(chunk: str) -> Any:
        nonlocal output_received
        output_received = True
        if callback is not None:
            return callback(chunk)

    kwargs["on_output"] = observe
    for attempt in range(2):
        session = service.session
        try:
            return perform_sync(call(run, session=session, timeout=min(timeout, transport.remaining()), **kwargs))
        except SessionExpiredError as error:
            if attempt or output_received:
                raise AuthenticationError(str(error)) from error
            transport._refresh()
    raise AssertionError("unreachable")


def workload_exec(
    service: Service,
    *,
    key: str,
    workload: str,
    rows: list[dict[str, Any]],
    instance: str | None,
    command: str,
    timeout: float,
    on_output: Callable[[str], None] | None,
    max_output_bytes: int | None = DEFAULT_MAX_OUTPUT_BYTES,
    output_to: OutputTarget = None,
    capture: bool = True,
) -> core.ExecResult:
    try:
        selected = core.select_exec_instance(workload, rows, instance)
    except JobShellError as error:
        raise ValidationError(str(error)) from error
    url = build_remote_cmd_ws_url(
        key, selected, workload=workload, base_url=service.client.base_url
    )
    result = authenticated_exec(
        service,
        core.exec_over_pty_websocket,
        timeout=timeout,
        url=url,
        command=command,
        on_output=on_output,
        max_output_bytes=max_output_bytes,
        output_to=output_to,
        capture=capture,
    )
    return replace(result, instance=selected)
