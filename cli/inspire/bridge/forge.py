"""GitHub Actions forge: client + workflow orchestration + artifact fetching.

Flattened from the former `bridge/forge/` package after Gitea support was
dropped. Contains a single concrete HTTP client (`GitHubClient`), thin
config accessors, and the workflow/artifact helpers that `notebook exec` can
fall back to when no SSH tunnel is available.
"""

from __future__ import annotations

import json
import logging
import time
import zipfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Optional
from urllib import error as urlerror
from urllib import request as urlrequest

from inspire.config import Config, ConfigError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ForgeAuthError(ConfigError):
    """Authentication/configuration error for forge access."""


class ForgeError(Exception):
    """Generic forge API or workflow error."""


# ---------------------------------------------------------------------------
# Config accessors
# ---------------------------------------------------------------------------


def _sanitize_token(token: str) -> str:
    """Strip common prefixes (``bearer `` / ``token ``) and surrounding whitespace."""
    token = token.strip()
    lower = token.lower()
    if lower.startswith("bearer "):
        return token[7:].strip()
    if lower.startswith("token "):
        return token[6:].strip()
    return token


def _get_active_repo(config: Config) -> str:
    """Get the GitHub repository in ``owner/repo`` form."""
    repo = (getattr(config, "github_repo", None) or "").strip()
    if not repo:
        raise ForgeAuthError(
            "GitHub operations require INSP_GITHUB_REPO to be set.\n"
            "Use 'owner/repo' format.\n"
            "Example: export INSP_GITHUB_REPO='my-org/my-repo'"
        )
    if "/" not in repo:
        raise ForgeAuthError(
            f"Invalid INSP_GITHUB_REPO format '{repo}'. Expected 'owner/repo'."
        )
    return repo


def _get_active_token(config: Config) -> str:
    """Get the sanitized GitHub token."""
    token = (getattr(config, "github_token", None) or "").strip()
    if not token:
        raise ForgeAuthError("GitHub operations require INSP_GITHUB_TOKEN to be set.")
    return _sanitize_token(token)


def _get_active_server(config: Config) -> str:
    """Get the GitHub server URL (supports GitHub Enterprise)."""
    return (getattr(config, "github_server", None) or "https://github.com").rstrip("/")


def _get_active_workflow_file(config: Config, workflow_type: str) -> str:
    """Get the workflow filename for ``'sync'`` / ``'bridge'``."""
    if workflow_type == "sync":
        return getattr(config, "github_sync_workflow", "sync_code.yml")
    if workflow_type == "bridge":
        return getattr(config, "github_bridge_workflow", "run_bridge_action.yml")
    return "workflow.yml"


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@dataclass
class GitHubClient:
    """Thin HTTP client for the GitHub Actions REST API (supports GHE)."""

    token: str
    server_url: str

    def get_auth_header(self) -> str:
        return f"Bearer {self.token}"

    def get_api_base(self, repo: str) -> str:
        """``https://api.github.com/repos/<repo>/actions`` or the GHE ``/api/v3/`` form."""
        if self.server_url == "https://github.com":
            return f"https://api.github.com/repos/{repo}/actions"
        return f"{self.server_url}/api/v3/repos/{repo}/actions"

    def get_raw_file_url(self, repo: str, branch: str, filepath: str) -> str:
        """Raw file URL for the current branch."""
        if self.server_url == "https://github.com":
            raw_base = "https://raw.githubusercontent.com"
        else:
            # GitHub Enterprise or custom — prepend ``raw.`` to the host.
            raw_base = self.server_url.replace("https://", "https://raw.")
        return f"{raw_base}/{repo}/{branch}/{filepath}"

    def get_pagination_params(self, limit: int, page: int) -> str:
        return f"per_page={limit}&page={page}"

    def _build_request(
        self,
        method: str,
        url: str,
        data: Optional[dict] = None,
        accept: str = "application/json",
    ) -> urlrequest.Request:
        headers = {
            "Authorization": self.get_auth_header(),
            "Accept": accept,
            "User-Agent": "inspire-skill",
        }
        if data is not None:
            body = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:
            body = None

        req = urlrequest.Request(url, data=body, headers=headers)
        req.get_method = lambda: method  # type: ignore[method-assign]
        return req

    def request_json(self, method: str, url: str, data: Optional[dict] = None) -> dict:
        """JSON request with 3-retry backoff on 5xx / URLError."""
        max_retries = 3
        retry_delay = 2.0

        for attempt in range(max_retries + 1):
            try:
                req = self._build_request(method, url, data)
                with urlrequest.urlopen(req, timeout=60) as resp:
                    charset = resp.headers.get_content_charset("utf-8")
                    payload = resp.read().decode(charset)
                    if not payload:
                        return {}
                    return json.loads(payload)
            except urlerror.HTTPError as e:
                detail = None
                try:
                    raw = e.read().decode("utf-8")
                    parsed = json.loads(raw)
                    detail = parsed.get("message") or parsed.get("error")
                except Exception:
                    pass
                msg = f"API error {e.code} for {url}"
                if detail:
                    msg += f": {detail}"
                if e.code >= 500 and attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise ForgeError(msg)
            except urlerror.URLError as e:
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise ForgeError(f"API request failed for {url}: {e}")

        return {}

    def request_bytes(self, method: str, url: str) -> bytes:
        """Binary request with 3-retry backoff."""
        max_retries = 3
        retry_delay = 2.0

        for attempt in range(max_retries + 1):
            try:
                logger.debug("Forge request_bytes %s %s (attempt %d)", method, url, attempt + 1)
                req = self._build_request(method, url, data=None, accept="application/octet-stream")
                with urlrequest.urlopen(req, timeout=120) as resp:
                    return resp.read()
            except urlerror.HTTPError as e:
                debug_body = ""
                try:
                    raw = e.read()
                    if raw:
                        debug_body = raw.decode("utf-8", "replace")[:500]
                except Exception:
                    pass
                logger.debug("Forge HTTPError %s for %s, body=%r", e.code, url, debug_body)
                msg = f"API error {e.code} for {url}"
                if e.code >= 500 and attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise ForgeError(msg)
            except urlerror.URLError as e:
                logger.debug("Forge URLError for %s: %s (attempt %d)", url, e, attempt + 1)
                if attempt < max_retries:
                    time.sleep(retry_delay)
                    continue
                raise ForgeError(f"API request failed for {url}: {e}")

        return b""


