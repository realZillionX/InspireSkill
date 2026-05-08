"""Interactive shell support for distributed training job instances."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import select
import signal
import shutil
import socket
import ssl
import struct
import sys
import termios
import tty
from dataclasses import dataclass
from types import TracebackType
from typing import BinaryIO
from urllib.parse import urlencode, urlsplit

import click

from inspire.platform.web.browser_api.core import _browser_api_path, _get_base_url
from inspire.platform.web.session import WebSession, get_web_session
from inspire.platform.web.session.proxy import get_rtunnel_proxy_override

RUNNING_INSTANCE_STATUS = "instance_running"
SHELL_BOOTSTRAP = "command -v bash >/dev/null 2>&1 && exec bash || exec sh\n"
CTRL_RIGHT_BRACKET = b"\x1d"


class JobShellError(RuntimeError):
    """Raised when a job shell cannot be opened."""


class JobShellAuthError(JobShellError):
    """Raised when the remote shell websocket rejects the session."""


@dataclass(frozen=True)
class JobInstance:
    """Normalized job instance metadata used by the shell selector."""

    name: str
    status: str
    rank: int | None
    raw: dict


def _instance_name(raw: dict) -> str:
    for key in ("name", "instance_name", "pod_name", "podName"):
        value = str(raw.get(key) or "").strip()
        if value:
            return value
    return ""


def _instance_status(raw: dict) -> str:
    for key in ("instance_status", "status", "instanceStatus"):
        value = str(raw.get(key) or "").strip()
        if value:
            return value
    return ""


def _instance_rank(raw: dict, name: str) -> int | None:
    for key in ("rank", "instance_rank", "global_rank", "index", "replica_index"):
        value = raw.get(key)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
    match = re.search(r"-(\d+)$", name)
    return int(match.group(1)) if match else None


def normalize_job_instances(items: list[dict]) -> list[JobInstance]:
    """Normalize raw ``instance_list`` items and keep entries with names."""
    instances: list[JobInstance] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _instance_name(item)
        if not name:
            continue
        instances.append(
            JobInstance(
                name=name,
                status=_instance_status(item),
                rank=_instance_rank(item, name),
                raw=item,
            )
        )
    return instances


def select_job_instance(
    instances: list[JobInstance],
    *,
    instance_name: str | None = None,
    rank: int | None = None,
    prompt: bool = False,
) -> JobInstance:
    """Select a running instance by name, rank, or human prompt."""
    running = [
        inst for inst in instances if inst.status.lower() == RUNNING_INSTANCE_STATUS.lower()
    ]
    if not running:
        raise JobShellError("No running instances found for this job.")

    if instance_name:
        matches = [inst for inst in running if inst.name == instance_name]
        if len(matches) == 1:
            return matches[0]
        if matches:
            raise JobShellError(f"Multiple running instances match {instance_name!r}.")
        candidates = ", ".join(inst.name for inst in running[:5])
        raise JobShellError(
            f"No running instance named {instance_name!r}. Running instances: {candidates}"
        )

    if rank is not None:
        matches = [inst for inst in running if inst.rank == rank]
        if len(matches) == 1:
            return matches[0]
        if matches:
            names = ", ".join(inst.name for inst in matches[:5])
            raise JobShellError(f"Multiple running instances have rank {rank}: {names}")
        candidates = ", ".join(
            f"{inst.name}(rank={inst.rank})" if inst.rank is not None else inst.name
            for inst in running[:5]
        )
        raise JobShellError(f"No running instance with rank {rank}. Running instances: {candidates}")

    if len(running) == 1:
        return running[0]

    candidates = "\n".join(
        f"  {idx}. {inst.name}"
        + (f" (rank={inst.rank})" if inst.rank is not None else "")
        for idx, inst in enumerate(running, start=1)
    )
    if prompt:
        click.echo("Multiple running instances found:")
        click.echo(candidates)
        choice = click.prompt(
            "Select instance",
            type=click.IntRange(1, len(running)),
            default=1,
            show_default=True,
        )
        return running[choice - 1]

    raise JobShellError(
        "Multiple running instances found. Pass --instance or --rank.\n" + candidates
    )


def build_remote_cmd_ws_url(job_id: str, instance_name: str) -> str:
    """Build the train-job remote shell websocket URL."""
    base_url = _get_base_url().rstrip("/")
    parsed = urlsplit(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    netloc = parsed.netloc
    path = _browser_api_path("/train_job/remote_cmd")
    query = urlencode({"job_id": job_id, "instance_name": instance_name})
    return f"{scheme}://{netloc}{path}?{query}"


def _cookie_value(session: WebSession, name: str) -> str | None:
    cookies = session.storage_state.get("cookies") if session.storage_state else None
    if isinstance(cookies, list):
        for cookie in cookies:
            if isinstance(cookie, dict) and cookie.get("name") == name:
                value = str(cookie.get("value") or "").strip()
                if value:
                    return value
    if session.cookies and session.cookies.get(name):
        return str(session.cookies[name])
    return None


def build_remote_cmd_headers(session: WebSession) -> dict[str, str]:
    """Build websocket handshake headers for the remote command endpoint."""
    base_url = _get_base_url().rstrip("/")
    cookie = _cookie_value(session, "inspire-session")
    if not cookie:
        raise JobShellAuthError("Missing inspire-session cookie in cached web session.")
    return {
        "Origin": base_url,
        "Cookie": f"inspire-session={cookie}",
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ),
    }


class _WebSocketClient:
    """Minimal RFC 6455 client for the platform PTY websocket."""

    def __init__(self, url: str, headers: dict[str, str], *, timeout: float = 30.0) -> None:
        self.url = url
        self.headers = headers
        self.timeout = timeout
        self.sock: socket.socket | ssl.SSLSocket | None = None
        self._recv_buffer = b""

    def __enter__(self) -> "_WebSocketClient":
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        del exc_type, exc, tb
        self.close()

    def fileno(self) -> int:
        if self.sock is None:
            raise JobShellError("websocket is not connected")
        return self.sock.fileno()

    def connect(self) -> None:
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"ws", "wss"}:
            raise JobShellError(f"Unsupported websocket scheme: {parsed.scheme}")
        host = parsed.hostname
        if not host:
            raise JobShellError("Websocket URL has no host")
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        raw = self._create_socket(parsed.scheme, host, port)
        if parsed.scheme == "wss":
            context = ssl.create_default_context()
            sock: socket.socket | ssl.SSLSocket = context.wrap_socket(raw, server_hostname=host)
        else:
            sock = raw
        sock.settimeout(self.timeout)

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        target = parsed.path or "/"
        if parsed.query:
            target = f"{target}?{parsed.query}"
        host_header = host if parsed.port is None else f"{host}:{port}"
        lines = [
            f"GET {target} HTTP/1.1",
            f"Host: {host_header}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
        ]
        lines.extend(f"{name}: {value}" for name, value in self.headers.items())
        request = "\r\n".join(lines) + "\r\n\r\n"
        sock.sendall(request.encode("ascii"))

        response, extra = self._read_http_response(sock)
        status_line = response.split("\r\n", 1)[0]
        parts = status_line.split()
        status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
        if status == 401:
            sock.close()
            raise JobShellAuthError("Remote shell websocket rejected the session (401).")
        if status != 101:
            sock.close()
            raise JobShellError(f"Remote shell websocket handshake failed: {status_line}")
        accept = self._header_value(response, "Sec-WebSocket-Accept")
        expected = base64.b64encode(
            hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
        ).decode("ascii")
        if accept and accept != expected:
            sock.close()
            raise JobShellError("Remote shell websocket handshake returned an invalid accept key.")
        self._recv_buffer = extra
        sock.settimeout(None)
        self.sock = sock

    def _create_socket(self, scheme: str, host: str, port: int) -> socket.socket:
        proxy_url = self._proxy_url(scheme)
        if not proxy_url:
            return socket.create_connection((host, port), timeout=self.timeout)

        proxy = urlsplit(proxy_url)
        if proxy.scheme not in {"http", "https"}:
            raise JobShellError(
                "Job shell websocket proxy only supports HTTP(S) proxies. "
                f"Configured proxy scheme: {proxy.scheme or 'unknown'}"
            )
        proxy_host = proxy.hostname
        if not proxy_host:
            return socket.create_connection((host, port), timeout=self.timeout)
        proxy_port = proxy.port or (443 if proxy.scheme == "https" else 80)
        sock = socket.create_connection((proxy_host, proxy_port), timeout=self.timeout)
        if proxy.scheme == "https":
            context = ssl.create_default_context()
            sock = context.wrap_socket(sock, server_hostname=proxy_host)
        connect_lines = [
            f"CONNECT {host}:{port} HTTP/1.1",
            f"Host: {host}:{port}",
        ]
        if proxy.username:
            userinfo = f"{proxy.username}:{proxy.password or ''}"
            token = base64.b64encode(userinfo.encode()).decode("ascii")
            connect_lines.append(f"Proxy-Authorization: Basic {token}")
        request = "\r\n".join(connect_lines) + "\r\n\r\n"
        sock.sendall(request.encode("ascii"))
        response, _ = self._read_http_response(sock)
        status_line = response.split("\r\n", 1)[0]
        parts = status_line.split()
        status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
        if status != 200:
            sock.close()
            raise JobShellError(f"Proxy CONNECT failed: {status_line}")
        return sock

    @staticmethod
    def _proxy_url(scheme: str) -> str:
        del scheme
        return str(get_rtunnel_proxy_override() or "").strip()

    @staticmethod
    def _read_http_response(sock: socket.socket | ssl.SSLSocket) -> tuple[str, bytes]:
        chunks: list[bytes] = []
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
            data = b"".join(chunks)
            if len(data) > 65536:
                raise JobShellError("Remote shell websocket handshake response is too large.")
        header, _, extra = data.partition(b"\r\n\r\n")
        return (header + b"\r\n\r\n").decode("iso-8859-1", errors="replace"), extra

    @staticmethod
    def _header_value(response: str, name: str) -> str | None:
        prefix = f"{name.lower()}:"
        for line in response.split("\r\n")[1:]:
            if line.lower().startswith(prefix):
                return line.split(":", 1)[1].strip()
        return None

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8", errors="ignore"))

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        if self.sock is None:
            raise JobShellError("websocket is not connected")
        first = 0x80 | opcode
        length = len(payload)
        mask = os.urandom(4)
        if length < 126:
            header = struct.pack("!BB", first, 0x80 | length)
        elif length <= 0xFFFF:
            header = struct.pack("!BBH", first, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", first, 0x80 | 127, length)
        masked = bytes(byte ^ mask[idx % 4] for idx, byte in enumerate(payload))
        self.sock.sendall(header + mask + masked)

    def recv_frame(self) -> tuple[int, bytes]:
        if self.sock is None:
            raise JobShellError("websocket is not connected")
        header = self._recv_exact(2)
        first, second = header[0], header[1]
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._recv_exact(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._recv_exact(8))[0]
        mask = self._recv_exact(4) if masked else b""
        payload = self._recv_exact(length) if length else b""
        if masked:
            payload = bytes(byte ^ mask[idx % 4] for idx, byte in enumerate(payload))
        return opcode, payload

    def _recv_exact(self, size: int) -> bytes:
        if self.sock is None:
            raise JobShellError("websocket is not connected")
        chunks: list[bytes] = []
        remaining = size
        if self._recv_buffer:
            chunk = self._recv_buffer[:remaining]
            chunks.append(chunk)
            remaining -= len(chunk)
            self._recv_buffer = self._recv_buffer[len(chunk) :]
        while remaining > 0:
            chunk = self.sock.recv(remaining)
            if not chunk:
                raise EOFError("websocket closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def close(self) -> None:
        sock = self.sock
        if sock is None:
            return
        try:
            self._send_frame(0x8)
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass
        self.sock = None


def _terminal_size() -> tuple[int, int]:
    size = shutil.get_terminal_size(fallback=(80, 24))
    return size.columns, size.lines


def _stty_command() -> str:
    columns, rows = _terminal_size()
    return f"stty columns {columns} rows {rows}\n"


def _write_stdout(stdout: BinaryIO, payload: bytes) -> None:
    stdout.write(payload)
    stdout.flush()


def run_remote_shell(
    *,
    job_id: str,
    instance_name: str,
    session: WebSession,
    stdin=None,  # noqa: ANN001
    stdout=None,  # noqa: ANN001
    websocket_cls: type[_WebSocketClient] = _WebSocketClient,
) -> int:
    """Open the remote PTY websocket and proxy local stdio."""
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    stdout_buffer = getattr(stdout, "buffer", stdout)
    ws_url = build_remote_cmd_ws_url(job_id, instance_name)
    headers = build_remote_cmd_headers(session)
    old_term = None
    raw_mode = bool(getattr(stdin, "isatty", lambda: False)())

    with websocket_cls(ws_url, headers) as ws:
        ws.send_text(SHELL_BOOTSTRAP)
        ws.send_text(_stty_command())

        def resize_handler(signum, frame):  # noqa: ANN001
            del signum, frame
            try:
                ws.send_text(_stty_command())
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
                        _write_stdout(stdout_buffer, payload)
                if stdin in ready:
                    data = os.read(stdin.fileno(), 4096)
                    if not data:
                        stdin_open = False
                        continue
                    if CTRL_RIGHT_BRACKET in data:
                        return 0
                    ws.send_text(data.decode("utf-8", errors="ignore"))
        finally:
            if raw_mode and old_term is not None:
                termios.tcsetattr(stdin.fileno(), termios.TCSADRAIN, old_term)
                if previous_winch is not None:
                    signal.signal(signal.SIGWINCH, previous_winch)


def open_job_shell(
    *,
    job_id: str,
    instance_name: str,
    session: WebSession | None = None,
    websocket_cls: type[_WebSocketClient] = _WebSocketClient,
) -> int:
    """Open a job shell, refreshing the web session once after a 401 handshake."""
    active_session = session or get_web_session()
    try:
        return run_remote_shell(
            job_id=job_id,
            instance_name=instance_name,
            session=active_session,
            websocket_cls=websocket_cls,
        )
    except JobShellAuthError:
        refreshed = get_web_session(force_refresh=True)
        return run_remote_shell(
            job_id=job_id,
            instance_name=instance_name,
            session=refreshed,
            websocket_cls=websocket_cls,
        )
