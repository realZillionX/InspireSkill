"""Discovery mode: discover workspaces, projects, compute groups, and paths."""

from __future__ import annotations

import concurrent.futures
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from inspire.cli.utils.id_resolver import is_full_uuid, is_partial_id
from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import Config
from inspire.config.toml import _project_config_write_path
from inspire.platform.web.session import AuthenticationError
from inspire.platform.web.session.browser_launch import is_playwright_browser_runtime_error
from .toml_helpers import _toml_dumps

from inspire.platform.web.browser_api.core import _set_base_url

logger = logging.getLogger(__name__)

_USERNAME_PLACEHOLDERS = frozenset({"your_username"})
# Older account configs shipped this placeholder as their [api] base_url.
# Newly written ones carry the real default, so this only rescues files on disk.
_BASE_URL_PLACEHOLDER = "https://api.example.com"
_ACCOUNT_CONFIG_KEYS = frozenset(
    {
        "auth",
        "api",
        "proxy",
        "paths",
        "tunnel",
        "remote_env",
        "path_aliases",
        "projects",
        "project_catalog",
        "compute_groups",
        "defaults",
    }
)
_PROJECT_CONFIG_KEYS = frozenset(
    {
        "context",
        "paths",
        "job",
        "notebook",
        "profiles",
        "remote_env",
        "path_aliases",
        "cli",
        "defaults",
    }
)
_PROJECT_HANDLE_PREFIXES = (
    "compute-group-",
    "workspace-",
    "project-",
    "proj-",
    "lcg-",
    "ws-",
)

_HANDLE_BODY_RE = re.compile(r"^[0-9a-f]+(?:-[0-9a-f]+)*$", re.IGNORECASE)


def _is_handle_shaped_body(body: str) -> bool:
    return len(body.replace("-", "")) >= 3 and bool(_HANDLE_BODY_RE.match(body))


@dataclass(frozen=True)
class _DiscoveryPersistRequest:
    force: bool
    scope: str
    config: Config
    browser_api_module: Any
    session: Any
    workspace_id: str
    projects: list[Any]
    selected_project: Any
    prompted_credentials: tuple[str, str, str] | None
    non_interactive: bool = False
    verbose: bool = False


def _progress(verbose: bool, message: str) -> None:
    if verbose:
        logger.debug("%s", message)


