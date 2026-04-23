"""Discovery mode: probe workspaces, projects, compute groups, and shared paths."""

from __future__ import annotations

import concurrent.futures
from copy import deepcopy
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple

import click

from inspire.config import (
    CONFIG_FILENAME,
    PROJECT_CONFIG_DIR,
    Config,
)

from .env_detect import _redact_token_like_text
from .toml_helpers import _toml_dumps

from inspire.platform.web.browser_api.core import _set_base_url
from inspire.platform.web.browser_api.notebooks import NotebookFailedError

_CATALOG_DROP_FIELDS = frozenset(
    {
        "id",
        "alias",
        "workspace_id",
        "probed_at",
        "probe_notebook_id",
        "probe_error",
    }
)

_LEGACY_WORKSPACE_ALIASES = frozenset({"cpu", "gpu", "internet", "hpc", "whole_node"})


class _ProbeDefaults(NamedTuple):
    ssh_runtime: object
    ssh_public_key: str
    probe_workspace_id: str
    logic_compute_group_id: str
    quota_id: str
    cpu_count: int
    memory_size: int
    selected_image: object
    task_priority: int
    shm_size: int


@dataclass(frozen=True)
class _DiscoveryPersistRequest:
    force: bool
    config: Config
    browser_api_module: object
    session: object
    account_key: str
    workspace_id: str
    projects: list[object]
    selected_project: object
    probe_shared_path: bool
    probe_limit: int
    probe_keep_notebooks: bool
    probe_pubkey: str | None
    probe_timeout: int
    prompted_credentials: tuple[str, str, str] | None
    cli_target_dir: str | None


def _slugify_alias(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text


def _make_unique_alias(alias: str, used: set[str]) -> str:
    base = alias
    counter = 2
    while alias in used:
        alias = f"{base}-{counter}"
        counter += 1
    used.add(alias)
    return alias


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _extract_global_user_dir(path: str, *, account_key: str | None) -> str | None:
    path = str(path or "").strip()
    if not path:
        return None

    if account_key:
        marker = f"/global_user/{account_key}"
        idx = path.find(marker)
        if idx != -1:
            return path[: idx + len(marker)]

    match = re.search(r"(/.*?/global_user/[^/]+)", path)
    if match:
        return match.group(1)
    return None


def _derive_shared_path_group(path: str, *, account_key: str | None) -> str | None:
    path = str(path or "").strip()
    if not path:
        return None

    global_user_dir = _extract_global_user_dir(path, account_key=account_key)
    if global_user_dir:
        if "/project/" in global_user_dir:
            root_match = re.search(r"(?P<root>/.*?)/project/", global_user_dir)
            if root_match and "/global_user/" in global_user_dir:
                root = root_match.group("root").rstrip("/")
                user_dir = global_user_dir.split("/global_user/", 1)[1].split("/", 1)[0].strip()
                if user_dir:
                    return f"{root}/global_user/{user_dir}"
        return global_user_dir

    # Heuristic: many workdir paths are under a per-volume project root like:
    #   /inspire/hdd/project/.../<user-dir>
    # The shared filesystem root for the account is typically:
    #   /inspire/hdd/global_user/<user-dir>
    # When we can infer the user directory from the workdir, prefer grouping by
    # the derived global_user path so fallback project selection doesn't cross
    # volume boundaries.
    if "/project/" in path:
        root_match = re.search(r"(?P<root>/.*?)/project/", path)
        if root_match:
            root = root_match.group("root").rstrip("/")

            user_dir = ""
            segments = [seg for seg in path.split("/") if seg]
            if account_key:
                for seg in reversed(segments):
                    if account_key in seg:
                        user_dir = seg
                        break

            if not user_dir:
                remainder_match = re.search(r"/project/[^/]+(?P<rest>/.*)?$", path)
                rest = remainder_match.group("rest") if remainder_match else ""
                rest_segments = [seg for seg in (rest or "").split("/") if seg]
                if rest_segments:
                    if rest_segments[0] == "global_user" and len(rest_segments) >= 2:
                        user_dir = rest_segments[1]
                    else:
                        user_dir = rest_segments[0]

            if user_dir:
                return f"{root}/global_user/{user_dir}"

    match = re.search(r"(/.*?/project/[^/]+)", path)
    if match:
        return match.group(1)

    return None


def _load_ssh_public_key(pubkey_path: str | None) -> str:
    candidates: list[Path]

    if pubkey_path:
        candidates = [Path(pubkey_path).expanduser()]
    else:
        candidates = [
            Path.home() / ".ssh" / "id_ed25519.pub",
            Path.home() / ".ssh" / "id_rsa.pub",
        ]

    for path in candidates:
        if path.exists():
            key = path.read_text(encoding="utf-8", errors="ignore").strip()
            if key:
                return key

    raise ValueError(
        "No SSH public key found. Provide --pubkey PATH or generate one with 'ssh-keygen'."
    )


def _select_probe_cpu_compute_group_id(compute_groups: list[dict[str, Any]]) -> str | None:
    gpu_name_tokens = (
        "H200",
        "H100",
        "A100",
        "A800",
        "H800",
        "4090",
        "A6000",
        "V100",
        "T4",
        "L40",
        "RTX",
        "GPU",
    )

    def _group_id(group: dict[str, Any]) -> str:
        return str(group.get("logic_compute_group_id") or group.get("id") or "").strip()

    def _looks_gpu_like(group: dict[str, Any]) -> bool:
        if group.get("gpu_type_stats"):
            return True
        gpu_type = str(group.get("gpu_type") or "").strip()
        if gpu_type:
            return True
        name = str(group.get("name") or "").upper()
        return any(token in name for token in gpu_name_tokens)

    # Strong preference: groups explicitly labeled as CPU.
    for group in compute_groups:
        if not isinstance(group, dict):
            continue
        if _looks_gpu_like(group):
            continue
        name = str(group.get("name") or "").upper()
        if "CPU" not in name:
            continue
        group_id = _group_id(group)
        if group_id:
            return group_id

    # Next: any group that does not look GPU-like.
    for group in compute_groups:
        if not isinstance(group, dict):
            continue
        if _looks_gpu_like(group):
            continue
        group_id = _group_id(group)
        if group_id:
            return group_id

    # Last resort: pick the first group we can.
    for group in compute_groups:
        if not isinstance(group, dict):
            continue
        group_id = _group_id(group)
        if group_id:
            return group_id

    return None


def _select_probe_cpu_quota(schedule: dict[str, Any]) -> tuple[str, int, int]:
    quota_list: Any = schedule.get("quota", [])
    if isinstance(quota_list, str):
        quota_list = json.loads(quota_list) if quota_list else []
    if not isinstance(quota_list, list):
        quota_list = []

    cpu_quotas = [q for q in quota_list if isinstance(q, dict) and q.get("gpu_count", 0) == 0]
    selected = None
    for quota in cpu_quotas:
        cpu_count = quota.get("cpu_count")
        if cpu_count is None:
            continue
        if selected is None or cpu_count < selected.get("cpu_count", 0):
            selected = quota

    if selected is None and cpu_quotas:
        selected = cpu_quotas[0]

    quota_id = str((selected or {}).get("id") or "").strip()
    cpu_count = int((selected or {}).get("cpu_count") or 4)
    memory_size = int((selected or {}).get("memory_size") or 32)
    return quota_id, cpu_count, memory_size


def _select_probe_image(images: list[object], *, preferred: str | None = None) -> object | None:
    if not images:
        return None

    preferred_text = str(preferred or "").strip().lower()
    if preferred_text:
        for img in images:
            name = str(getattr(img, "name", "") or "").lower()
            url = str(getattr(img, "url", "") or "").lower()
            image_id = str(getattr(img, "image_id", "") or "").strip()
            if preferred_text in name or preferred_text in url or preferred == image_id:
                return img

    for img in images:
        name = str(getattr(img, "name", "") or "").lower()
        if "pytorch" in name:
            return img
    return images[0]


def _build_shared_path_probe_command(account_key: str) -> str:
    import shlex

    account = shlex.quote(account_key)
    return (
        f"INSPIRE_ACCOUNT_KEY={account} "
        'PYTHON_BIN="$(command -v python3 || command -v python)" && '
        '"$PYTHON_BIN" - <<PY\n'
        "import json\n"
        "import os\n"
        "import pathlib\n"
        "import re\n"
        "\n"
        "account = os.environ.get('INSPIRE_ACCOUNT_KEY', '').strip()\n"
        "pwd = str(pathlib.Path().resolve())\n"
        "home = os.path.expanduser('~')\n"
        "\n"
        "found = ''\n"
        "\n"
        "def pick_from_global_user(global_user_dir: pathlib.Path) -> str:\n"
        "    if not account or not global_user_dir.is_dir():\n"
        "        return ''\n"
        "    direct = global_user_dir / account\n"
        "    if direct.is_dir():\n"
        "        return str(direct)\n"
        "    try:\n"
        "        children = list(global_user_dir.iterdir())[:200]\n"
        "    except Exception:\n"
        "        return ''\n"
        "    candidates = []\n"
        "    for child in children:\n"
        "        if not child.is_dir():\n"
        "            continue\n"
        "        name = child.name\n"
        "        if name.endswith(account):\n"
        "            candidates.append(child)\n"
        "        elif account in name:\n"
        "            candidates.append(child)\n"
        "    if not candidates:\n"
        "        return ''\n"
        "    candidates.sort(key=lambda p: (not p.name.endswith(account), len(p.name)))\n"
        "    return str(candidates[0])\n"
        "\n"
        "bases = [pathlib.Path('/inspire'), pathlib.Path('/train'), pathlib.Path('/shared'), pathlib.Path('/mnt'), pathlib.Path('/data')]\n"
        "for base in bases:\n"
        "    if not base.is_dir():\n"
        "        continue\n"
        "    found = pick_from_global_user(base / 'global_user')\n"
        "    if found:\n"
        "        break\n"
        "    try:\n"
        "        for child in list(base.iterdir())[:60]:\n"
        "            if not child.is_dir():\n"
        "                continue\n"
        "            found = pick_from_global_user(child / 'global_user')\n"
        "            if found:\n"
        "                break\n"
        "        if found:\n"
        "            break\n"
        "    except Exception:\n"
        "        continue\n"
        "\n"
        "def extract(value: str) -> str:\n"
        "    if account and f'/global_user/{account}' in value:\n"
        "        head = value.split(f'/global_user/{account}', 1)[0]\n"
        "        return head + f'/global_user/{account}'\n"
        "    match = re.search(r'(/.*?/global_user/[^/]+)', value)\n"
        "    return match.group(1) if match else ''\n"
        "\n"
        "if not found:\n"
        "    for value in (pwd, home):\n"
        "        extracted = extract(value)\n"
        "        if extracted:\n"
        "            found = extracted\n"
        "            break\n"
        "\n"
        "print(json.dumps({'account': account, 'pwd': pwd, 'home': home, 'global_user_dir': found}, ensure_ascii=False))\n"
        "PY\n"
    )


def _probe_project_shared_path_group(
    *,
    browser_api_module,  # noqa: ANN001
    session,  # noqa: ANN001
    workspace_id: str,
    account_key: str,
    project_id: str,
    project_name: str,
    project_alias: str,
    ssh_public_key: str,
    ssh_runtime,  # noqa: ANN001
    logic_compute_group_id: str,
    quota_id: str,
    cpu_count: int,
    memory_size: int,
    image_id: str,
    image_url: str,
    shm_size: int,
    task_priority: int,
    keep_notebook: bool,
    timeout: int,
) -> dict[str, Any]:
    from inspire.bridge.tunnel.models import BridgeProfile, TunnelConfig
    from inspire.bridge.tunnel.ssh_exec import run_ssh_command

    result: dict[str, Any] = {
        "notebook_id": None,
        "shared_path_group": None,
        "probe_data": None,
        "probe_error": None,
    }

    timeout = max(60, int(timeout))

    notebook_id: str | None = None
    try:
        name = f"insp-probe-{project_alias}-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
        resource_spec_price = {
            "cpu_type": "",
            "cpu_count": int(cpu_count),
            "gpu_type": "",
            "gpu_count": 0,
            "memory_size_gib": int(memory_size),
            "logic_compute_group_id": logic_compute_group_id,
            "quota_id": quota_id,
        }

        created = browser_api_module.create_notebook(
            name=name,
            project_id=project_id,
            project_name=project_name,
            image_id=image_id,
            image_url=image_url,
            logic_compute_group_id=logic_compute_group_id,
            quota_id=quota_id,
            gpu_type="",
            gpu_count=0,
            cpu_count=int(cpu_count),
            memory_size=int(memory_size),
            shared_memory_size=int(shm_size),
            auto_stop=True,
            workspace_id=workspace_id,
            session=session,
            task_priority=int(task_priority),
            resource_spec_price=resource_spec_price,
        )
        notebook_id = str((created or {}).get("notebook_id") or "").strip() or None
        result["notebook_id"] = notebook_id
        if not notebook_id:
            result["probe_error"] = "Notebook create succeeded but did not return notebook_id"
            return result

        browser_api_module.wait_for_notebook_running(
            notebook_id=notebook_id,
            session=session,
            timeout=timeout,
        )

        proxy_url = browser_api_module.setup_notebook_rtunnel(
            notebook_id=notebook_id,
            ssh_public_key=ssh_public_key,
            ssh_runtime=ssh_runtime,
            session=session,
            headless=True,
            timeout=min(timeout, 600),
        )

        bridge = BridgeProfile(
            name="probe",
            proxy_url=proxy_url,
            ssh_user="root",
            ssh_port=22222,
            has_internet=True,
        )
        tunnel_config = TunnelConfig(bridges={"probe": bridge}, default_bridge="probe")

        command = _build_shared_path_probe_command(account_key=account_key)

        last_error: str | None = None
        completed = None
        deadline = time.monotonic() + timeout
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            remaining = max(0.0, deadline - time.monotonic())
            per_attempt_timeout = max(10, min(60, int(remaining) if remaining else 10))

            try:
                completed = run_ssh_command(
                    command,
                    config=tunnel_config,
                    timeout=per_attempt_timeout,
                    capture_output=True,
                    check=False,
                    quiet_proxy=True,
                )
                if completed.returncode == 0:
                    break
                last_error = (completed.stderr or "").strip() or (completed.stdout or "").strip()
            except Exception as e:
                last_error = _redact_token_like_text(str(e))

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            pause = min(20.0, 2.0 + (attempt * 1.5))
            time.sleep(min(pause, remaining))

        if completed is None or completed.returncode != 0:
            summary = (last_error or "SSH probe failed").strip()
            result["probe_error"] = _redact_token_like_text(summary)[:2000]
            return result

        stdout = completed.stdout or ""
        probe_data = None
        for line in reversed([ln.strip() for ln in stdout.splitlines() if ln.strip()]):
            if not (line.startswith("{") and line.endswith("}")):
                continue
            try:
                probe_data = json.loads(line)
                break
            except Exception:
                continue

        result["probe_data"] = probe_data
        if isinstance(probe_data, dict):
            global_user_dir = str(probe_data.get("global_user_dir") or "").strip()
            if global_user_dir:
                result["shared_path_group"] = global_user_dir
        return result
    except NotebookFailedError as e:
        result["probe_error"] = f"Notebook failed: {e.status}"
        if e.events:
            result["probe_error"] += f" - {e.events}"
        return result
    except Exception as e:  # pragma: no cover - network/runtime dependent
        result["probe_error"] = _redact_token_like_text(str(e))
        return result
    finally:
        if notebook_id and not keep_notebook:
            try:
                browser_api_module.stop_notebook(notebook_id=notebook_id, session=session)
            except Exception:
                pass


def _discover_workspace_aliases() -> dict[str, str]:
    """Collect workspace alias overrides from environment variables."""
    env_cpu = (os.getenv("INSPIRE_WORKSPACE_CPU_ID") or "").strip()
    env_gpu = (os.getenv("INSPIRE_WORKSPACE_GPU_ID") or "").strip()
    env_internet = (os.getenv("INSPIRE_WORKSPACE_INTERNET_ID") or "").strip()

    overrides: dict[str, str] = {}
    if env_cpu:
        overrides["cpu"] = env_cpu
    if env_gpu:
        overrides["gpu"] = env_gpu
    if env_internet:
        overrides["internet"] = env_internet
    return overrides


def _ensure_playwright_browser() -> None:
    """Check that the Playwright Chromium browser is installed; offer to install it."""
    import subprocess
    import sys

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        return  # already installed
    except Exception:
        pass

    click.echo()
    click.echo(
        "Playwright Chromium browser is required for SSO authentication "
        "(one-time ~150 MB download)."
    )
    if not click.confirm("Install Chromium now?", default=True):
        click.echo("Cannot proceed without a browser for SSO login.")
        raise SystemExit(1)

    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=False,
    )
    if result.returncode != 0:
        click.echo(click.style("Chromium installation failed.", fg="red"))
        raise SystemExit(1)


