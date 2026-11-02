"""Managed aria2c discovery."""

from __future__ import annotations

from pathlib import Path

from ytdl import aria2


def test_find_aria2c_prefers_managed_binary(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(aria2, "_data_dir", lambda: tmp_path)
    target = tmp_path / "bin" / aria2._binary_name()
    target.parent.mkdir(parents=True)
    target.write_bytes(b"binary")
    assert aria2.find_aria2c() == target


def test_install_returns_existing_binary(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(aria2, "_data_dir", lambda: tmp_path)
    target = tmp_path / "bin" / aria2._binary_name()
    target.parent.mkdir(parents=True)
    target.write_bytes(b"binary")
    assert aria2.install_aria2c() == target
