"""Tests for `inspire notebook url` / proxy URL helpers."""

from __future__ import annotations

import json
from types import SimpleNamespace

from click.testing import CliRunner

from inspire.cli.commands.notebook import url_cmd as url_cmd_mod
from inspire.cli.commands.notebook.transport import NotebookTransportPolicy
from inspire.cli.main import main as cli_main
from inspire.platform.web import browser_api as browser_api_mod
from inspire.platform.web.browser_api import playwright_notebooks as pw

_NOTEBOOK_ID = "bae66d5d-8423-4730-aa06-96a770748109"
_BASE_URL = "https://qz.sii.edu.cn"
_SUFFIX = (
    "/ws-6e6ba362-e98e-45b2-9c5a-311998e93d65"
    "/project-84370d52-6e91-4911-8116-5840a97e984c"
    "/user-263239cf-402f-4ae0-a8e2-2fcca034026c"
    f"/vscode/{_NOTEBOOK_ID}/ed659e4b-012e-4d94-9439-c67eebc771d5"
)
_GATEWAY = "https://nat2-notebook-inspire.sii.edu.cn"
_IDE_URL = f"{_GATEWAY}{_SUFFIX}"


# ---------------------------------------------------------------------------
# URL normalization
# ---------------------------------------------------------------------------


def test_suffix_is_host_less_and_starts_with_slash() -> None:
    raw = f"{_IDE_URL}/lab?folder=/root"
    suffix = pw._vscode_proxy_suffix(raw)
    assert suffix == _SUFFIX
    assert suffix is not None and suffix.startswith("/ws-")


def test_suffix_forces_vscode_marker() -> None:
    # The loaded IDE may be something other than VSCode; the marker (segment
    # after user-<id>) is rewritten to vscode regardless.
    raw = "https://gw/ws-a/project-b/user-c/jupyter/run/tok/lab"
    assert pw._vscode_proxy_suffix(raw) == "/ws-a/project-b/user-c/vscode/run/tok"


def test_suffix_keeps_query_token_when_no_path_token() -> None:
    raw = "https://gw/ws-a/project-b/user-c/anyide/run/?token=secret&x=1"
    assert pw._vscode_proxy_suffix(raw) == "/ws-a/project-b/user-c/vscode/run?token=secret"


def test_suffix_returns_none_without_gateway_structure() -> None:
    assert pw._vscode_proxy_suffix("https://h/ide?notebook_id=x") is None
    assert pw._vscode_proxy_suffix("https://h/api/user-c/jupyter/run/tok") is None
    assert pw._vscode_proxy_suffix("") is None


def test_ide_gateway_url_keeps_host_and_strips_proxy_suffix() -> None:
    # A cached rtunnel proxy URL normalizes to the bare IDE gateway URL.
    proxy_url = f"{_IDE_URL}/proxy/8080/"
    assert pw._ide_gateway_url(proxy_url) == _IDE_URL


def test_ide_gateway_url_requires_a_host() -> None:
    assert pw._ide_gateway_url(_SUFFIX) is None  # host-less input
    assert pw._ide_gateway_url("") is None


def test_build_port_forward_url_keeps_ide_gateway_host() -> None:
    out = pw.build_notebook_port_forward_url(
        _IDE_URL,
        port=30000,
        service_path="/v1/models?limit=1",
    )
    assert out == (f"{_IDE_URL}/proxy/30000/v1/models?limit=1")


# ---------------------------------------------------------------------------
# _find_ide_gateway_url
# ---------------------------------------------------------------------------


class _FakeFrame:
    def __init__(self, url: str) -> None:
        self.url = url


class _FakePage:
    def __init__(self, frame_urls: list[str], url: str = "") -> None:
        self.frames = [_FakeFrame(u) for u in frame_urls]
        self.url = url


def test_find_returns_full_ide_url_from_gateway_frame() -> None:
    page = _FakePage(["about:blank", f"{_IDE_URL}/lab"])
    assert pw._find_ide_gateway_url(page) == _IDE_URL


def test_find_returns_none_when_no_gateway_frame() -> None:
    page = _FakePage(["about:blank", "https://h/other"])
    assert pw._find_ide_gateway_url(page) is None


# ---------------------------------------------------------------------------
# resolve_notebook_vscode_proxy_suffix — cache / probe / browser
# ---------------------------------------------------------------------------