def _resolve_credentials_interactive(
    config: object,
    *,
    cli_username: str | None,
    cli_base_url: str | None,
    allow_config_password: bool = False,
) -> tuple[str, str, str]:
    """Resolve base_url, username, and password, prompting when missing."""
    placeholder = "https://api.example.com"

    # --- base_url ---
    base_url = (cli_base_url or "").strip()
    if not base_url:
        cfg_base_url = str(getattr(config, "base_url", "") or "").strip()
        if cfg_base_url and cfg_base_url != placeholder:
            base_url = cfg_base_url
    if not base_url:
        base_url = click.prompt("Platform URL", type=str).strip()
    if not base_url:
        click.echo(click.style("Platform URL is required.", fg="red"))
        raise SystemExit(1)

    # --- username ---
    username = (cli_username or "").strip()
    if not username:
        cfg_username = str(getattr(config, "username", "") or "").strip()
        if cfg_username:
            username = cfg_username
    if not username:
        username = click.prompt("Username", type=str).strip()
    if not username:
        click.echo(click.style("Username is required.", fg="red"))
        raise SystemExit(1)

    # --- password ---
    # When the caller explicitly provided credentials (allow_config_password=True),
    # the config/env password is likely valid — use it to support non-interactive
    # --force mode.  In the session-failed fallback path the old password may be
    # stale, so always prompt for a fresh one.
    password = ""
    if allow_config_password:
        password = str(getattr(config, "password", "") or "").strip()
    if not password:
        password = click.prompt("Password", type=str, hide_input=True)
    if not password:
        click.echo(click.style("Password is required.", fg="red"))
        raise SystemExit(1)

    return username, password, base_url


def _ensure_ssh_key() -> None:
    """Check for an SSH key; offer to generate one if missing."""
    import subprocess

    ssh_dir = Path.home() / ".ssh"
    candidates = [ssh_dir / "id_ed25519.pub", ssh_dir / "id_rsa.pub"]
    if any(p.exists() for p in candidates):
        return

    click.echo()
    click.echo("No SSH key found. SSH keys are needed for bridge/tunnel/notebook SSH features.")

    # Non-interactive contexts (CI, tests) must not block on prompts or fail on EOF.
    stdin = click.get_text_stream("stdin")
    if not getattr(stdin, "isatty", lambda: False)():
        click.echo("Skipping SSH key generation in non-interactive mode.")
        return

    if not click.confirm("Generate a new ed25519 SSH key?", default=True):
        return

    ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_path = ssh_dir / "id_ed25519"
    result = subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(key_path), "-N", "", "-C", "inspire-skill"],
        capture_output=True,
    )
    if result.returncode == 0:
        click.echo(f"SSH key generated: {key_path}")
    else:
        click.echo(click.style("SSH key generation failed.", fg="yellow"))


def _merge_alias_map(
    *,
    existing: dict[str, str],
    discovered: dict[str, str],
) -> dict[str, str]:
    merged = dict(existing)
    existing_ids = {v for v in existing.values() if isinstance(v, str) and v}
    used_aliases = set(existing.keys())

    alias_for_id: dict[str, str] = {}
    for alias, project_id in existing.items():
        if isinstance(project_id, str) and project_id and project_id not in alias_for_id:
            alias_for_id[project_id] = alias

    for alias, project_id in discovered.items():
        if not isinstance(project_id, str) or not project_id:
            continue
        if project_id in existing_ids:
            continue
        candidate = alias
        if not candidate:
            candidate = project_id
        candidate = _make_unique_alias(candidate, used_aliases)
        merged[candidate] = project_id

    return merged


