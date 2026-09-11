"""File transfers against fake contents servers and fake SSH/SCP only."""
from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from dataclasses import FrozenInstanceError
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

import httpx
import pytest
import requests
from test_sdk import client as client
from test_sdk_exec import job_ref
from inspire import InspireAsyncClient, NotebookRef, TransferResult, ValidationError
from inspire.services.execution import notebook_transfer as core, remote_exec
from inspire.sdk import notebook_transfer as sdk


@pytest.fixture
def contents(client, monkeypatch, request):
    root = getattr(request, "param", "/")
    files = {}
    calls = []
    base = "https://example.invalid/api/v2/notebook/lab/nb/"

    def request(method, url, **options):
        calls.append((method, url, options))
        if url == base + "lab":
            return SimpleNamespace(status_code=200, text='<script id="jupyter-config-data">' + json.dumps({"serverRoot": root}) + "</script>")
        assert options["headers"]["X-XSRFToken"] == "fake-xsrf"
        path = unquote(urlsplit(url).path.split("api/contents/", 1)[1])
        status = 200
        if method == "GET":
            model = files.get(path)
            if model is None:
                status, model = 404, {}
            elif options.get("params", {}).get("content") == 0:
                model = {key: value for key, value in model.items() if key != "content"}
        else:
            assert method == "PUT"
            model = dict(options["json"])
            if model["type"] == "file":
                model["size"] = len(base64.b64decode(model["content"]))
            files[path] = model
            status = 201
        return SimpleNamespace(status_code=status, json=lambda: model)

    @contextmanager
    def connection(url):
        assert url == base + "lab"
        yield SimpleNamespace(cookies={"_xsrf": "fake-xsrf"}, request=request,
                              get=lambda url, **kw: request("GET", url, **kw))

    monkeypatch.setattr(sdk, "_notebook_jupyter_url", lambda *a: base + "lab")
    monkeypatch.setattr(client._transport, "application_connection", connection)
    monkeypatch.setattr(remote_exec, "cached_notebook_bridge", lambda **kw: None)
    return files, calls


@pytest.mark.parametrize("data", [b"one\r\ntwo\nlast\r", bytes(range(256)) * 50, b""],
                         ids=["mixed-newlines", "binary", "empty"])
def test_jupyter_round_trip(client, contents, tmp_path, data):
    source = tmp_path / "source"
    source.write_bytes(data)
    remote = "parent space/中文/#?% '$(touch nope).bin"
    ref = job_ref(client, NotebookRef)
    result = client.notebooks.upload(ref, local=source, remote=remote)
    target = tmp_path / "new parent" / "target"
    back = client.notebooks.download(ref, local=target, remote=remote)
    assert target.read_bytes() == data
    assert result == TransferResult(str(source), remote, len(data), "jupyter", 1, remote_path="/" + remote)
    assert back.bytes_transferred == len(data)
    assert contents[0]["parent space"]["type"] == "directory"
    assert contents[0]["parent space/中文"]["type"] == "directory"
    assert any("%E4%B8%AD" in url and "%23%3F%25" in url for _, url, _ in contents[1])
    with pytest.raises(FrozenInstanceError):
        result.transport = "ssh"


def test_jupyter_cap_and_overwrite(client, contents, tmp_path):
    source = tmp_path / "source"
    source.write_bytes(b"12345")
    ref = job_ref(client, NotebookRef)
    with pytest.raises(ValidationError, match="transport='ssh'.*max_bytes"):
        client.notebooks.upload(ref, local=source, remote="x", max_bytes=4)
    assert not contents[1]
    client.notebooks.upload(ref, local=source, remote="x", max_bytes=5)
    with pytest.raises(ValidationError, match="already exists"):
        client.notebooks.upload(ref, local=source, remote="x", overwrite=False)
    target = tmp_path / "target"
    with pytest.raises(ValidationError, match="transport='ssh'"):
        client.notebooks.download(ref, local=target, remote="x", max_bytes=4)
    assert not target.exists()
    target.write_bytes(b"keep")
    with pytest.raises(ValidationError, match="already exists"):
        client.notebooks.download(ref, local=target, remote="x", overwrite=False)
    assert target.read_bytes() == b"keep"


