"""Browser-free PTY websocket transport and instance selection."""

from __future__ import annotations

from inspire.platform.web.offload import offload
import contextlib
import asyncio
from typing import Generator, Any, cast
import base64
import hashlib
import os
import re
import socket
import ssl
import struct
from dataclasses import dataclass
from types import TracebackType
from urllib.parse import urlencode, urlsplit
from inspire.platform.web.browser_api.core import _get_base_url
from inspire.platform.web.session import WebSession
from inspire.platform.web.session.proxy import get_websocket_proxy

RUNNING_INSTANCE_STATUS = "instance_running"
CTRL_RIGHT_BRACKET = b"\x1d"

# Typing `exit` ends the remote shell, but the gateway keeps the websocket
# open and sends nothing further — verified 2026-08-15 against a running H100
# instance, which stayed silent for 40s with no close frame. So the shell has
# to announce its own exit: run it as a child instead of `exec`ing it, and
# have the surviving parent print a marker.
#
# The literal is split across two quoted strings so the bootstrap line the
# terminal echoes back never contains the contiguous marker — only the
# shell's own `printf` emits it, which is what makes matching it safe.
SHELL_EXIT_MARKER = "INSPIRE_SHELL_CLOSED_7f31a0"


def shell_exit_announce(marker: str = SHELL_EXIT_MARKER) -> str:
    """Return the `printf` that emits the exit marker without echoing it."""
    head, tail = marker[:15], marker[15:]
    return f"printf '%s' '{head}''{tail}'"


SHELL_BOOTSTRAP = f"command -v bash >/dev/null 2>&1 && bash || sh; {shell_exit_announce()}\n"

SHELL_ESCAPE_NOTE = "Leave the shell with `exit`, or press Ctrl+] to drop the session."


class ShellExitWatcher:
    """Scan a remote output stream for the shell's own exit marker.

    Keeps the trailing bytes of each chunk so a marker split across two
    websocket frames is still recognised, and withholds the marker itself from
    what reaches the terminal.
    """

    def __init__(self, marker: str = SHELL_EXIT_MARKER) -> None:
        self._marker = marker.encode()
        self._tail = b""

    def feed(self, payload: bytes) -> tuple[bytes, bool]:
        """Return the bytes safe to print, and whether the shell has exited.

        Withheld bytes carry over into the next call, so the returned slice is
        always taken from the combined buffer rather than from ``payload``.
        Only a suffix that is genuinely a partial marker is held back — this
        sits in the path of every keystroke echo, so withholding a fixed-size
        tail would make an interactive shell feel laggy.
        """
        buffer = self._tail + payload
        index = buffer.find(self._marker)
        if index != -1:
            self._tail = b""
            return buffer[:index], True
        keep = 0
        for size in range(min(len(self._marker) - 1, len(buffer)), 0, -1):
            if buffer.endswith(self._marker[:size]):
                keep = size
                break
        self._tail = buffer[len(buffer) - keep :] if keep else b""
        return buffer[: len(buffer) - keep], False

    def flush(self) -> bytes:
        pending, self._tail = self._tail, b""
        return pending


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


def instance_name(raw: dict) -> str:
    for key in ("name", "instance_name", "pod_name", "podName"):
        value = str(raw.get(key) or "").strip()
        if value:
            return value
    return ""


def instance_status(raw: dict) -> str:
    for key in ("instance_status", "status", "instanceStatus"):
        value = str(raw.get(key) or "").strip()
        if value:
            return value
    return ""