def create_forge_client(config: Config) -> GitHubClient:
    """Build a GitHubClient from the active config."""
    return GitHubClient(token=_get_active_token(config), server_url=_get_active_server(config))


# Backwards-compatible alias: older call-sites may still reference ForgeClient as a type hint.
ForgeClient = GitHubClient


# ---------------------------------------------------------------------------
# Workflow-run helpers (parsing)
# ---------------------------------------------------------------------------


def _extract_total_count(response: dict) -> Optional[int]:
    total_count = response.get("total_count") or response.get("total") or response.get("count")
    try:
        return int(total_count) if total_count is not None else None
    except (TypeError, ValueError):
        return None


def _parse_event_inputs(run: dict) -> dict:
    event_payload = run.get("event_payload", "")
    if not event_payload:
        return {}
    try:
        payload = json.loads(event_payload)
    except (json.JSONDecodeError, TypeError):
        return {}
    inputs = payload.get("inputs", {}) or {}
    return inputs if isinstance(inputs, dict) else {}


def _matches_inputs(inputs: dict, expected_inputs: dict) -> bool:
    for key, value in expected_inputs.items():
        if not value:
            continue
        if str(inputs.get(key, "")) != str(value):
            return False
    return True


def _find_run_by_inputs(runs: list, expected_inputs: dict) -> Optional[dict]:
    for run in runs:
        inputs = _parse_event_inputs(run)
        if not inputs:
            continue
        if _matches_inputs(inputs, expected_inputs):
            return run
    return None


# ---------------------------------------------------------------------------
# Workflows
# ---------------------------------------------------------------------------


def trigger_workflow_dispatch(
    config: Config,
    workflow_file: str,
    inputs: dict,
    ref: str = "main",
) -> dict:
    """Trigger a workflow via workflow_dispatch; raise ForgeError on failure."""
    repo = _get_active_repo(config)
    client = create_forge_client(config)
    url = f"{client.get_api_base(repo)}/workflows/{workflow_file}/dispatches"
    data = {"ref": ref, "inputs": inputs}
    try:
        return client.request_json("POST", url, data)
    except ForgeError as e:
        raise ForgeError(f"Failed to trigger workflow: {e}")


def trigger_bridge_action_workflow(
    config: Config,
    raw_command: str,
    remote_cwd: str | None,
    artifact_paths: list[str],
    request_id: str,
    denylist: Optional[list[str]] = None,
) -> None:
    """Trigger the bridge-action workflow for arbitrary remote command execution."""
    inputs = {
        "raw_command": raw_command,
        "denylist": "\n".join(denylist or []),
        "remote_cwd": remote_cwd or "",
        "artifact_paths": "\n".join(artifact_paths),
        "request_id": request_id,
    }
    trigger_workflow_dispatch(config, _get_active_workflow_file(config, "bridge"), inputs)


def get_workflow_runs(config: Config, limit: int = 20) -> list:
    repo = _get_active_repo(config)
    client = create_forge_client(config)
    url = f"{client.get_api_base(repo)}/runs?{client.get_pagination_params(limit, 1)}"
    try:
        response = client.request_json("GET", url)
        return response.get("workflow_runs", []) or []
    except ForgeError as e:
        raise ForgeError(f"Failed to get workflow runs: {e}")


def get_workflow_run(config: Config, run_id: str) -> dict:
    repo = _get_active_repo(config)
    client = create_forge_client(config)
    url = f"{client.get_api_base(repo)}/runs/{run_id}"
    try:
        return client.request_json("GET", url)
    except ForgeError as e:
        raise ForgeError(f"Failed to get workflow run: {e}")


