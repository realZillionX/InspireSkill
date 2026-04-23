"""Per-account config layer — the sole source of identity for the CLI.

Identity and account-wide settings live at::

    ~/.inspire/accounts/<current>/config.toml

Sections: ``[auth]``, ``[api]``, ``[proxy]``, ``[ssh]``, ``[workspaces]``,
``[projects]``, ``[defaults]``, ``[[compute_groups]]``, ``[remote_env]``.

**No ``[accounts."<user>"]`` nesting, no ``[context].account`` pointer.**
One account = one file. Without an active account
(``~/.inspire/current`` absent or pointing at a missing directory),
this layer is a no-op and the caller is free to continue — callers that
require credentials will get a clear "run 'inspire account add'" error
from :func:`inspire.config.load_runtime._validate_required_config`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from inspire.config.models import SOURCE_GLOBAL, ConfigError
from inspire.config.toml import _flatten_toml, _load_toml, _toml_key_to_field

from .load_common import _apply_defaults_overrides, _parse_alias_map

# Keys whose *value* must differ per repository — a single account is used
# across many repos, each with its own remote workdir / Inspire project /
# GitHub repo binding. Putting these at account level silently shadows the
# correct project-level value, so we reject them outright. Other project-
# scope fields (workspace aliases, default images/priority, workflow names,
# etc.) *can* live at the account layer as defaults and are not policed here.
ACCOUNT_LAYER_DISALLOWED_KEYS = frozenset(
    {
        "paths.target_dir",
        "paths.log_pattern",
        "github.repo",
        "job.project_id",
        "job.workspace_id",
        "notebook.post_start",
    }
)


def _resolve_account_config_path() -> Path | None:
    """Return the active account's ``config.toml`` path, or ``None``.

    ``None`` means either no active account (``~/.inspire/current`` missing)
    or the active account has no config file yet (fresh ``account add``
    without running ``init``).
    """
    try:
        from inspire.accounts import account_config_path, current_account
    except ImportError:  # pragma: no cover - accounts module ships with the CLI
        return None
    name = current_account()
    if not name:
        return None
    path = account_config_path(name)
    return path if path.exists() else None


def _apply_account_layer(
    *,
    config_dict: dict[str, Any],
    sources: dict[str, str],
) -> Path | None:
    """Apply the active account's ``config.toml``.

    Returns the path that was read, or ``None`` if no account config applies.
    The source label is ``SOURCE_GLOBAL`` — this layer occupies the slot
    that the legacy global config used to fill; callers that inspect
    ``sources`` do not need to learn a new source label.
    """
    account_path = _resolve_account_config_path()
    if account_path is None:
        return None

    raw = _load_toml(account_path)

    # Guard against stray legacy sections copied into a per-account file.
    # ``[accounts."<user>"]`` and ``[context]`` have no meaning in the new
    # layout (one account = one file), so drop them rather than let the
    # legacy parsers surface confusing behaviour.
    raw.pop("accounts", None)
    raw.pop("context", None)

    # Reject per-repository keys anywhere in the file. A single account
    # spans many repos; these values must live in each repo's own
    # ./.inspire/config.toml or come from env vars.
    _reject_per_repo_keys(raw, account_path)

    compute_groups = raw.pop("compute_groups", [])
    remote_env = {str(k): str(v) for k, v in raw.pop("remote_env", {}).items()}

    defaults: dict[str, Any] = {}
    raw_defaults = raw.pop("defaults", {})
    if isinstance(raw_defaults, dict):
        defaults = raw_defaults

    projects = _parse_alias_map(raw.pop("projects", {}))

    workspaces: dict[str, str] = {}
    raw_workspaces = raw.get("workspaces") or {}
    if isinstance(raw_workspaces, dict):
        workspaces = {str(k): str(v) for k, v in raw_workspaces.items()}

    flat = _flatten_toml(raw)
    for toml_key, value in flat.items():
        field_name = _toml_key_to_field(toml_key)
        if field_name and field_name in config_dict:
            config_dict[field_name] = value
            sources[field_name] = SOURCE_GLOBAL

    if compute_groups:
        config_dict["compute_groups"] = compute_groups
        sources["compute_groups"] = SOURCE_GLOBAL
    if remote_env:
        config_dict["remote_env"] = remote_env
        sources["remote_env"] = SOURCE_GLOBAL
    if workspaces:
        config_dict["workspaces"] = workspaces
        sources["workspaces"] = SOURCE_GLOBAL
    if projects:
        config_dict["projects"] = projects
        sources["projects"] = SOURCE_GLOBAL

    _apply_defaults_overrides(
        defaults=defaults,
        config_dict=config_dict,
        sources=sources,
        source_name=SOURCE_GLOBAL,
    )
    return account_path


def _reject_per_repo_keys(raw: dict[str, Any], account_path: Path) -> None:
    flat = _flatten_toml(raw)
    offending = sorted(k for k in flat if k in ACCOUNT_LAYER_DISALLOWED_KEYS)
    if not offending:
        return
    raise ConfigError(
        f"Account config at {account_path} contains per-repository keys: "
        f"{', '.join(offending)}. These must live in the repo's own "
        "./.inspire/config.toml (run 'inspire init --discover' from inside "
        "the repo), not at the account level — a single account usually has "
        "many repos with different remote workdirs / Inspire projects / "
        "GitHub bindings, and placing per-repo values here silently shadows "
        "the correct ones."
    )


__all__ = [
    "ACCOUNT_LAYER_DISALLOWED_KEYS",
    "_apply_account_layer",
    "_resolve_account_config_path",
]