def instance_rank(raw: dict, name: str) -> int | None:
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
        name = instance_name(item)
        if not name:
            continue
        instances.append(
            JobInstance(
                name=name,
                status=instance_status(item),
                rank=instance_rank(item, name),
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
    """Select one running instance without prompting (prompt is CLI-only)."""
    running = [inst for inst in instances if inst.status.lower() == RUNNING_INSTANCE_STATUS.lower()]
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
        raise JobShellError(
            f"No running instance with rank {rank}. Running instances: {candidates}"
        )

    if len(running) == 1:
        return running[0]

    candidates = "\n".join(
        f"  {idx}. {inst.name}" + (f" (rank={inst.rank})" if inst.rank is not None else "")
        for idx, inst in enumerate(running, start=1)
    )
    raise JobShellError(
        "Multiple running instances found. Pass --instance or --rank.\n" + candidates
    )


# The PTY sockets are the REST-shaped half of `/api/v2` -- no `?Action=`, so
# an inventory built from Action names reports them as absent. They exist; an
# Action-shaped inventory is simply the wrong instrument for them.
#
# **Neither query parameter is the same name on every route.** The console
# remaps both per workload and so must we -- serving does not even call its
# handle `job_id`. Every combination was measured against a running workload
# of that type; getting one wrong fails in two ways, neither an error message:
#
#   hpc + instance_name      -> socket upgrades, then returns nothing at all.
#                               No error, no close frame, just a shell that
#                               never speaks. (`instance_id` gave 53 bytes.)
#   ray/serving + wrong key  -> handshake refused with a bare `HTTP/1.1 200 OK`
#                               instead of the 101 upgrade.
#
# So a shell that hangs or refuses is the first thing to suspect if a new
# workload is added here by analogy rather than by measurement.
REMOTE_CMD_PATH = "/api/v2/train_job/remote_cmd"
#: workload -> (path, handle parameter, instance parameter)
PTY_ROUTES: dict[str, tuple[str, str, str]] = {
    "job": (REMOTE_CMD_PATH, "job_id", "instance_name"),
    "hpc": ("/api/v2/hpc_jobs/instances/exec", "job_id", "instance_id"),
    "ray": ("/api/v2/ray_job/instances/exec", "job_id", "instance_id"),
    "serving": (
        "/api/v2/inference_servings/instances/exec",
        "inference_serving_id",
        "instance_id",
    ),
}


def build_remote_cmd_ws_url(
    job_id: str, instance_name: str, *, workload: str = "job", base_url: str | None = None
) -> str:
    """Build a workload's remote shell websocket URL.

    One path per workload, no fallback -- a second path here could only hide a
    real failure of the first.
    """
    try:
        path, handle_key, instance_key = PTY_ROUTES[workload]
    except KeyError:
        raise JobShellError(f"No remote shell endpoint for workload {workload!r}.") from None
    base_url = (base_url or _get_base_url()).rstrip("/")
    parsed = urlsplit(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    netloc = parsed.netloc
    query = urlencode({handle_key: job_id, instance_key: instance_name})
    return f"{scheme}://{netloc}{path}?{query}"


def cookie_value(session: WebSession, name: str) -> str | None:
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


def build_remote_cmd_headers(session: WebSession, *, base_url: str | None = None) -> dict[str, str]:
    """Build websocket handshake headers for the remote command service."""
    base_url = (base_url or _get_base_url()).rstrip("/")
    cookie = cookie_value(session, "inspire-session")
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


class WebSocketClient:
    """Minimal RFC 6455 client for the platform PTY websocket."""

    def __init__(self, url: str, headers: dict[str, str], *, timeout: float = 30.0) -> None:
        self.url = url
        self.headers = headers
        self.timeout = timeout
        self.sock: socket.socket | ssl.SSLSocket | None = None
        self._recv_buffer = b""
        self._protocol = _WebSocketProtocol()

    def __enter__(self) -> "WebSocketClient":
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
        self.sock = raw
        try:
            if parsed.scheme == "wss":
                context = ssl.create_default_context()
                sock: socket.socket | ssl.SSLSocket = context.wrap_socket(raw, server_hostname=host)
            else:
                sock = raw
            self.sock = sock
            sock.settimeout(self.timeout)

            request, key = _handshake_request(parsed, self.headers)
            sock.sendall(request)
            response, extra = self._read_http_response(sock)
            _validate_handshake(response, key)
            self._recv_buffer = extra
            sock.settimeout(None)
            self.sock = sock

        except BaseException:
            if self.sock is not None:
                # Preserve the handshake failure even when the socket is already gone.
                with contextlib.suppress(Exception):
                    self.sock.close()
                self.sock = None
            raise

    def _create_socket(self, scheme: str, host: str, port: int) -> socket.socket:
        proxy_url = self._proxy_url(self.url)
        if not proxy_url:
            return socket.create_connection((host, port), timeout=self.timeout)

        proxy = urlsplit(proxy_url)
        if proxy.scheme not in {"http", "https"}:
            raise JobShellError(
                "WebSocket proxy only supports HTTP(S) proxies. "
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
    def _proxy_url(target_url: str) -> str:
        return str(get_websocket_proxy(target_url) or "").strip()

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

    def has_pending_data(self) -> bool:
        return bool(self._recv_buffer) or (
            isinstance(self.sock, ssl.SSLSocket) and self.sock.pending() > 0
        )

    def set_read_timeout(self, timeout: float) -> None:
        if self.sock is not None:
            self.sock.settimeout(timeout)

    def send_pong(self, payload: bytes) -> None:
        self._send_frame(0xA, payload)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode("utf-8", errors="ignore"))

    def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        if self.sock is None:
            raise JobShellError("websocket is not connected")
        self.sock.sendall(self._protocol.encode(opcode, payload))

    def recv_frame(self) -> tuple[int, bytes]:
        if self.sock is None:
            raise JobShellError("websocket is not connected")
        return _drive_protocol(self._protocol.frame(), self._recv_exact)

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
        # The peer may already be gone; still close the local socket.
        with contextlib.suppress(Exception):
            self._send_frame(0x8)
        # Cleanup must not prevent clearing the local socket reference.
        with contextlib.suppress(Exception):
            sock.close()
        self.sock = None

def _handshake_request(parsed: Any, headers: dict[str, str]) -> tuple[bytes, str]:
    host = parsed.hostname
    port = parsed.port or (443 if parsed.scheme == "wss" else 80)
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
    lines.extend(f"{name}: {value}" for name, value in headers.items())
    request = "\r\n".join(lines) + "\r\n\r\n"
    return request.encode("ascii"), key

def _validate_handshake(response: str, key: str) -> None:
    status_line = response.split("\r\n", 1)[0]
    parts = status_line.split()
    status = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
    if status == 401:
        raise JobShellAuthError("Remote shell websocket rejected the session (401).")
    if status != 101:
        raise JobShellError(f"Remote shell websocket handshake failed: {status_line}")
    accept = WebSocketClient._header_value(response, "Sec-WebSocket-Accept")
    expected = base64.b64encode(
        hashlib.sha1(
            (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode(),
            usedforsecurity=False,
        ).digest()
    ).decode("ascii")
    if accept and accept != expected:
        raise JobShellError(
            "Remote shell websocket handshake returned an invalid accept key."
        )

def _encode_frame(opcode: int, payload: bytes) -> bytes:
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
    return header + mask + masked


def _drive_protocol(program: Generator[int, bytes, tuple[int, bytes]], read: Any) -> tuple[int, bytes]:
    try:
        size = next(program)
        while True:
            size = program.send(read(size))
    except StopIteration as done:
        return done.value
    finally:
        program.close()


class _WebSocketProtocol:
    """Single framing/fragmentation state machine; yields exact read sizes."""

    def __init__(self) -> None:
        self.close_payload = b""
        self.close_sent = False
        self.fragment_opcode: int | None = None
        self.fragments = bytearray()

    def encode(self, opcode: int, payload: bytes) -> bytes:
        if opcode == 8:
            if self.close_sent:
                return b""
            self.close_sent = True
            payload = payload or self.close_payload
        return _encode_frame(opcode, payload)

    def frame(self) -> Generator[int, bytes, tuple[int, bytes]]:
        while True:
            header = yield 2
            first, second = header
            opcode, final = first & 15, bool(first & 128)
            if first & 112 or opcode not in (0, 1, 2, 8, 9, 10):
                raise JobShellError("Invalid websocket frame flags or opcode")
            length = second & 127
            if opcode >= 8 and (not final or length > 125):
                raise JobShellError("Invalid websocket control frame")
            if length == 126:
                length = struct.unpack("!H", (yield 2))[0]
            elif length == 127:
                length = struct.unpack("!Q", (yield 8))[0]
                if length >> 63:
                    raise JobShellError("Invalid websocket payload length")
            mask = (yield 4) if second & 128 else b""
            payload = (yield length) if length else b""
            if mask:
                payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
            if opcode >= 8:
                if opcode == 8 and len(payload) == 1:
                    raise JobShellError("Invalid websocket close payload")
                if opcode == 8:
                    self.close_payload = payload
                return opcode, payload
            if opcode == 0:
                if self.fragment_opcode is None:
                    raise JobShellError("Unexpected websocket continuation")
                self.fragments.extend(payload)
                if final:
                    result = self.fragment_opcode, bytes(self.fragments)
                    self.fragment_opcode = None
                    self.fragments.clear()
                    return result
            else:
                if self.fragment_opcode is not None:
                    raise JobShellError("Expected websocket continuation")
                if final:
                    return opcode, payload
                self.fragment_opcode = opcode
                self.fragments.extend(payload)


class AsyncWebSocketClient:
    """Asyncio streams adapter for the same PTY wire protocol."""

    def __init__(self, url: str, headers: dict[str, str], *, timeout: float = 30.0):
        self.url, self.headers, self.timeout = url, headers, timeout
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._protocol = _WebSocketProtocol()

    async def __aenter__(self) -> AsyncWebSocketClient:
        await self.connect()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    async def connect(self) -> None:
        try:
            await asyncio.wait_for(self._connect(), self.timeout)
        except BaseException:
            await self.close()
            raise

    async def _connect(self) -> None:
        parsed = urlsplit(self.url)
        if parsed.scheme not in {"ws", "wss"}:
            raise JobShellError(f"Unsupported websocket scheme: {parsed.scheme}")
        host = parsed.hostname
        if not host:
            raise JobShellError("Websocket URL has no host")
        port = parsed.port or (443 if parsed.scheme == "wss" else 80)
        proxy = urlsplit(await offload(WebSocketClient._proxy_url, self.url))
        if proxy.scheme and proxy.scheme not in {"http", "https"}:
            raise JobShellError("WebSocket proxy only supports HTTP(S) proxies. "
                                f"Configured proxy scheme: {proxy.scheme}")
        target_host = proxy.hostname or host
        target_port = (proxy.port or (443 if proxy.scheme == "https" else 80)) if proxy.hostname else port
        tls = proxy.scheme == "https" if proxy.hostname else parsed.scheme == "wss"
        self.reader, self.writer = await asyncio.open_connection(
            target_host, target_port, ssl=await offload(ssl.create_default_context) if tls else None,
        )
        if proxy.hostname:
            lines = [f"CONNECT {host}:{port} HTTP/1.1", f"Host: {host}:{port}"]
            if proxy.username:
                token = base64.b64encode(f"{proxy.username}:{proxy.password or ''}".encode()).decode("ascii")
                lines.append(f"Proxy-Authorization: Basic {token}")
            await self._write(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
            response = await self._response()
            if response.split()[1:2] != ["200"]:
                raise JobShellError(f"Proxy CONNECT failed: {response.splitlines()[0]}")
            if parsed.scheme == "wss":
                loop = asyncio.get_running_loop()
                protocol = self.writer.transport.get_protocol()
                transport = await loop.start_tls(
                    self.writer.transport, protocol, await offload(ssl.create_default_context),
                    server_hostname=host,
                )
                if transport is None:
                    raise JobShellError("Websocket TLS upgrade failed")
                # Python 3.10 has no StreamWriter.start_tls. Keep the same writer
                # alive so its destructor cannot close the upgraded connection.
                cast(Any, self.writer)._transport = transport
                cast(Any, protocol)._replace_writer(self.writer)
        request, key = _handshake_request(parsed, self.headers)
        await self._write(request)
        _validate_handshake(await self._response(), key)

    async def _response(self) -> str:
        assert self.reader is not None
        try:
            data = await self.reader.readuntil(b"\r\n\r\n")
        except asyncio.LimitOverrunError as error:
            raise JobShellError("Remote shell websocket handshake response is too large.") from error
        return data.decode("iso-8859-1", errors="replace")

    async def _write(self, data: bytes) -> None:
        if self.writer is None:
            raise JobShellError("websocket is not connected")
        self.writer.write(data)
        await self.writer.drain()

    async def _send_frame(self, opcode: int, payload: bytes = b"") -> None:
        await self._write(self._protocol.encode(opcode, payload))

    async def send_text(self, text: str) -> None:
        await self._send_frame(1, text.encode("utf-8", errors="ignore"))

    async def send_pong(self, payload: bytes) -> None:
        await self._send_frame(10, payload)

    async def recv_frame(self) -> tuple[int, bytes]:
        if self.reader is None:
            raise JobShellError("websocket is not connected")
        program = self._protocol.frame()
        try:
            size = next(program)
            while True:
                size = program.send(await self.reader.readexactly(size))
        except StopIteration as done:
            return done.value
        except asyncio.IncompleteReadError as error:
            raise EOFError("websocket closed") from error
        finally:
            program.close()

    async def close(self) -> None:
        if self.writer is None:
            return
        try:
            # A disconnected peer must not prevent releasing the local stream.
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._send_frame(8), min(self.timeout, 1.0))
        finally:
            writer, self.writer = self.writer, None
            # Best-effort cleanup also runs when sending the close frame is cancelled.
            with contextlib.suppress(Exception):
                writer.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(writer.wait_closed(), 1.0)