def wait_for_workflow_completion(
    config: Config,
    run_id: str,
    timeout: Optional[int] = None,
) -> dict:
    """Poll ``get_workflow_run`` until the run reaches a terminal state."""
    timeout_seconds = timeout or config.remote_timeout or 90
    deadline = time.time() + max(5, int(timeout_seconds))

    while True:
        if time.time() > deadline:
            raise TimeoutError(
                f"Workflow timed out after {timeout_seconds} seconds.\n"
                "To increase the timeout, set: export INSP_REMOTE_TIMEOUT=<seconds>"
            )

        run = get_workflow_run(config, run_id)
        status = run.get("status")
        conclusion = run.get("conclusion")
        if status in ("completed", "success", "failure"):
            return {
                "status": status,
                "conclusion": conclusion or status,
                "run_id": run_id,
                "html_url": run.get("html_url", ""),
            }
        time.sleep(3)


def wait_for_bridge_action_completion(
    config: Config,
    request_id: str,
    timeout: Optional[int] = None,
) -> dict:
    """Poll the workflow-runs list for a bridge-action matching ``request_id``."""
    repo = _get_active_repo(config)
    client = create_forge_client(config)
    timeout_seconds = int(timeout) if timeout is not None else int(config.bridge_action_timeout)
    deadline = time.time() + max(5, int(timeout_seconds))

    limit = 20

    def _find_matching_run(runs_list: list) -> Optional[dict]:
        run = _find_run_by_inputs(runs_list, {"request_id": request_id})
        if not run:
            return None
        status = run.get("status")
        conclusion = run.get("conclusion")
        logger.debug("Found matching run: status=%s, conclusion=%s", status, conclusion)
        if status in ("completed", "success", "failure"):
            return {
                "status": status,
                "conclusion": conclusion or status,
                "run_id": run.get("id"),
                "html_url": run.get("html_url", ""),
            }
        return None

    while True:
        if time.time() > deadline:
            raise TimeoutError(f"Bridge action timed out after {timeout_seconds} seconds.")

        try:
            runs_url = f"{client.get_api_base(repo)}/runs?{client.get_pagination_params(limit, 1)}"
            response = client.request_json("GET", runs_url)
            runs = response.get("workflow_runs", []) or []

            match = _find_matching_run(runs)
            if match:
                return match

            total_count = _extract_total_count(response)
            if total_count and total_count > limit:
                last_page = (total_count + limit - 1) // limit
                runs_url = (
                    f"{client.get_api_base(repo)}/runs?"
                    f"{client.get_pagination_params(limit, last_page)}"
                )
                response = client.request_json("GET", runs_url)
                runs = response.get("workflow_runs", []) or []
                match = _find_matching_run(runs)
                if match:
                    return match
        except ForgeError:
            pass

        time.sleep(3)


# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------


def download_bridge_artifact(config: Config, request_id: str, local_path: Path) -> None:
    """Download the bridge-action artifact zip from the ``logs`` branch."""
    repo = _get_active_repo(config)
    client = create_forge_client(config)

    artifact_name = f"bridge-action-{request_id}"
    raw_url = client.get_raw_file_url(repo, "logs", f"{artifact_name}.zip")

    try:
        data = client.request_bytes("GET", raw_url)
        if data and len(data) > 0:
            local_path.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(BytesIO(data)) as zf:
                zf.extractall(local_path)
            return
    except ForgeError:
        pass

    raise ForgeError(f"Artifact not found: {artifact_name}")


def fetch_bridge_output_log(config: Config, request_id: str) -> Optional[str]:
    """Fetch ``output.log`` from a bridge-action artifact on the ``logs`` branch."""
    repo = _get_active_repo(config)
    client = create_forge_client(config)

    artifact_name = f"bridge-action-{request_id}"
    raw_url = client.get_raw_file_url(repo, "logs", f"{artifact_name}.zip")

    try:
        data = client.request_bytes("GET", raw_url)
        if data and len(data) > 0:
            with zipfile.ZipFile(BytesIO(data)) as zf:
                for member in zf.infolist():
                    if member.filename == "output.log" or member.filename.endswith("/output.log"):
                        with zf.open(member) as f:
                            return f.read().decode("utf-8", errors="replace")
    except ForgeError:
        pass

    return None


__all__ = [
    # Errors
    "ForgeAuthError",
    "ForgeError",
    # Config accessors
    "_sanitize_token",
    "_get_active_repo",
    "_get_active_token",
    "_get_active_server",
    "_get_active_workflow_file",
    # Client
    "ForgeClient",
    "GitHubClient",
    "create_forge_client",
    # Workflow-run parsing helpers
    "_extract_total_count",
    "_parse_event_inputs",
    "_matches_inputs",
    "_find_run_by_inputs",
    # Workflows
    "trigger_workflow_dispatch",
    "trigger_bridge_action_workflow",
    "get_workflow_runs",
    "get_workflow_run",
    "wait_for_workflow_completion",
    "wait_for_bridge_action_completion",
    # Artifacts
    "download_bridge_artifact",
    "fetch_bridge_output_log",
]