@pytest.mark.parametrize("path", ["../x", "a/../../x", "a/../x", "/../x", "a/%2e%2e/x", "a/%252e%252e/x", "a\\..\\x"])
@pytest.mark.parametrize("transport", ["auto", "jupyter", "ssh"])
def test_traversal_rejected_before_lookup(client, monkeypatch, tmp_path, path, transport):
    monkeypatch.setattr(client.notebooks, "_resolve", lambda *a: pytest.fail("lookup"))
    with pytest.raises(ValidationError, match="traversal"):
        client.notebooks.download(job_ref(client, NotebookRef), local=tmp_path / "x",
                                  remote=path, transport=transport)


@pytest.fixture
def ssh(client, monkeypatch):
    if os.name != "posix":
        pytest.skip(
            "Executing the Linux notebook helper on local paths requires a POSIX filesystem and /tmp."
        )
    calls = []
    monkeypatch.setattr(remote_exec, "cached_notebook_bridge", lambda **kw: "cached")
    monkeypatch.setattr(core, "load_tunnel_config", lambda **kw: "fake-config")

    def run(**kwargs):
        parts = shlex.split(kwargs["command"])
        assert parts[:2] == ["python3", "-c"]
        # Execute only the fixed transfer program on local temporary test paths.
        result = subprocess.run([sys.executable, "-c", *parts[2:]], capture_output=True, text=True)
        return remote_exec.ExecResult(result.returncode, result.stdout + result.stderr,
                                     result.stdout, result.stderr, True, "ssh")

    def scp(**kwargs):
        calls.append(kwargs)
        assert kwargs["bridge_name"] == "cached" and kwargs["config"] == "fake-config"
        assert kwargs["remote_path"].startswith("/tmp/inspire-transfer-")
        source, target = Path(kwargs["local_path"]), Path(kwargs["remote_path"])
        if kwargs["download"]:
            source, target = target, source
        if source.is_dir():
            assert kwargs["recursive"]
            shutil.copytree(source, target)
        else:
            shutil.copyfile(source, target)
        return subprocess.CompletedProcess([], 0, "", "")

    monkeypatch.setattr(core, "exec_in_notebook_ssh", run)
    monkeypatch.setattr(core, "run_scp_transfer", scp)
    return calls


@pytest.mark.parametrize("recursive", [False, True])
def test_ssh_round_trip_and_no_clobber(client, ssh, tmp_path, recursive):
    source = tmp_path / "source"
    data = bytes(range(256)) + b"\r\ntext\r"
    if recursive:
        source.mkdir()
        (source / "空 folder").mkdir()
        (source / "one").write_bytes(data)
        (source / "two").write_bytes(b"second\r\n")
    else:
        source.write_bytes(data)
    remote = str(tmp_path / "remote parent" / "中文 ' $() * ? destination")
    ref = job_ref(client, NotebookRef)
    result = client.notebooks.upload(ref, local=source, remote=remote, recursive=recursive,
                                     max_bytes=1)
    assert result.transport == "ssh"
    target = tmp_path / "download" / "target"
    back = client.notebooks.download(ref, local=target, remote=remote, recursive=recursive)
    assert core.inventory(target) == core.inventory(source)
    assert back.bytes_transferred == result.bytes_transferred
    assert back.files_transferred == (2 if recursive else 1)
    assert (target / "one" if recursive else target).read_bytes() == data
    assert len(ssh) == 2
    with pytest.raises(ValidationError, match="already exists"):
        client.notebooks.upload(ref, local=source, remote=remote, recursive=recursive, overwrite=False)
    with pytest.raises(ValidationError, match="already exists"):
        client.notebooks.download(ref, local=target, remote=remote, recursive=recursive, overwrite=False)


def test_scp_failure_does_not_publish(client, ssh, monkeypatch, tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"complete")
    target.write_bytes(b"keep")

    def fail(**kwargs):
        Path(kwargs["local_path"]).write_bytes(b"partial")
        return subprocess.CompletedProcess([], 1, "", "broken")

    monkeypatch.setattr(core, "run_scp_transfer", fail)
    with pytest.raises(ValidationError, match="SCP transfer failed"):
        client.notebooks.download(job_ref(client, NotebookRef), local=target, remote=str(source))
    assert target.read_bytes() == b"keep"


