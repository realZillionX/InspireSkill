from __future__ import annotations

import shutil
import tarfile
import urllib.request
from pathlib import Path

from inspire.bridge.tunnel import rtunnel as rtunnel_module


class _DummyConfig:
    def __init__(self, rtunnel_bin: Path) -> None:
        self.rtunnel_bin = rtunnel_bin


def _build_archive_with_rtunnel(tmp_path: Path, payload: bytes) -> Path:
    binary = tmp_path / "rtunnel"
    binary.write_bytes(payload)
    archive = tmp_path / "rtunnel.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(binary, arcname="rtunnel")
    return archive


def test_ensure_rtunnel_binary_keeps_existing_usable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bin_path = tmp_path / "rtunnel"
    bin_path.write_bytes(b"ok")
    bin_path.chmod(0o755)
    cfg = _DummyConfig(bin_path)

    monkeypatch.setattr(rtunnel_module, "_is_rtunnel_binary_usable", lambda _path: True)
    monkeypatch.setattr(
        urllib.request,
        "urlretrieve",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not download")),
    )

    resolved = rtunnel_module._ensure_rtunnel_binary(cfg)  # noqa: SLF001

    assert resolved == bin_path
    assert bin_path.read_bytes() == b"ok"


def test_ensure_rtunnel_binary_redownloads_when_existing_binary_is_unusable(
    tmp_path: Path,
    monkeypatch,
) -> None:
    bin_path = tmp_path / "rtunnel"
    bin_path.write_bytes(b"old")
    bin_path.chmod(0o755)
    cfg = _DummyConfig(bin_path)

    archive = _build_archive_with_rtunnel(tmp_path, payload=b"new")

    monkeypatch.setattr(
        rtunnel_module,
        "_is_rtunnel_binary_usable",
        lambda path: path.exists() and path.read_bytes() == b"new",
    )
    monkeypatch.setattr(
        rtunnel_module, "_get_rtunnel_download_url", lambda: "https://example/rt.tgz"
    )
    monkeypatch.setattr(
        urllib.request,
        "urlretrieve",
        lambda _url, dest: shutil.copyfile(archive, dest),
    )

    resolved = rtunnel_module._ensure_rtunnel_binary(cfg)  # noqa: SLF001

    assert resolved == bin_path
    assert bin_path.read_bytes() == b"new"
