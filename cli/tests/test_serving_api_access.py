"""Invocation contracts, URL safety, and credential output boundaries."""

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from click.testing import CliRunner

from inspire.cli.commands.account import api_key as key_cli
from inspire.cli.commands.account import key_export
from inspire.cli.commands.serving import serving_api as api_cli
from inspire.cli.commands.serving import serving_commands
from inspire.services.serving.serving_access import invocation_info, serving_endpoint
from inspire.services.serving.serving_output import public_serving, public_serving_list_item
from inspire.services.utils.json_formatter import format_json
from inspire.cli.main import main
from inspire.platform.web.browser_api import api_keys

SECRET = "test-only-secret-never-print"
ENDPOINT = "https://inference-serving-abc.example.org"


def detail(kind="CUSTOM", endpoint=ENDPOINT):
    return {
        "name": "demo",
        "status": "STOPPED",
        "inference_serving_type": kind,
        "extra_info": {"service": endpoint, "token": SECRET},
    }


@pytest.mark.parametrize(
    "url",
    [
        "https://user:password@example.org",
        "https://example.org?token=secret",
        "https://example.org#secret",
        "https://example.org/private/token",
        "https://example.org?",
        "https://example.org#",
        "https://example.org\nX:bad",
        "https://example.org:99999",
        "javascript:alert(1)",
        "https://example.org/../",
        "https://bad\\host",
        "https://bad'host",
        "https://example.org:0",
        "https://example.org\x7f",
    ],
)
def test_unknown_or_credential_url_is_not_published(url):
    assert serving_endpoint(detail(endpoint=url)) == ""
    assert "endpoint" not in public_serving(detail(endpoint=url))


@pytest.mark.parametrize("url", [ENDPOINT, "http://localhost:8000", "https://[::1]:8443/"])
def test_origin_is_preserved(url):
    assert serving_endpoint(detail(endpoint=url)) == url.rstrip("/")


def test_endpoint_survives_both_list_projections_and_json_without_extra_data():
    row = SimpleNamespace(name="demo", raw=detail())
    public = public_serving_list_item(row)
    assert public["endpoint"] == ENDPOINT
    output = format_json(public, preserve_raw={"endpoint"})
    assert json.loads(output)["data"]["endpoint"] == ENDPOINT
    assert SECRET not in output
    assert "endpoint" not in json.loads(format_json(public))["data"]
    # Opting into engineering fields must never opt into secret/ID fields.
    assert (
        json.loads(
            format_json({"api_key": SECRET, "id": "private"}, preserve_raw={"api_key", "id"})
        )["data"]
        == {}
    )


def test_custom_example_does_not_guess_openai_routes():
    info = invocation_info(detail(), "demo", "session-1")
    assert "/v1" not in info["example"]
    assert "base_url" not in info
    assert "Authorization: Bearer $INF_API_KEY" in info["example"]
    assert "x-inspire-inference-key: session-1" in info["example"]
    assert SECRET not in repr(info)
    assert info["status"] == "STOPPED"


def test_openai_example_and_shell_quoting():
    info = invocation_info(detail("EXCLUSIVE"), "demo", "a'; $(echo bad)")
    import shlex

    args = shlex.split(info["example"].replace("\\\n", ""))
    assert "x-inspire-inference-key: a'; $(echo bad)" in args
    assert info["base_url"] == ENDPOINT + "/v1"
    assert args[1] == ENDPOINT + "/v1/chat/completions"


