"""Discovery mode: discover workspaces, projects, compute groups, and paths."""

from __future__ import annotations

import concurrent.futures
from copy import deepcopy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click

from inspire.cli.utils.raw_ids import scrub_raw_ids
from inspire.config import (
    CONFIG_FILENAME,
    PROJECT_CONFIG_DIR,
    Config,
)
from .toml_helpers import _toml_dumps

from inspire.platform.web.browser_api.core import _set_base_url

_CATALOG_DROP_FIELDS = frozenset(
    {
        "id",
        "alias",
        "workspace_id",
    }
)


@dataclass(frozen=True)
class _DiscoveryPersistRequest:
    force: bool
    config: Config
    browser_api_module: Any
    session: Any
    account_key: str
    workspace_id: str
    projects: list[Any]
    selected_project: Any
    prompted_credentials: tuple[str, str, str] | None


def _slugify_alias(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = text.strip("-")
    return text


def _workspace_label_for_output(config: Config, session: Any, workspace_id: str) -> str:
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
    config: Config,
    session: Any,
    workspace_errors: list[tuple[str, str]],
) -> str:
    sample = ", ".join(
        f"{_workspace_label_for_output(config, session, ws)}: {scrub_raw_ids(msg)}"
        for ws, msg in workspace_errors[:3]
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


def _ensure_playwright_browser() -> None:
    """Check that the local browser runtime is installed; offer to install it."""
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
    click.echo("A local browser runtime is required for platform login (one-time ~150 MB download).")
    if not click.confirm("Install Chromium now?", default=True):
        click.echo("Cannot proceed without a browser for platform login.")
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
    projects: list[Any],
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

        # Use the platform name directly — no slugify / no short raw-ID alias.
        key = name or _make_unique_alias("project", set(discovered_map))
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
                "Could not detect a real workspace_id from the authenticated session. "
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
    config: Config,
    browser_api_module,  # noqa: ANN001
    session,  # noqa: ANN001
    workspace_id: str,
    force: bool,
    requested_project: str | None = None,
) -> tuple[list[Any], Any]:
    projects, workspace_errors = _collect_discovery_projects(
        browser_api_module=browser_api_module,
        session=session,
        workspace_id=workspace_id,
    )

    if not projects:
        if workspace_errors:
            sample = _workspace_error_sample(config, session, workspace_errors)
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
        sample = _workspace_error_sample(config, session, workspace_errors)
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
        if rq.startswith("project-"):
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

    if force:
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
                "Multiple projects available — no project is selected implicitly. "
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
            f"Select project for this repository (1-{len(projects)})",
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

    for key in ("train_job_workdir",):
        if str(project_account_section.get(key) or "").strip():
            continue
        value = str(global_account_section.get(key) or "").strip()
        if value:
            project_account_section[key] = value