def test_explicit_transport_and_recursive_hint(client, contents, tmp_path):
    ref = job_ref(client, NotebookRef)
    with pytest.raises(ValidationError, match="inspire notebook connection refresh example"):
        client.notebooks.download(ref, local=tmp_path / "x", remote="x", transport="ssh")
    with pytest.raises(ValidationError, match="transport='ssh'"):
        client.notebooks.download(ref, local=tmp_path / "x", remote="x", recursive=True)


@pytest.mark.parametrize("absolute", [False, True])
def test_async_real_application_dispatch(client, monkeypatch, tmp_path, absolute):
    from inspire.sdk import _async_runtime
    from inspire.platform.web.browser_api import notebooks

    remote = "/project/dir/file" if absolute else "dir/file"
    models = {}
    requests_seen = []
    monkeypatch.setattr(_async_runtime, "InspireClient", lambda **kw: client)
    monkeypatch.setattr(notebooks, "_notebook_v2", lambda *a: {"jupyter_url": "https://example.invalid/proxy/lab"})
    monkeypatch.setattr(remote_exec, "cached_notebook_bridge", lambda **kw: None)

    async def send(_http, request, **kwargs):
        requests_seen.append(request)
        if request.url.path.endswith("/lab"):
            return httpx.Response(200, text='<script id="jupyter-config-data">{"serverRoot":"/project"}</script>',
                                  headers={"set-cookie": "_xsrf=fake; Path=/"}, request=request)
        assert request.headers["X-XSRFToken"] == "fake"
        path = request.url.path.split("api/contents/", 1)[1]
        if request.method == "PUT":
            model = json.loads(request.content)
            if model["type"] == "file":
                model["size"] = len(base64.b64decode(model["content"]))
            models[path] = model
            return httpx.Response(201, json=model, request=request)
        model = models.get(path)
        return httpx.Response(200 if model else 404, json=model or {}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", send)
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(bytes(range(256)) + b"\r\n")

    async def scenario():
        async with InspireAsyncClient("alpha") as ac:
            ref = job_ref(client, NotebookRef)
            result = await ac.notebooks.upload(ref, local=source, remote=remote)
            back = await ac.notebooks.download(ref, local=target, remote=remote)
            assert isinstance(result, TransferResult)
            assert result.bytes_transferred == back.bytes_transferred == source.stat().st_size
            assert result.transport == back.transport == "jupyter"
            assert result.remote == back.remote == remote
            assert result.remote_path == back.remote_path == "/project/dir/file"
    asyncio.run(scenario())
    assert target.read_bytes() == source.read_bytes()
    assert set(models) == {"dir", "dir/file"}
    assert len([r for r in requests_seen if r.method == "PUT"]) == 2


def test_async_ssh_round_trip(client, ssh, monkeypatch, tmp_path):
    from inspire.sdk import _async_runtime

    monkeypatch.setattr(_async_runtime, "InspireClient", lambda **kw: client)
    source, remote, target = (tmp_path / name for name in ("source", "remote", "target"))
    source.write_bytes(b"binary\x00\xff\r\n")

    async def scenario():
        async with InspireAsyncClient("alpha") as ac:
            ref = job_ref(client, NotebookRef)
            up = await ac.notebooks.upload(ref, local=source, remote=str(remote))
            down = await ac.notebooks.download(ref, local=target, remote=str(remote))
            assert up.bytes_transferred == down.bytes_transferred == source.stat().st_size
            assert up.transport == down.transport == "ssh"
            assert up.remote == down.remote == str(remote)
            assert up.remote_path == down.remote_path == str(remote)
    asyncio.run(scenario())
    assert target.read_bytes() == source.read_bytes()


def test_local_publication_failure_preserves_existing(tmp_path, monkeypatch):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"new")
    target.write_bytes(b"old")

    def fail(source, destination):
        Path(destination).write_bytes(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(core.shutil, "copyfile", fail)
    with pytest.raises(OSError, match="disk full"):
        core.publish(source, target, True)
    assert target.read_bytes() == b"old"
    assert not list(tmp_path.glob(".inspire-transfer-*"))


def test_jupyter_failed_put_is_not_replayed(client, monkeypatch, tmp_path):
    from inspire.sdk.exceptions import MutationUncertainError
    from inspire.platform.web.browser_api import notebooks

    monkeypatch.setattr(notebooks, "_notebook_v2", lambda *a: {"jupyter_url": "https://example.invalid/lab"})
    puts = []

    def send(_http, request, **kwargs):
        response = requests.Response()
        response.url = request.url
        response.status_code = 200 if request.url.endswith("/lab") else 404
        response._content = (b'<script id="jupyter-config-data">{"serverRoot":"/"}</script>'
                             if request.url.endswith("/lab") else b"{}")
        if request.method == "PUT":
            puts.append(request)
            raise requests.ConnectionError("response lost")
        return response

    monkeypatch.setattr(requests.Session, "send", send)
    source = tmp_path / "source"
    source.write_bytes(b"new")
    with pytest.raises(MutationUncertainError):
        client.notebooks.upload(job_ref(client, NotebookRef), local=source, remote="file", transport="jupyter")
    assert len(puts) == 1


def test_default_cap_refuses_before_application_connection(client, contents, tmp_path):
    source = tmp_path / "large"
    with source.open("wb") as stream:
        stream.truncate(core.DEFAULT_JUPYTER_MAX_BYTES + 1)
    with pytest.raises(ValidationError, match="transport='ssh'"):
        client.notebooks.upload(job_ref(client, NotebookRef), local=source, remote="large")
    assert not contents[1]


def test_forced_jupyter_does_not_probe_cached_bridge(client, contents, monkeypatch, tmp_path):
    monkeypatch.setattr(remote_exec, "cached_notebook_bridge", lambda **kw: pytest.fail("bridge probe"))
    source = tmp_path / "source"
    source.write_bytes(b"x")
    assert client.notebooks.upload(job_ref(client, NotebookRef), local=source, remote="x",
                                   transport="jupyter").transport == "jupyter"


def test_corrupt_download_preserves_destination(client, contents, tmp_path):
    contents[0]["file"] = dict(type="file", size=3, format="base64", content="!!!!")
    target = tmp_path / "target"
    target.write_bytes(b"old")
    with pytest.raises(ValidationError):
        client.notebooks.download(job_ref(client, NotebookRef), local=target, remote="file")
    assert target.read_bytes() == b"old"


def test_ssh_upload_failure_preserves_destination(client, ssh, monkeypatch, tmp_path):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"new")
    target.write_bytes(b"old")

    def fail(**kwargs):
        Path(kwargs["remote_path"]).write_bytes(b"partial")
        return subprocess.CompletedProcess([], 1, "", "interrupted")

    monkeypatch.setattr(core, "run_scp_transfer", fail)
    with pytest.raises(ValidationError, match="SCP transfer failed"):
        client.notebooks.upload(job_ref(client, NotebookRef), local=source, remote=str(target))
    assert target.read_bytes() == b"old"