def _patch_env(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(pw, "_get_base_url", lambda: _BASE_URL)
    monkeypatch.setattr(pw, "_active_account_name", lambda: None)
    monkeypatch.setattr(pw, "_write_cached_ide_url", lambda *a, **k: None)


def test_resolve_uses_cache_when_probe_live(monkeypatch) -> None:  # noqa: ANN001
    _patch_env(monkeypatch)
    monkeypatch.setattr(pw, "_read_cached_ide_url", lambda *a, **k: _IDE_URL)
    monkeypatch.setattr(pw, "_warm_ide_url_candidates", lambda *a, **k: [])
    monkeypatch.setattr(pw, "_is_ide_url_live", lambda *a, **k: True)

    def _no_browser(*a, **k):  # noqa: ANN002,ANN003
        raise AssertionError("browser must not run on a live cache hit")

    monkeypatch.setattr(pw, "resolve_notebook_ide_url", _no_browser)

    assert pw.resolve_notebook_vscode_proxy_suffix(_NOTEBOOK_ID, session=object()) == _SUFFIX


def test_resolve_reuses_warm_rtunnel_candidate(monkeypatch) -> None:  # noqa: ANN001
    _patch_env(monkeypatch)
    monkeypatch.setattr(pw, "_read_cached_ide_url", lambda *a, **k: None)
    monkeypatch.setattr(pw, "_warm_ide_url_candidates", lambda *a, **k: [_IDE_URL])
    monkeypatch.setattr(pw, "_is_ide_url_live", lambda *a, **k: True)
    monkeypatch.setattr(
        pw,
        "resolve_notebook_ide_url",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("browser must not run")),
    )

    assert pw.resolve_notebook_vscode_proxy_suffix(_NOTEBOOK_ID, session=object()) == _SUFFIX


def test_resolve_falls_back_to_browser_when_stale(monkeypatch) -> None:  # noqa: ANN001
    _patch_env(monkeypatch)
    monkeypatch.setattr(pw, "_read_cached_ide_url", lambda *a, **k: _IDE_URL)
    monkeypatch.setattr(pw, "_warm_ide_url_candidates", lambda *a, **k: [])
    monkeypatch.setattr(pw, "_is_ide_url_live", lambda *a, **k: False)  # token rotated
    monkeypatch.setattr(pw, "resolve_notebook_ide_url", lambda *a, **k: _IDE_URL)

    assert pw.resolve_notebook_vscode_proxy_suffix(_NOTEBOOK_ID, session=object()) == _SUFFIX


def test_resolve_refresh_skips_cache_and_probe(monkeypatch) -> None:  # noqa: ANN001
    _patch_env(monkeypatch)
    monkeypatch.setattr(
        pw,
        "_read_cached_ide_url",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("cache read on refresh")),
    )
    monkeypatch.setattr(
        pw,
        "_is_ide_url_live",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("probe on refresh")),
    )
    monkeypatch.setattr(pw, "resolve_notebook_ide_url", lambda *a, **k: _IDE_URL)

    out = pw.resolve_notebook_vscode_proxy_suffix(_NOTEBOOK_ID, session=object(), refresh=True)
    assert out == _SUFFIX


def test_resolve_returns_none_when_browser_fails(monkeypatch) -> None:  # noqa: ANN001
    _patch_env(monkeypatch)
    monkeypatch.setattr(pw, "_read_cached_ide_url", lambda *a, **k: None)
    monkeypatch.setattr(pw, "_warm_ide_url_candidates", lambda *a, **k: [])
    monkeypatch.setattr(pw, "resolve_notebook_ide_url", lambda *a, **k: None)

    assert pw.resolve_notebook_vscode_proxy_suffix(_NOTEBOOK_ID, session=object()) is None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _patch_resolve(monkeypatch) -> list[str]:  # noqa: ANN001
    opened: list[str] = []
    monkeypatch.setattr(
        url_cmd_mod,
        "_resolve_notebook",
        lambda ctx, notebook, workspace: (
            SimpleNamespace(storage_state={}),
            _BASE_URL,
            _NOTEBOOK_ID,
        ),
    )
    monkeypatch.setattr(
        url_cmd_mod,
        "preflight_notebook_transport_policy",
        lambda *_a, **_k: NotebookTransportPolicy(
            notebook="nb",
            notebook_id=_NOTEBOOK_ID,
            public_internet=True,
            reason="test",
        ),
    )
    monkeypatch.setattr(
        url_cmd_mod.webbrowser,
        "open",
        lambda url, **_kwargs: opened.append(url) or True,
    )
    return opened


def test_url_opens_entrance_link_without_printing_it(monkeypatch) -> None:  # noqa: ANN001
    opened = _patch_resolve(monkeypatch)
    result = CliRunner().invoke(cli_main, ["notebook", "url", "nb", "--workspace", "CPU资源空间"])
    assert result.exit_code == 0
    assert opened == [f"{_BASE_URL}/ide?notebook_id={_NOTEBOOK_ID}"]
    assert result.output == "Opened notebook 'nb'.\n"
    assert _BASE_URL not in result.output
    assert _NOTEBOOK_ID not in result.output


def test_url_json_is_name_only(monkeypatch) -> None:  # noqa: ANN001
    opened = _patch_resolve(monkeypatch)
    result = CliRunner().invoke(
        cli_main, ["--json", "notebook", "url", "nb", "--workspace", "CPU资源空间"]
    )
    assert result.exit_code == 0
    assert opened == [f"{_BASE_URL}/ide?notebook_id={_NOTEBOOK_ID}"]
    data = json.loads(result.output)["data"]
    assert data == {"name": "nb", "status": "opened"}
    assert _BASE_URL not in result.output
    assert _NOTEBOOK_ID not in result.output