def _resolve_project_catalog_aliases(
    *,
    project_data: dict[str, Any],
    project_account_section: dict[str, Any],
    projects: list[Any],
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
    projects: list[Any],
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

    Notably *not* stored: full ``workdir``. It is derivable from ``path`` +
    the storage tier + the user, and caching it made the account config noisy.
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

    Older helpers put account-wide catalogs on the project side so a single-repo
    user could operate from one file. Under the per-account layout those are
    account-wide state, so copy them here before the project-config stripper
    removes them.
    """
    compute_groups = project_data.get("compute_groups")
    if isinstance(compute_groups, list) and compute_groups:
        global_data["compute_groups"] = compute_groups

    projects = project_data.get("projects")
    if isinstance(projects, dict) and projects:
        merged_proj = dict(global_data.get("projects") or {})
        merged_proj.update({str(k): str(v) for k, v in projects.items()})
        global_data["projects"] = merged_proj


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
# with the path-friendly tier first so `ssd` is suggested when the catalog
# workdir cannot be parsed. See `references/resources-and-paths.md` for the
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
    account_key: str,
    project_topic: str,
    selected_tier: str,
) -> dict[str, str]:
    user = str(account_key or "").strip().strip("/")
    topic = str(project_topic or "").strip().strip("/")
    if not user or not topic:
        return {}

    tier_names = set(_STORAGE_TIER_NAMES)
    if selected_tier not in tier_names:
        selected_tier = "ssd"

    aliases: dict[str, str] = {}
    for tier in _STORAGE_TIER_NAMES:
        me = f"/inspire/{tier}/project/{topic}/{user}/"
        public = f"/inspire/{tier}/project/{topic}/public/"
        global_me = f"/inspire/{tier}/global_user/{user}/"
        aliases[f"{tier}.me"] = me
        aliases[f"{tier}.public"] = public
        aliases[f"{tier}.global-me"] = global_me
        if tier == selected_tier:
            aliases["me"] = me
            aliases["public"] = public
            aliases["global-me"] = global_me
    return aliases


def _persist_default_path_aliases(
    *,
    project_data: dict[str, Any],
    account_key: str,
    selected_project: Any,
    project_catalog: dict[str, dict[str, Any]],
    selected_tier: str,
    force: bool,
) -> None:
    project_id = str(getattr(selected_project, "project_id", "") or "").strip()
    entry = project_catalog.get(project_id, {})
    project_topic = str(entry.get("path") or "").strip()
    if not project_topic:
        return

    defaults = _default_path_aliases(
        account_key=account_key,
        project_topic=project_topic,
        selected_tier=selected_tier,
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


def _select_default_path_alias_tier(*, force: bool) -> str:
    if force:
        return "ssd"
    return _prompt_storage_tier("")


_PROJECT_CONFIG_DISALLOWED_SECTIONS = (
    "accounts",  # legacy catalog nesting
    "auth",  # identity — belongs to account layer
    "api",  # account-wide
    "proxy",  # account-wide
    "workspaces",
    "projects",  # account-wide alias map
    "project_catalog",  # account-wide per-project metadata
    "account",  # account-level workdir
    "compute_groups",  # account-wide (array of tables)
)


def _strip_account_level_from_project(project_data: dict[str, Any]) -> None:
    """Enforce the project-config contract for per-repository sections.

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

    section.pop("workspaces", None)

    # Array-of-tables and dict sections move verbatim to the top level.
    for key in ("projects", "project_catalog", "compute_groups"):
        if key in section:
            global_data[key] = section.pop(key)

    # Passwords live in [auth] at the top level.
    password = section.pop("password", None)
    if password:
        auth_section = global_data.setdefault("auth", {})
        if isinstance(auth_section, dict):
            auth_section["password"] = password

    # Account-level train_job_workdir remains under [account].
    for key in ("train_job_workdir",):
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
    selected_project: Any,
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
        account_key=account_key,
        selected_project=selected_project,
        project_catalog=project_catalog,
        selected_tier=selected_tier,
        force=force,
    )

    # Strip everything that isn't per-repo state — a single account may use
    # many repos, and every one duplicating the workspace/compute_groups
    # catalog is both noisy and divergent-on-refresh.
    _strip_account_level_from_project(project_data)

    project_data.pop("defaults", None)
    for sub_key in ("job", "notebook"):
        project_data.pop(sub_key, None)

    project_path.parent.mkdir(parents=True, exist_ok=True)
    # Always UTF-8: TOML spec mandates UTF-8, and on Windows the default
    # locale (GBK / cp936 on Chinese Windows) would otherwise corrupt
    # non-ASCII paths/names — see issue #2.
    project_path.write_text(_toml_dumps(project_data), encoding="utf-8")


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
    prompted_credentials = request.prompted_credentials
    global_path = Config.writable_config_path()
    if global_path is None:
        raise click.ClickException("No active account configured. Run `inspire account add` first.")
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
    _persist_compute_groups(
        project_data=project_data,
        project_account_section=project_account_section,
        global_data=global_data,
        global_account_section=account_section,
        compute_groups=compute_groups,
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
    # keeps only ``{name, path}`` (see ``_populate_project_catalog``).
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
            for key in list(entry.keys()):
                if key not in {"name", "path"}:
                    entry.pop(key, None)
            if not entry:
                catalog.pop(project_id, None)
        if not catalog:
            global_data.pop("project_catalog", None)

    global_path.parent.mkdir(parents=True, exist_ok=True)
    # Always UTF-8 — see project_path.write_text above for the Windows
    # GBK story.
    global_path.write_text(_toml_dumps(global_data), encoding="utf-8")
    if prompted_credentials:
        try:
            global_path.chmod(0o600)
        except OSError:
            pass

    selected_alias = alias_for_id.get(selected_project.project_id)
    if not selected_alias:
        selected_alias = _slugify_alias(selected_project.name) or "default"
    selected_tier = _select_default_path_alias_tier(force=force)
    _write_discovered_project_config(
        project_path=project_path,
        project_data=project_data,
        config=config,
        account_key=account_key,
        selected_alias=selected_alias,
        selected_project=selected_project,
        project_catalog=project_catalog,
        force=force,
        selected_tier=selected_tier,
    )

    _ensure_ssh_key()
    _print_discover_completion(
        global_path=global_path,
        project_path=project_path,
        prompted_credentials=prompted_credentials,
    )


def _init_discover_mode(
    force: bool,
    *,
    cli_username: str | None = None,
    cli_base_url: str | None = None,
    cli_select_project: str | None = None,
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
    )

    click.echo(click.style("Discovering account catalog...", bold=True))
    click.echo(f"Account: {account_key}")
    projects, selected_project = _load_projects_for_discovery(
        config=config,
        browser_api_module=browser_api_module,
        session=session,
        workspace_id=workspace_id,
        force=force,
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
                prompted_credentials=prompted_credentials,
            )
        )
    finally:
        _close_browser_client()
