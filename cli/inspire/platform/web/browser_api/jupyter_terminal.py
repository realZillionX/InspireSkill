from __future__ import annotations

import base64
import json
import os
import re
import select
import signal
import shlex
import sys
import termios
import tty
import uuid
from dataclasses import dataclass
from typing import Protocol, Optional
from urllib.parse import urlsplit

from inspire.cli.utils.terminal_io import write_stream_output
from inspire.platform.web.browser_api import rtunnel as rtunnel_module
from inspire.platform.web.browser_api.core import (
    _in_asyncio_loop,
    _launch_browser,
    _new_context,
    _run_in_thread,
)
from inspire.platform.web.browser_api.playwright_notebooks import open_notebook_lab
from inspire.platform.web.session import WebSession
from inspire.platform.web.session import get_web_session

JUPYTER_DONE_PREFIX = "__INSPIRE_JUPYTER_DONE_"
MISSING_MARKER_RETURN_CODE = 124
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


class _Evaluatable(Protocol):
    def evaluate(self, _expression: str, arg: object | None = None) -> object: ...


class _LabFrameLike(_Evaluatable, Protocol):
    url: str


class _TextWebSocket(Protocol):
    def send_text(self, text: str) -> None: ...


@dataclass(frozen=True)
class JupyterCommandResult:
    returncode: int
    output: str
    completed: bool
    marker: str


@dataclass(frozen=True)
class NetworkEndpointResult:
    group: str
    host: str
    port: int
    ok: bool


@dataclass(frozen=True)
class NotebookNetworkProbe:
    public_internet: bool | None
    endpoints: tuple[NetworkEndpointResult, ...]

    @property
    def public_successes(self) -> list[str]:
        return [
            f"{item.host}:{item.port}"
            for item in self.endpoints
            if item.group == "PUBLIC" and item.ok
        ]

    @property
    def public_failures(self) -> list[str]:
        return [
            f"{item.host}:{item.port}"
            for item in self.endpoints
            if item.group == "PUBLIC" and not item.ok
        ]


PUBLIC_PROBE_ENDPOINTS: tuple[tuple[str, int], ...] = (
    ("www.baidu.com", 443),
    ("www.qq.com", 443),
    ("www.163.com", 443),
    ("mirrors.tuna.tsinghua.edu.cn", 443),
)


def new_completion_marker() -> str:
    return f"{JUPYTER_DONE_PREFIX}{uuid.uuid4().hex}"


def build_jupyter_exec_command(command: str, *, marker: str) -> str:
    script = "\n".join(
        [
            "set +e",
            "(",
            command,
            ")",
            "__inspire_status=$?",
            f"printf '\\n%s:exit:%s\\n' {shlex.quote(marker)} \"$__inspire_status\"",
            "exit \"$__inspire_status\"",
            "",
        ]
    )
    encoded = base64.b64encode(script.encode()).decode("ascii")
    return f"echo '{encoded}' | base64 -d | bash\r"


def _strip_jupyter_terminal_prelude(output: str) -> str:
    lines = output.splitlines(keepends=True)
    command_line_end = 0
    for index, line in enumerate(lines):
        plain = _ANSI_CSI_RE.sub("", line)
        if "echo '" in plain and "| base64 -d | bash" in plain:
            command_line_end = index + 1

    cleaned = "".join(lines[command_line_end:])
    return re.sub(r"^(?:\x1b\[\?2004[lh]\r?|\r)+", "", cleaned)


def parse_jupyter_exec_output(raw_output: str, *, marker: str) -> JupyterCommandResult:
    pattern = re.compile(rf"{re.escape(marker)}:exit:(\d+)\s*")
    match = pattern.search(raw_output)
    if not match:
        return JupyterCommandResult(
            returncode=MISSING_MARKER_RETURN_CODE,
            output=raw_output,
            completed=False,
            marker=marker,
        )
    output = _strip_jupyter_terminal_prelude(raw_output[: match.start()])
    return JupyterCommandResult(
        returncode=int(match.group(1)),
        output=output,
        completed=True,
        marker=marker,
    )


def build_network_probe_command(
    *,
    public_endpoints: tuple[tuple[str, int], ...] = PUBLIC_PROBE_ENDPOINTS,
) -> str:
    lines = [
        "probe_one() {",
        '  group="$1"; host="$2"; port="$3"',
        '  if timeout 3 bash -c "</dev/tcp/${host}/${port}" >/dev/null 2>&1; then',
        '    echo "$group $host $port ok"',
        "  else",
        '    echo "$group $host $port fail"',
        "  fi",
        "}",
    ]
    for host, port in public_endpoints:
        lines.append(f"probe_one PUBLIC {shlex.quote(host)} {int(port)} &")
    lines.append("wait")
    return "\n".join(lines)