def test_symlink_parent_cannot_escape_publication_directory(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    link = tmp_path / "link"
    link.symlink_to(outside, target_is_directory=True)
    source = tmp_path / "source"
    source.write_bytes(b"x")
    with pytest.raises(ValueError, match="symbolic link"):
        core.publish(source, link / "escaped", True)
    assert not (outside / "escaped").exists()


@pytest.fixture
def shared_container(client, ssh, monkeypatch, tmp_path):
    """Both real transfer adapters share a disk; only their starting roots differ."""
    root = tmp_path / "jupyter root"
    home = tmp_path / "ssh home"
    root.mkdir()
    home.mkdir()
    monkeypatch.chdir(home)
    lab = "https://example.invalid/proxy/lab"
    calls = []

    def request(method, url, **options):
        calls.append((method, url))
        if url == lab:
            page = '<script id="jupyter-config-data" type="application/json">' + json.dumps({"serverRoot": str(root)}) + '</script>'
            return SimpleNamespace(status_code=200, text=page)
        relative = unquote(urlsplit(url).path.split("api/contents/", 1)[1])
        path = root / relative
        if method == "PUT":
            model = options["json"]
            if model["type"] == "directory":
                path.mkdir()
            else:
                path.write_bytes(base64.b64decode(model["content"]))
            return SimpleNamespace(status_code=201)
        if not path.exists():
            return SimpleNamespace(status_code=404, json=lambda: {})
        model = {"type": "directory" if path.is_dir() else "file", "size": path.stat().st_size}
        if path.is_file() and options.get("params", {}).get("content") != 0:
            model.update(format="base64", content=base64.b64encode(path.read_bytes()).decode())
        return SimpleNamespace(status_code=200, json=lambda: model)

    @contextmanager
    def connection(url):
        yield SimpleNamespace(cookies={"_xsrf": "fake"}, request=request,
                              get=lambda url, **kw: request("GET", url, **kw))

    def execute(transport, **kwargs):
        proc = subprocess.run(kwargs["command"], shell=True, cwd=root if transport == "jupyter" else home,
                              capture_output=True, text=True)
        return remote_exec.ExecResult(proc.returncode, proc.stdout + proc.stderr,
                                     proc.stdout, proc.stderr, True, transport)

    monkeypatch.setattr(sdk, "_notebook_jupyter_url", lambda *a: lab)
    monkeypatch.setattr(client._transport, "application_connection", connection)

    async def execute_jupyter_async(**kwargs):
        return execute("jupyter", **kwargs)

    monkeypatch.setattr(remote_exec, "exec_in_notebook_jupyter_async", execute_jupyter_async)
    monkeypatch.setattr(remote_exec, "exec_in_notebook_jupyter", lambda **kw: execute("jupyter", **kw))
    monkeypatch.setattr(remote_exec, "exec_in_notebook_ssh", lambda **kw: execute("ssh", **kw))
    return root, home, calls


@pytest.mark.parametrize("upload_transport,download_transport", [("jupyter", "ssh"), ("ssh", "jupyter")])
@pytest.mark.parametrize("absolute", [True, False])
def test_cross_transport_same_file(client, shared_container, tmp_path, upload_transport, download_transport, absolute):
    root, home, _ = shared_container
    relative = "data/中文 #%.bin"
    remote = str(root / relative) if absolute else relative
    source, target = tmp_path / "source", tmp_path / "target"
    data = bytes(range(256)) + b"\r\nunchanged\x00"
    source.write_bytes(data)
    ref = job_ref(client, NotebookRef)
    client.notebooks.upload(ref, local=source, remote=remote, transport=upload_transport)
    client.notebooks.download(ref, local=target, remote=remote, transport=download_transport)
    assert target.read_bytes() == data
    assert (root / relative).read_bytes() == data
    assert not (home / relative).exists()


@pytest.mark.parametrize("upload_transport", ["jupyter", "ssh"])
@pytest.mark.parametrize("exec_transport", ["jupyter", "ssh"])
@pytest.mark.parametrize("absolute", [True, False])
def test_cross_transport_exec_observes_upload(client, shared_container, tmp_path, upload_transport, exec_transport, absolute):
    root, _, _ = shared_container
    remote = str(root / "data/file") if absolute else "data/file"
    source = tmp_path / "source"
    source.write_bytes(b"proof\r\n\x00\xff")
    ref = job_ref(client, NotebookRef)
    client.notebooks.upload(ref, local=source, remote=remote, transport=upload_transport)
    result = client.notebooks.exec(ref, command="base64 < " + shlex.quote(remote),
                                   cwd=None if absolute else str(root), transport=exec_transport)
    assert result.returncode == 0, result.output
    assert base64.b64decode(result.stdout or result.output) == source.read_bytes()


@pytest.mark.parametrize("contents", ["/project"], indirect=True)
@pytest.mark.parametrize("download", [False, True])
@pytest.mark.parametrize("transport", ["jupyter", "auto"])
def test_cross_transport_outside_root_refused(client, contents, monkeypatch, tmp_path, download, transport):
    _, calls = contents
    monkeypatch.setattr(remote_exec, "cached_notebook_bridge", lambda **kw: None)
    local = tmp_path / "local"
    local.write_bytes(b"keep")
    method = client.notebooks.download if download else client.notebooks.upload
    with pytest.raises(ValidationError, match='outside.*Jupyter.*transport="ssh"'):
        method(job_ref(client, NotebookRef), local=local, remote="/project-other/file", transport=transport)
    assert local.read_bytes() == b"keep"
    assert not any("api/contents/" in url for _, url, _ in calls)


@pytest.mark.parametrize("page", ["", "<html>login</html>", '<script id="jupyter-config-data">invalid</script>',
                                  '<script id="jupyter-config-data">[]</script>'])
def test_contents_root_missing_or_invalid(page):
    with pytest.raises(ValueError, match='serverRoot.*transport="ssh"'):
        core.jupyter_contents_root(page)


@pytest.mark.parametrize("root", [None, 3, "relative", "", "//host/path", "/a/../b", "/a\n", "/a\\b"])
def test_contents_root_rejects_invalid_paths(root):
    with pytest.raises(ValueError, match='serverRoot.*transport="ssh"'):
        core.jupyter_contents_root('<script id="jupyter-config-data">' + json.dumps({"serverRoot": root}) + '</script>')


def test_contents_root_parses_page_config():
    page = """<!doctype html><script>{"serverRoot":"/wrong"}</script>
    <script type='application/json' id='jupyter-config-data'>
    {"serverRoot": "/project space/中文/"}
    </script><script>other()</script>"""
    assert core.jupyter_contents_root(page) == "/project space/中文"
    assert core.contents_path("/project space/中文/data", "/project space/中文") == "data"
    assert core.contents_path("/data", "/") == "data"
    with pytest.raises(ValueError, match="names the Jupyter contents root"):
        core.contents_path("/project", "/project")


def test_root_cached_per_notebook_and_client(client, shared_container, monkeypatch, tmp_path):
    from dataclasses import replace
    from inspire.sdk.notebooks import Notebooks

    root, _, calls = shared_container
    source = tmp_path / "source"
    source.write_bytes(b"cache")
    ref = job_ref(client, NotebookRef)
    parses = []
    parse = core.jupyter_contents_root

    def discover(page):
        parses.append(page)
        return parse(page)

    monkeypatch.setattr(core, "jupyter_contents_root", discover)
    for transport in ("ssh", "ssh", "jupyter", "jupyter", "ssh"):
        result = client.notebooks.upload(ref, local=source, remote="data/file", transport=transport)
        assert result.remote == "data/file"
    assert len(parses) == 1
    # The first SSH transfer discovers the root; subsequent SSH transfers do no HTTP.
    assert len([url for _, url in calls if url.endswith("/lab")]) == 3
    client.notebooks.upload(replace(ref, key="another-notebook"), local=source, remote="data/file", transport="ssh")
    assert len(parses) == 2
    assert Notebooks(client)._contents_roots == {}
    assert (root / "data/file").read_bytes() == b"cache"


def test_absolute_and_relative_remote_are_aliases(client, shared_container, tmp_path):
    root, _, _ = shared_container
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"same file")
    ref = job_ref(client, NotebookRef)
    client.notebooks.upload(ref, local=source, remote=str(root / "data/file"), transport="jupyter")
    client.notebooks.download(ref, local=target, remote="data/file", transport="ssh")
    assert target.read_bytes() == source.read_bytes()
    with pytest.raises(ValidationError, match="already exists"):
        client.notebooks.upload(ref, local=source, remote="data/file", transport="ssh", overwrite=False)


