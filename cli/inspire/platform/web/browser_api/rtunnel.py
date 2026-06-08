"""Notebook rtunnel setup: commands, state, probe, verify, and flow.

Merged from the rtunnel subpackage. The public entry point is
``setup_notebook_rtunnel`` (async-safe wrapper around the sync flow).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

try:
    from playwright.sync_api import Error as _PlaywrightError
except ImportError:  # pragma: no cover - Playwright may be unavailable in some environments.
    PlaywrightError: type[Exception] = Exception
else:
    PlaywrightError = _PlaywrightError


from inspire.platform.web.browser_api.core import (
    _browser_api_path,
    _get_base_url,
    _in_asyncio_loop,
    _launch_browser,
    _new_context,
    _run_in_thread,
)
from inspire.bridge.tunnel import load_tunnel_config
from inspire.platform.web.session import WebSession, build_requests_session, get_web_session

import logging

_log = logging.getLogger("inspire.platform.web.browser_api.rtunnel")


@dataclass(frozen=True)
class _SetupScriptSendResult:
    sent_via_ws: bool
    cleanup: Callable[[], None] | None = None


# ============================================================================
# Commands
# ============================================================================

BOOTSTRAP_SENTINEL = "/tmp/.inspire_rtunnel_bootstrap_v1"
SETUP_DONE_MARKER = "INSPIRE_RTUNNEL_SETUP_DONE"
RTUNNEL_MISSING_MARKER = "INSPIRE_RTUNNEL_MISSING_IN_BOOTSTRAP"
OPENSSH_INSTALL_FAILED_MARKER = "INSPIRE_OPENSSH_INSTALL_FAILED"
OPENSSH_INSTALL_FAILED_FILE = "/tmp/.inspire_openssh_install_failed"
OPENSSH_INSTALL_LOG = "/tmp/inspire-openssh-install.log"
OPENSSH_JAMMY_INSTALL_FAILED_MARKER = OPENSSH_INSTALL_FAILED_MARKER
OPENSSH_JAMMY_INSTALL_FAILED_FILE = OPENSSH_INSTALL_FAILED_FILE
OPENSSH_JAMMY_INSTALL_LOG = OPENSSH_INSTALL_LOG
SII_UBUNTU_APT_MIRROR = "http://nexus.sii.shaipower.online/repository/ubuntu"

# Canonical path of the InspireSkill offline SSH-bootstrap kit on the Inspire
# platform's global_public fileset — mounted read-only in every notebook
# container. Layout (see the kit's MANIFEST.txt):
#   <root>/rtunnel/linux-{amd64,arm64}/rtunnel   (static Go binary)
#   <root>/sshd-debs/*.deb                        (legacy, no longer used here)
#
# This is the canonical offline source for rtunnel. OpenSSH is installed or
# corrected through the SII internal Ubuntu apt mirror selected from the
# container's /etc/os-release codename; the old kit sshd-debs path is no
# longer used by the bootstrap script.
INSPIRE_BOOTSTRAP_ROOT = "/inspire/hdd/global_public/inspire-skill-bootstrap/v1"


def build_rtunnel_setup_commands(
    *,
    port: int,
    ssh_port: int,
    ssh_public_key: Optional[str],
) -> list[str]:
    """Build the shell emitted into a notebook container to bring up sshd +
    rtunnel.

    rtunnel is always read from the global_public kit. OpenSSH install or
    version correction always uses the SII internal Ubuntu apt mirror selected
    from the container's Ubuntu codename.
    """
    if ssh_public_key:
        ssh_public_key_escaped = ssh_public_key.replace("'", "'\"'\"'")
        key_line = (
            "mkdir -p /root/.ssh && chmod 700 /root/.ssh && echo "
            f"'{ssh_public_key_escaped}' >> /root/.ssh/authorized_keys && chmod 600 "
            "/root/.ssh/authorized_keys"
        )
    else:
        key_line = "mkdir -p /root/.ssh && chmod 700 /root/.ssh"

    cmd_lines: list[str] = [
        f"PORT={port}",
        f"SSH_PORT={ssh_port}",
        key_line,
        f"BOOTSTRAP_SENTINEL={BOOTSTRAP_SENTINEL}",
        f"KIT={INSPIRE_BOOTSTRAP_ROOT}",
        f"OPENSSH_INSTALL_FAILED_FILE={OPENSSH_INSTALL_FAILED_FILE}",
        f"OPENSSH_INSTALL_LOG={OPENSSH_INSTALL_LOG}",
        f"SII_UBUNTU_APT_MIRROR={SII_UBUNTU_APT_MIRROR}",
        # Detect container arch once — rtunnel ships one binary per Linux arch.
        '_RT_ARCH=$(uname -m 2>/dev/null); '
        'case "$_RT_ARCH" in arm64|aarch64) _RT_ARCH=arm64;; *) _RT_ARCH=amd64;; esac',
        # rtunnel is statically-linked (Go) and lives read-only on GPFS at
        # /inspire/hdd/global_public/inspire-skill-bootstrap/v1/rtunnel/linux-<arch>/rtunnel
        # We exec it straight from there — no copy / chmod into the container.
        '_RT_BIN="$KIT/rtunnel/linux-${_RT_ARCH}/rtunnel"',
        '_OS_ID=$(. /etc/os-release 2>/dev/null && echo "${ID:-}" || true)',
        '_OS_CODENAME=$(. /etc/os-release 2>/dev/null && '
        'echo "${VERSION_CODENAME:-${UBUNTU_CODENAME:-}}" || true)',
        '_OPENSSH_SERVER_VERSION=$(dpkg-query -W -f=\'${Version}\' openssh-server 2>/dev/null || true)',
        '_OPENSSH_CLIENT_VERSION=$(dpkg-query -W -f=\'${Version}\' openssh-client 2>/dev/null || true)',
        '_OPENSSH_SFTP_VERSION=$(dpkg-query -W -f=\'${Version}\' openssh-sftp-server 2>/dev/null || true)',
        '_OPENSSH_BINARY_VERSION=$(/usr/sbin/sshd -V 2>&1 || true)',
        'echo "[inspire bootstrap] distro=${_OS_ID:-unknown}/${_OS_CODENAME:-unknown} '
        'openssh-server=${_OPENSSH_SERVER_VERSION:-none} '
        'openssh-client=${_OPENSSH_CLIENT_VERSION:-none} '
        'openssh-sftp-server=${_OPENSSH_SFTP_VERSION:-none}"',
        '_fail_openssh(){ touch "$OPENSSH_INSTALL_FAILED_FILE"; '
        'echo "[inspire bootstrap] ERROR: $1" | tee -a "$OPENSSH_INSTALL_LOG"; }',
    ]

    # sshd:
    #   - Ubuntu containers are matched to their own codename at runtime.
    #   - Missing or mismatched OpenSSH is installed/corrected from SII apt.
    #   - Non-Ubuntu containers may reuse an existing sshd, but bootstrap will
    #     not install OpenSSH from the old kit debs path.
    cmd_lines.append(
        'rm -f "$OPENSSH_INSTALL_FAILED_FILE"; '
        'if [ "$_OS_ID" = "ubuntu" ] && [ -n "$_OS_CODENAME" ]; then '
        'echo "[inspire bootstrap] resolving Ubuntu ${_OS_CODENAME} OpenSSH via SII internal apt mirror" '
        '| tee "$OPENSSH_INSTALL_LOG"; '
        '_apt_source="/tmp/inspire-openssh-${_OS_CODENAME}.sources.list"; '
        '_apt_sourceparts=/tmp/inspire-empty-apt-sourceparts; '
        '_apt_lists="/tmp/inspire-openssh-${_OS_CODENAME}-apt-lists"; '
        'rm -rf "$_apt_sourceparts"; mkdir -p "$_apt_sourceparts"; '
        'rm -rf "$_apt_lists"; mkdir -p "$_apt_lists/partial"; '
        'printf "%s\\n" '
        '"deb $SII_UBUNTU_APT_MIRROR $_OS_CODENAME main restricted universe multiverse" '
        '"deb $SII_UBUNTU_APT_MIRROR $_OS_CODENAME-updates main restricted universe multiverse" '
        '"deb $SII_UBUNTU_APT_MIRROR $_OS_CODENAME-security main restricted universe multiverse" '
        '> "$_apt_source"; '
        '_apt_opts="-o Dir::Etc::sourcelist=$_apt_source '
        '-o Dir::Etc::sourceparts=$_apt_sourceparts -o Dir::State::lists=$_apt_lists '
        '-o APT::Get::List-Cleanup=0"; '
        'export DEBIAN_FRONTEND=noninteractive; '
        'if ! timeout 45 apt-get $_apt_opts -o Acquire::Retries=2 -o Acquire::http::Timeout=15 update -qq '
        '>>"$OPENSSH_INSTALL_LOG" 2>&1; then '
        '_fail_openssh "apt update failed; SII internal Ubuntu apt mirror is unreachable or rejected."; '
        'else '
        '_OPENSSH_SERVER_CANDIDATE=$(apt-cache $_apt_opts policy openssh-server '
        '2>>"$OPENSSH_INSTALL_LOG" | awk \'/Candidate:/ {print $2; exit}\' || true); '
        '_OPENSSH_CLIENT_CANDIDATE=$(apt-cache $_apt_opts policy openssh-client '
        '2>>"$OPENSSH_INSTALL_LOG" | awk \'/Candidate:/ {print $2; exit}\' || true); '
        '_OPENSSH_SFTP_CANDIDATE=$(apt-cache $_apt_opts policy openssh-sftp-server '
        '2>>"$OPENSSH_INSTALL_LOG" | awk \'/Candidate:/ {print $2; exit}\' || true); '
        'echo "[inspire bootstrap] internal apt candidate openssh-server=${_OPENSSH_SERVER_CANDIDATE:-none}" '
        '>>"$OPENSSH_INSTALL_LOG"; '
        'if [ -z "$_OPENSSH_SERVER_CANDIDATE" ] || [ "$_OPENSSH_SERVER_CANDIDATE" = "(none)" ] '
        '|| [ -z "$_OPENSSH_CLIENT_CANDIDATE" ] || [ "$_OPENSSH_CLIENT_CANDIDATE" = "(none)" ] '
        '|| [ -z "$_OPENSSH_SFTP_CANDIDATE" ] || [ "$_OPENSSH_SFTP_CANDIDATE" = "(none)" ]; then '
        '_fail_openssh "SII internal Ubuntu apt mirror did not provide OpenSSH packages for ${_OS_CODENAME}."; '
        'else '
        '_need_openssh=0; '
        'if [ ! -x /usr/sbin/sshd ]; then _need_openssh=1; fi; '
        'if [ "$_OPENSSH_SERVER_VERSION" != "$_OPENSSH_SERVER_CANDIDATE" ]; then _need_openssh=1; fi; '
        'if [ "$_OPENSSH_CLIENT_VERSION" != "$_OPENSSH_CLIENT_CANDIDATE" ]; then _need_openssh=1; fi; '
        'if [ "$_OPENSSH_SFTP_VERSION" != "$_OPENSSH_SFTP_CANDIDATE" ]; then _need_openssh=1; fi; '
        'if [ "$_need_openssh" = "1" ]; then '
        'if [ -n "$_OPENSSH_SERVER_VERSION" ] || [ -n "$_OPENSSH_CLIENT_VERSION" ] '
        '|| [ -n "$_OPENSSH_SFTP_VERSION" ]; then '
        'timeout 45 apt-get $_apt_opts remove -y -qq '
        'openssh-server openssh-client openssh-sftp-server '
        '>>"$OPENSSH_INSTALL_LOG" 2>&1 || true; '
        'dpkg --remove --force-depends openssh-server openssh-client openssh-sftp-server '
        '>>"$OPENSSH_INSTALL_LOG" 2>&1 || true; fi; '
        'if ! timeout 120 apt-get $_apt_opts install -y -qq --allow-downgrades --no-install-recommends '
        '"openssh-server=$_OPENSSH_SERVER_CANDIDATE" "openssh-client=$_OPENSSH_CLIENT_CANDIDATE" '
        '"openssh-sftp-server=$_OPENSSH_SFTP_CANDIDATE" '
        '>>"$OPENSSH_INSTALL_LOG" 2>&1; then '
        '_fail_openssh "apt install failed; SII internal Ubuntu apt mirror did not provide usable OpenSSH packages."; '
        'else '
        'echo "[inspire bootstrap] installed OpenSSH from SII internal Ubuntu apt mirror" '
        '>>"$OPENSSH_INSTALL_LOG"; '
        'fi; '
        'else '
        'echo "[inspire bootstrap] existing OpenSSH matches internal apt candidate" '
        '>>"$OPENSSH_INSTALL_LOG"; '
        'fi; '
        '_OPENSSH_SERVER_VERSION=$(dpkg-query -W -f=\'${Version}\' openssh-server 2>/dev/null || true); '
        '_OPENSSH_CLIENT_VERSION=$(dpkg-query -W -f=\'${Version}\' openssh-client 2>/dev/null || true); '
        '_OPENSSH_SFTP_VERSION=$(dpkg-query -W -f=\'${Version}\' openssh-sftp-server 2>/dev/null || true); '
        '_OPENSSH_BINARY_VERSION=$(/usr/sbin/sshd -V 2>&1 || true); '
        'if [ ! -x /usr/sbin/sshd ]; then '
        '_fail_openssh "OpenSSH install completed without /usr/sbin/sshd."; '
        'elif [ "$_OPENSSH_SERVER_VERSION" != "$_OPENSSH_SERVER_CANDIDATE" ]; then '
        '_fail_openssh "installed OpenSSH does not match the internal apt candidate for ${_OS_CODENAME}."; '
        'elif [ "$_OPENSSH_CLIENT_VERSION" != "$_OPENSSH_CLIENT_CANDIDATE" ]; then '
        '_fail_openssh "installed OpenSSH client does not match the internal apt candidate for ${_OS_CODENAME}."; '
        'elif [ "$_OPENSSH_SFTP_VERSION" != "$_OPENSSH_SFTP_CANDIDATE" ]; then '
        '_fail_openssh "installed OpenSSH SFTP server does not match the internal apt candidate for ${_OS_CODENAME}."; '
        'fi; '
        'fi; fi; '
        'elif [ ! -x /usr/sbin/sshd ]; then '
        '_fail_openssh "OpenSSH is missing and this image is not a detectable Ubuntu release; '
        'internal apt installation was skipped."; '
        "fi"
    )

    # Sentinel bookkeeping (both pieces in place → sentinel set; else clear).
    cmd_lines.append(
        'if [ -x "$_RT_BIN" ] && [ -x /usr/sbin/sshd ] '
        '&& [ ! -f "$OPENSSH_INSTALL_FAILED_FILE" ]; then '
        'touch "$BOOTSTRAP_SENTINEL"; '
        'else rm -f "$BOOTSTRAP_SENTINEL"; fi'
    )

    # These follow-ups keep sshd start robust across bare images and images
    # left behind by older bootstrap attempts:
    #   (a) the privilege-separation user `sshd` must exist; without it sshd dies with
    #       "Privilege separation user sshd does not exist".
    #   (b) sshd insists on opening /etc/ssh/sshd_config. We write a minimal
    #       config when it is missing (no Port/ListenAddress, those are set via
    #       -o); writing them in the file would conflict because
    #       sshd ADDs -o ListenAddress on top of config ListenAddress and
    #       the second bind() fails with "Address already in use".
    cmd_lines.append(
        "getent passwd sshd >/dev/null 2>&1 || "
        "useradd -r -M -d /run/sshd -s /usr/sbin/nologin sshd >/dev/null 2>&1 || true"
    )
    cmd_lines.append(
        'if [ -x /usr/sbin/sshd ] && [ ! -f /etc/ssh/sshd_config ]; then '
        "mkdir -p /etc/ssh; "
        "printf 'UsePAM no\\nStrictModes no\\nSubsystem sftp /usr/lib/openssh/sftp-server\\n' "
        "> /etc/ssh/sshd_config; fi"
    )

    # Start sshd on SSH_PORT if not already running.
    cmd_lines.append(
        'if [ ! -f "$OPENSSH_INSTALL_FAILED_FILE" ] && [ -x /usr/sbin/sshd ] '
        '&& ! ps -ef | grep -q "[s]shd -p $SSH_PORT"; then '
        "mkdir -p /run/sshd && chmod 0755 /run/sshd; "
        "ssh-keygen -A >/dev/null 2>&1 || true; "
        '/usr/sbin/sshd -p "$SSH_PORT" -o ListenAddress=127.0.0.1 -o PermitRootLogin=yes '
        "-o PasswordAuthentication=no -o PubkeyAuthentication=yes "
        ">/tmp/sshd-bootstrap.log 2>&1 & fi"
    )

    # Start rtunnel server: listen on PORT (WSS-reachable from the platform
    # Bridge), forward to 127.0.0.1:SSH_PORT. Exec'd directly from GPFS.
    cmd_lines.append(
        'if [ -x "$_RT_BIN" ] && ! ps -ef | '
        'grep -Eq "[r]tunnel .*([[:space:]]|:)$PORT([[:space:]]|$)"; then '
        'nohup "$_RT_BIN" "$SSH_PORT" "$PORT" '
        ">/tmp/rtunnel-server.log 2>&1 & fi"
    )

    # Status markers for the client-side terminal tailer.
    cmd_lines.append(
        'if ps -ef | grep -Eq "[r]tunnel .*([[:space:]]|:)$PORT([[:space:]]|$)"; then '
        'echo "INSPIRE_RTUNNEL_STATUS=running"; '
        'else echo "INSPIRE_RTUNNEL_STATUS=not_running"; fi'
    )
    cmd_lines.append(
        'if [ ! -x "$_RT_BIN" ]; then '
        f'echo {RTUNNEL_MISSING_MARKER}; '
        "fi"
    )
    cmd_lines.append(
        'if [ -f "$OPENSSH_INSTALL_FAILED_FILE" ]; then '
        f'echo {OPENSSH_INSTALL_FAILED_MARKER}; '
        "fi"
    )
    cmd_lines.append(f"echo {SETUP_DONE_MARKER}")

    return cmd_lines


# ============================================================================
# State
# ============================================================================

_CACHE_BASENAME = "rtunnel-proxy-state"
_CACHE_VERSION = 1
DEFAULT_PROXY_CACHE_TTL_SECONDS = 8 * 60 * 60


def _normalize_account(account: Optional[str]) -> Optional[str]:
    if not account:
        return None
    value = account.strip()
    if not value:
        return None
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", value)
    return normalized or None


def _active_account_name() -> str | None:
    try:
        from inspire.accounts import current_account

        return current_account()
    except Exception:
        return None


def _resolve_rtunnel_account(
    *,
    explicit: Optional[str],
    session: Optional[WebSession] = None,
) -> str | None:
    return (explicit or "").strip() or _active_account_name() or (
        str(getattr(session, "login_username", "") or "").strip() or None
    )


def _default_cache_dir() -> Path:
    return Path.home() / ".cache" / "inspire-skill"


def get_rtunnel_state_file(
    *,
    account: Optional[str],
    cache_dir: Optional[Path] = None,
) -> Path:
    if cache_dir is None and account:
        try:
            from inspire.accounts import account_dir, account_exists

            if account_exists(account):
                return account_dir(account) / f"{_CACHE_BASENAME}.json"
        except Exception:
            pass

    root = cache_dir or _default_cache_dir()
    normalized = _normalize_account(account)
    if normalized:
        return root / f"{_CACHE_BASENAME}-{normalized}.json"
    return root / f"{_CACHE_BASENAME}.json"


def _load_state_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"version": _CACHE_VERSION, "notebooks": {}}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {"version": _CACHE_VERSION, "notebooks": {}}
    if not isinstance(raw, dict):
        return {"version": _CACHE_VERSION, "notebooks": {}}
    notebooks = raw.get("notebooks")
    if not isinstance(notebooks, dict):
        notebooks = {}
    return {"version": raw.get("version", _CACHE_VERSION), "notebooks": notebooks}


def _save_state_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp_path, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def get_cached_rtunnel_proxy_candidates(
    *,
    notebook_id: str,
    port: int,
    base_url: str,
    account: Optional[str],
    ttl_seconds: int = DEFAULT_PROXY_CACHE_TTL_SECONDS,
    cache_dir: Optional[Path] = None,
    now_ts: Optional[float] = None,
) -> list[str]:
    state_file = get_rtunnel_state_file(account=account, cache_dir=cache_dir)
    payload = _load_state_file(state_file)
    notebooks = payload.get("notebooks", {})
    entry = notebooks.get(notebook_id)
    if not isinstance(entry, dict):
        return []

    proxy_url = str(entry.get("proxy_url") or "").strip()
    entry_port = int(entry.get("port") or 0)
    entry_base_url = str(entry.get("base_url") or "").rstrip("/")
    updated_at = float(entry.get("updated_at") or 0)
    now = now_ts if now_ts is not None else time.time()
    if not proxy_url:
        return []
    if entry_port and entry_port != port:
        return []
    if entry_base_url and entry_base_url != base_url.rstrip("/"):
        return []
    if ttl_seconds > 0 and updated_at > 0 and (now - updated_at) > ttl_seconds:
        return []
    return [proxy_url]


def save_rtunnel_proxy_state(
    *,
    notebook_id: str,
    proxy_url: str,
    port: int,
    ssh_port: int,
    base_url: str,
    account: Optional[str],
    cache_dir: Optional[Path] = None,
    now_ts: Optional[float] = None,
) -> None:
    state_file = get_rtunnel_state_file(account=account, cache_dir=cache_dir)
    payload = _load_state_file(state_file)
    notebooks = payload.setdefault("notebooks", {})
    if not isinstance(notebooks, dict):
        notebooks = {}
        payload["notebooks"] = notebooks

    notebooks[notebook_id] = {
        "proxy_url": proxy_url,
        "port": int(port),
        "ssh_port": int(ssh_port),
        "base_url": base_url.rstrip("/"),
        "updated_at": float(now_ts if now_ts is not None else time.time()),
    }
    payload["version"] = _CACHE_VERSION
    _save_state_file(state_file, payload)


# ============================================================================
# Verify
# ============================================================================


def redact_proxy_url(proxy_url: str) -> str:
    """Redact sensitive tokens from a notebook proxy URL for logs/errors.

    Proxy URLs may contain tokens either as a path segment:
      /jupyter/<notebook>/<token>/proxy/<port>/
    or as a query parameter:
      .../proxy/<port>/?token=<token>
    """
    proxy_url = str(proxy_url or "").strip()
    if not proxy_url:
        return proxy_url

    try:
        from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

        parts = urlsplit(proxy_url)
        path_segments = parts.path.split("/")

        for marker in ("jupyter", "vscode"):
            for idx, seg in enumerate(path_segments):
                if seg != marker:
                    continue
                # /<marker>/<notebook>/<token>/proxy/<port>/ -> token is idx+2
                if idx + 1 < len(path_segments) and path_segments[idx + 1]:
                    path_segments[idx + 1] = "<notebook>"
                if idx + 3 < len(path_segments) and path_segments[idx + 3] == "proxy":
                    if idx + 2 < len(path_segments) and path_segments[idx + 2]:
                        path_segments[idx + 2] = "<redacted>"

        raw_prefix_labels = {
            "ws-": "<workspace>",
            "project-": "<project>",
            "user-": "<user>",
            "notebook-": "<notebook>",
            "nb-": "<notebook>",
        }
        for idx, seg in enumerate(path_segments):
            lower = seg.lower()
            for prefix, label in raw_prefix_labels.items():
                if lower.startswith(prefix):
                    path_segments[idx] = label
                    break

        redacted_path = "/".join(path_segments)

        if parts.query:
            query_items = parse_qsl(parts.query, keep_blank_values=True)
            redacted_items: list[tuple[str, str]] = []
            for key, value in query_items:
                if key.lower() in {"token", "access_token"}:
                    redacted_items.append((key, "<redacted>" if value else value))
                else:
                    redacted_items.append((key, value))
            redacted_query = urlencode(redacted_items)
        else:
            redacted_query = parts.query

        return urlunsplit(
            (parts.scheme, parts.netloc, redacted_path, redacted_query, parts.fragment)
        )
    except (ValueError, TypeError, AttributeError):
        # Best-effort fallback: redact obvious token query patterns.
        if "token=" in proxy_url:
            before, _, after = proxy_url.partition("token=")
            if "&" in after:
                _token, _, rest = after.partition("&")
                return before + "token=<redacted>&" + rest
            return before + "token=<redacted>"
        return proxy_url


def _is_rtunnel_proxy_ready(*, status: int, body: str) -> bool:
    text = (body or "").strip().lower()

    if status == 200:
        if not text:
            return True
        if (
            "econnrefused" in text
            or "connection refused" in text
            or "404 page not found" in text
            or "<html" in text
            or "<!doctype html" in text
            or "jupyter server" in text
        ):
            return False
        return True

    # A plain-text 404 is ambiguous: it could be the platform gateway
    # returning "route not found" for a non-existent /vscode/ path, or
    # an rtunnel WebSocket server replying to an HTTP GET.  Treating it
    # as ready caused false positives when the derived vscode URL didn't
    # exist, so we no longer accept 404 as "reachable" during polling.
    return False


def _is_plain_text_404_response(*, status: int, body: str) -> bool:
    text = (body or "").strip().lower()
    return status == 404 and "page not found" in text and "<html" not in text


_TOKEN_QUERY_RE = re.compile(r"(?i)(token=)[^\s&'\"]+")
_TOKEN_PATH_RE = re.compile(r"(/(?:jupyter|vscode)/[^/]+/)([^/]+)(/proxy/)")


def _redact_token_like_text(text: str) -> str:
    value = str(text or "")
    if not value:
        return value

    value = _TOKEN_QUERY_RE.sub(r"\1<redacted>", value)
    value = _TOKEN_PATH_RE.sub(r"\1<redacted>\3", value)
    return value


def _summarize_request_error(error: Exception) -> str:
    """Return a safe single-line summary for Playwright request errors."""
    message = str(error).strip()
    if not message:
        return error.__class__.__name__
    headline = message.splitlines()[0].strip()
    # Avoid Playwright call logs that may include cookies/tokens.
    return _redact_token_like_text(headline)


def _diagnostic_is_inconclusive_http_probe(diagnostic: str) -> bool:
    lowered = str(diagnostic or "").strip().lower()
    return "plain-text 404" in lowered and "page not found" in lowered


def _all_inconclusive_http_probe_diagnostics(diagnostics: list[str]) -> bool:
    if not diagnostics:
        return False
    return all(_diagnostic_is_inconclusive_http_probe(item) for item in diagnostics)


def wait_for_rtunnel_reachable(
    *,
    proxy_url: str,
    timeout_s: int,
    context: Any,
    page: Any,
) -> None:
    """Wait until rtunnel becomes reachable via the notebook proxy URL, or raise ValueError."""
    import sys as _sys

    display_url = redact_proxy_url(proxy_url)
    _sys.stderr.write(f"  Polling proxy URL: {display_url}\n")
    _sys.stderr.flush()

    start = time.time()
    last_status = None
    last_progress_time = start
    attempt = 0
    consecutive_404 = 0
    while time.time() - start < timeout_s:
        attempt += 1
        elapsed = time.time() - start
        if time.time() - last_progress_time >= 30:
            _sys.stderr.write(f"  Waiting for rtunnel... ({int(elapsed)}s elapsed)\n")
            _sys.stderr.flush()
            last_progress_time = time.time()
        try:
            resp = context.request.get(proxy_url, timeout=5000)
            try:
                body = resp.text()
            except (PlaywrightError, AttributeError, RuntimeError, TypeError, ValueError):
                body = ""
            last_status = _redact_token_like_text(f"{resp.status} {body[:200].strip()}")
            if attempt <= 3 and not _is_plain_text_404_response(status=resp.status, body=body):
                _sys.stderr.write(f"  Attempt {attempt}: {last_status}\n")
                _sys.stderr.flush()
            if _is_rtunnel_proxy_ready(status=resp.status, body=body):
                return
            # Track consecutive plain-text 404 responses.  Both the
            # platform gateway and rtunnel's Go HTTP handler return this
            # for non-WebSocket requests.  Either way the HTTP probe
            # will never succeed, so bail out early.
            if _is_plain_text_404_response(status=resp.status, body=body):
                consecutive_404 += 1
            else:
                consecutive_404 = 0
        except (
            PlaywrightError,
            ConnectionError,
            OSError,
            RuntimeError,
            TimeoutError,
            ValueError,
        ) as e:
            last_status = _summarize_request_error(e)
            if attempt <= 3:
                _sys.stderr.write(f"  Attempt {attempt}: {last_status}\n")
                _sys.stderr.flush()

        # Early-exit check is outside the try/except so the ValueError
        # propagates to the caller instead of being swallowed.
        if consecutive_404 >= 3 and (time.time() - start) >= 2:
            raise ValueError(
                f"HTTP readiness probe stayed inconclusive with plain-text 404 on "
                f"{consecutive_404} consecutive attempts ({int(time.time() - start)}s elapsed).\n"
                f"Proxy URL: {display_url}\n"
                f"Last response: {last_status}"
            )

        elapsed = time.time() - start
        if elapsed < 3:
            poll_ms = 180
        elif elapsed < 8:
            poll_ms = 300
        elif elapsed < 20:
            poll_ms = 650
        else:
            poll_ms = 1000
        page.wait_for_timeout(poll_ms)

    error_msg = (
        f"rtunnel server did not become reachable within {timeout_s}s.\n"
        f"Proxy URL: {display_url}\n"
        f"Last response: {last_status}\n\n"
        "Debugging hints:\n"
        "  1. Check if rtunnel binary is present: ls -la /tmp/rtunnel\n"
        "  2. Check rtunnel server log: cat /tmp/rtunnel-server.log\n"
        "  3. Check if sshd/dropbear is running: ps aux | grep -E 'sshd|dropbear'\n"
        "  4. Check dropbear log: cat /tmp/dropbear.log\n"
        "  5. Try running with --debug-playwright to see the browser\n"
        "  6. Screenshot saved to /tmp/notebook_terminal_debug.png"
    )
    raise ValueError(error_msg)


# ============================================================================
# Probe
# ============================================================================

_PROXY_PORT_PATTERN = re.compile(r"/proxy/\d+/")


def _rewrite_proxy_port(proxy_url: str, port: int) -> str:
    if f"/proxy/{port}/" in proxy_url:
        return proxy_url
    if _PROXY_PORT_PATTERN.search(proxy_url):
        return _PROXY_PORT_PATTERN.sub(f"/proxy/{port}/", proxy_url, count=1)
    return proxy_url


def _is_reachable_proxy_response(*, status_code: int, body: str) -> bool:
    text = (body or "").strip().lower()

    if status_code == 200:
        if "econnrefused" in text or "connection refused" in text:
            return False
        if "<html" in text:
            return False
        return True

    # A plain-text 404 is ambiguous: it could be the platform gateway
    # returning "route not found" for a non-existent proxy path, or
    # an rtunnel WebSocket server replying to an HTTP GET.  The false
    # positives from gateway 404s cause broken proxy URLs to be cached
    # and reused, so we no longer accept 404 as "reachable".
    return False


def _response_body_prefix(response: Any, *, limit: int = 400) -> str:
    try:
        chunks: list[str] = []
        seen = 0
        for chunk in response.iter_content(chunk_size=min(limit, 400), decode_unicode=True):
            if not chunk:
                continue
            text = chunk if isinstance(chunk, str) else chunk.decode("utf-8", errors="replace")
            chunks.append(text)
            seen += len(text)
            if seen >= limit:
                break
        return "".join(chunks)[:limit]
    except Exception:
        return ""


def _candidate_urls_from_tunnel_config(
    *,
    notebook_id: str,
    port: int,
    account: Optional[str],
) -> list[str]:
    try:
        config = load_tunnel_config(account=account)
    except (OSError, ValueError, TypeError):
        return []

    candidates: list[str] = []
    for bridge in config.bridges.values():
        proxy_url = str(getattr(bridge, "proxy_url", "") or "")
        if notebook_id not in proxy_url or "/proxy/" not in proxy_url:
            continue
        candidates.append(_rewrite_proxy_port(proxy_url, port))
    return candidates


def probe_existing_rtunnel_proxy_url(
    *,
    notebook_id: str,
    port: int,
    session: WebSession,
    candidate_urls: Optional[list[str]] = None,
    account: Optional[str] = None,
    cache_ttl_seconds: int = DEFAULT_PROXY_CACHE_TTL_SECONDS,
) -> str | None:
    """Return the existing proxy URL if it looks reachable (otherwise None)."""
    base_url = _get_base_url().rstrip("/")
    notebook_lab_path = _browser_api_path(f"/notebook/lab/{notebook_id}/proxy/{port}/")
    known_proxy_url = f"{base_url}{notebook_lab_path}"

    resolved_account = _resolve_rtunnel_account(explicit=account, session=session)
    urls: list[str] = [known_proxy_url]
    if candidate_urls:
        urls.extend(candidate_urls)
    urls.extend(
        get_cached_rtunnel_proxy_candidates(
            notebook_id=notebook_id,
            port=port,
            base_url=base_url,
            account=resolved_account,
            ttl_seconds=cache_ttl_seconds,
        )
    )
    urls.extend(
        _candidate_urls_from_tunnel_config(
            notebook_id=notebook_id,
            port=port,
            account=resolved_account,
        )
    )
    deduped_urls = list(dict.fromkeys(urls))

    http = None
    try:
        http = build_requests_session(session, base_url)
        for url in deduped_urls:
            resp = None
            try:
                resp = http.get(url, timeout=(5, 5), stream=True)
            except Exception:
                continue
            try:
                body = _response_body_prefix(resp)
                if not _is_reachable_proxy_response(status_code=resp.status_code, body=body):
                    continue
                try:
                    save_rtunnel_proxy_state(
                        notebook_id=notebook_id,
                        proxy_url=url,
                        port=port,
                        ssh_port=22222,
                        base_url=base_url,
                        account=resolved_account,
                    )
                except OSError:
                    pass
                return url
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
        return None
    except (OSError, ValueError, RuntimeError, AttributeError):
        return None
    finally:
        try:
            if http is not None:
                http.close()
        except (OSError, AttributeError):
            pass


# ============================================================================
# Flow
# ============================================================================


def _timing_enabled() -> bool:
    value = os.environ.get("INSPIRE_RTUNNEL_TIMING", "")
    return value.strip().lower() in {"1", "true", "yes"}


class _StepTimer:
    """Lightweight per-step timing collector for the rtunnel setup flow.

    When *enabled* is ``False`` every method is a no-op (zero overhead).
    """

    def __init__(self, *, enabled: bool = False) -> None:
        self._enabled = enabled
        self._steps: list[tuple[str, float]] = []  # (label, elapsed_s)
        self._last = time.monotonic() if enabled else 0.0

    def mark(self, label: str) -> float:
        """Record elapsed time since the previous mark.

        Returns the step duration in seconds (0.0 when disabled).
        """
        if not self._enabled:
            return 0.0
        import sys as _sys

        now = time.monotonic()
        elapsed = now - self._last
        self._last = now
        self._steps.append((label, elapsed))
        _sys.stderr.write(f"  [timing] {label}: {elapsed:.3f}s\n")
        _sys.stderr.flush()
        return elapsed

    def summary(self) -> None:
        """Print a visual summary table to stderr."""
        if not self._enabled or not self._steps:
            return
        import sys as _sys

        total = sum(s for _, s in self._steps)
        if total <= 0:
            return

        max_label = max(len(label) for label, _ in self._steps)
        bar_width = 30

        _sys.stderr.write("\n  ── rtunnel timing summary ──\n")
        for label, elapsed in self._steps:
            pct = elapsed / total * 100
            bar_len = int(round(pct / 100 * bar_width))
            bar = "#" * bar_len
            _sys.stderr.write(f"  {label:<{max_label}}  {elapsed:6.2f}s  {pct:5.1f}%  {bar}\n")
        _sys.stderr.write(f"  {'TOTAL':<{max_label}}  {total:6.2f}s\n")
        _sys.stderr.flush()


def _jupyter_server_base(lab_url: str) -> str:
    """Derive the Jupyter server base URL from a lab frame URL.

    Only strips ``/lab`` when it is the **final** path segment (the
    JupyterLab UI route), not when ``/lab/`` appears mid-path as part
    of the platform's proxy path (e.g. ``/api/v1/notebook/lab/{id}/``).
    """
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(lab_url)
    path = parts.path.rstrip("/")
    if path.endswith("/lab"):
        path = path[:-4]
    if not path.endswith("/"):
        path = path + "/"
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _build_jupyter_xsrf_headers(context: Any) -> dict[str, str]:
    """Return Jupyter XSRF headers from browser context cookies (best-effort)."""
    headers: dict[str, str] = {}
    try:
        for cookie in context.cookies():
            if cookie.get("name") == "_xsrf":
                headers["X-XSRFToken"] = cookie["value"]
                break
    except (AttributeError, KeyError, TypeError):
        pass
    return headers


def _create_terminal_via_api(context: Any, lab_url: str) -> str | None:
    """Create a JupyterLab terminal via REST API.

    Uses ``context.request`` which shares the browser session's cookies.
    JupyterLab requires an ``_xsrf`` cookie value in the ``X-XSRFToken``
    header for state-changing requests.
    Returns the terminal name (e.g. ``"1"``) on success, or ``None``.
    """
    base = _jupyter_server_base(lab_url)
    api_url = f"{base}api/terminals"
    try:
        headers = _build_jupyter_xsrf_headers(context)
        resp = context.request.post(api_url, headers=headers, timeout=10000)
        if resp.status in (200, 201):
            data = resp.json()
            return data.get("name")
    except (
        PlaywrightError,
        ConnectionError,
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
        TypeError,
    ):
        pass
    return None


def _delete_terminal_via_api(
    context: Any,
    *,
    lab_url: str,
    term_name: str,
) -> bool:
    """Delete a Jupyter terminal by name (best-effort cleanup)."""
    from urllib.parse import quote

    safe_term_name = (term_name or "").strip()
    if not safe_term_name:
        return False

    base = _jupyter_server_base(lab_url)
    api_url = f"{base}api/terminals/{quote(safe_term_name, safe='')}"
    try:
        headers = _build_jupyter_xsrf_headers(context)
        resp = context.request.delete(api_url, headers=headers, timeout=5000)
        # 404 means the terminal is already gone, which is a successful cleanup.
        return resp.status in (200, 204, 404)
    except (
        PlaywrightError,
        ConnectionError,
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
        TypeError,
    ):
        return False


def _extract_jupyter_token(lab_url: str) -> str | None:
    from urllib.parse import parse_qs, urlsplit

    parsed = urlsplit(lab_url)
    query_token = parse_qs(parsed.query).get("token", [None])[0]
    if query_token:
        return query_token

    path_parts = [part for part in parsed.path.split("/") if part]
    try:
        jupyter_index = path_parts.index("jupyter")
        if len(path_parts) > jupyter_index + 2:
            return path_parts[jupyter_index + 2]
    except ValueError:
        return None
    return None


def _build_terminal_websocket_url(lab_url: str, term_name: str) -> str:
    from urllib.parse import urlencode, urlsplit, urlunsplit

    base = _jupyter_server_base(lab_url)
    parsed = urlsplit(base)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    base_path = parsed.path if parsed.path.endswith("/") else f"{parsed.path}/"
    ws_path = f"{base_path}terminals/websocket/{term_name}"

    token = _extract_jupyter_token(lab_url)
    query = urlencode({"token": token}) if token else ""
    return urlunsplit((scheme, parsed.netloc, ws_path, query, ""))


def _send_terminal_command_via_websocket(
    page_or_frame: Any,
    *,
    ws_url: str,
    command: str,
    timeout_ms: int = 5000,
    completion_marker: str | None = None,
) -> bool:
    """Send a command to a Jupyter terminal via WebSocket.

    *page_or_frame* should be the Playwright frame whose origin matches the
    WebSocket URL (typically the JupyterLab iframe, not the outer page) so
    that the browser creates a same-origin WebSocket connection.

    Waits for a shell prompt (``["stdout", ...]`` message) before sending
    stdin so that the command is not lost if bash hasn't initialized yet.

    When *completion_marker* is set, the function keeps the WebSocket open
    after sending and waits until the marker string appears in a subsequent
    stdout message.  This allows callers to block until a setup script
    finishes (e.g. ``INSPIRE_RTUNNEL_SETUP_DONE``).
    """
    stdin_payload = command.rstrip("\r\n") + "\r"
    try:
        result = page_or_frame.evaluate(
            """
                async ({ wsUrl, stdinData, timeoutMs, promptTimeoutMs, marker }) => {
                  return await new Promise((resolve) => {
                    let settled = false;
                    let sent = false;
                    let socket = null;
                    const finish = (ok) => {
                      if (settled) return;
                      settled = true;
                      try {
                        if (socket) socket.close();
                      } catch (_) {}
                      resolve(ok);
                    };

                    const timer = setTimeout(() => finish(false), timeoutMs);

                    const CHUNK = 2048;
                    const DELAY = 50;
                    const doSend = () => {
                      if (sent || settled) return;
                      sent = true;
                      const chunks = [];
                      for (let i = 0; i < stdinData.length; i += CHUNK)
                        chunks.push(stdinData.slice(i, i + CHUNK));
                      let idx = 0;
                      const next = () => {
                        if (settled) return;
                        try {
                          socket.send(JSON.stringify(["stdin", chunks[idx]]));
                        } catch (_) {
                          clearTimeout(timer);
                          finish(false);
                          return;
                        }
                        idx++;
                        if (idx < chunks.length) {
                          setTimeout(next, DELAY);
                        } else if (!marker) {
                          setTimeout(() => {
                            clearTimeout(timer);
                            finish(true);
                          }, 180);
                        }
                      };
                      next();
                    };

                    try {
                      socket = new WebSocket(wsUrl);
                    } catch (_) {
                      clearTimeout(timer);
                      finish(false);
                      return;
                    }

                    let stdoutBuf = "";
                    const promptRe = /[$#]\\s*$/;
                    socket.addEventListener("message", (ev) => {
                      try {
                        const msg = JSON.parse(ev.data);
                        if (Array.isArray(msg) && msg[0] === "stdout") {
                          const text = String(msg[1]);
                          if (!sent) {
                            stdoutBuf += text;
                            if (promptRe.test(stdoutBuf)) {
                              doSend();
                            }
                          } else if (marker && text.includes(marker)) {
                            clearTimeout(timer);
                            finish(true);
                          }
                        }
                      } catch (_) {}
                    });

                    socket.addEventListener("open", () => {
                      // Fall back after promptTimeoutMs in case
                      // the shell never emits a recognisable prompt.
                      setTimeout(() => doSend(), promptTimeoutMs);
                    });

                    socket.addEventListener("error", () => {
                      clearTimeout(timer);
                      finish(false);
                    });

                    socket.addEventListener("close", (ev) => {
                      if (!settled) {
                        clearTimeout(timer);
                        finish(false);
                      }
                    });
                  });
                }
                """,
            {
                "wsUrl": ws_url,
                "stdinData": stdin_payload,
                "timeoutMs": int(timeout_ms),
                "promptTimeoutMs": min(int(timeout_ms) - 500, 3000),
                "marker": completion_marker or "",
            },
        )
        return bool(result)
    except (PlaywrightError, AttributeError, RuntimeError, TypeError, ValueError):
        return False


def _send_setup_command_via_terminal_ws(
    *,
    context: Any,
    lab_frame: Any,
    batch_cmd: str,
) -> bool:
    term_name = _create_terminal_via_api(context, lab_frame.url)
    if not term_name:
        return False

    try:
        ws_url = _build_terminal_websocket_url(lab_frame.url, term_name)
        return _send_terminal_command_via_websocket(
            lab_frame,
            ws_url=ws_url,
            command=batch_cmd,
            # The websocket path should wait until the remote setup script emits
            # the explicit completion marker. Otherwise the CLI can start
            # probing rtunnel/SSH while package install and process startup are
            # still in flight, which makes bootstrap look flaky.
            timeout_ms=120000,
            completion_marker=SETUP_DONE_MARKER,
        )
    finally:
        _delete_terminal_via_api(context, lab_url=lab_frame.url, term_name=term_name)


class RtunnelMissingInContainerError(RuntimeError):
    """Raised when the bootstrap script finishes but rtunnel is still absent.

    rtunnel may be copied to ``/tmp/rtunnel`` by older bootstrap paths, or run
    directly from the global_public kit by the current zero-copy path.
    """


class OpenSSHInternalInstallError(RuntimeError):
    """Raised when OpenSSH install/correction through the internal apt mirror failed."""


OpenSSHJammyInstallError = OpenSSHInternalInstallError


def _probe_terminal_command_markers_via_ws(
    *,
    context: Any,
    lab_frame: Any,
    command: str,
    markers: dict[str, Any],
    timeout_ms: int = 5000,
) -> Any | None:
    """Run a tiny terminal command and return the value mapped by a marker.

    ``None`` means the terminal/WS probe itself was inconclusive.
    """
    term_name = _create_terminal_via_api(context, lab_frame.url)
    if not term_name:
        return None

    marker_tokens = list(markers)

    try:
        ws_url = _build_terminal_websocket_url(lab_frame.url, term_name)
        # Jupyter terminals echo stdin before command output.  Marker probes often
        # include both success and failure markers in shell branches, so sending
        # the raw command can make the echoed command look like a real marker.
        # Ship the probe body through a base64 wrapper; terminal echo then
        # contains only the encoded payload while stdout still carries markers.
        probe_command = _build_batch_setup_script([command.rstrip("\r\n")])
        stdin_data = f"{probe_command}\r"
        try:
            result = lab_frame.evaluate(
                """
                async ({ wsUrl, stdinData, timeoutMs, promptTimeoutMs, markers }) => {
                  return await new Promise((resolve) => {
                    let settled = false;
                    let sent = false;
                    let socket = null;
                    const finish = (value) => {
                      if (settled) return;
                      settled = true;
                      try { if (socket) socket.close(); } catch (_) {}
                      resolve(value);
                    };
                    const timer = setTimeout(() => finish(null), timeoutMs);
                    const doSend = () => {
                      if (sent || settled) return;
                      sent = true;
                      try { socket.send(JSON.stringify(["stdin", stdinData])); }
                      catch (_) { clearTimeout(timer); finish(null); }
                    };
                    try { socket = new WebSocket(wsUrl); }
                    catch (_) { clearTimeout(timer); finish(null); return; }
                    let buf = "";
                    const promptRe = /[$#]\\s*$/;
                    socket.addEventListener("message", (ev) => {
                      try {
                        const msg = JSON.parse(ev.data);
                        if (!Array.isArray(msg) || msg[0] !== "stdout") return;
                        const text = String(msg[1]);
                        buf += text;
                        if (!sent) {
                          if (promptRe.test(buf)) doSend();
                          return;
                        }
                        for (const marker of markers) {
                          if (buf.includes(marker)) {
                            clearTimeout(timer); finish(marker);
                            return;
                          }
                        }
                      } catch (_) {}
                    });
                    socket.addEventListener("open", () => {
                      setTimeout(() => doSend(), promptTimeoutMs);
                    });
                    socket.addEventListener("error", () => {
                      clearTimeout(timer); finish(null);
                    });
                    socket.addEventListener("close", () => {
                      if (!settled) { clearTimeout(timer); finish(null); }
                    });
                  });
                }
                """,
                {
                    "wsUrl": ws_url,
                    "stdinData": stdin_data,
                    "timeoutMs": int(timeout_ms),
                    "promptTimeoutMs": min(int(timeout_ms) - 500, 2500),
                    "markers": marker_tokens,
                },
            )
        except (PlaywrightError, AttributeError, RuntimeError, TypeError, ValueError) as exc:
            _log.debug("terminal marker probe failed to evaluate: %s", exc)
            return None

        return markers.get(result)
    finally:
        try:
            _delete_terminal_via_api(context, lab_url=lab_frame.url, term_name=term_name)
        except Exception:
            pass


def _check_rtunnel_present_via_ws(
    *,
    context: Any,
    lab_frame: Any,
    timeout_ms: int = 5000,
) -> Optional[bool]:
    """Return ``True`` when rtunnel is executable inside the container.

    ``False`` means confirmed missing; ``None`` means the probe was
    inconclusive. Callers should only surface a rtunnel-missing error on an
    explicit ``False``.
    """
    present_marker = "__INSPIRE_RTUNNEL_PRESENT__"
    absent_marker = "__INSPIRE_RTUNNEL_ABSENT__"
    command = (
        '_rt_arch=$(uname -m 2>/dev/null); '
        'case "$_rt_arch" in arm64|aarch64) _rt_arch=arm64;; *) _rt_arch=amd64;; esac; '
        f'_kit_rt="{INSPIRE_BOOTSTRAP_ROOT}/rtunnel/linux-${{_rt_arch}}/rtunnel"; '
        f'([ -x /tmp/rtunnel ] || [ -x "$_kit_rt" ]) && echo {present_marker} '
        f'|| echo {absent_marker}'
    )
    return _probe_terminal_command_markers_via_ws(
        context=context,
        lab_frame=lab_frame,
        command=command,
        markers={present_marker: True, absent_marker: False},
        timeout_ms=timeout_ms,
    )


def _check_openssh_install_failed_via_ws(
    *,
    context: Any,
    lab_frame: Any,
    timeout_ms: int = 5000,
) -> Optional[bool]:
    failed_marker = "__INSPIRE_OPENSSH_FAILED__"
    ok_marker = "__INSPIRE_OPENSSH_OK__"
    command = (
        f'[ -f "{OPENSSH_INSTALL_FAILED_FILE}" ] '
        f'&& echo {failed_marker} || echo {ok_marker}'
    )
    return _probe_terminal_command_markers_via_ws(
        context=context,
        lab_frame=lab_frame,
        command=command,
        markers={failed_marker: True, ok_marker: False},
        timeout_ms=timeout_ms,
    )


_check_openssh_jammy_install_failed_via_ws = _check_openssh_install_failed_via_ws


def _build_batch_setup_script(cmd_lines: list[str]) -> str:
    """Encode setup commands as a single base64-wrapped bash line.

    Instead of typing each command separately (fragile if the terminal
    loses focus), we ship the entire script as::

        echo '<base64>' | base64 -d | bash
    """
    import base64

    script = "\n".join(cmd_lines) + "\n"
    encoded = base64.b64encode(script.encode()).decode()
    return f"echo '{encoded}' | base64 -d | bash"


_TERMINAL_TAB_SELECTOR = "li.lm-TabBar-tab:has-text('Terminal'), li.lm-TabBar-tab:has-text('终端')"
_TERMINAL_CARD_SELECTOR = (
    "div.jp-LauncherCard:has-text('Terminal'), div.jp-LauncherCard:has-text('终端')"
)
_TERMINAL_INPUT_SELECTORS = (
    "textarea.xterm-helper-textarea",
    "div.xterm-helper-textarea textarea",
)
_FAST_API_XTERM_ATTACH_TIMEOUT_MS = 1600
_FAST_API_MENU_READY_TIMEOUT_MS = 2500
_FAST_TERMINAL_TAB_CLICK_TIMEOUT_MS = 900
_FAST_TERMINAL_CARD_WAIT_TIMEOUT_MS = 3500
_FAST_TERMINAL_CARD_CLICK_TIMEOUT_MS = 2500
_FAST_MENU_ACTION_TIMEOUT_MS = 1800
_API_TERMINAL_PROGRESSIVE_WAIT_MS = 1800
_API_TERMINAL_RECOVERY_WAIT_MS = 900
_API_TERMINAL_POLL_MS = 220
_API_TERMINAL_TAB_POKE_INTERVAL_MS = 1200
_FOCUS_INPUT_WAIT_TIMEOUT_MS = 900
_FOCUS_INPUT_CLICK_TIMEOUT_MS = 500
_FOCUS_TAB_CLICK_TIMEOUT_MS = 450
_FOCUS_TEXTAREA_ATTACH_TIMEOUT_MS = 3000
_FOCUS_RETRY_PASSES = 4


def _wait_for_terminal_surface(
    lab_frame: Any,
    *,
    timeout_ms: int,
) -> bool:
    try:
        lab_frame.locator(".xterm").first.wait_for(state="attached", timeout=timeout_ms)
        return True
    except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError):
        pass

    for selector in _TERMINAL_INPUT_SELECTORS:
        try:
            if lab_frame.locator(selector).first.count() > 0:
                return True
        except (PlaywrightError, RuntimeError, AttributeError, TypeError, ValueError):
            pass
    return False


def _wait_for_terminal_surface_progressive(
    lab_frame: Any,
    page: Any,
    *,
    total_timeout_ms: int,
    poll_ms: int = _API_TERMINAL_POLL_MS,
    tab_poke_interval_ms: int = _API_TERMINAL_TAB_POKE_INTERVAL_MS,
) -> bool:
    start = time.monotonic()
    last_tab_poke = -tab_poke_interval_ms
    min_probe_ms = 80

    while True:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        remaining_ms = total_timeout_ms - elapsed_ms
        if remaining_ms <= 0:
            return False

        probe_timeout_ms = max(min_probe_ms, min(280, remaining_ms))
        if _wait_for_terminal_surface(lab_frame, timeout_ms=probe_timeout_ms):
            return True

        elapsed_ms = int((time.monotonic() - start) * 1000)
        if elapsed_ms - last_tab_poke >= tab_poke_interval_ms:
            _click_terminal_tab(
                lab_frame,
                page,
                timeout_ms=min(_FAST_TERMINAL_TAB_CLICK_TIMEOUT_MS, 350),
                settle_ms=40,
            )
            last_tab_poke = elapsed_ms

        elapsed_ms = int((time.monotonic() - start) * 1000)
        remaining_ms = total_timeout_ms - elapsed_ms
        if remaining_ms <= 0:
            return False
        page.wait_for_timeout(max(40, min(poll_ms, remaining_ms)))


def _wait_for_file_menu_ready(
    lab_frame: Any,
    *,
    timeout_ms: int,
) -> bool:
    per_label_timeout = max(300, timeout_ms // 2)
    for label in ("File", "文件"):
        try:
            lab_frame.get_by_role("menuitem", name=label).first.wait_for(
                state="visible",
                timeout=per_label_timeout,
            )
            return True
        except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError):
            pass
    return False


def _click_terminal_tab(
    lab_frame: Any,
    page: Any,
    *,
    timeout_ms: int,
    settle_ms: int = 80,
) -> bool:
    try:
        term_tab = lab_frame.locator(_TERMINAL_TAB_SELECTOR).first
        if term_tab.count() <= 0:
            return False
        term_tab.click(timeout=timeout_ms)
        if settle_ms > 0:
            page.wait_for_timeout(settle_ms)
        return True
    except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError, TypeError):
        return False


def _open_terminal_from_file_menu(
    lab_frame: Any,
    *,
    action_timeout_ms: int,
) -> bool:
    for labels in (("File", "New", "Terminal"), ("文件", "新建", "终端")):
        file_label, new_label, terminal_label = labels
        try:
            lab_frame.get_by_role("menuitem", name=file_label).first.click(
                timeout=action_timeout_ms
            )
            lab_frame.get_by_role("menuitem", name=new_label).first.hover(timeout=action_timeout_ms)
            lab_frame.get_by_role("menuitem", name=terminal_label).first.click(
                timeout=action_timeout_ms
            )
            return True
        except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError):
            pass
    return False


def _verify_terminal_focus(lab_frame: Any) -> bool:
    """Check that document.activeElement is the xterm textarea."""
    try:
        tag = lab_frame.evaluate("document.activeElement?.tagName?.toLowerCase()")
        cls = lab_frame.evaluate("document.activeElement?.className || ''")
        return tag == "textarea" and "xterm" in cls
    except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError, TypeError):
        return False


def _focus_terminal_input(
    lab_frame: Any,
    page: Any,
) -> bool:
    # Gate: wait for xterm.js to create its helper textarea.
    # The .xterm container attaches before the textarea is created,
    # so _wait_for_terminal_surface() may pass while focus is impossible.
    textarea_found = False
    for sel in _TERMINAL_INPUT_SELECTORS:
        try:
            lab_frame.locator(sel).first.wait_for(
                state="attached", timeout=_FOCUS_TEXTAREA_ATTACH_TIMEOUT_MS
            )
            textarea_found = True
            break
        except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError):
            pass

    if not textarea_found:
        return False

    for pass_idx in range(_FOCUS_RETRY_PASSES):
        # Dismiss any dialog that may be stealing focus (e.g. jp-mod-accept)
        if pass_idx == 0:
            _dismiss_terminal_dialog_once(lab_frame=lab_frame, page=page, settle_ms=80)

        # Try 1: Click the visible .xterm container (triggers xterm.js internal focus)
        try:
            xterm_el = lab_frame.locator(".xterm").first
            if xterm_el.count() > 0:
                xterm_el.click(timeout=_FOCUS_INPUT_CLICK_TIMEOUT_MS, force=True)
                page.wait_for_timeout(40)
                if _verify_terminal_focus(lab_frame):
                    return True
        except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError):
            pass

        # Try 2: Atomic JS — dispatch mousedown on .xterm then focus textarea
        try:
            ok = lab_frame.evaluate(
                """(() => {
                    const xterm = document.querySelector('.xterm');
                    if (xterm) {
                        xterm.dispatchEvent(new MouseEvent('mousedown', {bubbles: true}));
                        xterm.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
                    }
                    const el = document.querySelector('textarea.xterm-helper-textarea');
                    if (!el) return false;
                    el.focus();
                    return document.activeElement === el;
                })()"""
            )
            if ok:
                return True
        except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError, TypeError):
            pass

        _click_terminal_tab(
            lab_frame,
            page,
            timeout_ms=_FOCUS_TAB_CLICK_TIMEOUT_MS,
            settle_ms=40,
        )
        page.wait_for_timeout(120)

    return False


def _log_terminal_status(message: str) -> None:
    import sys as _sys

    _sys.stderr.write(message + "\n")
    _sys.stderr.flush()


def _wait_for_api_terminal_surface(
    lab_frame: Any,
    page: Any,
) -> bool:
    if _wait_for_terminal_surface(lab_frame, timeout_ms=500):
        return True
    if _wait_for_terminal_surface_progressive(
        lab_frame,
        page,
        total_timeout_ms=_API_TERMINAL_PROGRESSIVE_WAIT_MS,
    ):
        return True
    return _wait_for_terminal_surface_progressive(
        lab_frame,
        page,
        total_timeout_ms=_API_TERMINAL_RECOVERY_WAIT_MS,
    )


def _open_terminal_via_rest_api(
    *,
    context: Any,
    page: Any,
    lab_frame: Any,
) -> tuple[bool, bool, str | None]:
    lab_url = lab_frame.url
    term_name = _create_terminal_via_api(context, lab_url)
    if not term_name:
        return False, False, None

    _log_terminal_status(f"  Created terminal '{term_name}' via REST API.")
    server_base = _jupyter_server_base(lab_url)
    term_url = f"{server_base}lab/terminals/{term_name}?reset"
    try:
        lab_frame.goto(term_url, timeout=15000, wait_until="domcontentloaded")
        if _wait_for_terminal_surface(lab_frame, timeout_ms=_FAST_API_XTERM_ATTACH_TIMEOUT_MS):
            return True, True, term_name
    except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError) as _nav_err:
        _log_terminal_status(
            f"  REST API terminal created but navigation failed ({type(_nav_err).__name__}: {str(_nav_err)[:150]}), trying DOM fallbacks..."
        )
        return False, True, term_name

    _log_terminal_status(
        "  REST API terminal created but xterm not yet visible; continuing with API terminal path."
    )
    return _wait_for_api_terminal_surface(lab_frame, page), True, term_name


def _recover_api_terminal_surface(
    *,
    lab_frame: Any,
    page: Any,
) -> bool:
    if _click_terminal_tab(
        lab_frame,
        page,
        timeout_ms=min(_FAST_TERMINAL_TAB_CLICK_TIMEOUT_MS, 500),
        settle_ms=60,
    ) and _wait_for_terminal_surface_progressive(
        lab_frame,
        page,
        total_timeout_ms=900,
    ):
        return True

    menu_ready = _wait_for_file_menu_ready(lab_frame, timeout_ms=_FAST_API_MENU_READY_TIMEOUT_MS)
    if (
        menu_ready
        and _open_terminal_from_file_menu(
            lab_frame,
            action_timeout_ms=_FAST_MENU_ACTION_TIMEOUT_MS,
        )
        and _wait_for_terminal_surface_progressive(
            lab_frame,
            page,
            total_timeout_ms=2200,
        )
    ):
        return True

    return False


def _wait_for_terminal_entry_point(
    *,
    lab_frame: Any,
    api_term_created: bool,
) -> None:
    if api_term_created:
        _wait_for_file_menu_ready(lab_frame, timeout_ms=_FAST_API_MENU_READY_TIMEOUT_MS)
        return

    try:
        lab_frame.locator(_TERMINAL_CARD_SELECTOR).first.wait_for(state="visible", timeout=45000)
    except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError):
        _wait_for_file_menu_ready(lab_frame, timeout_ms=45000)


def _dismiss_terminal_dialog_once(
    *,
    lab_frame: Any,
    page: Any,
    settle_ms: int,
) -> bool:
    for label in ("Dismiss", "OK", "Accept", "No", "否", "不接收", "取消", "确定"):
        try:
            btn = lab_frame.get_by_role("button", name=label)
            if btn.count() > 0:
                btn.first.click(timeout=1000)
                page.wait_for_timeout(settle_ms)
                return True
        except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError):
            pass

    # Fallback: click the accept button by CSS class (covers unlabeled/localized dialogs)
    for selector in (
        "button.jp-Dialog-button.jp-mod-accept",
        "button.jp-Dialog-close",
        "button[aria-label='Close']",
    ):
        try:
            btn = lab_frame.locator(selector)
            if btn.count() > 0:
                btn.first.click(timeout=1000)
                page.wait_for_timeout(settle_ms)
                return True
        except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError):
            pass

    return False


def _open_terminal_card(
    *,
    lab_frame: Any,
    api_term_created: bool,
) -> bool:
    terminal_card = lab_frame.locator(_TERMINAL_CARD_SELECTOR)
    card_wait_timeout = _FAST_TERMINAL_CARD_WAIT_TIMEOUT_MS if api_term_created else 8000
    card_click_timeout = _FAST_TERMINAL_CARD_CLICK_TIMEOUT_MS if api_term_created else 8000
    try:
        terminal_card.first.wait_for(state="visible", timeout=card_wait_timeout)
        terminal_card.first.click(timeout=card_click_timeout)
        return True
    except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError):
        return False


def _open_terminal_card_from_launcher(
    *,
    lab_frame: Any,
    page: Any,
    api_term_created: bool,
) -> bool:
    try:
        launcher_btn = lab_frame.locator(
            "button[title*='Launcher'], button[aria-label*='Launcher']"
        ).first
        if launcher_btn.count() > 0:
            launcher_btn.click(timeout=1200)
            page.wait_for_timeout(150)
    except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError):
        return False

    return _open_terminal_card(lab_frame=lab_frame, api_term_created=api_term_created)


def _open_terminal_via_dom_fallback(
    *,
    lab_frame: Any,
    page: Any,
    api_term_created: bool,
) -> bool:
    if _click_terminal_tab(
        lab_frame,
        page,
        timeout_ms=_FAST_TERMINAL_TAB_CLICK_TIMEOUT_MS,
        settle_ms=100,
    ):
        return True

    if _open_terminal_card(lab_frame=lab_frame, api_term_created=api_term_created):
        return True
    if _open_terminal_card_from_launcher(
        lab_frame=lab_frame,
        page=page,
        api_term_created=api_term_created,
    ):
        return True

    menu_action_timeout = _FAST_MENU_ACTION_TIMEOUT_MS if api_term_created else 2000
    if _open_terminal_from_file_menu(lab_frame, action_timeout_ms=menu_action_timeout):
        return True

    return api_term_created and _wait_for_terminal_surface(lab_frame, timeout_ms=1200)


def _open_or_create_terminal(
    context: Any,
    page: Any,
    lab_frame: Any,
) -> tuple[bool, str | None]:
    """Open a terminal in JupyterLab.  REST API first, then DOM fallbacks."""
    terminal_ready, api_term_created, term_name = _open_terminal_via_rest_api(
        context=context,
        page=page,
        lab_frame=lab_frame,
    )
    if terminal_ready:
        return True, term_name

    if api_term_created and _recover_api_terminal_surface(lab_frame=lab_frame, page=page):
        return True, term_name

    _wait_for_terminal_entry_point(lab_frame=lab_frame, api_term_created=api_term_created)
    _dismiss_terminal_dialog_once(lab_frame=lab_frame, page=page, settle_ms=150)

    if not _open_terminal_via_dom_fallback(
        lab_frame=lab_frame,
        page=page,
        api_term_created=api_term_created,
    ):
        return False, term_name

    _click_terminal_tab(
        lab_frame,
        page,
        timeout_ms=_FAST_TERMINAL_TAB_CLICK_TIMEOUT_MS,
        settle_ms=80,
    )
    _dismiss_terminal_dialog_once(lab_frame=lab_frame, page=page, settle_ms=120)
    return True, term_name


def _build_vscode_proxy_url(page, *, port: int) -> str | None:  # noqa: ANN001
    from urllib.parse import parse_qs, urlparse

    vscode_url = None
    for frame in page.frames:
        if "/vscode/" in (frame.url or ""):
            vscode_url = frame.url
            break
    if not vscode_url:
        return None

    parsed = urlparse(vscode_url)
    token = parse_qs(parsed.query).get("token", [None])[0]
    base = vscode_url.split("?", 1)[0].rstrip("/")
    proxy_url = f"{base}/proxy/{port}/"
    if token:
        proxy_url = f"{proxy_url}?token={token}"
    return proxy_url


def _derive_vscode_proxy_url(proxy_url: str) -> str | None:
    """Derive a VSCode proxy URL from a Jupyter proxy URL.

    Many platform deployments expose both:
      - /jupyter/<notebook>/<token>/proxy/<port>/
      - /vscode/<notebook>/<token>/proxy/<port>/

    The VSCode proxy is generally more reliable for WebSocket-based tunnels.
    """
    proxy_url = str(proxy_url or "").strip()
    if not proxy_url:
        return None
    if "/vscode/" in proxy_url:
        return proxy_url
    if "/jupyter/" not in proxy_url:
        return None
    return proxy_url.replace("/jupyter/", "/vscode/", 1)


def _extract_probe_error_summary(error: Exception) -> str:
    message = str(error).strip()
    if not message:
        return error.__class__.__name__

    lines = [line.strip() for line in message.splitlines() if line.strip()]
    if not lines:
        return error.__class__.__name__

    headline = lines[0]
    last_response = next((line for line in lines if line.startswith("Last response:")), "")
    if last_response:
        return f"{headline}; {last_response}"
    return headline


def _ensure_proxy_readiness_with_fallback(
    *,
    proxy_url: str,
    port: int,
    timeout: int,
    context,  # noqa: ANN001
    page,  # noqa: ANN001
) -> tuple[str, list[str]]:
    import sys as _sys

    diagnostics: list[str] = []
    primary_verify_timeout_s = max(20, min(timeout, 60))

    derived_vscode_url = _derive_vscode_proxy_url(proxy_url)
    if derived_vscode_url and derived_vscode_url != proxy_url:
        _sys.stderr.write(
            f"  Probing VSCode proxy URL first: {redact_proxy_url(derived_vscode_url)}\n"
        )
        _sys.stderr.flush()
        try:
            # Short timeout: the vscode path is speculative (derived by
            # replacing /jupyter/ → /vscode/).  If it exists, the proxy
            # will respond quickly; don't burn the full timeout here.
            wait_for_rtunnel_reachable(
                proxy_url=derived_vscode_url,
                timeout_s=min(6, timeout),
                context=context,
                page=page,
            )
            return derived_vscode_url, diagnostics
        except (
            PlaywrightError,
            ConnectionError,
            OSError,
            RuntimeError,
            TimeoutError,
            ValueError,
        ) as derived_error:
            diagnostics.append(f"derived={_extract_probe_error_summary(derived_error)}")

    try:
        wait_for_rtunnel_reachable(
            proxy_url=proxy_url,
            timeout_s=primary_verify_timeout_s,
            context=context,
            page=page,
        )
        return proxy_url, diagnostics
    except (
        PlaywrightError,
        ConnectionError,
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
    ) as primary_error:
        diagnostics.append(f"primary={_extract_probe_error_summary(primary_error)}")

    fallback_proxy_url = _build_vscode_proxy_url(page, port=port)
    if not fallback_proxy_url:
        try:
            vscode_tab = page.locator('img[alt="vscode"]').first
            if vscode_tab.count() > 0:
                vscode_tab.click(timeout=1500)
                page.wait_for_timeout(200)
        except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError):
            pass
        fallback_proxy_url = _build_vscode_proxy_url(page, port=port)

    best_for_ssh = proxy_url
    if fallback_proxy_url and fallback_proxy_url != proxy_url:
        best_for_ssh = fallback_proxy_url

    if not fallback_proxy_url or fallback_proxy_url == proxy_url:
        if _all_inconclusive_http_probe_diagnostics(diagnostics):
            _sys.stderr.write(
                "  HTTP readiness probe was inconclusive; continuing with SSH preflight.\n"
            )
        else:
            _sys.stderr.write(
                "  Proxy did not pass HTTP readiness; continuing with SSH preflight.\n"
            )
        _sys.stderr.flush()
        return best_for_ssh, diagnostics

    _sys.stderr.write(f"  Trying alternate proxy URL: {redact_proxy_url(fallback_proxy_url)}\n")
    _sys.stderr.flush()
    try:
        wait_for_rtunnel_reachable(
            proxy_url=fallback_proxy_url,
            timeout_s=max(12, min(timeout, 45)),
            context=context,
            page=page,
        )
        return fallback_proxy_url, diagnostics
    except (
        PlaywrightError,
        ConnectionError,
        OSError,
        RuntimeError,
        TimeoutError,
        ValueError,
    ) as fallback_error:
        diagnostics.append(f"fallback={_extract_probe_error_summary(fallback_error)}")
        if _all_inconclusive_http_probe_diagnostics(diagnostics):
            _sys.stderr.write(
                "  HTTP readiness probe remained inconclusive; continuing with SSH preflight.\n"
            )
        else:
            _sys.stderr.write(
                "  Fallback proxy did not pass HTTP readiness; continuing with SSH preflight.\n"
            )
        _sys.stderr.flush()
        return best_for_ssh, diagnostics


def _send_rtunnel_setup_script(
    *,
    context: Any,
    page: Any,
    lab_frame: Any,
    batch_cmd: str,
    timer: "_StepTimer",
) -> _SetupScriptSendResult:
    import sys as _sys

    setup_sent_via_ws = False
    try:
        setup_sent_via_ws = _send_setup_command_via_terminal_ws(
            context=context,
            lab_frame=lab_frame,
            batch_cmd=batch_cmd,
        )
    except (PlaywrightError, RuntimeError, TimeoutError, ValueError):
        setup_sent_via_ws = False

    if setup_sent_via_ws:
        _sys.stderr.write("  Sent setup script via Jupyter terminal WebSocket.\n")
        _sys.stderr.flush()
        timer.mark("open_terminal")
        timer.mark("focus_xterm")
        timer.mark("build_and_send_cmd")
        return _SetupScriptSendResult(sent_via_ws=True)

    _sys.stderr.write("  WebSocket terminal setup unavailable, using browser automation.\n")
    _sys.stderr.flush()

    browser_term_name: str | None = None
    browser_term_lab_url = str(getattr(lab_frame, "url", "") or "")

    def _cleanup_browser_terminal() -> None:
        if not browser_term_name:
            return
        try:
            _delete_terminal_via_api(
                context, lab_url=browser_term_lab_url, term_name=browser_term_name
            )
        except Exception:
            pass

    try:
        result, browser_term_name = _open_or_create_terminal(context, page, lab_frame)
        if not result:
            if _send_setup_command_via_terminal_ws(
                context=context,
                lab_frame=lab_frame,
                batch_cmd=batch_cmd,
            ):
                _sys.stderr.write(
                    "  Recovered by dispatching setup script via terminal WebSocket.\n"
                )
                _sys.stderr.flush()
                timer.mark("open_terminal")
                timer.mark("focus_xterm")
                timer.mark("build_and_send_cmd")
                return _SetupScriptSendResult(
                    sent_via_ws=True,
                    cleanup=_cleanup_browser_terminal,
                )
            raise ValueError("Failed to open Jupyter terminal")
        timer.mark("open_terminal")

        if not _focus_terminal_input(lab_frame, page):
            page.wait_for_timeout(350)
            if not _wait_for_terminal_surface(lab_frame, timeout_ms=2000):
                if _send_setup_command_via_terminal_ws(
                    context=context,
                    lab_frame=lab_frame,
                    batch_cmd=batch_cmd,
                ):
                    _sys.stderr.write(
                        "  xterm surface absent; dispatched setup via terminal WebSocket.\n"
                    )
                    _sys.stderr.flush()
                    timer.mark("focus_xterm")
                    timer.mark("build_and_send_cmd")
                    return _SetupScriptSendResult(
                        sent_via_ws=True,
                        cleanup=_cleanup_browser_terminal,
                    )
                raise ValueError("Failed to focus Jupyter terminal: xterm surface not ready")
            if not _focus_terminal_input(lab_frame, page):
                if _send_setup_command_via_terminal_ws(
                    context=context,
                    lab_frame=lab_frame,
                    batch_cmd=batch_cmd,
                ):
                    _sys.stderr.write(
                        "  xterm focus failed; dispatched setup via terminal WebSocket.\n"
                    )
                    _sys.stderr.flush()
                    timer.mark("focus_xterm")
                    timer.mark("build_and_send_cmd")
                    return _SetupScriptSendResult(
                        sent_via_ws=True,
                        cleanup=_cleanup_browser_terminal,
                    )
                raise ValueError("Failed to focus Jupyter terminal input")
        timer.mark("focus_xterm")

        _sys.stderr.write(
            f"  Executing setup script ({len(batch_cmd)} chars) in notebook terminal...\n"
        )
        _sys.stderr.flush()
        page.keyboard.insert_text(batch_cmd)
        page.keyboard.press("Enter")
        timer.mark("build_and_send_cmd")
        return _SetupScriptSendResult(
            sent_via_ws=False,
            cleanup=_cleanup_browser_terminal,
        )
    except Exception:
        _cleanup_browser_terminal()
        raise


def _wait_for_setup_completion(
    *,
    page: Any,
    setup_sent_via_ws: bool,
    timer: "_StepTimer",
) -> None:
    # Both WS and Playwright paths need time for the setup commands to execute
    # (dpkg install, dropbear keygen/start, rtunnel start).  The WS path sends
    # commands instantly but they still take time to run on the remote.
    if not setup_sent_via_ws:
        # xterm.js renders to <canvas>, so Playwright text locators are blind
        # to output. A short delay lets setup finish before HTTP probe checks.
        page.wait_for_timeout(3000)
    else:
        # WS path waits for SETUP_DONE_MARKER, so only a short settle is needed.
        page.wait_for_timeout(500)
    timer.mark("wait_marker")


def _capture_terminal_debug_artifact(*, page: Any, timer: "_StepTimer") -> None:
    try:
        page.screenshot(path="/tmp/notebook_terminal_debug.png")
    except (PlaywrightError, OSError, RuntimeError, TimeoutError, ValueError, TypeError):
        pass
    timer.mark("screenshot")


def _verify_and_cache_rtunnel_proxy(
    *,
    notebook_id: str,
    jupyter_proxy_url: str,
    port: int,
    ssh_port: int,
    timeout: int,
    context: Any,
    page: Any,
    account: str | None,
    timer: "_StepTimer",
) -> str:
    import sys as _sys

    _sys.stderr.write(
        f"  Verifying rtunnel is reachable at: {redact_proxy_url(jupyter_proxy_url)}\n"
    )
    _sys.stderr.flush()
    proxy_url, probe_diagnostics = _ensure_proxy_readiness_with_fallback(
        proxy_url=jupyter_proxy_url,
        port=port,
        timeout=timeout,
        context=context,
        page=page,
    )
    if probe_diagnostics:
        if _all_inconclusive_http_probe_diagnostics(probe_diagnostics):
            _log.debug("HTTP readiness diagnostics: %s", " | ".join(probe_diagnostics))
        else:
            _sys.stderr.write("  Proxy readiness summary: " + " | ".join(probe_diagnostics) + "\n")
        _sys.stderr.flush()
    timer.mark("verify_proxy")

    try:
        save_rtunnel_proxy_state(
            notebook_id=notebook_id,
            proxy_url=proxy_url,
            port=port,
            ssh_port=ssh_port,
            base_url=_get_base_url(),
            account=account,
        )
    except OSError:
        pass
    timer.mark("save_state")
    return proxy_url


def _setup_notebook_rtunnel_sync(
    notebook_id: str,
    port: int = 31337,
    ssh_port: int = 22222,
    ssh_public_key: Optional[str] = None,
    session: Optional[WebSession] = None,
    headless: bool = True,
    timeout: int = 120,
) -> str:
    """Sync implementation for setup_notebook_rtunnel."""
    import sys as _sys

    from playwright.sync_api import sync_playwright

    from inspire.platform.web.browser_api.playwright_notebooks import (
        build_jupyter_proxy_url,
        open_notebook_lab,
    )

    timing = _timing_enabled()
    timer = _StepTimer(enabled=timing)

    if session is None:
        session = get_web_session()
    account = _resolve_rtunnel_account(explicit=None, session=session)
    timer.mark("session_init")

    existing = probe_existing_rtunnel_proxy_url(
        notebook_id=notebook_id,
        port=port,
        session=session,
        account=account,
    )
    if existing:
        timer.mark("probe_existing")
        timer.summary()
        _sys.stderr.write("Using existing rtunnel connection (fast path).\n")
        _sys.stderr.flush()
        return existing

    timer.mark("probe_existing")
    _sys.stderr.write("Setting up rtunnel tunnel via browser automation...\n")
    _sys.stderr.flush()

    with sync_playwright() as p:
        browser = _launch_browser(p, headless=headless)
        timer.mark("playwright_launch")
        context = _new_context(browser, storage_state=session.storage_state)
        page = context.new_page()
        timer.mark("context_and_page")
        setup_terminal_cleanup: Callable[[], None] | None = None

        try:
            lab_frame = open_notebook_lab(
                page,
                notebook_id=notebook_id,
                timeout=max(60000, int(timeout) * 1000),
                session=session,
            )
            timer.mark("open_lab")
            jupyter_proxy_url = build_jupyter_proxy_url(lab_frame.url, port=port)
            timer.mark("build_proxy_url")

            try:
                lab_frame.locator("text=加载中").first.wait_for(state="hidden", timeout=30000)
            except (PlaywrightError, TimeoutError, RuntimeError, AttributeError, ValueError):
                pass
            timer.mark("wait_spinner")

            cmd_lines = build_rtunnel_setup_commands(
                port=port,
                ssh_port=ssh_port,
                ssh_public_key=ssh_public_key,
            )
            batch_cmd = _build_batch_setup_script(cmd_lines)
            _log.debug("Setup script length: %d chars, %d commands", len(batch_cmd), len(cmd_lines))
            setup_send_result = _send_rtunnel_setup_script(
                context=context,
                page=page,
                lab_frame=lab_frame,
                batch_cmd=batch_cmd,
                timer=timer,
            )
            setup_sent_via_ws = setup_send_result.sent_via_ws
            setup_terminal_cleanup = setup_send_result.cleanup
            _log.debug("Setup script sent via WS: %s", setup_sent_via_ws)
            _wait_for_setup_completion(
                page=page,
                setup_sent_via_ws=setup_sent_via_ws,
                timer=timer,
            )
            openssh_install_failed = _check_openssh_install_failed_via_ws(
                context=context,
                lab_frame=lab_frame,
            )
            timer.mark("check_openssh_install")
            _log.debug("openssh_install_failed=%s", openssh_install_failed)
            if openssh_install_failed is True:
                raise OpenSSHInternalInstallError(
                    "OpenSSH install/correction failed. "
                    "This bootstrap path requires the SII internal Ubuntu apt mirror."
                )
            # Detect the "no rtunnel in image + no public network" case up
            # front so the CLI can surface a structured repair hint instead
            # of making the user sit through a 120s proxy-verify timeout.
            # ``None`` (probe inconclusive) falls through to verify so a
            # transient WS glitch doesn't get blamed on image prep.
            rtunnel_present = _check_rtunnel_present_via_ws(
                context=context,
                lab_frame=lab_frame,
            )
            timer.mark("check_rtunnel_present")
            _log.debug("rtunnel_present=%s", rtunnel_present)
            if rtunnel_present is False:
                raise RtunnelMissingInContainerError(
                    "rtunnel binary missing inside the notebook container, "
                    "and bootstrap could not fetch one (no public network)."
                )
            _capture_terminal_debug_artifact(page=page, timer=timer)
            return _verify_and_cache_rtunnel_proxy(
                notebook_id=notebook_id,
                jupyter_proxy_url=jupyter_proxy_url,
                port=port,
                ssh_port=ssh_port,
                timeout=timeout,
                context=context,
                page=page,
                account=account,
                timer=timer,
            )

        finally:
            if setup_terminal_cleanup is not None:
                setup_terminal_cleanup()
            timer.summary()
            # Rely on sync_playwright() shutdown to terminate browser processes.
            # Explicit context/browser close can hang on some deployments.


# ============================================================================
# Public entry point
# ============================================================================


def setup_notebook_rtunnel(
    notebook_id: str,
    port: int = 31337,
    ssh_port: int = 22222,
    ssh_public_key: Optional[str] = None,
    session: Optional[WebSession] = None,
    headless: bool = True,
    timeout: int = 120,
) -> str:
    """Ensure the notebook exposes an rtunnel server via Jupyter proxy."""
    if _in_asyncio_loop():
        return _run_in_thread(
            _setup_notebook_rtunnel_sync,
            notebook_id=notebook_id,
            port=port,
            ssh_port=ssh_port,
            ssh_public_key=ssh_public_key,
            session=session,
            headless=headless,
            timeout=timeout,
        )
    return _setup_notebook_rtunnel_sync(
        notebook_id=notebook_id,
        port=port,
        ssh_port=ssh_port,
        ssh_public_key=ssh_public_key,
        session=session,
        headless=headless,
        timeout=timeout,
    )


__all__ = [
    "OpenSSHInternalInstallError",
    "OpenSSHJammyInstallError",
    "OPENSSH_INSTALL_FAILED_MARKER",
    "OPENSSH_INSTALL_FAILED_FILE",
    "OPENSSH_INSTALL_LOG",
    "OPENSSH_JAMMY_INSTALL_LOG",
    "RtunnelMissingInContainerError",
    "SII_UBUNTU_APT_MIRROR",
    "setup_notebook_rtunnel",
]