def parse_network_probe_output(output: str) -> NotebookNetworkProbe:
    endpoints: list[NetworkEndpointResult] = []
    for line in output.splitlines():
        parts = line.strip().split()
        if len(parts) != 4:
            continue
        group, host, port_raw, status = parts
        if group != "PUBLIC":
            continue
        if not port_raw.isdigit():
            continue
        endpoints.append(
            NetworkEndpointResult(
                group=group,
                host=host,
                port=int(port_raw),
                ok=status == "ok",
            )
        )
    public_items = [item for item in endpoints if item.group == "PUBLIC"]
    public = any(item.ok for item in public_items) if public_items else None
    return NotebookNetworkProbe(
        public_internet=public,
        endpoints=tuple(endpoints),
    )


def build_jupyter_terminal_ws_url(lab_url: str, term_name: str) -> str:
    return rtunnel_module._build_terminal_websocket_url(lab_url, term_name)


def build_shell_bootstrap(*, cwd: str | None, env_exports: str) -> str:
    if cwd:
        return f"{env_exports}cd {shlex.quote(cwd)} && exec $SHELL -l\r"
    if env_exports:
        return f"{env_exports}exec $SHELL -l\r"
    return "exec $SHELL -l\r"


def _send_terminal_command_capture_via_websocket(
    page_or_frame: _Evaluatable,
    *,
    ws_url: str,
    stdin_data: str,
    timeout_ms: int,
    marker: str,
) -> Optional[JupyterCommandResult]:
    prompt_timeout_ms = max(0, min(timeout_ms - 500, 3000))
    try:
        raw_output = page_or_frame.evaluate(
            """
            async ({ wsUrl, stdinData, timeoutMs, promptTimeoutMs, marker }) => {
              return await new Promise((resolve, reject) => {
                let settled = false;
                let sent = false;
                let socket = null;
                let output = "";
                const finish = (value) => {
                  if (settled) return;
                  settled = true;
                  clearTimeout(timer);
                  try {
                    if (socket) socket.close();
                  } catch (_) {}
                  resolve(value);
                };
                const fail = (err) => {
                  if (settled) return;
                  settled = true;
                  clearTimeout(timer);
                  try {
                    if (socket) socket.close();
                  } catch (_) {}
                  reject(err);
                };
                const timer = setTimeout(() => finish(output), timeoutMs);
                const doSend = () => {
                  if (sent || settled) return;
                  sent = true;
                  const CHUNK = 2048;
                  const DELAY = 50;
                  const chunks = [];
                  for (let i = 0; i < stdinData.length; i += CHUNK) {
                    chunks.push(stdinData.slice(i, i + CHUNK));
                  }
                  let idx = 0;
                  const next = () => {
                    if (settled) return;
                    try {
                      socket.send(JSON.stringify(["stdin", chunks[idx]]));
                    } catch (err) {
                      fail(err);
                      return;
                    }
                    idx++;
                    if (idx < chunks.length) {
                      setTimeout(next, DELAY);
                    }
                  };
                  next();
                };
                socket = new WebSocket(wsUrl);
                socket.onopen = () => {
                  setTimeout(doSend, promptTimeoutMs);
                };
                socket.onerror = () => fail(new Error("terminal websocket error"));
                socket.onmessage = (event) => {
                  let msg = null;
                  try {
                    msg = JSON.parse(event.data);
                  } catch (_) {
                    return;
                  }
                  if (!Array.isArray(msg) || msg.length < 2) return;
                  if (msg[0] !== "stdout") return;
                  const text = String(msg[1] || "");
                  output += text;
                  if (!sent && /[$#]\\s*$/.test(text)) {
                    doSend();
                  }
                  if (sent) {
                    const donePrefix = marker + ":exit:";
                    const doneAt = output.indexOf(donePrefix);
                    if (
                      doneAt >= 0 &&
                      /^\\d+\\s/.test(output.slice(doneAt + donePrefix.length))
                    ) {
                      finish(output);
                    }
                  }
                };
              });
            }
            """,
            {
                "wsUrl": ws_url,
                "stdinData": stdin_data,
                "timeoutMs": max(int(timeout_ms), 1000),
                "promptTimeoutMs": prompt_timeout_ms,
                "marker": marker,
            },
        )
    except Exception:
        return None
    return parse_jupyter_exec_output(str(raw_output or ""), marker=marker)