def test_ssh_absolute_outside_root_needs_no_jupyter(client, ssh, monkeypatch, tmp_path):
    monkeypatch.setattr(sdk, "_notebook_jupyter_url", lambda *a: pytest.fail("Jupyter lookup"))
    source, remote, target = (tmp_path / name for name in ("source", "remote", "target"))
    source.write_bytes(b"outside")
    ref = job_ref(client, NotebookRef)
    client.notebooks.upload(ref, local=source, remote=str(remote), transport="ssh")
    client.notebooks.download(ref, local=target, remote=str(remote), transport="ssh")
    assert target.read_bytes() == source.read_bytes()


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("transport", ["jupyter", "ssh"])
@pytest.mark.parametrize("absolute", [False, True])
@pytest.mark.parametrize("exec_transport", ["jupyter", "ssh"])
def test_resolved_path_bridges_transfer_and_exec(
    client, shared_container, monkeypatch, tmp_path,
    asynchronous, transport, absolute, exec_transport,
):
    from inspire.sdk import _async_runtime

    root, home, _ = shared_container
    relative = "data/中文 ' model.bin"
    expected = root / relative
    # Keep the original request, including harmless normalization differences.
    remote = str(expected) if absolute else "./data//中文 ' model.bin"
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(bytes(range(256)) + b"\r\n")
    ref = job_ref(client, NotebookRef)

    def command(uploaded):
        assert uploaded.remote == remote
        assert Path(uploaded.remote_path).is_absolute()
        assert uploaded.remote_path == str(expected)
        assert expected.read_bytes() == source.read_bytes()
        assert not (home / relative).exists()
        return "base64 < " + shlex.quote(uploaded.remote_path)

    async def scenario():
        async with InspireAsyncClient("alpha") as ac:
            uploaded = await ac.notebooks.upload(ref, local=source, remote=remote, transport=transport)
            executed = await ac.notebooks.exec(ref, command=command(uploaded), transport=exec_transport)
            downloaded = await ac.notebooks.download(ref, local=target, remote=remote, transport=transport)
            return executed, downloaded

    if asynchronous:
        monkeypatch.setattr(_async_runtime, "InspireClient", lambda **kw: client)
        executed, downloaded = asyncio.run(scenario())
    else:
        uploaded = client.notebooks.upload(ref, local=source, remote=remote, transport=transport)
        executed = client.notebooks.exec(ref, command=command(uploaded), transport=exec_transport)
        downloaded = client.notebooks.download(ref, local=target, remote=remote, transport=transport)
    assert executed.returncode == 0, executed.output
    assert base64.b64decode(executed.stdout or executed.output) == source.read_bytes()
    assert downloaded.remote == remote
    assert downloaded.remote_path == str(expected)
    assert target.read_bytes() == source.read_bytes()