def test_api_command_json_preserves_generated_example(monkeypatch):
    monkeypatch.setattr(api_cli, "get_web_session", lambda: object())
    monkeypatch.setattr(serving_commands, "_resolve_workspace_id", lambda *a, **k: "ws-internal")
    monkeypatch.setattr(
        serving_commands, "_run_readonly_serving_operation", lambda *a, **k: detail()
    )
    result = CliRunner().invoke(main, ["--json", "serving", "api", "demo", "--workspace", "space"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)["data"]
    assert data["endpoint"] == ENDPOINT
    assert "Bearer $INF_API_KEY" in data["example"]
    assert SECRET not in result.output


@pytest.mark.parametrize("affinity", ["bad\r\nAuthorization: secret", "", "a" * 257])
def test_header_injection_is_rejected_before_network(affinity):
    result = CliRunner().invoke(
        main, ["serving", "api", "demo", "--workspace", "space", "--affinity-key", affinity]
    )
    assert result.exit_code != 0


def test_key_wrappers_use_console_contract_and_discard_secret(monkeypatch):
    calls = []
    session = object()

    def request(s, method, path, **kw):
        calls.append((s, method, path, kw["body"]))
        action = path.split("=")[-1]
        if action == "GetMyAPIList":
            result = {
                "items": [{"key_id": "internal", "name": "test", "created_at": 12, "value": SECRET}]
            }
        else:
            result = {"value": SECRET}
        return {"Result": result}

    monkeypatch.setattr(api_keys, "_request_json", request)
    keys = api_keys.list_api_keys(session=session)
    assert SECRET not in repr(keys) and "internal" not in repr(keys)
    assert keys[0].created_at == "12"
    api_keys.create_api_key("test", session=session)
    assert api_keys.get_api_key_plaintext("internal", session=session) == SECRET
    api_keys.delete_api_key("internal", session=session)
    assert [c[3] for c in calls] == [
        {},
        {"key_name": "test"},
        {"api_key_id": "internal"},
        {"api_key_id": "internal"},
    ]
    assert all(c[0] is session and c[1] == "POST" for c in calls)


def test_key_wrapper_does_not_echo_backend_error(monkeypatch):
    def fail(*a, **k):
        raise ValueError(SECRET)

    monkeypatch.setattr(api_keys, "_request_json", fail)
    with pytest.raises(ValueError) as caught:
        api_keys.get_api_key_plaintext("internal", session=object())
    assert SECRET not in str(caught.value)


@pytest.mark.parametrize("payload", [{}, {"items": None}, {"items": [{}]}])
def test_invalid_key_list_does_not_become_empty(monkeypatch, payload):
    monkeypatch.setattr(api_keys, "_call", lambda *a, **k: payload)
    with pytest.raises(ValueError):
        api_keys.list_api_keys(session=object())


@pytest.fixture
def mock_keys(monkeypatch):
    monkeypatch.setattr(key_cli, "get_web_session", lambda: object())
    monkeypatch.setattr(
        api_keys, "list_api_keys", lambda **k: [api_keys.APIKeyInfo("private-id", "demo", "123")]
    )
    fetch = Mock(return_value=SECRET)
    monkeypatch.setattr(api_keys, "get_api_key_plaintext", fetch)
    return fetch


def test_export_is_private_and_no_secret_in_output(tmp_path, mock_keys):
    target = tmp_path / "key"
    result = CliRunner().invoke(
        main, ["--json", "account", "api-key", "export", "demo", "--output", str(target)]
    )
    assert result.exit_code == 0, result.output
    assert target.read_text() == SECRET + "\n"
    if sys.platform != "win32":
        assert target.stat().st_mode & 0o777 == 0o600
    else:
        assert "current-user-only ACL" in result.output
    assert SECRET not in result.output and "private-id" not in result.output
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX export preflight")
@pytest.mark.parametrize("symlink", [False, True])
def test_export_refuses_existing_file_or_symlink_before_secret_fetch(tmp_path, mock_keys, symlink):
    target = tmp_path / "key"
    if symlink:
        target.symlink_to(tmp_path / "missing")
    else:
        target.write_text("keep")
    result = CliRunner().invoke(
        main, ["account", "api-key", "export", "demo", "--output", str(target)]
    )
    assert result.exit_code != 0
    mock_keys.assert_not_called()
    if not symlink:
        assert target.read_text() == "keep"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX atomic export")
def test_atomic_export_handles_destination_race(tmp_path, monkeypatch):
    target = tmp_path / "key"
    real_link = os.link

    def racing_link(src, dst):
        Path(dst).write_text("racer")
        real_link(src, dst)

    monkeypatch.setattr(os, "link", racing_link)
    with pytest.raises(FileExistsError):
        key_cli.export_private_key(SECRET, target)
    assert target.read_text() == "racer"
    assert list(tmp_path.iterdir()) == [target]


def test_delete_requires_confirmation_without_network(mock_keys):
    result = CliRunner().invoke(main, ["--json", "account", "api-key", "delete", "demo"])
    assert result.exit_code != 0
    mock_keys.assert_not_called()


def test_ambiguous_name_needs_pick(monkeypatch, mock_keys):
    monkeypatch.setattr(
        api_keys,
        "list_api_keys",
        lambda **k: [api_keys.APIKeyInfo("a", "demo", "1"), api_keys.APIKeyInfo("b", "demo", "2")],
    )
    # Name resolution is shared by export, run and delete on every platform.
    with pytest.raises(ValueError, match="--pick"):
        key_cli._resolve_key("demo", None, object())
    mock_keys.assert_not_called()


def test_create_accepted_but_refresh_failure_is_not_reported_as_failed(monkeypatch):
    monkeypatch.setattr(key_cli, "get_web_session", lambda: object())
    monkeypatch.setattr(
        api_keys, "list_api_keys", Mock(side_effect=[[], ValueError("unavailable")])
    )
    create = Mock()
    monkeypatch.setattr(api_keys, "create_api_key", create)
    result = CliRunner().invoke(main, ["--json", "account", "api-key", "create", "--name", "demo"])
    assert result.exit_code == 0, result.output
    assert "confirmation pending" in result.output
    create.assert_called_once()


def test_windows_export_requires_acl_tool_before_fetching_secret(tmp_path, monkeypatch, mock_keys):
    monkeypatch.setattr(key_cli.sys, "platform", "win32")
    monkeypatch.setattr(
        key_export.shutil,
        "which",
        lambda name: None,
    )
    result = CliRunner().invoke(
        main, ["account", "api-key", "export", "demo", "--output", str(tmp_path / "key")]
    )
    assert result.exit_code != 0 and "PowerShell" in result.output
    mock_keys.assert_not_called()
    assert not list(tmp_path.iterdir())
