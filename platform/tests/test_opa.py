from __future__ import annotations

import hashlib
import io
import os
import runpy
import sys

import pytest

from rlp_platform import opa


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


@pytest.fixture
def fake_download(monkeypatch):
    """Serve `payload` for any URL and pin this machine's asset to its checksum."""
    seen: list[str] = []

    def serve(payload: bytes, sha256: str | None = None):
        def urlopen(url, timeout):
            seen.append(url)
            return FakeResponse(payload)

        monkeypatch.setattr(opa.urllib.request, "urlopen", urlopen)
        monkeypatch.setattr(opa, "current_platform", lambda: ("linux", "x86_64"))
        sha = sha256 or hashlib.sha256(payload).hexdigest()
        monkeypatch.setitem(opa.ASSETS, ("linux", "x86_64"), ("opa_linux_amd64_static", sha))
        return seen

    return serve


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Linux", "x86_64", ("linux", "x86_64")),
        ("Linux", "aarch64", ("linux", "arm64")),
        ("Darwin", "arm64", ("darwin", "arm64")),
        ("Windows", "AMD64", ("windows", "x86_64")),
    ],
)
def test_current_platform(monkeypatch, system, machine, expected):
    monkeypatch.setattr(opa.platform, "system", lambda: system)
    monkeypatch.setattr(opa.platform, "machine", lambda: machine)
    assert opa.current_platform() == expected


def test_every_pinned_asset_is_this_version_with_a_sha256():
    assert opa.OPA_VERSION.count(".") == 2
    for asset, sha in opa.ASSETS.values():
        assert asset.startswith("opa_")
        assert len(sha) == 64
        int(sha, 16)


def test_find_uses_path(monkeypatch, tmp_path):
    exe = tmp_path / ("opa.exe" if os.name == "nt" else "opa")
    exe.write_text("", encoding="utf-8")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert opa.find() == str(exe)


def test_find_without_opa(monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(opa.OpaError, match="opa is not on PATH"):
        opa.find()


def test_install_checks_the_sha256_and_marks_it_executable(fake_download, tmp_path):
    seen = fake_download(b"binary")
    path = opa.install(tmp_path / "bin" / "opa")
    assert path.read_bytes() == b"binary"
    assert os.access(path, os.X_OK) or os.name == "nt"
    assert seen == [
        f"https://github.com/open-policy-agent/opa/releases/download/v{opa.OPA_VERSION}/opa_linux_amd64_static"
    ]


def test_install_rejects_a_wrong_checksum(fake_download, tmp_path):
    fake_download(b"tampered", sha256="0" * 64)
    with pytest.raises(opa.OpaError, match="expected 0000"):
        opa.install(tmp_path / "opa")
    assert not (tmp_path / "opa").exists()


def test_install_reports_a_download_error(monkeypatch, tmp_path):
    def urlopen(url, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr(opa.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(opa, "current_platform", lambda: ("linux", "x86_64"))
    with pytest.raises(opa.OpaError, match=r"could not download .*: connection refused"):
        opa.install(tmp_path / "opa")


def test_install_on_an_unpinned_platform(monkeypatch, tmp_path):
    monkeypatch.setattr(opa, "current_platform", lambda: ("plan9", "mips"))
    with pytest.raises(opa.OpaError, match="no pinned OPA build for plan9 mips"):
        opa.install(tmp_path / "opa")


def test_main(fake_download, tmp_path, capsys):
    fake_download(b"binary")
    assert opa.main(["opa", str(tmp_path / "opa")]) == 0
    assert f"installed OPA {opa.OPA_VERSION}" in capsys.readouterr().out


def test_main_usage(capsys):
    assert opa.main(["opa"]) == 2
    assert "usage" in capsys.readouterr().err


def test_main_error(fake_download, tmp_path, capsys):
    fake_download(b"tampered", sha256="0" * 64)
    assert opa.main(["opa", str(tmp_path / "opa")]) == 1
    assert capsys.readouterr().err.startswith("error: ")


def test_running_the_module_exits_with_mains_code(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["opa"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_path(opa.__file__, run_name="__main__")
    assert exc.value.code == 2
    assert "usage: python -m rlp_platform.opa DEST" in capsys.readouterr().err