def run_command_capture_in_existing_lab(
    *,
    context: object,
    lab_frame: _LabFrameLike,
    command: str,
    timeout_ms: int,
    marker: str | None = None,
) -> JupyterCommandResult:
    effective_marker = marker or new_completion_marker()
    term_name = rtunnel_module._create_terminal_via_api(context, lab_frame.url)
    if not term_name:
        return JupyterCommandResult(
            returncode=MISSING_MARKER_RETURN_CODE,
            output="",
            completed=False,
            marker=effective_marker,
        )
    try:
        ws_url = rtunnel_module._build_terminal_websocket_url(lab_frame.url, term_name)
        result = _send_terminal_command_capture_via_websocket(
            lab_frame,
            ws_url=ws_url,
            stdin_data=build_jupyter_exec_command(command, marker=effective_marker),
            timeout_ms=timeout_ms,
            marker=effective_marker,
        )
        if result is None:
            return JupyterCommandResult(
                returncode=MISSING_MARKER_RETURN_CODE,
                output="",
                completed=False,
                marker=effective_marker,
            )
        return result
    finally:
        rtunnel_module._delete_terminal_via_api(
            context,
            lab_url=lab_frame.url,
            term_name=term_name,
        )


def run_command_capture_in_notebook(
    *,
    notebook_id: str,
    command: str,
    session: Optional[WebSession] = None,
    headless: bool = True,
    timeout: int = 60,
    marker: str | None = None,
) -> JupyterCommandResult:
    if _in_asyncio_loop():
        return _run_in_thread(
            _run_command_capture_in_notebook_sync,
            notebook_id=notebook_id,
            command=command,
            session=session,
            headless=headless,
            timeout=timeout,
            marker=marker,
        )
    return _run_command_capture_in_notebook_sync(
        notebook_id=notebook_id,
        command=command,
        session=session,
        headless=headless,
        timeout=timeout,
        marker=marker,
    )


def probe_notebook_network(
    *,
    notebook_id: str,
    session: Optional[WebSession] = None,
    timeout: int = 30,
) -> NotebookNetworkProbe:
    result = run_command_capture_in_notebook(
        notebook_id=notebook_id,
        command=build_network_probe_command(),
        session=session,
        timeout=timeout,
    )
    if not result.completed:
        return NotebookNetworkProbe(
            public_internet=None,
            endpoints=(),
        )
    return parse_network_probe_output(result.output)


def _jupyter_ws_headers(session: WebSession, ws_url: str) -> dict[str, str]:
    parsed = urlsplit(ws_url)
    origin_scheme = "https" if parsed.scheme == "wss" else "http"
    headers = {
        "Origin": f"{origin_scheme}://{parsed.netloc}",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }
    cookie_pairs: list[str] = []
    cookies = session.storage_state.get("cookies") if session.storage_state else None
    if isinstance(cookies, list):
        for cookie in cookies:
            if not isinstance(cookie, dict):
                continue
            name = str(cookie.get("name") or "").strip()
            value = str(cookie.get("value") or "").strip()
            if name and value:
                cookie_pairs.append(f"{name}={value}")
    for name, value in (session.cookies or {}).items():
        if name and value:
            pair = f"{name}={value}"
            if pair not in cookie_pairs:
                cookie_pairs.append(pair)
    if cookie_pairs:
        headers["Cookie"] = "; ".join(cookie_pairs)
    return headers


def _send_jupyter_stdin(ws: _TextWebSocket, text: str) -> None:
    ws.send_text(json.dumps(["stdin", text]))


