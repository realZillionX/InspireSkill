"""Image lookups must read the workspace they were asked about.

Images are stored per workspace: every `ListImages` request carries
`registry_hint: {workspace_id}`. Reading that id off the session instead of
taking it as an argument silently addresses a different registry — and on a
real account it did: the session's active workspace held an empty registry
while all 67 custom images lived in another, so `--image <私有镜像>` could not
be resolved by any create command and `image list` reported none at all.

These lock the argument in at both layers: the platform wrapper honours it, and
every CLI resolver that reaches for a catalogue passes one down.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Any

import pytest

from inspire.platform.web.browser_api import images as images_module


class _FakeSession:
    """A session whose own workspace is *not* the one under test."""

    workspace_id = "ws-session-default"
    storage_state: dict[str, Any] = {"cookies": [{"name": "x", "value": "y"}]}


def _capture_registry_hint(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    def fake_image_v2(session, action: str, body=None, *, timeout: int = 30):  # noqa: ANN001
        seen["action"] = action
        seen["registry_hint"] = (body or {}).get("filter", {}).get("registry_hint")
        return {"images": []}

    monkeypatch.setattr(images_module, "_image_v2", fake_image_v2)
    return seen


def test_lister_addresses_the_requested_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _capture_registry_hint(monkeypatch)

    images_module.list_images_by_source(
        source="private",
        session=_FakeSession(),
        workspace_id="ws-elsewhere",
    )

    assert seen["action"] == "ListImages"
    assert seen["registry_hint"] == {"workspace_id": "ws-elsewhere"}


def test_lister_falls_back_to_the_session_only_when_not_asked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Omitting the id is still allowed; it just means "wherever I am"."""
    seen = _capture_registry_hint(monkeypatch)

    images_module.list_images_by_source(source="private", session=_FakeSession())

    assert seen["registry_hint"] == {"workspace_id": "ws-session-default"}


_RESOLVERS = (
    ("inspire/cli/utils/image_resolver.py", "resolve_image_url"),
    ("inspire/cli/commands/serving/serving_commands.py", "_resolve_image_for_create"),
    ("inspire/cli/commands/ray/ray_commands.py", "_resolve_image_id"),
)


@pytest.mark.parametrize(("module_path", "func_name"), _RESOLVERS)
def test_cli_image_resolvers_take_a_workspace(module_path: str, func_name: str) -> None:
    """A resolver that only takes `session` will silently read the wrong registry."""
    source = (Path(__file__).resolve().parents[1] / module_path).read_text(encoding="utf-8")
    tree = ast.parse(source, module_path)
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == func_name
    )
    names = {arg.arg for arg in func.args.args} | {arg.arg for arg in func.args.kwonlyargs}

    assert "workspace_id" in names, f"{func_name} must take the registry workspace explicitly"


def test_every_lister_call_site_names_the_workspace() -> None:
    """No caller may fall through to the session's workspace by accident.

    The wrapper keeps its default so a genuinely session-scoped caller stays
    possible, which means only a sweep can catch a call site that forgot.
    """
    root = Path(__file__).resolve().parents[1] / "inspire"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "images.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name != "list_images_by_source":
                continue
            if not any(kw.arg == "workspace_id" for kw in node.keywords):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")

    assert not offenders, (
        "these call `list_images_by_source` without naming a workspace, so they read "
        f"whatever registry the session happens to point at: {offenders}"
    )


def test_lister_signature_documents_the_argument() -> None:
    sig = inspect.signature(images_module.list_images_by_source)
    assert sig.parameters["workspace_id"].kind is inspect.Parameter.KEYWORD_ONLY