def _build_project_aliases(
    projects: list[object],
    *,
    existing: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Build the ``[projects]`` table keyed by the platform's real project name.

    Keys are the project names returned by the platform (``"CI-情境智能"`` etc.),
    not short slugs. Agents that read ``inspire config context --json`` see
    meaningful identifiers, not random 2-letter aliases.
    """
    existing_map = existing or {}
    alias_for_id: dict[str, str] = {}
    for alias, project_id in existing_map.items():
        if isinstance(project_id, str) and project_id and project_id not in alias_for_id:
            alias_for_id[project_id] = alias

    discovered_map: dict[str, str] = {}
    discovered_alias_for_id: dict[str, str] = {}

    for project in projects:
        project_id = str(getattr(project, "project_id", "") or "").strip()
        name = str(getattr(project, "name", "") or "").strip()
        if not project_id:
            continue
        if project_id in alias_for_id:
            discovered_alias_for_id[project_id] = alias_for_id[project_id]
            continue

        # Use the platform name directly — no slugify / no short alias.
        key = name or f"project-{project_id.split('-')[-1][:8]}"
        discovered_map[key] = project_id
        discovered_alias_for_id[project_id] = key

    merged = _merge_alias_map(existing=existing_map, discovered=discovered_map)
    discovered_alias_for_id.update(
        {v: k for k, v in merged.items() if v not in discovered_alias_for_id}
    )
    return merged, discovered_alias_for_id


def _merge_compute_groups(
    existing: list[dict[str, Any]] | None,
    discovered: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for item in existing or []:
        if not isinstance(item, dict):
            continue
        group_id = str(item.get("id") or "").strip()
        if not group_id:
            continue
        by_id[group_id] = dict(item)

    for item in discovered:
        if not isinstance(item, dict):
            continue
        group_id = str(item.get("id") or "").strip()
        if not group_id:
            continue
        merged = dict(by_id.get(group_id, {}))
        existing_ws = set(merged.get("workspace_ids") or [])
        new_ws = set(item.get("workspace_ids") or [])
        merged.update({k: v for k, v in item.items() if v is not None and v != ""})
        combined = sorted(existing_ws | new_ws)
        if combined:
            merged["workspace_ids"] = combined
        by_id[group_id] = merged

    merged_list = list(by_id.values())
    for entry in merged_list:
        for k in [k for k, v in entry.items() if v == ""]:
            del entry[k]
    merged_list.sort(
        key=lambda entry: (str(entry.get("gpu_type") or ""), str(entry.get("name") or "").lower())
    )
    return merged_list


def _resolve_discover_runtime(
    *,
    config: Config,
    web_session_module,  # noqa: ANN001
    default_workspace_id: str,
    cli_username: str | None,
    cli_base_url: str | None,
) -> tuple[object, tuple[str, str, str] | None, str, str]:
    # When the caller explicitly provides credentials via CLI flags, skip the
    # cached-session fast path so we honour the override instead of silently
    # using a session that belongs to a different user / base-url.
    session = None
    prompted_credentials: tuple[str, str, str] | None = None
    if cli_username or cli_base_url:
        _ensure_playwright_browser()
        username, password, base_url = _resolve_credentials_interactive(
            config,
            cli_username=cli_username,
            cli_base_url=cli_base_url,
            allow_config_password=True,
        )
        prompted_credentials = (username, password, base_url)
        click.echo("Logging in...")
        session = web_session_module.login_with_playwright(
            username,
            password,
            base_url=base_url,
        )
        click.echo("Logged in.")
    else:
        try:
            session = web_session_module.get_web_session(require_workspace=True)
        except (ValueError, RuntimeError):
            _ensure_playwright_browser()
            username, password, base_url = _resolve_credentials_interactive(
                config,
                cli_username=cli_username,
                cli_base_url=cli_base_url,
            )
            prompted_credentials = (username, password, base_url)
            click.echo("Logging in...")
            session = web_session_module.login_with_playwright(
                username,
                password,
                base_url=base_url,
            )
            click.echo("Logged in.")

    if prompted_credentials:
        account_key = prompted_credentials[0]
    else:
        account_key = (config.username or session.login_username or "").strip()
    if not account_key:
        click.echo(click.style("Could not resolve account key (username)", fg="red"))
        raise SystemExit(1)

    placeholder = "https://api.example.com"
    if prompted_credentials:
        _set_base_url(prompted_credentials[2])
    else:
        cfg_base_url = str(getattr(config, "base_url", "") or "").strip()
        if cfg_base_url and cfg_base_url != placeholder:
            _set_base_url(cfg_base_url)
        elif session.base_url:
            _set_base_url(session.base_url)

    workspace_id = str(session.workspace_id or "").strip()
    if not workspace_id or workspace_id == default_workspace_id:
        click.echo(
            click.style(
                "Could not detect a real workspace_id. Set INSPIRE_WORKSPACE_ID and retry.",
                fg="red",
            )
        )
        raise SystemExit(1)

    return session, prompted_credentials, account_key, workspace_id


def _candidate_workspace_ids_for_discovery(
    *,
    session,  # noqa: ANN001
    workspace_id: str,
) -> list[str]:
    """Return deduplicated workspace IDs to query during discovery."""
    candidates: list[str] = [workspace_id]
    candidates.extend(str(ws or "").strip() for ws in (session.all_workspace_ids or []))

    # Best-effort augmentation for stale/partial session metadata.
    try:
        from inspire.platform.web.browser_api.workspaces import try_enumerate_workspaces

        for ws in try_enumerate_workspaces(session, workspace_id=workspace_id):
            ws_id = str(ws.get("id") or "").strip()
            if ws_id:
                candidates.append(ws_id)
    except Exception:
        pass

    ordered_unique: list[str] = []
    seen: set[str] = set()
    for raw_ws in candidates:
        ws = str(raw_ws or "").strip()
        if not ws or ws in seen:
            continue
        seen.add(ws)
        ordered_unique.append(ws)
    return ordered_unique


def _collect_discovery_projects(
    *,
    browser_api_module,  # noqa: ANN001
    session,  # noqa: ANN001
    workspace_id: str,
) -> tuple[list[object], list[tuple[str, str]]]:
    """Collect projects across discovered workspaces (best-effort per workspace)."""
    workspace_ids = _candidate_workspace_ids_for_discovery(
        session=session,
        workspace_id=workspace_id,
    )

    discovered: list[object] = []
    errors: list[tuple[str, str]] = []
    seen_project_ids: set[str] = set()

    for ws_id in workspace_ids:
        try:
            ws_projects = browser_api_module.list_projects(workspace_id=ws_id, session=session)
        except Exception as exc:  # pragma: no cover - network/runtime dependent
            errors.append((ws_id, str(exc)))
            continue

        for project in ws_projects:
            project_id = str(getattr(project, "project_id", "") or "").strip()
            if not project_id:
                continue
            if project_id in seen_project_ids:
                continue
            seen_project_ids.add(project_id)
            discovered.append(project)

    return discovered, errors


def _load_projects_for_discovery(
    *,
    browser_api_module,  # noqa: ANN001
    session,  # noqa: ANN001
    workspace_id: str,
    force: bool,
    probe_shared_path: bool,
    probe_limit: int,
    requested_project: str | None = None,
) -> tuple[list[object], object]:
    projects, workspace_errors = _collect_discovery_projects(
        browser_api_module=browser_api_module,
        session=session,
        workspace_id=workspace_id,
    )

    if not projects:
        if workspace_errors:
            sample = ", ".join(f"{ws}: {msg}" for ws, msg in workspace_errors[:3])
            if len(workspace_errors) > 3:
                sample += ", ..."
            click.echo(
                click.style(
                    f"Failed to list projects across discovered workspaces "
                    f"({len(workspace_errors)} failed: {sample})",
                    fg="red",
                )
            )
        else:
            click.echo(click.style("No projects found for discovered workspaces", fg="red"))
        raise SystemExit(1)

    if workspace_errors and not force:
        sample = ", ".join(f"{ws}: {msg}" for ws, msg in workspace_errors[:3])
        if len(workspace_errors) > 3:
            sample += ", ..."
        click.echo(
            click.style(
                f"Warning: some workspaces failed during project discovery "
                f"({len(workspace_errors)}): {sample}",
                fg="yellow",
            )
        )

    if probe_shared_path and probe_limit < 0:
        click.echo(click.style("Invalid --probe-limit (must be >= 0)", fg="red"))
        raise SystemExit(1)

    # Explicit `--select-project <name|id>` takes precedence over every
    # heuristic and skips the interactive prompt entirely. Matches case-insensitively
    # on name, exactly on project_id.
    if requested_project:
        rq = requested_project.strip()
        match = None
        for project in projects:
            if project.name.lower() == rq.lower() or project.project_id == rq:
                match = project
                break
        if not match:
            available = ", ".join(
                f"{p.name} ({p.project_id})" for p in projects if p.name
            )
            click.echo(
                click.style(
                    f"--select-project {rq!r} not found. Candidates: {available}",
                    fg="red",
                )
            )
            raise SystemExit(1)
        return projects, match

    # Best platform-side guess, used only as a hint / single-project shortcut.
    # NEVER used as silent default when multiple projects exist.
    try:
        heuristic_pick, _ = browser_api_module.select_project(projects)
    except Exception:
        heuristic_pick = projects[0]

    if force:
        return projects, heuristic_pick

    click.echo()
    click.echo(click.style("Projects:", bold=True))
    for idx, project in enumerate(projects, start=1):
        suffix = project.get_quota_status() if hasattr(project, "get_quota_status") else ""
        click.echo(f"  {idx}. {project.name} ({project.project_id}){suffix}")

    if len(projects) == 1:
        # Single project — unambiguous, keep the zero-friction default.
        choice = click.prompt(
            "Select default project",
            type=int,
            default=1,
            show_default=True,
        )
    else:
        # Multi-project case: the platform heuristic (budget / priority /
        # alphabetical) has nothing to do with the current repo, so never
        # let Enter accept it. Force the user to pick a number explicitly.
        click.echo(
            click.style(
                "Multiple projects available — there is no repo-aware default. "
                "Pick the one your current work belongs to.",
                fg="yellow",
            )
        )
        hint_idx = next(
            (i for i, p in enumerate(projects, start=1)
             if p.project_id == heuristic_pick.project_id),
            1,
        )
        click.echo(
            click.style(
                f"(Platform heuristic suggests #{hint_idx} {heuristic_pick.name} — "
                "based on budget / priority only, not on your repo.)",
                fg="yellow",
            )
        )
        choice = click.prompt(
            f"Select default project (1-{len(projects)})",
            type=click.IntRange(1, len(projects)),
        )

    return projects, projects[choice - 1]


def _confirm_discovery_writes(*, force: bool, global_path: Path, project_path: Path) -> bool:
    if global_path.exists() and not force:
        click.echo()
        click.echo(click.style(f"Global config already exists: {global_path}", fg="yellow"))
        if not click.confirm(
            "Update it with discovered catalogs? (will rewrite file)", default=True
        ):
            click.echo("Aborted.")
            return False

    if project_path.exists() and not force:
        click.echo()
        click.echo(click.style(f"Project config already exists: {project_path}", fg="yellow"))
        if not click.confirm(
            "Update it with discovered context/defaults? (will rewrite file)", default=True
        ):
            click.echo("Aborted.")
            return False
    return True


def _load_discovery_global_state(
    *,
    global_path: Path,
    account_key: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    global_data: dict[str, Any] = {}
    if global_path.exists():
        global_data = Config._load_toml(global_path)

    accounts = global_data.setdefault("accounts", {})
    if not isinstance(accounts, dict):
        accounts = {}
        global_data["accounts"] = accounts

    account_section = accounts.get(account_key)
    if not isinstance(account_section, dict):
        account_section = {}
        accounts[account_key] = account_section

    return global_data, account_section


def _load_discovery_project_state(
    *,
    project_path: Path,
    account_key: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    project_data: dict[str, Any] = {}
    if project_path.exists():
        project_data = Config._load_toml(project_path)

    accounts = project_data.get("accounts")
    if not isinstance(accounts, dict):
        accounts = {}
        project_data["accounts"] = accounts

    account_section = accounts.get(account_key)
    if not isinstance(account_section, dict):
        account_section = {}
        accounts[account_key] = account_section

    return project_data, account_section


def _seed_project_discovery_metadata(
    *,
    project_data: dict[str, Any],
    project_account_section: dict[str, Any],
    global_data: dict[str, Any],
    global_account_section: dict[str, Any],
) -> None:
    if not isinstance(project_data.get("projects"), dict):
        global_projects = global_account_section.get("projects")
        if isinstance(global_projects, dict) and global_projects:
            project_data["projects"] = dict(global_projects)

    if not isinstance(project_data.get("workspaces"), dict):
        global_workspaces = global_data.get("workspaces")
        if not isinstance(global_workspaces, dict):
            global_workspaces = global_account_section.get("workspaces")
        if isinstance(global_workspaces, dict) and global_workspaces:
            project_data["workspaces"] = dict(global_workspaces)

    if not isinstance(project_data.get("compute_groups"), list):
        global_compute_groups = global_data.get("compute_groups")
        if not isinstance(global_compute_groups, list):
            global_compute_groups = global_account_section.get("compute_groups")
        if isinstance(global_compute_groups, list) and global_compute_groups:
            project_data["compute_groups"] = deepcopy(global_compute_groups)

    if not isinstance(project_account_section.get("project_catalog"), dict):
        global_catalog = global_account_section.get("project_catalog")
        if isinstance(global_catalog, dict) and global_catalog:
            project_account_section["project_catalog"] = deepcopy(global_catalog)

    for key in ("shared_path_group", "train_job_workdir"):
        if str(project_account_section.get(key) or "").strip():
            continue
        value = str(global_account_section.get(key) or "").strip()
        if value:
            project_account_section[key] = value


def _resolve_project_catalog_aliases(
    *,
    project_data: dict[str, Any],
    project_account_section: dict[str, Any],
    projects: list[object],
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    existing_projects = project_data.get("projects")
    if not isinstance(existing_projects, dict):
        existing_projects = project_account_section.get("projects")
    if not isinstance(existing_projects, dict):
        existing_projects = {}
    merged_projects, alias_for_id = _build_project_aliases(projects, existing=existing_projects)
    project_data["projects"] = merged_projects
    project_account_section.pop("projects", None)

    project_catalog = project_account_section.get("project_catalog")
    if not isinstance(project_catalog, dict):
        project_catalog = {}
        project_account_section["project_catalog"] = project_catalog

    typed_catalog: dict[str, dict[str, Any]] = {}
    for project_id, entry in project_catalog.items():
        if not isinstance(project_id, str):
            continue
        if isinstance(entry, dict):
            typed_catalog[project_id] = entry
        else:
            typed_catalog[project_id] = {}

    project_account_section["project_catalog"] = typed_catalog
    return alias_for_id, typed_catalog


def _populate_project_catalog(
    *,
    project_catalog: dict[str, dict[str, Any]],
    projects: list[object],
    browser_api_module,  # noqa: ANN001
    session,  # noqa: ANN001
    workspace_id: str,
    account_key: str,
    force: bool,
) -> None:
    """Populate per-project metadata kept at account level.

    Only two fields survive:

    * ``name``  — the platform's display name (for reference; redundant with
      the ``[projects]`` key but useful if a project gets renamed).
    * ``path``  — the ``<topic>`` segment of the shared-storage path
      (``/inspire/<tier>/project/<topic>/<user>/...``). Derived from the
      platform's reported train_job workdir; agents need it to construct
      remote paths for new repos under this project.

    Notably *not* stored: full ``workdir`` or ``shared_path_group`` — those
    are derivable from ``path`` + the storage tier + the user, and caching
    them made the account config noisy.
    """
    for project in projects:
        project_id = str(getattr(project, "project_id", "") or "").strip()
        if not project_id:
            continue

        entry = project_catalog.setdefault(project_id, {})
        name = str(getattr(project, "name", "") or "").strip()
        if name:
            entry["name"] = name

        project_workspace_id = str(getattr(project, "workspace_id", "") or workspace_id).strip()
        existing_path = str(entry.get("path") or "").strip()
        if existing_path and not force:
            continue

        try:
            workdir = (
                browser_api_module.get_train_job_workdir(
                    project_id=project_id,
                    workspace_id=project_workspace_id,
                    session=session,
                )
                or ""
            ).strip()
        except Exception:
            workdir = ""

        if not workdir:
            continue

        # Parse the <topic> segment: /inspire/<tier>/project/<topic>/...
        parts = [p for p in workdir.split("/") if p]
        try:
            idx = parts.index("project")
            if idx + 1 < len(parts):
                entry["path"] = parts[idx + 1]
        except ValueError:
            pass


def _update_account_shared_path_group(
    *,
    account_section: dict[str, Any],
    project_catalog: dict[str, dict[str, Any]],
    force: bool,
) -> None:
    global_user_groups: set[str] = set()
    for entry in project_catalog.values():
        shared_group = str(entry.get("shared_path_group") or "").strip()
        if shared_group and "/global_user/" in shared_group:
            global_user_groups.add(shared_group)

    if len(global_user_groups) != 1:
        return

    shared_path_group = next(iter(global_user_groups))
    if force or not str(account_section.get("shared_path_group") or "").strip():
        account_section["shared_path_group"] = shared_path_group


def _print_shared_path_group_summary(
    *,
    projects: list[object],
    project_catalog: dict[str, dict[str, Any]],
    alias_for_id: dict[str, str],
) -> None:
    shared_group_to_aliases: dict[str, list[str]] = {}
    for project in projects:
        project_id = str(getattr(project, "project_id", "") or "").strip()
        if not project_id:
            continue
        alias = str(alias_for_id.get(project_id) or "").strip()
        if not alias:
            alias = _slugify_alias(str(getattr(project, "name", "") or "").strip()) or project_id

        entry = project_catalog.get(project_id) or {}
        shared_group = str(entry.get("shared_path_group") or "").strip()
        shared_group_to_aliases.setdefault(shared_group or "<unknown>", []).append(alias)

    click.echo()
    if len(shared_group_to_aliases) == 1 and "<unknown>" not in shared_group_to_aliases:
        group = next(iter(shared_group_to_aliases))
        click.echo(click.style("Shared path group:", bold=True) + f" {group}")
        return

    click.echo(click.style("Shared path groups:", bold=True))
    for group, aliases in sorted(
        shared_group_to_aliases.items(),
        key=lambda item: (item[0] == "<unknown>", item[0]),
    ):
        click.echo(f"  - {group} ({len(aliases)} project(s))")
        sample = ", ".join(sorted(aliases)[:8])
        if sample:
            suffix = " ..." if len(aliases) > 8 else ""
            click.echo(f"      {sample}{suffix}")
    if "<unknown>" in shared_group_to_aliases:
        click.echo("  Hint: run with --probe-shared-path to populate unknown shared-path groups.")


def _get_existing_workspace_aliases(
    *,
    project_data: dict[str, Any],
    project_account_section: dict[str, Any],
    global_data: dict[str, Any],
    global_account_section: dict[str, Any],
) -> dict[str, str]:
    existing_workspaces = project_data.get("workspaces")
    if not isinstance(existing_workspaces, dict):
        existing_workspaces = project_account_section.get("workspaces")
    if not isinstance(existing_workspaces, dict):
        existing_workspaces = global_data.get("workspaces")
    if not isinstance(existing_workspaces, dict):
        existing_workspaces = global_account_section.get("workspaces")
    if not isinstance(existing_workspaces, dict):
        return {}
    return dict(existing_workspaces)


def _is_legacy_workspace_alias(value: str) -> bool:
    return str(value or "").strip().lower() in _LEGACY_WORKSPACE_ALIASES


def _workspace_name_map(
    workspaces: dict[str, str],
    *,
    include_legacy: bool = False,
) -> tuple[list[str], dict[str, str]]:
    workspace_ids: list[str] = []
    workspace_names: dict[str, str] = {}
    for raw_name, raw_workspace_id in workspaces.items():
        name = str(raw_name or "").strip()
        workspace_id = str(raw_workspace_id or "").strip()
        if not name or not workspace_id:
            continue
        if not include_legacy and _is_legacy_workspace_alias(name):
            continue
        if workspace_id not in workspace_names:
            workspace_ids.append(workspace_id)
            workspace_names[workspace_id] = name
    return workspace_ids, workspace_names


def _workspace_role_score(name: str, alias: str) -> int:
    low = name.lower()
    if alias == "cpu":
        return 100 if "cpu" in low else 0
    if alias == "internet":
        if "上网" in name:
            return 100
        if "internet" in low:
            return 90
        if "net" in low:
            return 80
        return 0
    if alias == "gpu":
        if "cpu" in low or "上网" in name or "internet" in low:
            return 0
        if "分布式" in name or "训练" in name:
            return 120
        if "gpu" in low:
            return 110
        if "高性能" in name:
            return 100
        if "整节点" in name or "whole" in low or "node" in low:
            return 90
        if any(kw in low for kw in ("h100", "h200", "a100", "4090")):
            return 80
    return 0


def _real_gpu_workspace_ids(compute_groups: list[dict[str, Any]] | None) -> set[str]:
    workspace_ids: set[str] = set()
    for cg in compute_groups or []:
        gpu_type = str(cg.get("gpu_type") or "").strip()
        if not gpu_type or gpu_type.upper() == "CPU":
            continue
        for raw_workspace_id in cg.get("workspace_ids") or []:
            workspace_id = str(raw_workspace_id or "").strip()
            if workspace_id:
                workspace_ids.add(workspace_id)
    return workspace_ids


def _guess_workspace_id_from_map(
    *,
    workspaces: dict[str, str],
    alias: str,
) -> str | None:
    workspace_ids, workspace_names = _workspace_name_map(workspaces)
    guess_id = _guess_workspace_alias(alias, workspace_ids, workspace_names)
    if guess_id:
        return guess_id

    for raw_name, raw_workspace_id in workspaces.items():
        name = str(raw_name or "").strip()
        workspace_id = str(raw_workspace_id or "").strip()
        if name.lower() == alias and workspace_id:
            return workspace_id
    return None


def _find_workspace_key_for_context(
    *,
    workspaces: dict[str, str],
    alias: str,
    compute_groups: list[dict[str, Any]] | None = None,
) -> str | None:
    gpu_workspace_ids = _real_gpu_workspace_ids(compute_groups) if alias == "gpu" else set()
    best_name: str | None = None
    best_score = 0
    gpu_fallback_name: str | None = None
    for raw_name, raw_workspace_id in workspaces.items():
        name = str(raw_name or "").strip()
        workspace_id = str(raw_workspace_id or "").strip()
        if not name or not workspace_id or _is_legacy_workspace_alias(name):
            continue
        if gpu_workspace_ids and workspace_id not in gpu_workspace_ids:
            continue
        if gpu_workspace_ids and gpu_fallback_name is None:
            gpu_fallback_name = name
        score = _workspace_role_score(name, alias)
        if score > best_score:
            best_name = name
            best_score = score
    if best_name:
        return best_name
    if gpu_fallback_name:
        return gpu_fallback_name

    guess_id = _guess_workspace_id_from_map(workspaces=workspaces, alias=alias)
    if guess_id:
        for raw_name, raw_workspace_id in workspaces.items():
            name = str(raw_name or "").strip()
            workspace_id = str(raw_workspace_id or "").strip()
            if workspace_id != guess_id or not name or _is_legacy_workspace_alias(name):
                continue
            return name

    for raw_name in workspaces:
        name = str(raw_name or "").strip()
        if name.lower() == alias:
            return name

    actual_names = [
        str(raw_name or "").strip()
        for raw_name in workspaces
        if str(raw_name or "").strip()
        and not _is_legacy_workspace_alias(str(raw_name or "").strip())
    ]
    if len(actual_names) == 1:
        return actual_names[0]
    return None


def _merge_workspace_aliases(
    *,
    config: Config,
    merged_workspaces: dict[str, str],
    force: bool,
) -> dict[str, str]:
    config_workspaces = getattr(config, "workspaces", None)
    if isinstance(config_workspaces, dict):
        for raw_alias, raw_workspace_id in config_workspaces.items():
            alias = str(raw_alias or "").strip()
            workspace_value = str(raw_workspace_id or "").strip()
            if not alias or not workspace_value:
                continue
            if not _is_legacy_workspace_alias(alias):
                continue
            if force or alias not in merged_workspaces:
                merged_workspaces[alias] = workspace_value

    explicit_workspaces = {
    }
    for alias, raw_workspace_id in explicit_workspaces.items():
        workspace_value = str(raw_workspace_id or "").strip()
        if not workspace_value:
            continue
        if force or alias not in merged_workspaces:
            merged_workspaces[alias] = workspace_value

    if isinstance(config_workspaces, dict):
        for raw_alias, raw_workspace_id in config_workspaces.items():
            alias = str(raw_alias or "").strip()
            workspace_value = str(raw_workspace_id or "").strip()
            if not alias or not workspace_value:
                continue
            if _is_legacy_workspace_alias(alias):
                continue
            if force or alias not in merged_workspaces:
                merged_workspaces[alias] = workspace_value

    env_overrides = _discover_workspace_aliases()
    for alias, workspace_value in env_overrides.items():
        value = str(workspace_value or "").strip()
        if value:
            merged_workspaces[alias] = value
    return env_overrides


def _discover_workspace_options(
    *,
    session,  # noqa: ANN001
    workspace_id: str,
) -> tuple[list[str], dict[str, str]]:
    discovered_workspace_ids: list[str] = list(session.all_workspace_ids or [])
    discovered_workspace_names: dict[str, str] = dict(session.all_workspace_names or {})

    if len(discovered_workspace_ids) <= 1:
        try:
            from inspire.platform.web.browser_api.workspaces import try_enumerate_workspaces

            api_workspaces = try_enumerate_workspaces(session, workspace_id=workspace_id)
            for ws in api_workspaces:
                ws_id = str(ws.get("id") or "").strip()
                ws_name = str(ws.get("name") or "").strip()
                if ws_id and ws_id not in discovered_workspace_ids:
                    discovered_workspace_ids.append(ws_id)
                if ws_id and ws_name:
                    discovered_workspace_names.setdefault(ws_id, ws_name)
        except Exception:
            pass

    return discovered_workspace_ids, discovered_workspace_names


def _guess_workspace_alias(
    alias: str,
    discovered_workspace_ids: list[str],
    discovered_workspace_names: dict[str, str],
) -> str | None:
    """Return the best-guess workspace ID for *alias* (cpu/gpu/internet), or ``None``."""
    for ws_id in discovered_workspace_ids:
        name = (discovered_workspace_names.get(ws_id) or "").strip()
        if not name:
            continue
        low = name.lower()

        if alias == "cpu" and "cpu" in low:
            return ws_id
        if alias == "internet" and ("上网" in name or "internet" in low):
            return ws_id
        if alias == "gpu":
            gpu_hit = any(kw in low for kw in ("gpu", "h100", "h200")) or any(
                kw in name for kw in ("训练", "分布式", "高性能")
            )
            if gpu_hit and "cpu" not in low and "上网" not in name and "internet" not in low:
                return ws_id

    return None


def _prompt_workspace_aliases(
    *,
    force: bool,
    workspace_id: str,
    merged_workspaces: dict[str, str],
    env_overrides: dict[str, str],
    discovered_workspace_ids: list[str],
    discovered_workspace_names: dict[str, str],
) -> None:
    added_named_workspace = False
    for ws_id in discovered_workspace_ids:
        workspace_value = str(ws_id or "").strip()
        workspace_name = str(discovered_workspace_names.get(ws_id) or "").strip()
        if not workspace_value or not workspace_name:
            continue
        added_named_workspace = True
        if force or workspace_name not in merged_workspaces:
            merged_workspaces[workspace_name] = workspace_value

    if added_named_workspace:
        if force:
            actual_workspace_names = {
                str(raw_name or "").strip()
                for raw_name in discovered_workspace_names.values()
                if str(raw_name or "").strip()
            }
            covered_workspace_ids = {
                str(raw_workspace_id or "").strip()
                for raw_name, raw_workspace_id in merged_workspaces.items()
                if str(raw_name or "").strip() in actual_workspace_names
                and str(raw_workspace_id or "").strip()
            }
            duplicate_aliases = [
                str(raw_name or "").strip()
                for raw_name, raw_workspace_id in merged_workspaces.items()
                if str(raw_name or "").strip()
                and str(raw_name or "").strip() not in actual_workspace_names
                and str(raw_workspace_id or "").strip() in covered_workspace_ids
            ]
            for alias in duplicate_aliases:
                merged_workspaces.pop(alias, None)
        return

    # Keep the legacy fallback only when discovery could not resolve workspace names.
    for alias in ("cpu", "gpu", "internet"):
        if alias in env_overrides:
            continue
        guess = _guess_workspace_alias(alias, discovered_workspace_ids, discovered_workspace_names)
        merged_workspaces.setdefault(alias, guess or workspace_id)


def _persist_workspace_aliases(
    *,
    project_data: dict[str, Any],
    project_account_section: dict[str, Any],
    global_data: dict[str, Any],
    global_account_section: dict[str, Any],
    config: Config,
    session,  # noqa: ANN001
    workspace_id: str,
    force: bool,
) -> None:
    merged_workspaces = _get_existing_workspace_aliases(
        project_data=project_data,
        project_account_section=project_account_section,
        global_data=global_data,
        global_account_section=global_account_section,
    )
    env_overrides = _merge_workspace_aliases(
        config=config,
        merged_workspaces=merged_workspaces,
        force=force,
    )
    discovered_workspace_ids, discovered_workspace_names = _discover_workspace_options(
        session=session,
        workspace_id=workspace_id,
    )
    _prompt_workspace_aliases(
        force=force,
        workspace_id=workspace_id,
        merged_workspaces=merged_workspaces,
        env_overrides=env_overrides,
        discovered_workspace_ids=discovered_workspace_ids,
        discovered_workspace_names=discovered_workspace_names,
    )
    project_data["workspaces"] = merged_workspaces
    project_account_section.pop("workspaces", None)


def _persist_api_base_url(
    *,
    global_data: dict[str, Any],
    account_section: dict[str, Any],
    config: Config,
) -> None:
    base_url = (config.base_url or "").strip()
    if base_url and base_url != "https://api.example.com":
        api_section = global_data.get("api")
        if not isinstance(api_section, dict):
            api_section = {}
            global_data["api"] = api_section
        api_section.setdefault("base_url", base_url)
    account_section.pop("api", None)


def _discover_docker_registry(
    *,
    global_data: dict[str, Any],
    browser_api_module,  # noqa: ANN001
    session,  # noqa: ANN001
    workspace_id: str,
) -> None:
    """Auto-detect docker_registry from image URLs returned by the platform."""
    api_section = global_data.get("api")
    if isinstance(api_section, dict) and api_section.get("docker_registry"):
        return  # already set

    try:
        images = browser_api_module.list_images(
            workspace_id=workspace_id, source="SOURCE_OFFICIAL", session=session
        )
    except Exception:
        return

    for img in images:
        url = str(getattr(img, "url", "") or "").strip()
        if not url:
            continue
        # Image URLs look like "registry.host/path/image:tag" — extract hostname.
        url = url.split("://", 1)[-1]  # strip scheme if present
        host = url.split("/", 1)[0]
        if host and "." in host:
            if not isinstance(api_section, dict):
                api_section = {}
                global_data["api"] = api_section
            api_section["docker_registry"] = host
            return


def _discover_compute_groups(
    *,
    browser_api_module,  # noqa: ANN001
    session,  # noqa: ANN001
    workspace_id: str,
) -> list[dict[str, Any]]:
    compute_groups: list[dict[str, Any]] = []
    try:
        raw_groups = browser_api_module.list_compute_groups(
            workspace_id=workspace_id, session=session
        )
        gpu_types: dict[str, str] = {}
        try:
            availability = browser_api_module.get_accurate_gpu_availability(
                workspace_id=workspace_id, session=session
            )
            gpu_types = {
                str(item.group_id): str(item.gpu_type)
                for item in availability
                if getattr(item, "group_id", None)
            }
        except Exception:
            gpu_types = {}

        for group in raw_groups:
            if not isinstance(group, dict):
                continue
            group_id = str(group.get("logic_compute_group_id") or group.get("id") or "").strip()
            name = str(group.get("name") or "").strip()
            if not group_id or not name:
                continue

            location = str(
                group.get("location")
                or group.get("location_name")
                or group.get("cluster_name")
                or ""
            ).strip()
            if not location and "(" in name and name.endswith(")"):
                location = name.rsplit("(", 1)[-1].rstrip(")").strip()

            cg_entry: dict[str, Any] = {"name": name, "id": group_id}
            gpu_type = str(gpu_types.get(group_id, "") or "").strip()
            if gpu_type:
                cg_entry["gpu_type"] = gpu_type
            if location:
                cg_entry["location"] = location
            compute_groups.append(cg_entry)
    except Exception:
        return []
    return compute_groups


def _correct_workspace_aliases(
    merged_workspaces: dict[str, str],
    compute_groups: list[dict[str, Any]],
) -> None:
    """Fix workspace aliases using actual compute-group GPU types.

    The initial guess (``_guess_workspace_alias``) relies on workspace *names*
    only.  After compute groups are discovered we know which workspaces actually
    contain GPU resources and can correct mis-classifications — e.g. a workspace
    named "高性能计算" that contains only CPU groups should not be the "gpu"
    alias.
    """

    current_gpu_ws = str(merged_workspaces.get("gpu") or "").strip()
    gpu_workspace_ids = _real_gpu_workspace_ids(compute_groups)
    if current_gpu_ws and current_gpu_ws in gpu_workspace_ids:
        return

    preferred_gpu_ws: str | None = None
    preferred_score = 0
    fallback_gpu_ws: str | None = None
    for raw_name, raw_workspace_id in merged_workspaces.items():
        name = str(raw_name or "").strip()
        workspace_id = str(raw_workspace_id or "").strip()
        if not name or not workspace_id or workspace_id not in gpu_workspace_ids:
            continue
        if fallback_gpu_ws is None:
            fallback_gpu_ws = workspace_id
        score = 1000 if name.lower() == "gpu" else _workspace_role_score(name, "gpu")
        if score > preferred_score:
            preferred_gpu_ws = workspace_id
            preferred_score = score

    corrected_gpu_ws = preferred_gpu_ws or fallback_gpu_ws
    if corrected_gpu_ws and current_gpu_ws:
        merged_workspaces["gpu"] = corrected_gpu_ws


def _persist_compute_groups(
    *,
    project_data: dict[str, Any],
    project_account_section: dict[str, Any],
    global_data: dict[str, Any],
    global_account_section: dict[str, Any],
    compute_groups: list[dict[str, Any]],
) -> None:
    existing_compute_groups = project_data.get("compute_groups")
    if not isinstance(existing_compute_groups, list):
        existing_compute_groups = project_account_section.get("compute_groups")
    if not isinstance(existing_compute_groups, list):
        existing_compute_groups = global_data.get("compute_groups")
    if not isinstance(existing_compute_groups, list):
        existing_compute_groups = global_account_section.get("compute_groups")
    if not isinstance(existing_compute_groups, list):
        existing_compute_groups = []
    if compute_groups:
        project_data["compute_groups"] = _merge_compute_groups(
            existing_compute_groups, compute_groups
        )
    project_account_section.pop("compute_groups", None)


def _extract_workspace_ids_from_compute_groups(
    compute_groups: list[dict[str, Any]] | None,
) -> set[str]:
    workspace_ids: set[str] = set()
    for item in compute_groups or []:
        if not isinstance(item, dict):
            continue
        for ws_id in item.get("workspace_ids") or []:
            value = str(ws_id or "").strip()
            if value:
                workspace_ids.add(value)
    return workspace_ids


def _cleanup_global_discovery_metadata(
    *,
    global_data: dict[str, Any],
    account_key: str,
) -> None:
    """Prune the empty ``[accounts.<user>]`` nesting after promotion.

    The persister helpers historically fan writes into both the project
    config and a legacy ``[accounts.<user>]`` subtable; by the time this
    runs, :func:`_promote_account_section_to_toplevel` has already lifted
    the useful parts to the top of ``global_data``, so all that's left
    to do is drop the now-empty skeleton.
    """
    accounts = global_data.get("accounts")
    if not isinstance(accounts, dict):
        return

    account_section = accounts.get(account_key)
    if not isinstance(account_section, dict):
        if not accounts:
            global_data.pop("accounts", None)
        return

    if not account_section:
        accounts.pop(account_key, None)
    if not accounts:
        global_data.pop("accounts", None)


def _copy_account_level_from_project(
    *, project_data: dict[str, Any], global_data: dict[str, Any]
) -> None:
    """Hoist account-level catalogs that the persisters wrote into
    ``project_data`` up to ``global_data``.

    Older helpers (``_persist_workspace_aliases``, ``_persist_compute_groups``)
    put the discovered ``[workspaces]`` alias table and ``[[compute_groups]]``
    list on the project side so a single-repo user could operate from one
    file. Under the per-account layout those are account-wide state, so
    copy them here before the project-config stripper removes them.
    """
    workspaces = project_data.get("workspaces")
    if isinstance(workspaces, dict) and workspaces:
        merged = dict(global_data.get("workspaces") or {})
        merged.update({str(k): str(v) for k, v in workspaces.items()})
        global_data["workspaces"] = merged

    compute_groups = project_data.get("compute_groups")
    if isinstance(compute_groups, list) and compute_groups:
        global_data["compute_groups"] = compute_groups

    projects = project_data.get("projects")
    if isinstance(projects, dict) and projects:
        merged_proj = dict(global_data.get("projects") or {})
        merged_proj.update({str(k): str(v) for k, v in projects.items()})
        global_data["projects"] = merged_proj


def _resolve_probe_defaults(
    *,
    config: Config,
    merged_workspaces: dict[str, str],
    workspace_id: str,
    browser_api_module,  # noqa: ANN001
    session,  # noqa: ANN001
    probe_pubkey: str | None,
) -> _ProbeDefaults:
    try:
        ssh_public_key = _load_ssh_public_key(probe_pubkey)
    except ValueError as e:
        click.echo(click.style(str(e), fg="red"))
        raise SystemExit(1) from e

    try:
        from inspire.config.ssh_runtime import resolve_ssh_runtime_config

        ssh_runtime = resolve_ssh_runtime_config()
    except Exception as e:
        click.echo(click.style(f"Failed to resolve SSH runtime config: {e}", fg="red"))
        raise SystemExit(1) from e

    probe_workspace_id = str(
        _guess_workspace_id_from_map(workspaces=merged_workspaces, alias="cpu")
        or workspace_id
    ).strip()
    if not probe_workspace_id:
        probe_workspace_id = workspace_id

    try:
        notebook_groups = browser_api_module.list_notebook_compute_groups(
            workspace_id=probe_workspace_id,
            session=session,
        )
        logic_compute_group_id = _select_probe_cpu_compute_group_id(notebook_groups)
        if not logic_compute_group_id:
            raise ValueError("No CPU compute group found")

        schedule = browser_api_module.get_notebook_schedule(
            workspace_id=probe_workspace_id,
            session=session,
        )
        quota_id, cpu_count, memory_size = _select_probe_cpu_quota(schedule)

        images = browser_api_module.list_images(
            workspace_id=probe_workspace_id,
            session=session,
        )
        selected_image = _select_probe_image(images)
        if not selected_image:
            raise ValueError("No images available")
    except Exception as e:
        click.echo(click.style(f"Failed to resolve probe defaults: {e}", fg="red"))
        raise SystemExit(1) from e

    shm_size = int(config.shm_size) if config.shm_size is not None else 32
    task_priority = int(config.job_priority) if config.job_priority is not None else 10
    task_priority = max(1, min(9, task_priority))

    return _ProbeDefaults(
        ssh_runtime=ssh_runtime,
        ssh_public_key=ssh_public_key,
        probe_workspace_id=probe_workspace_id,
        logic_compute_group_id=logic_compute_group_id,
        quota_id=quota_id,
        cpu_count=cpu_count,
        memory_size=memory_size,
        selected_image=selected_image,
        task_priority=task_priority,
        shm_size=shm_size,
    )


def _build_probe_project_list(
    *,
    projects: list[object],
    project_catalog: dict[str, dict[str, Any]],
    force: bool,
    probe_limit: int,
) -> list[object]:
    to_probe: list[object] = []
    for project in projects:
        entry = project_catalog.get(project.project_id) or {}
        shared = str(entry.get("shared_path_group") or "").strip()
        error = str(entry.get("probe_error") or "").strip()
        if not force and shared and not error:
            continue
        to_probe.append(project)
    if probe_limit:
        to_probe = to_probe[:probe_limit]
    return to_probe


def _apply_probe_result(
    *,
    entry: dict[str, Any],
    probe_result: dict[str, Any],
) -> None:
    entry["probed_at"] = _utc_now_iso()
    if probe_result.get("notebook_id"):
        entry["probe_notebook_id"] = probe_result["notebook_id"]

    shared_path_group = str(probe_result.get("shared_path_group") or "").strip()
    if shared_path_group:
        entry["shared_path_group"] = shared_path_group
        entry.pop("probe_error", None)
        return

    probe_error = str(probe_result.get("probe_error") or "").strip()
    if probe_error:
        entry["probe_error"] = probe_error


def _run_shared_path_probe(
    *,
    browser_api_module,  # noqa: ANN001
    session,  # noqa: ANN001
    account_key: str,
    projects: list[object],
    project_catalog: dict[str, dict[str, Any]],
    alias_for_id: dict[str, str],
    force: bool,
    probe_limit: int,
    probe_keep_notebooks: bool,
    probe_timeout: int,
    probe_defaults: _ProbeDefaults,
) -> None:
    to_probe = _build_probe_project_list(
        projects=projects,
        project_catalog=project_catalog,
        force=force,
        probe_limit=probe_limit,
    )
    if not to_probe:
        click.echo("No projects require probing.")
        return

    for idx, project in enumerate(to_probe, start=1):
        project_id = str(getattr(project, "project_id", "") or "").strip()
        project_name = str(getattr(project, "name", "") or "").strip()
        project_alias = str(
            alias_for_id.get(project_id) or _slugify_alias(project_name) or project_id
        )
        click.echo(f"[{idx}/{len(to_probe)}] {project_name} ({project_alias})")

        probe_result = _probe_project_shared_path_group(
            browser_api_module=browser_api_module,
            session=session,
            workspace_id=probe_defaults.probe_workspace_id,
            account_key=account_key,
            project_id=project_id,
            project_name=project_name,
            project_alias=project_alias,
            ssh_public_key=probe_defaults.ssh_public_key,
            ssh_runtime=probe_defaults.ssh_runtime,
            logic_compute_group_id=probe_defaults.logic_compute_group_id,
            quota_id=probe_defaults.quota_id,
            cpu_count=probe_defaults.cpu_count,
            memory_size=probe_defaults.memory_size,
            image_id=str(getattr(probe_defaults.selected_image, "image_id", "") or ""),
            image_url=str(getattr(probe_defaults.selected_image, "url", "") or ""),
            shm_size=probe_defaults.shm_size,
            task_priority=probe_defaults.task_priority,
            keep_notebook=probe_keep_notebooks,
            timeout=probe_timeout,
        )

        entry = project_catalog.setdefault(project_id, {"id": project_id})
        _apply_probe_result(entry=entry, probe_result=probe_result)


def _drop_catalog_runtime_fields(project_catalog: dict[str, dict[str, Any]]) -> None:
    for entry in project_catalog.values():
        for field in _CATALOG_DROP_FIELDS:
            entry.pop(field, None)


def _persist_prompted_credentials(
    *,
    global_data: dict[str, Any],
    account_section: dict[str, Any],
    prompted_credentials: tuple[str, str, str] | None,
) -> None:
    if not prompted_credentials:
        return
    _, prompted_password, prompted_base_url = prompted_credentials
    account_section["password"] = prompted_password
    api = global_data.get("api")
    if not isinstance(api, dict):
        api = {}
        global_data["api"] = api
    api["base_url"] = prompted_base_url


def _get_or_create_dict_table(
    *,
    container: dict[str, Any],
    key: str,
) -> dict[str, Any]:
    section = container.get(key)
    if isinstance(section, dict):
        return section
    section = {}
    container[key] = section
    return section


# Storage tiers exposed under `/inspire/<tier>/project/<proj>/...`. Ordered
# with the best default first so `ssd` is the suggested tier when the catalog
# workdir cannot be parsed. See `references/browser-api.md` / SKILL.md for
# the empirical capacity and throughput data behind these choices.
_STORAGE_TIERS: tuple[tuple[str, str], ...] = (
    ("ssd",     "gpfs_flash — fast tier, best for training hot path / active working set"),
    ("hdd",     "gpfs_hdd — general purpose; project fileset fills up fast, watch quota"),
    ("qb-ilm",  "qb_prod_ipfs01 — large tier, good read bandwidth"),
    ("qb-ilm2", "qb_prod_ipfs02 — largest tier, usually the most free capacity"),
)
_STORAGE_TIER_NAMES: tuple[str, ...] = tuple(name for name, _ in _STORAGE_TIERS)


def _detect_storage_tier(path: str) -> str | None:
    """Return the tier component of an ``/inspire/<tier>/...`` path, or None."""
    if not path:
        return None
    parts = path.strip().split("/")
    if len(parts) >= 3 and parts[1] == "inspire" and parts[2] in _STORAGE_TIER_NAMES:
        return parts[2]
    return None


def _substitute_storage_tier(path: str, new_tier: str) -> str:
    """Rewrite ``/inspire/<old>/...`` to ``/inspire/<new>/...``; no-op otherwise."""
    parts = path.split("/")
    if len(parts) >= 3 and parts[1] == "inspire" and parts[2] in _STORAGE_TIER_NAMES:
        parts[2] = new_tier
        return "/".join(parts)
    return path


def _prompt_storage_tier(current_path: str) -> str:
    """Ask the user to pick an Inspire storage tier.

    The platform API's ``/train_job/workdir`` historically returns an
    ``/inspire/hdd/...`` path — and HDD filesets are commonly 100% full
    on busy projects, so that default is frequently wrong. Strategy:

    - If the catalog-suggested path already points to ssd / qb-ilm /
      qb-ilm2, trust it and use that as the pre-selected default.
    - Otherwise (catalog points at hdd, or path is unparseable), pre-select
      ``ssd`` so the user has to deliberately opt into hdd rather than
      inherit it silently.

    The catalog's original choice is still annotated in the listing so the
    user knows what the platform proposed.
    """
    detected = _detect_storage_tier(current_path)
    if detected in (None, "hdd"):
        suggested = "ssd"
    else:
        suggested = detected
    click.echo("")
    click.echo("Remote workspace storage tier — pick where `target_dir` lives:")
    for tier, desc in _STORAGE_TIERS:
        marker = "  (catalog default)" if tier == detected else ""
        click.echo(f"  {tier:<8} {desc}{marker}")
    choice = click.prompt(
        "Storage tier",
        type=click.Choice(_STORAGE_TIER_NAMES, case_sensitive=False),
        default=suggested,
        show_default=True,
    )
    return str(choice).lower()


def _prompt_target_dir(
    *,
    force: bool,
    cli_target_dir: str | None,
    config: Config | None = None,
    selected_project: object,
    project_catalog: dict[str, dict[str, Any]],
) -> str | None:
    """Prompt for target_dir, using the catalog workdir as suggestion.

    Interactive path: first ask for a storage tier (ssd / hdd / qb-ilm /
    qb-ilm2) and rewrite ``/inspire/<tier>/`` in the catalog-suggested
    workdir accordingly, then prompt for the full path with the tier-
    substituted default pre-filled. This lets users opt out of the
    platform's HDD default without having to hand-edit the path.

    The picker is skipped when ``--force`` is set (non-interactive) or
    when an explicit ``--target-dir`` was passed on the CLI.
    """
    project_id = str(getattr(selected_project, "project_id", "") or "").strip()
    entry = project_catalog.get(project_id, {})
    catalog_workdir = str(entry.get("workdir") or "").strip()

    if force:
        existing = str(getattr(config, "target_dir", "") or "").strip() if config else ""
        return cli_target_dir or existing or catalog_workdir or None

    if cli_target_dir:
        default = cli_target_dir
    else:
        # Prefer the user's existing target_dir over the catalog-suggested
        # workdir — they may have appended a project-specific subdirectory
        # (e.g. `.../chj_code/<repo>`) that the catalog doesn't know about.
        # Only fall back to the catalog workdir if no previous value exists.
        existing = str(getattr(config, "target_dir", "") or "").strip() if config else ""
        default = existing or catalog_workdir or ""
        if default:
            tier = _prompt_storage_tier(default)
            if tier != _detect_storage_tier(default):
                default = _substitute_storage_tier(default, tier)

    if default:
        result = click.prompt(
            "Target directory on shared filesystem",
            default=default,
            show_default=True,
        )
    else:
        result = click.prompt(
            "Target directory on shared filesystem (e.g. /inspire/...)",
            default="",
            show_default=False,
        )
    return result.strip() or None


def _persist_context_defaults(
    *,
    context: dict[str, Any],
    project_data: dict[str, Any],
) -> None:
    """No-op — see the block comment below.

    Deliberately do NOT auto-guess ``[context].workspace_cpu`` /
    ``workspace_gpu`` defaults. The fuzzy matcher that used to live here
    guessed from workspace names and usually picked the wrong one —
    per-repo workspace preference is a user decision, not something to
    infer. Users can set ``[context].workspace`` /
    ``[context].workspace_cpu`` / ``[context].workspace_gpu`` by hand when
    they need to pin a default for a given repo.
    """
    # Intentionally empty.
    return


_PROJECT_CONFIG_DISALLOWED_SECTIONS = (
    "accounts",  # legacy catalog nesting
    "auth",  # identity — belongs to account layer
    "api",  # account-wide
    "proxy",  # account-wide
    "workspaces",  # account-wide alias map
    "projects",  # account-wide alias map
    "project_catalog",  # account-wide per-project metadata
    "account",  # account-level shared_path_group / workdir
    "compute_groups",  # account-wide (array of tables)
)


def _strip_account_level_from_project(project_data: dict[str, Any]) -> None:
    """Enforce the project-config contract: only [context]/[paths]/[job]/[notebook]/[remote_env].

    Removes every section listed in :data:`_PROJECT_CONFIG_DISALLOWED_SECTIONS`
    and the legacy ``[context].account`` key, which the per-account loader
    ignores anyway.
    """
    for key in _PROJECT_CONFIG_DISALLOWED_SECTIONS:
        project_data.pop(key, None)
    context = project_data.get("context")
    if isinstance(context, dict):
        context.pop("account", None)


def _promote_account_section_to_toplevel(
    global_data: dict[str, Any], account_key: str
) -> None:
    """Move ``[accounts."<user>"]`` contents to top level on account config.

    The discover helpers still populate legacy-style nesting; under the new
    account-per-directory layout this nesting is explicitly disallowed by
    the loader. Promoting keeps the rest of the persisters intact while
    making the resulting file match the loader's contract.
    """
    accounts = global_data.get("accounts")
    if not isinstance(accounts, dict):
        return
    section = accounts.get(account_key)
    if not isinstance(section, dict):
        return

    # Array-of-tables and dict sections move verbatim to the top level.
    for key in ("workspaces", "projects", "project_catalog", "compute_groups"):
        if key in section:
            global_data[key] = section.pop(key)

    # Passwords live in [auth] at the top level.
    password = section.pop("password", None)
    if password:
        auth_section = global_data.setdefault("auth", {})
        if isinstance(auth_section, dict):
            auth_section["password"] = password

    # Account-level shared_path_group / train_job_workdir remain under [account].
    for key in ("shared_path_group", "train_job_workdir"):
        value = section.pop(key, None)
        if value:
            account_block = global_data.setdefault("account", {})
            if isinstance(account_block, dict):
                account_block[key] = value

    # Sub-tables like [accounts."<u>".api] / .ssh → top-level [api] / [ssh]
    # merge keys (account-specific values win over discovery defaults).
    for sub_key in ("api", "ssh"):
        sub = section.pop(sub_key, None)
        if isinstance(sub, dict) and sub:
            top = global_data.setdefault(sub_key, {})
            if isinstance(top, dict):
                top.update(sub)

    # Drop any remaining scalar overrides (they map to top-level schema keys).
    for field_name, value in list(section.items()):
        if isinstance(value, (dict, list)):
            continue
        section.pop(field_name, None)
        if value not in (None, ""):
            global_data[field_name] = value

    # Remove the now-empty nesting.
    if not section:
        accounts.pop(account_key, None)
    if not accounts:
        global_data.pop("accounts", None)


def _write_discovered_project_config(
    *,
    project_path: Path,
    project_data: dict[str, Any],
    config: Config,
    account_key: str,
    selected_alias: str,
    target_dir: str | None = None,
) -> None:
    # Build [context] from the discovered state and copy defaults that the
    # helpers may have stashed under top-level keys. Identity (username /
    # account) is NOT written — it belongs to the active account's config.
    context = _get_or_create_dict_table(container=project_data, key="context")
    context["project"] = selected_alias
    _persist_context_defaults(
        context=context,
        project_data=project_data,
    )

    if target_dir:
        paths_section = _get_or_create_dict_table(container=project_data, key="paths")
        paths_section["target_dir"] = target_dir

    # Strip everything that isn't per-repo state — a single account may use
    # many repos, and every one duplicating the workspace/compute_groups
    # catalog is both noisy and divergent-on-refresh.
    _strip_account_level_from_project(project_data)

    # "defaults" was a legacy umbrella section; job/notebook survive only
    # if they hold [job].image / [notebook].image per-repo overrides.
    project_data.pop("defaults", None)
    for sub_key in ("job", "notebook"):
        sub = project_data.get(sub_key)
        if isinstance(sub, dict) and not sub.get("image"):
            project_data.pop(sub_key, None)

    project_path.parent.mkdir(parents=True, exist_ok=True)
    project_path.write_text(_toml_dumps(project_data))


def _print_discover_completion(
    *,
    global_path: Path,
    project_path: Path,
    prompted_credentials: tuple[str, str, str] | None,
) -> None:
    click.echo()
    click.echo(click.style("Wrote configuration:", bold=True))
    click.echo(f"  - {global_path}")
    click.echo(f"  - {project_path}")
    click.echo()
    if prompted_credentials:
        click.echo("Note: prompted account password was stored in global config for this account.")
        click.echo(f"  Location: {global_path}")
        click.echo()
        click.echo("Ready to use:")
        click.echo("  inspire config show     # Verify configuration")
        click.echo("  inspire resources list  # View available GPUs")
        click.echo("  inspire notebook list   # List notebooks")
        return
    click.echo("Next steps:")
    click.echo("  Run: inspire config show")


def _persist_discovery_catalog(request: _DiscoveryPersistRequest) -> None:
    force = request.force
    config = request.config
    browser_api_module = request.browser_api_module
    session = request.session
    account_key = request.account_key
    workspace_id = request.workspace_id
    projects = request.projects
    selected_project = request.selected_project
    probe_shared_path = request.probe_shared_path
    probe_limit = request.probe_limit
    probe_keep_notebooks = request.probe_keep_notebooks
    probe_pubkey = request.probe_pubkey
    probe_timeout = request.probe_timeout
    prompted_credentials = request.prompted_credentials
    cli_target_dir = request.cli_target_dir

    global_path = Config.writable_config_path()
    project_path = Path.cwd() / PROJECT_CONFIG_DIR / CONFIG_FILENAME
    if not _confirm_discovery_writes(
        force=force, global_path=global_path, project_path=project_path
    ):
        return

    global_data, account_section = _load_discovery_global_state(
        global_path=global_path,
        account_key=account_key,
    )
    project_data, project_account_section = _load_discovery_project_state(
        project_path=project_path,
        account_key=account_key,
    )
    _seed_project_discovery_metadata(
        project_data=project_data,
        project_account_section=project_account_section,
        global_data=global_data,
        global_account_section=account_section,
    )
    alias_for_id, project_catalog = _resolve_project_catalog_aliases(
        project_data=project_data,
        project_account_section=project_account_section,
        projects=projects,
    )
    _populate_project_catalog(
        project_catalog=project_catalog,
        projects=projects,
        browser_api_module=browser_api_module,
        session=session,
        workspace_id=workspace_id,
        account_key=account_key,
        force=force,
    )
    project_workspace_ids = {
        str(getattr(project, "workspace_id", "") or "").strip()
        for project in projects
        if str(getattr(project, "workspace_id", "") or "").strip()
    }
    if len(project_workspace_ids) > 1:
        project_aliases = project_data.get("projects")
        if isinstance(project_aliases, dict) and project_aliases:
            account_section["projects"] = deepcopy(project_aliases)
        if project_catalog:
            account_section["project_catalog"] = deepcopy(project_catalog)
    else:
        account_section.pop("projects", None)
        account_section.pop("project_catalog", None)
    _update_account_shared_path_group(
        account_section=project_account_section,
        project_catalog=project_catalog,
        force=force,
    )
    _print_shared_path_group_summary(
        projects=projects,
        project_catalog=project_catalog,
        alias_for_id=alias_for_id,
    )

    _persist_workspace_aliases(
        project_data=project_data,
        project_account_section=project_account_section,
        global_data=global_data,
        global_account_section=account_section,
        config=config,
        session=session,
        workspace_id=workspace_id,
        force=force,
    )
    merged_workspaces = project_data.get("workspaces")
    if not isinstance(merged_workspaces, dict):
        merged_workspaces = {}

    _persist_api_base_url(
        global_data=global_data,
        account_section=account_section,
        config=config,
    )
    _discover_docker_registry(
        global_data=global_data,
        browser_api_module=browser_api_module,
        session=session,
        workspace_id=workspace_id,
    )
    all_ws_ids: set[str] = {workspace_id}
    for ws_id in list(session.all_workspace_ids or []):
        ws_str = str(ws_id or "").strip()
        if ws_str:
            all_ws_ids.add(ws_str)
    for ws_id in merged_workspaces.values():
        ws_str = str(ws_id or "").strip()
        if ws_str:
            all_ws_ids.add(ws_str)

    existing_project_compute_groups = project_data.get("compute_groups")
    if not isinstance(existing_project_compute_groups, list):
        existing_project_compute_groups = []

    known_workspace_ids = _extract_workspace_ids_from_compute_groups(
        existing_project_compute_groups
    )
    missing_workspace_ids = sorted(
        ws_id for ws_id in all_ws_ids if ws_id not in known_workspace_ids
    )

    compute_groups: list[dict[str, Any]] = list(existing_project_compute_groups)
    if missing_workspace_ids:
        max_workers = min(len(missing_workspace_ids), 6)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {
                pool.submit(
                    _discover_compute_groups,
                    browser_api_module=browser_api_module,
                    session=session,
                    workspace_id=ws_id,
                ): ws_id
                for ws_id in missing_workspace_ids
            }
            workspace_results: dict[str, list[dict[str, Any]]] = {}
            for future in concurrent.futures.as_completed(future_map):
                ws_id = future_map[future]
                try:
                    workspace_results[ws_id] = future.result()
                except Exception:
                    workspace_results[ws_id] = []

        for ws_id in missing_workspace_ids:
            for cg in workspace_results.get(ws_id, []):
                cg.setdefault("workspace_ids", [])
                if ws_id not in cg["workspace_ids"]:
                    cg["workspace_ids"].append(ws_id)
                compute_groups.append(cg)
    _correct_workspace_aliases(merged_workspaces, compute_groups)
    _persist_compute_groups(
        project_data=project_data,
        project_account_section=project_account_section,
        global_data=global_data,
        global_account_section=account_section,
        compute_groups=compute_groups,
    )

    if probe_shared_path:
        click.echo()
        click.echo(click.style("Probing shared filesystem paths...", bold=True))
        probe_defaults = _resolve_probe_defaults(
            config=config,
            merged_workspaces=merged_workspaces,
            workspace_id=workspace_id,
            browser_api_module=browser_api_module,
            session=session,
            probe_pubkey=probe_pubkey,
        )
        _run_shared_path_probe(
            browser_api_module=browser_api_module,
            session=session,
            account_key=account_key,
            projects=projects,
            project_catalog=project_catalog,
            alias_for_id=alias_for_id,
            force=force,
            probe_limit=probe_limit,
            probe_keep_notebooks=probe_keep_notebooks,
            probe_timeout=probe_timeout,
            probe_defaults=probe_defaults,
        )

    _drop_catalog_runtime_fields(project_catalog)
    _persist_prompted_credentials(
        global_data=global_data,
        account_section=account_section,
        prompted_credentials=prompted_credentials,
    )
    _cleanup_global_discovery_metadata(
        global_data=global_data,
        account_key=account_key,
    )

    # Final step before writing: lift account-wide data the persisters parked
    # on the project side, promote anything still under [accounts."<user>"]
    # nesting, and prune the empty legacy skeleton. The per-project catalog
    # keeps only ``{name, path}`` (see ``_populate_project_catalog``); any
    # legacy ``shared_path_group`` / ``workdir`` keys left in it are scrubbed
    # here so the account file stays clean.
    _copy_account_level_from_project(
        project_data=project_data, global_data=global_data
    )
    _promote_account_section_to_toplevel(global_data, account_key)
    global_data.pop("account", None)
    catalog = global_data.get("project_catalog")
    if isinstance(catalog, dict):
        for project_id, entry in list(catalog.items()):
            if not isinstance(entry, dict):
                catalog.pop(project_id, None)
                continue
            for stale in ("workdir", "shared_path_group"):
                entry.pop(stale, None)
            if not entry:
                catalog.pop(project_id, None)
        if not catalog:
            global_data.pop("project_catalog", None)

    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text(_toml_dumps(global_data))
    if prompted_credentials:
        try:
            global_path.chmod(0o600)
        except OSError:
            pass

    selected_alias = alias_for_id.get(selected_project.project_id)
    if not selected_alias:
        selected_alias = _slugify_alias(selected_project.name) or "default"
    target_dir = _prompt_target_dir(
        force=force,
        cli_target_dir=cli_target_dir,
        config=config,
        selected_project=selected_project,
        project_catalog=project_catalog,
    )
    _write_discovered_project_config(
        project_path=project_path,
        project_data=project_data,
        config=config,
        account_key=account_key,
        selected_alias=selected_alias,
        target_dir=target_dir,
    )

    resolved, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)
    if not str(getattr(resolved, "job_project_id", "") or "").startswith("project-"):
        click.echo(click.style("Wrote config, but could not resolve a project_id", fg="red"))
        raise SystemExit(1)

    _ensure_ssh_key()
    _print_discover_completion(
        global_path=global_path,
        project_path=project_path,
        prompted_credentials=prompted_credentials,
    )


def _init_discover_mode(
    force: bool,
    *,
    probe_shared_path: bool,
    probe_limit: int,
    probe_keep_notebooks: bool,
    probe_pubkey: str | None,
    probe_timeout: int,
    cli_username: str | None = None,
    cli_base_url: str | None = None,
    cli_target_dir: str | None = None,
    cli_select_project: str | None = None,
) -> None:
    """Initialize per-account catalogs by discovering projects and compute groups."""
    from inspire.platform.web import browser_api as browser_api_module
    from inspire.platform.web import session as web_session_module
    from inspire.platform.web.session.browser_client import _close_browser_client
    from inspire.platform.web.session import DEFAULT_WORKSPACE_ID

    config, _ = Config.from_files_and_env(require_credentials=False, require_target_dir=False)
    session, prompted_credentials, account_key, workspace_id = _resolve_discover_runtime(
        config=config,
        web_session_module=web_session_module,
        default_workspace_id=DEFAULT_WORKSPACE_ID,
        cli_username=cli_username,
        cli_base_url=cli_base_url,
    )

    click.echo(click.style("Discovering account catalog...", bold=True))
    click.echo(f"Account: {account_key}")
    click.echo(f"Workspace: {workspace_id}")
    projects, selected_project = _load_projects_for_discovery(
        browser_api_module=browser_api_module,
        session=session,
        workspace_id=workspace_id,
        force=force,
        probe_shared_path=probe_shared_path,
        probe_limit=probe_limit,
        requested_project=cli_select_project,
    )
    try:
        _persist_discovery_catalog(
            _DiscoveryPersistRequest(
                force=force,
                config=config,
                browser_api_module=browser_api_module,
                session=session,
                account_key=account_key,
                workspace_id=workspace_id,
                projects=projects,
                selected_project=selected_project,
                probe_shared_path=probe_shared_path,
                probe_limit=probe_limit,
                probe_keep_notebooks=probe_keep_notebooks,
                probe_pubkey=probe_pubkey,
                probe_timeout=probe_timeout,
                prompted_credentials=prompted_credentials,
                cli_target_dir=cli_target_dir,
            )
        )
    finally:
        _close_browser_client()