@pytest.mark.parametrize("contents", ["/project space/中文"], indirect=True)
@pytest.mark.parametrize("remote,expected", [
    ("./data//file", "/project space/中文/data/file"),
    ("/project space/中文/data/file", "/project space/中文/data/file"),
    ("/outside/file", "/outside/file"),
])
def test_ssh_path_resolution_without_local_remote_filesystem(
    client, contents, monkeypatch, tmp_path, remote, expected,
):
    monkeypatch.setattr(remote_exec, "cached_notebook_bridge", lambda **kw: "cached")
    calls = []

    def transfer(**kwargs):
        calls.append(kwargs)
        return TransferResult(kwargs["local"], kwargs["remote"], 4, "ssh",
                              remote_path=kwargs["remote"])

    monkeypatch.setattr(core, "transfer_ssh", transfer)
    source = tmp_path / "source"
    source.write_bytes(b"data")
    ref = job_ref(client, NotebookRef)
    for _ in range(2):
        result = client.notebooks.upload(ref, local=source, remote=remote, transport="ssh")
        assert result.remote == remote
        assert result.remote_path == expected
    assert [call["remote"] for call in calls] == [expected, expected]
    # Relative resolution discovers and caches the root; absolute SSH never needs Jupyter.
    assert len(contents[1]) == (0 if remote.startswith("/") else 1)


