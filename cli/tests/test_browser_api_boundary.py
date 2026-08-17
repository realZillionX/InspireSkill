"""Two invariants: the client is entirely on v2, and platform paths live in one place.

The v1 -> v2 migration was audited by sweeping
`inspire/platform/web/browser_api/`, and three call sites were missed because
they lived in the command layer instead: two metrics resolvers re-issuing
detail requests that already had migrated wrappers, and a notebook lookup
fetching the current user directly. Each kept talking to `/api/v1` long after
its domain had moved.

Nothing calls `/api/v1` any more, so the first test holds that at zero rather
than allowlisting the survivors. The second keeps request construction inside
`browser_api/` so the next sweep is one directory.

Both parse the AST, so comments and docstrings are not matches -- only real
string literals. Prose is deliberately exempt: a board's `url` field can still
arrive as a v1 path (see `browser-api.md`), and saying so must not fail CI.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "inspire"
_BROWSER_API = _SOURCE_ROOT / "platform" / "web" / "browser_api"

# Modules outside browser_api/ that legitimately name a platform path, with the
# reason each is allowed. Anything else is a bug: build the request in
# browser_api/ and call that wrapper instead.
_ALLOWED: dict[str, str] = {
    "cli/utils/job_shell.py": (
        "the four instance PTY sockets: WebSocket URLs, not JSON requests, so "
        "there is no wrapper shape for them to take"
    ),
    "platform/web/session/auth.py": (
        "session bootstrap: the authentication probe and workspace discovery "
        "run before any browser_api wrapper can be used"
    ),
}


def _relative(path: Path) -> str:
    return path.relative_to(_SOURCE_ROOT).as_posix()


def _docstring_nodes(tree: ast.Module) -> set[int]:
    """Ids of string constants that are docstrings, so prose is not flagged."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None)
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                ids.add(id(first.value))
    return ids


def _literals(path: Path, needle: str) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    docstrings = _docstring_nodes(tree)
    found: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings:
            continue
        if needle in node.value:
            found.append(f"line {node.lineno}: hardcodes {node.value!r}")

    return found


def test_no_v1_paths_remain() -> None:
    """The client is entirely on `/api/v2`, including login."""
    offenders = {
        _relative(path): hits
        for path in sorted(_SOURCE_ROOT.rglob("*.py"))
        if (hits := _literals(path, "/api/v1"))
    }

    assert not offenders, (
        "`/api/v1` is gone from this client -- every endpoint it used has a "
        "measured v2 counterpart.\n"
        + "\n".join(
            f"  {rel}\n" + "\n".join(f"    {hit}" for hit in hits)
            for rel, hits in offenders.items()
        )
        + "\n\nIf the platform grows something that only exists on v1, probe it by "
        "`browser-api.md` §7 first; an Action missing from discovery is not an "
        "Action that does not exist."
    )


def test_platform_paths_are_confined_to_browser_api() -> None:
    offenders: dict[str, list[str]] = {}

    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        if _BROWSER_API in path.parents or path == _BROWSER_API:
            continue
        rel = _relative(path)
        if rel in _ALLOWED:
            continue
        hits = _literals(path, "/api/v2")
        if hits:
            offenders[rel] = hits

    assert not offenders, (
        "Platform paths must be built inside browser_api/.\n"
        + "\n".join(
            f"  {rel}\n" + "\n".join(f"    {hit}" for hit in hits)
            for rel, hits in offenders.items()
        )
        + "\n\nMove the request into a browser_api wrapper and call that, or -- if it "
        "is not a JSON request at all and cannot live there -- add the module to "
        "_ALLOWED in this test with the reason."
    )


def test_allowlist_has_no_stale_entries() -> None:
    """An allowlisted module that no longer names a platform path should be dropped."""
    stale = [
        rel
        for rel in _ALLOWED
        if (_SOURCE_ROOT / rel).exists() and not _literals(_SOURCE_ROOT / rel, "/api/v2")
    ]
    assert not stale, (
        "These modules no longer reference a platform path; remove them from "
        f"_ALLOWED: {stale}"
    )


def test_allowlist_paths_exist() -> None:
    missing = [rel for rel in _ALLOWED if not (_SOURCE_ROOT / rel).exists()]
    assert not missing, f"_ALLOWED names modules that no longer exist: {missing}"


def test_request_json_clamps_a_page_size_above_the_gateway_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Above 5000 the gateway answers `page or page_size too large`."""
    from inspire.platform.web.browser_api import core as core_module

    sent: dict = {}

    def _fake(session, method, url, *, headers=None, body=None, timeout=30):  # noqa: ANN001
        sent["body"] = body
        return {"Result": {}}

    monkeypatch.setattr(core_module, "request_json", _fake)
    core_module._request_json(
        object(), "POST", "/api/v2/hpc?Action=ListJobs", referer="r", body={"page_size": 10000}
    )

    assert sent["body"]["page_size"] == 5000


def test_request_json_leaves_page_size_minus_one_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`-1` means every row and the gateway honours it."""
    from inspire.platform.web.browser_api import core as core_module

    sent: dict = {}

    def _fake(session, method, url, *, headers=None, body=None, timeout=30):  # noqa: ANN001
        sent["body"] = body
        return {"Result": {}}

    monkeypatch.setattr(core_module, "request_json", _fake)
    for requested in (-1, 100, 5000):
        core_module._request_json(
            object(), "POST", "/api/v2/x?Action=Y", referer="r", body={"page_size": requested}
        )
        assert sent["body"]["page_size"] == requested


def test_request_json_does_not_mutate_the_caller_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from inspire.platform.web.browser_api import core as core_module

    def _fake(session, method, url, *, headers=None, body=None, timeout=30):  # noqa: ANN001
        return {"Result": {}}

    monkeypatch.setattr(core_module, "request_json", _fake)
    body = {"page_size": 9999, "workspace_id": "ws-1"}
    core_module._request_json(object(), "POST", "/api/v2/x?Action=Y", referer="r", body=body)

    assert body["page_size"] == 9999