def _run_jupyter_terminal_shell(
    *,
    ws_url: str,
    session: WebSession,
    bootstrap: str,
    stdin=None,  # noqa: ANN001
    stdout=None,  # noqa: ANN001
) -> int:
    from inspire.cli.utils.job_shell import (
        CTRL_RIGHT_BRACKET,
        _WebSocketClient,
        _stty_command,
    )

    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stdout_buffer = getattr(stdout, "buffer", stdout)
    headers = _jupyter_ws_headers(session, ws_url)
    old_term = None
    raw_mode = bool(getattr(stdin, "isatty", lambda: False)())

    with _WebSocketClient(ws_url, headers) as ws:
        _send_jupyter_stdin(ws, bootstrap)
        _send_jupyter_stdin(ws, _stty_command().replace("\n", "\r"))

        def resize_handler(signum, frame):  # noqa: ANN001
            del signum, frame
            try:
                _send_jupyter_stdin(ws, _stty_command().replace("\n", "\r"))
            except Exception:
                pass

        previous_winch = None
        if raw_mode:
            old_term = termios.tcgetattr(stdin.fileno())
            tty.setraw(stdin.fileno())
            previous_winch = signal.getsignal(signal.SIGWINCH)
            signal.signal(signal.SIGWINCH, resize_handler)
        try:
            stdin_open = True
            while True:
                readers = [ws]
                if stdin_open and not getattr(stdin, "closed", False):
                    readers.append(stdin)
                ready, _, _ = select.select(readers, [], [])
                if ws in ready:
                    try:
                        opcode, payload = ws.recv_frame()
                    except EOFError:
                        return 0
                    if opcode == 0x8:
                        return 0
                    if opcode == 0x9:
                        ws._send_frame(0xA, payload)
                        continue
                    if opcode in {0x1, 0x2}:
                        text = payload.decode("utf-8", errors="ignore")
                        try:
                            msg = json.loads(text)
                        except json.JSONDecodeError:
                            write_stream_output(stdout_buffer, payload)
                            continue
                        if isinstance(msg, list) and len(msg) >= 2 and msg[0] == "stdout":
                            write_stream_output(
                                stdout_buffer,
                                str(msg[1] or "").encode(),
                            )
                if stdin in ready:
                    data = os.read(stdin.fileno(), 4096)
                    if not data:
                        stdin_open = False
                        continue
                    if CTRL_RIGHT_BRACKET in data:
                        return 0
                    _send_jupyter_stdin(ws, data.decode("utf-8", errors="ignore"))
        finally:
            if raw_mode and old_term is not None:
                termios.tcsetattr(stdin.fileno(), termios.TCSADRAIN, old_term)
                if previous_winch is not None:
                    signal.signal(signal.SIGWINCH, previous_winch)


def open_jupyter_terminal_shell(
    *,
    notebook_id: str,
    session: Optional[WebSession] = None,
    cwd: str | None = None,
    env_exports: str = "",
    timeout: int = 60,
) -> int:
    from playwright.sync_api import sync_playwright

    active_session = session or get_web_session()
    timeout_ms = max(int(timeout * 1000), 1000)
    with sync_playwright() as p:
        browser = _launch_browser(
            p,
            headless=True,
            account=active_session.account,
        )
        context = _new_context(
            browser,
            storage_state=active_session.storage_state,
            account=active_session.account,
        )
        page = context.new_page()

        term_name: str | None = None
        lab_url = ""
        try:
            lab_frame = open_notebook_lab(
                page,
                notebook_id=notebook_id,
                session=active_session,
                timeout=timeout_ms,
                prefer_direct=True,
            )
            lab_url = lab_frame.url
            term_name = rtunnel_module._create_terminal_via_api(context, lab_url)
            if not term_name:
                return MISSING_MARKER_RETURN_CODE
            ws_url = build_jupyter_terminal_ws_url(lab_url, term_name)
            return _run_jupyter_terminal_shell(
                ws_url=ws_url,
                session=active_session,
                bootstrap=build_shell_bootstrap(cwd=cwd, env_exports=env_exports),
            )
        finally:
            if term_name and lab_url:
                rtunnel_module._delete_terminal_via_api(
                    context,
                    lab_url=lab_url,
                    term_name=term_name,
                )
            try:
                context.close()
            finally:
                browser.close()


def _run_command_capture_in_notebook_sync(
    *,
    notebook_id: str,
    command: str,
    session: Optional[WebSession],
    headless: bool,
    timeout: int,
    marker: str | None,
) -> JupyterCommandResult:
    from playwright.sync_api import sync_playwright

    if session is None:
        session = get_web_session()

    effective_marker = marker or new_completion_marker()
    timeout_ms = max(int(timeout * 1000), 1000)
    with sync_playwright() as p:
        browser = _launch_browser(
            p,
            headless=headless,
            account=session.account,
        )
        context = _new_context(
            browser,
            storage_state=session.storage_state,
            account=session.account,
        )
        page = context.new_page()

        try:
            lab_frame = open_notebook_lab(
                page,
                notebook_id=notebook_id,
                timeout=timeout_ms,
                session=session,
                prefer_direct=True,
            )
            return run_command_capture_in_existing_lab(
                context=context,
                lab_frame=lab_frame,
                command=command,
                timeout_ms=timeout_ms,
                marker=effective_marker,
            )
        finally:
            try:
                context.close()
            finally:
                browser.close()