@pytest.mark.parametrize("overwrite", [False, True])
def test_local_publication_overwrite_policy(tmp_path, overwrite):
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(bytes(range(256)))
    target.write_bytes(b"old")
    if overwrite:
        core.publish(source, target, overwrite)
        assert target.read_bytes() == source.read_bytes()
    else:
        with pytest.raises(ValueError, match="already exists"):
            core.publish(source, target, overwrite)
        assert target.read_bytes() == b"old"
    assert not list(tmp_path.glob(".inspire-transfer-*"))


@pytest.mark.parametrize("asynchronous", [False, True])
def test_download_staging_under_symlinked_temp_root(
    client, ssh, monkeypatch, tmp_path, asynchronous,
):
    """An SDK-owned temp directory may have a system symlink above it."""
    from inspire.sdk import _async_runtime

    temp_root = tmp_path / "real-temp"
    temp_root.mkdir()
    temp_alias = tmp_path / "temp-alias"
    temp_alias.symlink_to(temp_root, target_is_directory=True)
    monkeypatch.setattr(core.tempfile, "tempdir", str(temp_alias))
    source, target = tmp_path / "remote", tmp_path / "download"
    source.write_bytes(b"complete\x00payload")
    ref = job_ref(client, NotebookRef)

    async def download():
        async with InspireAsyncClient("alpha") as ac:
            return await ac.notebooks.download(
                ref, local=target, remote=str(source), transport="ssh",
            )

    if asynchronous:
        monkeypatch.setattr(_async_runtime, "InspireClient", lambda **kw: client)
        result = asyncio.run(download())
    else:
        result = client.notebooks.download(ref, local=target, remote=str(source), transport="ssh")
    assert target.read_bytes() == source.read_bytes()
    assert result.bytes_transferred == source.stat().st_size
    assert not list(temp_root.iterdir())