def test_vscode_proxy_suffix_opens_ide_without_printing_path(monkeypatch) -> None:  # noqa: ANN001
    opened = _patch_resolve(monkeypatch)
    monkeypatch.setattr(
        browser_api_mod, "resolve_notebook_vscode_ide_url", lambda *a, **k: _IDE_URL
    )
    result = CliRunner().invoke(
        cli_main, ["notebook", "vscode-proxy-suffix", "nb", "--workspace", "CPU资源空间"]
    )
    assert result.exit_code == 0
    assert opened == [_IDE_URL]
    assert result.output == "Opened notebook 'nb'.\n"
    assert _SUFFIX not in result.output
    assert _NOTEBOOK_ID not in result.output


def test_vscode_proxy_suffix_json_is_name_only(monkeypatch) -> None:  # noqa: ANN001
    opened = _patch_resolve(monkeypatch)
    monkeypatch.setattr(
        browser_api_mod, "resolve_notebook_vscode_ide_url", lambda *a, **k: _IDE_URL
    )
    result = CliRunner().invoke(
        cli_main,
        ["--json", "notebook", "vscode-proxy-suffix", "nb", "--workspace", "CPU资源空间"],
    )
    assert result.exit_code == 0
    assert opened == [_IDE_URL]
    data = json.loads(result.output)["data"]
    assert data == {"name": "nb", "status": "opened"}
    assert _SUFFIX not in result.output
    assert _NOTEBOOK_ID not in result.output


def test_proxy_url_opens_service_without_printing_url(monkeypatch) -> None:  # noqa: ANN001
    opened = _patch_resolve(monkeypatch)
    monkeypatch.setattr(
        browser_api_mod,
        "resolve_notebook_port_forward_url",
        lambda *a, **k: f"{_IDE_URL}/proxy/{k['port']}{k['service_path']}",
    )
    result = CliRunner().invoke(
        cli_main,
        [
            "notebook",
            "proxy-url",
            "nb",
            "--workspace",
            "CPU资源空间",
            "--port",
            "30000",
            "--path",
            "/v1",
        ],
    )
    assert result.exit_code == 0
    assert opened == [f"{_IDE_URL}/proxy/30000/v1"]
    assert result.output == "Opened notebook 'nb'.\n"
    assert _IDE_URL not in result.output
    assert _NOTEBOOK_ID not in result.output


def test_proxy_url_json_is_name_only(monkeypatch) -> None:  # noqa: ANN001
    opened = _patch_resolve(monkeypatch)
    monkeypatch.setattr(
        browser_api_mod,
        "resolve_notebook_port_forward_url",
        lambda *a, **k: f"{_IDE_URL}/proxy/{k['port']}{k['service_path']}",
    )
    result = CliRunner().invoke(
        cli_main,
        [
            "--json",
            "notebook",
            "proxy-url",
            "nb",
            "--workspace",
            "CPU资源空间",
            "--port",
            "30000",
            "--path",
            "/v1",
        ],
    )
    assert result.exit_code == 0
    assert opened == [f"{_IDE_URL}/proxy/30000/v1"]
    data = json.loads(result.output)["data"]
    assert data == {"name": "nb", "status": "opened"}
    assert _IDE_URL not in result.output
    assert _NOTEBOOK_ID not in result.output


def test_vscode_proxy_suffix_refresh_passes_through(monkeypatch) -> None:  # noqa: ANN001
    opened = _patch_resolve(monkeypatch)
    seen: dict[str, object] = {}

    def _capture(notebook_id, **kwargs):  # noqa: ANN001,ANN003
        seen["notebook_id"] = notebook_id
        seen.update(kwargs)
        return _IDE_URL

    monkeypatch.setattr(browser_api_mod, "resolve_notebook_vscode_ide_url", _capture)
    result = CliRunner().invoke(
        cli_main,
        ["notebook", "vscode-proxy-suffix", "nb", "--workspace", "CPU资源空间", "--refresh"],
    )
    assert result.exit_code == 0
    assert opened == [_IDE_URL]
    assert seen["notebook_id"] == _NOTEBOOK_ID
    assert seen.get("refresh") is True
    assert _NOTEBOOK_ID not in result.output


def test_vscode_proxy_suffix_failure_errors(monkeypatch) -> None:  # noqa: ANN001
    opened = _patch_resolve(monkeypatch)
    monkeypatch.setattr(
        browser_api_mod, "resolve_notebook_vscode_ide_url", lambda *a, **k: None
    )
    result = CliRunner().invoke(
        cli_main, ["notebook", "vscode-proxy-suffix", "nb", "--workspace", "CPU资源空间"]
    )
    assert result.exit_code != 0
    assert "RUNNING" in result.stderr
    assert opened == []
    assert _NOTEBOOK_ID not in result.stderr
