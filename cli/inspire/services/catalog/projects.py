"""Project budget, detail and owner views."""

from __future__ import annotations

from inspire.services.utils.raw_ids import scrub_raw_ids
from inspire.platform.web import browser_api as browser_api_module
from inspire.services.utils.text import format_epoch


def _public_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return scrub_raw_ids(value).strip()


def _public_number(value: object) -> int | float | str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        text = scrub_raw_ids(value).strip()
        return text or None
    return None


def project_to_dict(proj: browser_api_module.ProjectInfo) -> dict:
    """Convert a ProjectInfo to the compact, name-only CLI representation."""
    view: dict[str, object] = {
        "name": scrub_raw_ids(proj.name),
        "priority": scrub_raw_ids(proj.priority_level or proj.priority_name),
        # Two different numbers, and the one this account is actually capped by
        # is the member one. They can differ by three orders of magnitude, so
        # publishing only one of them under the bare name `remaining_budget`
        # answered a different question than the one the reader asked.
        "my_remaining_budget": _public_number(proj.member_remain_budget),
        "project_remaining_budget": _public_number(proj.remain_budget),
    }
    return {key: value for key, value in view.items() if value not in ("", None, [])}


def _spent_number(value: object) -> int | float | str | None:
    """Read one field of the budget-usage record.

    Every value arrives thousands-separated (`"233,114.18"`), so the commas
    have to go before it is a number to anyone downstream.
    """
    if isinstance(value, str):
        return _public_number(value.replace(",", ""))
    return _public_number(value)


def project_detail_view(data: dict, usage: dict | None = None) -> dict[str, object]:
    owner_value = data.get("creator")
    owner: dict[str, object] = owner_value if isinstance(owner_value, dict) else {}
    spent: dict = usage if isinstance(usage, dict) else {}
    view: dict[str, object] = {
        "name": _public_text(data.get("name") or data.get("en_name")),
        "english_name": _public_text(data.get("en_name")),
        "description": _public_text(data.get("description")),
        "budget": _public_number(data.get("budget")),
        "remaining_budget": _public_number(data.get("remain_budget")),
        "spent_budget": _spent_number(spent.get("used")),
        "spent_on_training": _spent_number(spent.get("train")),
        "spent_on_inference": _spent_number(spent.get("inference")),
        "spent_on_storage": _spent_number(spent.get("storage")),
        "spent_on_private_workspace": _spent_number(spent.get("private_workspace")),
        "priority": _public_text(data.get("priority_name") or data.get("priority_level")),
        "created_at": format_epoch(data.get("created_at")) if data.get("created_at") else "",
        "creator": _public_text(owner.get("name")),
    }
    if view["english_name"] == view["name"]:
        view["english_name"] = ""
    return {key: value for key, value in view.items() if value not in ("", None)}


def owner_views(items: list[dict]) -> list[dict[str, str]]:
    owners: list[dict[str, str]] = []
    for item in items:
        name = _public_text(item.get("name"))
        if not name:
            continue
        owners.append({"name": name})
    return owners