def _slugify_alias(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def _workspace_label_for_output(session: Any, workspace_id: str) -> str:
    value = str(workspace_id or "").strip()
    if not value:
        return "(workspace name unavailable)"
    names = getattr(session, "all_workspace_names", None)
    if isinstance(names, dict):
        name = str(names.get(value) or "").strip()
        if name:
            return name
    return "(workspace name unavailable)"


def _workspace_error_sample(
    session: Any,
    workspace_errors: list[tuple[str, str]],
) -> str:
    sample = ", ".join(
        f"{_workspace_label_for_output(session, ws)}: unavailable"
        for ws, _msg in workspace_errors[:3]
    )
    if len(workspace_errors) > 3:
        sample += ", ..."
    return sample


def _make_unique_alias(alias: str, used: set[str]) -> str:
    base = alias
    counter = 2
    while alias in used:
        alias = f"{base}-{counter}"
        counter += 1
    used.add(alias)
    return alias


def _ensure_playwright_browser(*, non_interactive: bool = False) -> None:
    """Check that the local browser runtime is installed; offer to install it."""
    import subprocess
    import sys

    try:
        from playwright.sync_api import sync_playwright
        from inspire.platform.web.session.browser_launch import (
            chromium_launch_kwargs,
            playwright_install_args,
        )

        with sync_playwright() as p:
            browser = p.chromium.launch(**chromium_launch_kwargs(headless=True))
            browser.close()
        return  # already installed
    except Exception:
        pass

    install_args = playwright_install_args()
    if not non_interactive:
        click.echo()
        if "--with-deps" in install_args:
            click.echo(
                "A local browser runtime and Linux system dependencies are required for "
                "platform login (one-time setup)."
            )
        else:
            click.echo(
                "A local browser runtime is required for platform login "
                "(one-time ~150 MB download)."
            )
        if not click.confirm("Install Chromium now?", default=True):
            click.echo("Cannot proceed without a browser for platform login.")
            raise SystemExit(1)

    result = subprocess.run(
        [sys.executable, "-m", "playwright", *install_args],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        click.echo(
            click.style(
                "Chromium installation failed. Run `python -m playwright install chromium` "
                "and retry.",
                fg="red",
            ),
            err=True,
        )
        raise SystemExit(1)


def _resolve_credentials_interactive(
    config: object,
    *,
    cli_username: str | None,
    cli_base_url: str | None,
    allow_config_password: bool = False,
    confirm_config_username: bool = False,
    non_interactive: bool = False,
) -> tuple[str, str, str]:
    """Resolve base_url, username, and password, prompting when missing."""
    # --- base_url ---
    base_url = (cli_base_url or "").strip()
    if not base_url:
        base_url = _usable_base_url(getattr(config, "base_url", ""))
    if not base_url:
        if non_interactive:
            raise ValueError("Platform URL is required for non-interactive init.")
        base_url = click.prompt("Platform URL", type=str).strip()
    if not base_url:
        click.echo(click.style("Platform URL is required.", fg="red"))
        raise SystemExit(1)

    # --- username ---
    username = (cli_username or "").strip()
    if not username:
        cfg_username = _usable_username(getattr(config, "username", ""))
        if cfg_username and confirm_config_username and not non_interactive:
            username = click.prompt(
                "Platform login name (not display name)",
                default=cfg_username,
                type=str,
            ).strip()
        elif cfg_username:
            username = cfg_username
    if not username:
        if non_interactive:
            raise ValueError("Username is required for non-interactive init.")
        username = click.prompt(
            "Platform login name (not display name)",
            type=str,
        ).strip()
    if not username:
        click.echo(click.style("Username is required.", fg="red"))
        raise SystemExit(1)

    # --- password ---
    # When the caller explicitly provided credentials (allow_config_password=True),
    # the config/env password is likely valid — use it to support non-interactive
    # --force mode.  A cached password may be stale after a failed session, so
    # always prompt for a fresh one.
    password = ""
    if allow_config_password or non_interactive:
        password = str(getattr(config, "password", "") or "").strip()
    if not password:
        if non_interactive:
            raise ValueError("Password is required for non-interactive init.")
        password = click.prompt("Password", type=str, hide_input=True)
    if not password:
        click.echo(click.style("Password is required.", fg="red"))
        raise SystemExit(1)

    return username, password, base_url


def _ensure_ssh_key(*, non_interactive: bool = False) -> None:
    """Check for an SSH key; offer to generate one if missing."""
    import subprocess

    ssh_dir = Path.home() / ".ssh"
    candidates = [ssh_dir / "id_ed25519.pub", ssh_dir / "id_rsa.pub"]
    if any(p.exists() for p in candidates):
        return

    if non_interactive:
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
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        click.echo("SSH key generated.")
    else:
        click.echo(click.style("SSH key generation failed.", fg="yellow"))


def _usable_username(value: object) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lower() in _USERNAME_PLACEHOLDERS:
        return ""
    return text


def _usable_base_url(value: object) -> str:
    text = str(value or "").strip()
    if not text or text == _BASE_URL_PLACEHOLDER:
        return ""
    return text


def _looks_like_project_handle(value: object) -> bool:
    """Whether a discovered value is a handle rather than a usable name.

    This screens values the platform hands back during ``init`` discovery, so
    it carries its own prefix list. The CLI's input boundary deliberately
    accepts ``proj-``/``workspace-`` as ordinary names; here the only cost of
    a wider net is declining to record one discovered alias.
    """
    text = str(value or "").strip()
    if not text:
        return False
    if is_full_uuid(text):
        return True

    lowered = text.casefold()
    for prefix in _PROJECT_HANDLE_PREFIXES:
        if not lowered.startswith(prefix):
            continue
        body = lowered[len(prefix) :]
        return is_full_uuid(body) or is_partial_id(body) or _is_handle_shaped_body(body)
    return False


def _build_project_aliases(
    projects: list[Any],
    *,
    existing: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """Build aliases and a transient live-project lookup index.

    The returned platform-ID index is used only while processing live API
    objects and is never written to configuration.
    """
    existing_map = existing or {}
    projects_by_platform_id: dict[str, Any] = {}
    projects_by_name: dict[str, Any] = {}
    for project in projects:
        project_id = str(getattr(project, "project_id", "") or "").strip()
        name = str(getattr(project, "name", "") or "").strip()
        if project_id and name:
            projects_by_platform_id[project_id] = project
            projects_by_name.setdefault(name.casefold(), project)

    merged: dict[str, str] = {}
    project_alias_by_platform_id: dict[str, str] = {}
    used_aliases: set[str] = set()
    for raw_alias, raw_value in existing_map.items():
        alias = str(raw_alias or "").strip()
        value = str(raw_value or "").strip()
        if not alias or not value:
            continue
        project = projects_by_name.get(value.casefold())
        if project is None:
            continue
        project_id = str(getattr(project, "project_id", "") or "").strip()
        name = str(getattr(project, "name", "") or "").strip()
        if not project_id or not name:
            continue
        durable_alias = _make_unique_alias(alias, used_aliases)
        merged[durable_alias] = name
        project_alias_by_platform_id.setdefault(project_id, durable_alias)

    for project_id, project in projects_by_platform_id.items():
        if project_id in project_alias_by_platform_id:
            continue
        name = str(getattr(project, "name", "") or "").strip()
        alias = name if name not in used_aliases else _make_unique_alias(name, used_aliases)
        if alias == name:
            used_aliases.add(alias)
        merged[alias] = name
        project_alias_by_platform_id[project_id] = alias

    return merged, project_alias_by_platform_id


def _merge_compute_groups(
    existing: list[dict[str, Any]] | None,
    discovered: list[dict[str, Any]],
    *,
    workspace_names_by_id: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Merge compute-group metadata without persisting platform handles."""
    by_name: dict[tuple[str, str, str], dict[str, Any]] = {}
    workspace_names_by_id = workspace_names_by_id or {}

    for item in [*(existing or []), *discovered]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("logic_compute_group_name") or "").strip()
        if not name:
            continue
        gpu_type = str(item.get("gpu_type") or "").strip()
        location = str(item.get("location") or "").strip()
        key = (name.casefold(), gpu_type.casefold(), location.casefold())
        merged = by_name.setdefault(key, {"name": name})
        if gpu_type:
            merged["gpu_type"] = gpu_type
        if location:
            merged["location"] = location

        workspace_names = {
            str(value).strip()
            for value in (item.get("workspace_names") or [])
            if str(value or "").strip()
        }
        for workspace_id in item.get("workspace_ids") or []:
            workspace_name = workspace_names_by_id.get(str(workspace_id))
            if workspace_name:
                workspace_names.add(str(workspace_name).strip())
        existing_names = set(merged.get("workspace_names") or [])
        combined = sorted(existing_names | workspace_names)
        if combined:
            merged["workspace_names"] = combined

    merged_list = list(by_name.values())
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
    non_interactive: bool = False,
    verbose: bool = False,
) -> tuple[object, tuple[str, str, str] | None, str, str]:
    # When the caller explicitly provides credentials via CLI flags, skip the
    # cached-session fast path so we honour the override instead of silently
    # using a session that belongs to a different user / base-url.
    session = None
    prompted_credentials: tuple[str, str, str] | None = None
    if cli_username or cli_base_url:
        _ensure_playwright_browser(non_interactive=non_interactive)
        username, password, base_url = _resolve_credentials_interactive(
            config,
            cli_username=cli_username,
            cli_base_url=cli_base_url,
            allow_config_password=True,
            non_interactive=non_interactive,
        )
        prompted_credentials = (username, password, base_url)
        _progress(verbose, "Logging in...")
        session = web_session_module.login_with_playwright(
            username,
            password,
            base_url=base_url,
        )
        _progress(verbose, "Logged in.")
    else:
        try:
            session = web_session_module.get_web_session(require_workspace=True)
        except AuthenticationError:
            # A credential-bearing attempt already happened. Prompting and
            # calling login_with_playwright here would submit it a second time.
            raise
        except (ValueError, RuntimeError) as exc:
            _ensure_playwright_browser(non_interactive=non_interactive)
            if is_playwright_browser_runtime_error(exc):
                try:
                    session = web_session_module.get_web_session(
                        force_refresh=True,
                        require_workspace=True,
                    )
                except (ValueError, RuntimeError) as retry_exc:
                    if is_playwright_browser_runtime_error(retry_exc):
                        raise
            if session is None:
                username, password, base_url = _resolve_credentials_interactive(
                    config,
                    cli_username=cli_username,
                    cli_base_url=cli_base_url,
                    confirm_config_username=True,
                    non_interactive=non_interactive,
                )
                prompted_credentials = (username, password, base_url)
                _progress(verbose, "Logging in...")
                session = web_session_module.login_with_playwright(
                    username,
                    password,
                    base_url=base_url,
                )
                _progress(verbose, "Logged in.")

    if prompted_credentials:
        account_key = _usable_username(prompted_credentials[0])
    else:
        account_key = (
            _usable_username(getattr(session, "login_username", ""))
            or _usable_username(getattr(config, "username", ""))
        )
    if not account_key:
        click.echo(click.style("Could not resolve account key (username)", fg="red"))
        raise SystemExit(1)

    if prompted_credentials:
        _set_base_url(prompted_credentials[2])
    else:
        cfg_base_url = _usable_base_url(getattr(config, "base_url", ""))
        if cfg_base_url:
            _set_base_url(cfg_base_url)
        else:
            session_base_url = _usable_base_url(getattr(session, "base_url", ""))
            if session_base_url:
                _set_base_url(session_base_url)

    workspace_id = str(session.workspace_id or "").strip()
    if not workspace_id or workspace_id == default_workspace_id:
        click.echo(
            click.style(
                "Could not detect an accessible workspace from the authenticated session. "
                "Re-run `inspire init` after signing into an account that "
                "can see at least one workspace.",
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

        for workspace_payload in try_enumerate_workspaces(session, workspace_id=workspace_id):
            ws_id = str(workspace_payload.get("id") or "").strip()
            if ws_id:
                candidates.append(ws_id)
    except Exception:
        pass

    ordered_unique: list[str] = []
    seen: set[str] = set()
    for raw_ws in candidates:
        ws_text = str(raw_ws or "").strip()
        if not ws_text or ws_text in seen:
            continue
        seen.add(ws_text)
        ordered_unique.append(ws_text)
    return ordered_unique


def _collect_discovery_projects(
    *,
    browser_api_module,  # noqa: ANN001
    session,  # noqa: ANN001
    workspace_id: str,
) -> tuple[list[Any], list[tuple[str, str]]]:
    """Collect projects across discovered workspaces (best-effort per workspace)."""
    workspace_ids = _candidate_workspace_ids_for_discovery(
        session=session,
        workspace_id=workspace_id,
    )

    discovered: list[Any] = []
    errors: list[tuple[str, str]] = []
    seen_platform_project_ids: set[str] = set()

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
            if project_id in seen_platform_project_ids:
                continue
            seen_platform_project_ids.add(project_id)
            discovered.append(project)

    return discovered, errors


def _load_projects_for_discovery(
    *,
    browser_api_module,  # noqa: ANN001
    session,  # noqa: ANN001
    workspace_id: str,
    force: bool,
    requested_project: str | None = None,
    non_interactive: bool = False,
) -> tuple[list[Any], Any]:
    projects, workspace_errors = _collect_discovery_projects(
        browser_api_module=browser_api_module,
        session=session,
        workspace_id=workspace_id,
    )

    if not projects:
        if workspace_errors:
            sample = _workspace_error_sample(session, workspace_errors)
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
        sample = _workspace_error_sample(session, workspace_errors)
        click.echo(
            click.style(
                f"Warning: some workspaces failed during project discovery "
                f"({len(workspace_errors)}): {sample}",
                fg="yellow",
            )
        )

    # Explicit `--select-project <name>` takes precedence over every
    # heuristic and skips the interactive prompt entirely. Matches case-insensitively
    # on names only; copied platform values are not part of the CLI boundary.
    if requested_project:
        rq = requested_project.strip()
        if _looks_like_project_handle(rq):
            click.echo(
                click.style(
                    "--select-project takes a project name.",
                    fg="red",
                )
            )
            raise SystemExit(1)
        match = None
        for project in projects:
            if project.name.lower() == rq.lower():
                match = project
                break
        if not match:
            available = ", ".join(p.name for p in projects if p.name)
            click.echo(
                click.style(
                    f"--select-project {scrub_raw_ids(rq)!r} not found. "
                    f"Candidates: {available}",
                    fg="red",
                )
            )
            raise SystemExit(1)
        return projects, match

    # Best platform-side guess, used only as a hint / single-project shortcut.
    # NEVER used as a silent repository-context choice when multiple projects exist.
    try:
        heuristic_pick, _ = browser_api_module.select_project(projects)
    except Exception:
        heuristic_pick = projects[0]

    if force or non_interactive:
        return projects, heuristic_pick

    click.echo()
    click.echo(click.style("Projects:", bold=True))
    for idx, project in enumerate(projects, start=1):
        suffix = project.get_quota_status() if hasattr(project, "get_quota_status") else ""
        click.echo(f"  {idx}. {project.name}{suffix}")

    if len(projects) == 1:
        # Single project — unambiguous, keep the zero-friction prompt default.
        choice = click.prompt(
            "Select project for this repository",
            type=click.IntRange(1, 1),
            default=1,
            show_default=True,
        )
    else:
        click.echo(
            click.style(
                "Multiple projects available — no project is selected implicitly. "
                "Pick the one your current work belongs to.",
                fg="yellow",
            )
        )
        choice = click.prompt(
            f"Select project for this repository (1-{len(projects)})",
            type=click.IntRange(1, len(projects)),
        )

    return projects, projects[choice - 1]


def _confirm_discovery_writes(
    *,
    force: bool,
    scope: str,
    global_path: Path,
    project_path: Path,
    non_interactive: bool,
) -> bool:
    if global_path.exists() and not force:
        if non_interactive:
            raise ValueError(
                "Account configuration already exists; rerun non-interactive init with --force."
            )
        message = "Account configuration already exists."
        click.echo(click.style(message, fg="yellow"))
        if not click.confirm(
            "Update it with discovered catalogs? (will rewrite file)", default=True
        ):
            return False

    if scope == "project" and project_path.exists() and not force:
        if non_interactive:
            raise ValueError(
                "Project configuration already exists; rerun non-interactive init with --force."
            )
        message = "Project configuration already exists."
        click.echo(click.style(message, fg="yellow"))
        if not click.confirm(
            "Update it with discovered context/defaults? (will rewrite file)", default=True
        ):
            return False
    return True


def _load_discovery_global_state(
    *,
    global_path: Path,
) -> dict[str, Any]:
    raw_data: dict[str, Any] = {}
    if global_path.exists():
        raw_data = Config._load_toml(global_path)
    return {
        key: value for key, value in raw_data.items() if key in _ACCOUNT_CONFIG_KEYS
    }


def _load_discovery_project_state(
    *,
    project_path: Path,
) -> dict[str, Any]:
    raw_data: dict[str, Any] = {}
    if project_path.exists():
        raw_data = Config._load_toml(project_path)
    return {
        key: value for key, value in raw_data.items() if key in _PROJECT_CONFIG_KEYS
    }


def _resolve_project_catalog_aliases(
    *,
    global_data: dict[str, Any],
    projects: list[Any],
) -> tuple[dict[str, str], dict[str, dict[str, Any]]]:
    existing_projects = global_data.get("projects")
    if not isinstance(existing_projects, dict):
        existing_projects = {}

    project_catalog = global_data.get("project_catalog")
    if not isinstance(project_catalog, dict):
        project_catalog = {}

    merged_projects, project_alias_by_platform_id = _build_project_aliases(
        projects,
        existing=existing_projects,
    )
    global_data["projects"] = merged_projects

    typed_catalog: dict[str, dict[str, Any]] = {}
    for alias in merged_projects:
        entry = project_catalog.get(alias)
        if isinstance(entry, dict):
            typed_catalog[alias] = dict(entry)

    global_data["project_catalog"] = typed_catalog
    return project_alias_by_platform_id, typed_catalog


def _populate_project_catalog(
    *,
    project_catalog: dict[str, dict[str, Any]],
    projects: list[Any],
    browser_api_module,  # noqa: ANN001
    session,  # noqa: ANN001
    workspace_id: str,
    force: bool,
    project_alias_by_platform_id: dict[str, str],
) -> None:
    """Populate per-project metadata kept at account level.

    Only two fields survive:

    * ``name``  — the platform's display name (for reference; redundant with
      the ``[projects]`` key but useful if a project gets renamed).
    * ``path``  — the ``<topic>`` segment of the shared-storage path
      (``/inspire/<tier>/project/<topic>/<path_user>/...``). Derived from the
      platform file browser's project directory catalog; agents need it to
      construct remote paths for new repos under this project.
    * ``path_user`` — the platform filesystem personal-directory segment. This
      is not necessarily the login username, e.g. login ``253108120116`` may
      map to ``tongjingqi-CZXS25110029`` on shared storage.

    Notably *not* stored: full ``workdir``. It is derivable from ``path`` +
    the storage tier + ``path_user``, and caching it made the account config
    noisy.
    """
    directory_cache: dict[str, list[Any]] = {}

    def _directories_for_workspace(project_workspace_id: str) -> list[Any]:
        if project_workspace_id not in directory_cache:
            try:
                directory_cache[project_workspace_id] = (
                    browser_api_module.list_project_file_directories(
                        workspace_id=project_workspace_id,
                        session=session,
                    )
                    or []
                )
            except Exception:
                directory_cache[project_workspace_id] = []
        return directory_cache[project_workspace_id]

    for project in projects:
        project_id = str(getattr(project, "project_id", "") or "").strip()
        if not project_id:
            continue

        alias = project_alias_by_platform_id.get(project_id)
        if not alias:
            continue
        entry = project_catalog.setdefault(alias, {})
        name = str(getattr(project, "name", "") or "").strip()
        if name:
            entry["name"] = name

        project_workspace_id = str(getattr(project, "workspace_id", "") or workspace_id).strip()
        existing_path = str(entry.get("path") or "").strip()
        existing_path_user = str(entry.get("path_user") or "").strip()
        if existing_path and existing_path_user and not force:
            continue

        topic, path_user = _match_project_file_directory(
            project=project,
            entry=entry,
            directories=_directories_for_workspace(project_workspace_id),
        )
        if topic:
            entry["path"] = topic
        if path_user:
            entry["path_user"] = path_user


def _match_project_file_directory(
    *,
    project: Any,
    entry: dict[str, Any],
    directories: list[Any],
) -> tuple[str | None, str | None]:
    parsed: list[tuple[str, str | None, str]] = []
    for item in directories:
        directory = str(getattr(item, "directory", "") or "").strip()
        topic, path_user = _parse_project_workdir(directory)
        if not topic:
            continue
        parsed.append((topic, path_user, str(getattr(item, "name", "") or "").strip()))

    if not parsed:
        return None, None

    en_name = str(getattr(project, "en_name", "") or "").strip()
    existing_path = str(entry.get("path") or "").strip()
    project_name = str(getattr(project, "name", "") or "").strip()

    matches: list[tuple[str, str | None, str]] = []
    for topic_hint in (en_name, existing_path):
        if topic_hint:
            matches = [item for item in parsed if item[0] == topic_hint]
            if matches:
                break
    if not matches and project_name:
        name_matches = [item for item in parsed if item[2] == project_name]
        topics = {item[0] for item in name_matches}
        if len(topics) == 1:
            matches = name_matches

    if not matches:
        return None, None

    topic = matches[0][0]
    path_user = next((item[1] for item in matches if item[1]), None)
    return topic, path_user


def _parse_project_workdir(workdir: str) -> tuple[str | None, str | None]:
    """Parse ``/inspire/<tier>/project/<topic>/<path_user>/...``."""
    parts = [p for p in str(workdir or "").split("/") if p]
    try:
        idx = parts.index("project")
    except ValueError:
        return None, None

    topic = parts[idx + 1] if idx + 1 < len(parts) else None
    path_user = parts[idx + 2] if idx + 2 < len(parts) else None
    if path_user == "public":
        path_user = None
    return topic, path_user


def _persist_api_base_url(
    *,
    global_data: dict[str, Any],
    config: Config,
    session: Any | None = None,
) -> None:
    base_url = _usable_base_url(getattr(config, "base_url", ""))
    if not base_url and session is not None:
        base_url = _usable_base_url(getattr(session, "base_url", ""))
    if base_url:
        api_section = global_data.get("api")
        if not isinstance(api_section, dict):
            api_section = {}
            global_data["api"] = api_section
        if not _usable_base_url(api_section.get("base_url")):
            api_section["base_url"] = base_url


def _discover_compute_groups(
    *,
    browser_api_module,  # noqa: ANN001
    session,  # noqa: ANN001
    workspace_id: str,
) -> list[dict[str, Any]]:
    compute_groups: list[dict[str, Any]] = []
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

    workspace_names = getattr(session, "all_workspace_names", None)
    workspace_name = ""
    if isinstance(workspace_names, dict):
        workspace_name = str(workspace_names.get(workspace_id) or "").strip()

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

        cg_entry: dict[str, Any] = {"name": name}
        gpu_type = str(gpu_types.get(group_id, "") or "").strip()
        if gpu_type:
            cg_entry["gpu_type"] = gpu_type
        if location:
            cg_entry["location"] = location
        if workspace_name:
            cg_entry["workspace_names"] = [workspace_name]
        compute_groups.append(cg_entry)
    return compute_groups


def _persist_compute_groups(
    *,
    global_data: dict[str, Any],
    compute_groups: list[dict[str, Any]],
    workspace_names_by_id: dict[str, str] | None = None,
    failed_workspace_names: set[str] | None = None,
) -> None:
    existing_compute_groups = global_data.get("compute_groups")
    if not isinstance(existing_compute_groups, list):
        existing_compute_groups = []
    normalized_existing = _merge_compute_groups(
        [],
        existing_compute_groups,
        workspace_names_by_id=workspace_names_by_id,
    )
    failed_workspace_names = failed_workspace_names or set()
    preserved = [
        item
        for item in normalized_existing
        if failed_workspace_names.intersection(item.get("workspace_names") or [])
    ]
    merged = _merge_compute_groups(
        preserved,
        compute_groups,
        workspace_names_by_id=workspace_names_by_id,
    )
    if merged:
        global_data["compute_groups"] = merged
    else:
        global_data.pop("compute_groups", None)


def _persist_prompted_credentials(
    *,
    global_data: dict[str, Any],
    prompted_credentials: tuple[str, str, str] | None,
) -> None:
    if not prompted_credentials:
        return
    prompted_username, prompted_password, prompted_base_url = prompted_credentials
    auth = global_data.get("auth")
    if not isinstance(auth, dict):
        auth = {}
        global_data["auth"] = auth
    auth["username"] = prompted_username
    auth["password"] = prompted_password
    api = global_data.get("api")
    if not isinstance(api, dict):
        api = {}
        global_data["api"] = api
    api["base_url"] = prompted_base_url


def _persist_cached_session_identity(
    *,
    global_data: dict[str, Any],
    session: Any,
) -> None:
    username = _usable_username(getattr(session, "login_username", ""))
    if not username:
        return

    auth = global_data.get("auth")
    if not isinstance(auth, dict):
        auth = {}
        global_data["auth"] = auth
    if not _usable_username(auth.get("username")):
        auth["username"] = username


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
# with the path-friendly tier first so `ssd` is suggested when the catalog
# workdir cannot be parsed. See `references/paths.md` for the
# storage-tier guidance behind these choices.
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


def _default_path_aliases(
    *,
    project_topic: str,
    selected_tier: str,
    path_user: str | None = None,
) -> dict[str, str]:
    user = str(path_user or "").strip().strip("/")
    topic = str(project_topic or "").strip().strip("/")
    if not topic:
        return {}

    tier_names = set(_STORAGE_TIER_NAMES)
    if selected_tier not in tier_names:
        selected_tier = "ssd"

    aliases: dict[str, str] = {}
    for tier in _STORAGE_TIER_NAMES:
        public = f"/inspire/{tier}/project/{topic}/public/"
        aliases[f"{tier}.public"] = public
        if user:
            me = f"/inspire/{tier}/project/{topic}/{user}/"
            global_me = f"/inspire/{tier}/global_user/{user}/"
            aliases[f"{tier}.me"] = me
            aliases[f"{tier}.global-me"] = global_me
        if tier == selected_tier:
            aliases["public"] = public
            if user:
                aliases["me"] = me
                aliases["global-me"] = global_me
    return aliases


def _persist_default_path_aliases(
    *,
    project_data: dict[str, Any],
    selected_alias: str,
    project_catalog: dict[str, dict[str, Any]],
    selected_tier: str,
    force: bool,
) -> None:
    entry = project_catalog.get(selected_alias, {})
    project_topic = str(entry.get("path") or "").strip()
    path_user = str(entry.get("path_user") or "").strip()
    if not project_topic:
        return

    defaults = _default_path_aliases(
        project_topic=project_topic,
        selected_tier=selected_tier,
        path_user=path_user,
    )
    if not defaults:
        return

    existing = project_data.get("path_aliases")
    if not isinstance(existing, dict):
        existing = {}
        project_data["path_aliases"] = existing
    for alias, path in defaults.items():
        if force or not str(existing.get(alias) or "").strip():
            existing[alias] = path


def _prompt_storage_tier(current_path: str) -> str:
    """Ask the user to pick an Inspire storage tier.

    The file browser catalog commonly includes an ``/inspire/hdd/...`` path,
    and HDD filesets are often 100% full on busy projects, so that default is
    frequently wrong. Strategy:

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
        suggested = detected if detected is not None else "ssd"
    click.echo("")
    click.echo("Remote path storage tier — choose what the `me` alias should point to:")
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


def _select_default_path_alias_tier(*, force: bool, non_interactive: bool = False) -> str:
    if force or non_interactive:
        return "ssd"
    return _prompt_storage_tier("")


def _write_discovered_project_config(
    *,
    project_path: Path,
    project_data: dict[str, Any],
    selected_alias: str,
    project_catalog: dict[str, dict[str, Any]],
    force: bool,
    selected_tier: str,
) -> None:
    # Build [context] from the discovered state and copy defaults that the
    # helpers may have stashed under top-level keys. Identity (username /
    # account) is NOT written — it belongs to the active account's config.
    context = _get_or_create_dict_table(container=project_data, key="context")
    context["project"] = selected_alias
    _persist_default_path_aliases(
        project_data=project_data,
        selected_alias=selected_alias,
        project_catalog=project_catalog,
        selected_tier=selected_tier,
        force=force,
    )

    project_path.parent.mkdir(parents=True, exist_ok=True)
    # Always UTF-8: TOML spec mandates UTF-8, and on Windows the default
    # locale (GBK / cp936 on Chinese Windows) would otherwise corrupt
    # non-ASCII paths/names — see issue #2.
    project_path.write_text(_toml_dumps(project_data), encoding="utf-8")


def _persist_discovery_catalog(request: _DiscoveryPersistRequest) -> None:
    force = request.force
    scope = request.scope
    config = request.config
    browser_api_module = request.browser_api_module
    session = request.session
    workspace_id = request.workspace_id
    projects = request.projects
    selected_project = request.selected_project
    prompted_credentials = request.prompted_credentials
    non_interactive = request.non_interactive
    verbose = request.verbose
    global_path = Config.writable_config_path()
    if global_path is None:
        raise click.ClickException("No active account configured. Run `inspire account add` first.")
    project_path = _project_config_write_path()
    if not _confirm_discovery_writes(
        force=force,
        scope=scope,
        global_path=global_path,
        project_path=project_path,
        non_interactive=non_interactive,
    ):
        return

    _progress(verbose, "Preparing account catalog update...")
    global_data = _load_discovery_global_state(
        global_path=global_path,
    )
    if scope == "project":
        project_data = _load_discovery_project_state(
            project_path=project_path,
        )
    else:
        project_data = {}
    project_alias_by_platform_id, project_catalog = _resolve_project_catalog_aliases(
        global_data=global_data,
        projects=projects,
    )
    _progress(verbose, f"Discovering storage paths for {len(projects)} project(s)...")
    _populate_project_catalog(
        project_catalog=project_catalog,
        projects=projects,
        browser_api_module=browser_api_module,
        session=session,
        workspace_id=workspace_id,
        force=force,
        project_alias_by_platform_id=project_alias_by_platform_id,
    )
    _persist_api_base_url(
        global_data=global_data,
        config=config,
        session=session,
    )
    all_ws_ids: set[str] = {workspace_id}
    for ws_id in list(session.all_workspace_ids or []):
        ws_str = str(ws_id or "").strip()
        if ws_str:
            all_ws_ids.add(ws_str)

    workspace_ids = sorted(all_ws_ids)
    compute_groups: list[dict[str, Any]] = []
    failed_workspace_ids: set[str] = set()
    if workspace_ids:
        _progress(
            verbose,
            f"Discovering compute groups across {len(workspace_ids)} workspace(s)..."
        )
        max_workers = min(len(workspace_ids), 6)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
            future_map = {
                pool.submit(
                    _discover_compute_groups,
                    browser_api_module=browser_api_module,
                    session=session,
                    workspace_id=ws_id,
                ): ws_id
                for ws_id in workspace_ids
            }
            workspace_results: dict[str, list[dict[str, Any]]] = {}
            for future in concurrent.futures.as_completed(future_map):
                ws_id = future_map[future]
                try:
                    workspace_results[ws_id] = future.result()
                except Exception:
                    workspace_results[ws_id] = []
                    failed_workspace_ids.add(ws_id)

        for ws_id in workspace_ids:
            compute_groups.extend(workspace_results.get(ws_id, []))
    workspace_names_by_id = dict(
        getattr(session, "all_workspace_names", None) or {}
    )
    _persist_compute_groups(
        global_data=global_data,
        compute_groups=compute_groups,
        workspace_names_by_id=workspace_names_by_id,
        failed_workspace_names={
            workspace_names_by_id[workspace_id]
            for workspace_id in failed_workspace_ids
            if workspace_id in workspace_names_by_id
        },
    )

    _persist_prompted_credentials(
        global_data=global_data,
        prompted_credentials=prompted_credentials,
    )
    if not prompted_credentials:
        _persist_cached_session_identity(
            global_data=global_data,
            session=session,
        )
    catalog = global_data.get("project_catalog")
    if isinstance(catalog, dict):
        for alias, entry in list(catalog.items()):
            if not isinstance(entry, dict):
                catalog.pop(alias, None)
                continue
            for key in list(entry.keys()):
                if key not in {"name", "path", "path_user"}:
                    entry.pop(key, None)
            if not entry:
                catalog.pop(alias, None)
        if not catalog:
            global_data.pop("project_catalog", None)

    _progress(verbose, "Preparing default remote path aliases...")
    selected_tier = _select_default_path_alias_tier(
        force=force,
        non_interactive=non_interactive,
    )
    selected_alias = project_alias_by_platform_id.get(
        str(getattr(selected_project, "project_id", "") or "").strip()
    )
    if not selected_alias:
        selected_alias = _slugify_alias(
            str(getattr(selected_project, "name", "") or "")
        ) or "default"
    _persist_default_path_aliases(
        project_data=global_data,
        selected_alias=selected_alias,
        project_catalog=project_catalog,
        force=force,
        selected_tier=selected_tier,
    )

    _progress(verbose, "Writing configuration files...")
    global_path.parent.mkdir(parents=True, exist_ok=True)
    # Always UTF-8 — see project_path.write_text above for the Windows
    # GBK story.
    global_path.write_text(_toml_dumps(global_data), encoding="utf-8")
    if prompted_credentials:
        try:
            global_path.chmod(0o600)
        except OSError:
            pass

    if scope == "project":
        _write_discovered_project_config(
            project_path=project_path,
            project_data=project_data,
            selected_alias=selected_alias,
            project_catalog=project_catalog,
            force=force,
            selected_tier=selected_tier,
        )

    _ensure_ssh_key(non_interactive=non_interactive)


def _init_discover_mode(
    force: bool,
    *,
    scope: str = "global",
    cli_username: str | None = None,
    cli_base_url: str | None = None,
    cli_select_project: str | None = None,
    non_interactive: bool = False,
    verbose: bool = False,
) -> None:
    """Initialize per-account catalogs by discovering projects and compute groups."""
    from inspire.platform.web import browser_api as browser_api_module
    from inspire.platform.web import session as web_session_module
    from inspire.platform.web.session.browser_client import _close_browser_client
    from inspire.platform.web.session import DEFAULT_WORKSPACE_ID

    config, _ = Config.from_files_and_env(require_credentials=False)
    session, prompted_credentials, account_key, workspace_id = _resolve_discover_runtime(
        config=config,
        web_session_module=web_session_module,
        default_workspace_id=DEFAULT_WORKSPACE_ID,
        cli_username=cli_username,
        cli_base_url=cli_base_url,
        non_interactive=non_interactive,
        verbose=verbose,
    )

    _progress(verbose, "Discovering account catalog...")
    _progress(verbose, f"Account: {account_key}")
    _progress(verbose, "Discovering projects across accessible workspaces...")
    projects, selected_project = _load_projects_for_discovery(
        browser_api_module=browser_api_module,
        session=session,
        workspace_id=workspace_id,
        force=force,
        requested_project=cli_select_project,
        non_interactive=non_interactive,
    )
    _progress(verbose, f"Discovered {len(projects)} project(s).")
    try:
        _persist_discovery_catalog(
            _DiscoveryPersistRequest(
                force=force,
                scope=scope,
                config=config,
                browser_api_module=browser_api_module,
                session=session,
                workspace_id=workspace_id,
                projects=projects,
                selected_project=selected_project,
                prompted_credentials=prompted_credentials,
                non_interactive=non_interactive,
                verbose=verbose,
            )
        )
    finally:
        _close_browser_client()
