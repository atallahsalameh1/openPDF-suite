"""Updater pure-logic tests (M12, D21). Network calls are never made."""

from __future__ import annotations

import hashlib

import pytest

from openpdfsuite.infrastructure import updater


def test_parse_tag_strips_v_and_compares_numerically():
    assert updater.parse_tag("v0.2.1") == (0, 2, 1)
    assert updater.parse_tag("0.10.0") > updater.parse_tag("0.9.9")
    assert updater.parse_tag("v1") == (1,)


def test_is_newer():
    assert updater.is_newer("v0.2.0", "0.1.0")
    assert updater.is_newer("v0.1.1", "v0.1.0")
    assert not updater.is_newer("v0.1.0", "0.1.0")
    assert not updater.is_newer("v0.1.0", "0.2.0")


def test_pick_setup_asset():
    assets = [
        {"name": "OpenPDFSuite-Portable-0.2.0.zip", "browser_download_url": "z"},
        {"name": "OpenPDFSuiteSetup-0.2.0.exe", "browser_download_url": "e"},
        {"name": "OpenPDFSuiteSetup-0.2.0.exe.sha256",
         "browser_download_url": "s"},
    ]
    picked = updater.pick_setup_asset(assets)
    assert picked["exe"]["browser_download_url"] == "e"
    assert picked["sha256"]["browser_download_url"] == "s"


def test_pick_setup_asset_without_checksum_sidecar():
    picked = updater.pick_setup_asset(
        [{"name": "OpenPDFSuiteSetup-0.2.0.exe", "browser_download_url": "e"}])
    assert picked["exe"]["browser_download_url"] == "e"
    assert picked["sha256"] is None


def test_pick_setup_asset_none_when_no_installer():
    assert updater.pick_setup_asset(
        [{"name": "OpenPDFSuite-Portable-0.2.0.zip"}]) is None


@pytest.mark.parametrize("text,expected", [
    ("3d1f...a1" if False else "a" * 64, "a" * 64),
    ("a" * 64 + "  OpenPDFSuiteSetup-0.2.0.exe", "a" * 64),
    ("SHA256 hash of file:\n" + "b" * 64, ""),
])
def test_sha256_of_hash_file(text, expected):
    assert updater.sha256_of_hash_file(text) == expected


def test_verify_sha256(tmp_path):
    p = tmp_path / "installer.exe"
    p.write_bytes(b"payload bytes")
    good = hashlib.sha256(b"payload bytes").hexdigest()
    assert updater.verify_sha256(p, good)
    assert updater.verify_sha256(p, good.upper())
    assert not updater.verify_sha256(p, "0" * 64)


def test_download_to_local_file(tmp_path):
    src = tmp_path / "src.bin"
    src.write_bytes(b"installer-bytes" * 1000)
    dest = tmp_path / "updates" / "setup.exe"
    seen: list[tuple[int, int | None]] = []
    updater.download_to(src.as_uri(), dest,
                        progress=lambda d, t: seen.append((d, t)))
    assert dest.read_bytes() == src.read_bytes()
    assert not dest.with_suffix(".exe.part").exists()
    assert seen and seen[-1][0] == len(src.read_bytes())


def test_download_to_cleans_up_on_failure(tmp_path):
    from urllib.error import URLError

    dest = tmp_path / "updates" / "setup.exe"
    with pytest.raises(URLError):
        updater.download_to("file:///nonexistent-source.bin", dest)
    assert not dest.exists()
    assert not dest.with_suffix(".exe.part").exists()


def test_spawn_installer_detached(monkeypatch, tmp_path):
    import subprocess

    called = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda cmd, **kw: called.setdefault("cmd", cmd))
    p = tmp_path / "setup.exe"
    updater.spawn_installer(p)
    assert called["cmd"][0] == str(p)
    assert "/closeapplications" in called["cmd"]
