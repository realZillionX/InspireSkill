"""Platform requests must be built inside `browser_api/`, not scattered around.

The v1 -> v2 migration was audited by sweeping
`inspire/platform/web/browser_api/`, and three call sites were missed because
they lived in the command layer instead: two metrics resolvers re-issuing
detail requests that already had migrated wrappers, and a notebook lookup
fetching the current user directly. Each kept talking to `/api/v1` long after
its domain had moved.

This test makes that class of drift fail in CI rather than waiting for someone
to notice. It parses the AST, so comments and docstrings mentioning `/api/v1`
are not matches -- only real string literals and `_browser_api_path` calls are.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SOURCE_ROOT = Path(__file__).resolve().parents[1] / "inspire"
_BROWSER_API = _SOURCE_ROOT / "platform" / "web" / "browser_api"

# Modules outside browser_api/ that legitimately name a platform path, with the
# reason each is allowed. Anything else is a bug: build the request in
# browser_api/ and call that wrapper instead.
_ALLOWED: dict[str, str] = {
    "platform/web/session/auth.py": (
        "session bootstrap: logs in and discovers workspace routes before any "
        "browser_api wrapper can be used"
    ),
    "cli/utils/job_shell.py": (
        "`job shell` remote-command WebSocket; no v2 service exposes a shell, "
        "exec or terminal Action"
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


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    docstrings = _docstring_nodes(tree)
    found: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name == "_browser_api_path":
                found.append(f"line {node.lineno}: calls _browser_api_path()")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstrings:
                continue
            if "/api/v1" in node.value:
                found.append(f"line {node.lineno}: hardcodes {node.value!r}")

    return found


def test_platform_paths_are_confined_to_browser_api() -> None:
    offenders: dict[str, list[str]] = {}

    for path in sorted(_SOURCE_ROOT.rglob("*.py")):
        if _BROWSER_API in path.parents or path == _BROWSER_API:
            continue
        rel = _relative(path)
        if rel in _ALLOWED:
            continue
        hits = _violations(path)
        if hits:
            offenders[rel] = hits

    assert not offenders, (
        "Platform paths must be built inside browser_api/.\n"
        + "\n".join(
            f"  {rel}\n" + "\n".join(f"    {hit}" for hit in hits)
            for rel, hits in offenders.items()
        )
        + "\n\nMove the request into a browser_api wrapper and call that, or -- if the "
        "endpoint genuinely has no v2 counterpart and cannot live there -- add the "
        "module to _ALLOWED in this test with the reason."
    )


def test_allowlist_has_no_stale_entries() -> None:
    """An allowlisted module that no longer names a platform path should be dropped."""
    stale = [
        rel
        for rel in _ALLOWED
        if (_SOURCE_ROOT / rel).exists() and not _violations(_SOURCE_ROOT / rel)
    ]
    assert not stale, (
        "These modules no longer reference a platform path; remove them from "
        f"_ALLOWED: {stale}"
    )


def test_allowlist_paths_exist() -> None:
    missing = [rel for rel in _ALLOWED if not (_SOURCE_ROOT / rel).exists()]
    assert not missing, f"_ALLOWED names modules that no longer exist: {missing}"
