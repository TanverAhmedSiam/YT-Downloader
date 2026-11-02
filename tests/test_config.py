"""Configuration loading, flattening, validation, and saving tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from ytdl.config import (
    Config,
    ConfigError,
    _flatten_config,
    default_config_paths,
    find_config,
    load_config,
    resolve_config_path,
    save_config,
)
from ytdl.paths import DEFAULT_MUSIC_TEMPLATE, DEFAULT_VIDEO_TEMPLATE


def test_defaults_are_sane() -> None:
    config = Config()
    assert config.format == "best"
    assert config.quality is None
    assert config.bitrate == 320
    assert config.directory == "downloads"
    assert config.concurrent == 3
    assert config.retries == 10
    assert config.music is False
    assert config.sponsorblock == "sponsor,selfpromo"


def test_public_dict_hides_credentials() -> None:
    config = Config(
        cookies="secret.txt",
        cookies_from_browser="chrome",
        proxy="http://user:pass@example.com",
        archive=".cache/archive.txt",
    )
    public = config.public_dict()
    assert public["cookies"] == "configured"
    assert public["cookies_from_browser"] == "configured"
    assert public["proxy"] == "configured"
    assert public["archive"] == "configured"


def test_flat_config_is_accepted() -> None:
    merged = _flatten_config({"format": "1080p", "directory": "media"})
    assert merged == {"format": "1080p", "directory": "media"}


def test_sectioned_config_is_flattened() -> None:
    merged = _flatten_config(
        {
            "download": {"format": "720p", "concurrent": 4},
            "paths": {"directory": "D:/Media"},
            "metadata": {"thumbnail": False},
        }
    )
    assert merged == {
        "format": "720p",
        "concurrent": 4,
        "directory": "D:/Media",
        "thumbnail": False,
    }


def test_aliases_are_normalized() -> None:
    merged = _flatten_config(
        {
            "download_dir": "media",
            "outtmpl": "%(title)s.%(ext)s",
            "subs": True,
            "parallel": 8,
            "ratelimit": "10M",
            "cookie_file": "cookies.txt",
        }
    )
    assert merged["directory"] == "media"
    assert merged["output"] == "%(title)s.%(ext)s"
    assert merged["subtitles"] is True
    assert merged["concurrent"] == 8
    assert merged["rate_limit"] == "10M"
    assert merged["cookies"] == "cookies.txt"
    assert "download_dir" not in merged
    assert "outtmpl" not in merged


def test_unknown_keys_are_ignored() -> None:
    config = Config.model_validate(_flatten_config({"future_option": True, "format": "720p"}))
    assert config.format == "720p"


def test_output_template_property() -> None:
    assert Config().output_template == DEFAULT_VIDEO_TEMPLATE
    assert Config(music=True).output_template == DEFAULT_MUSIC_TEMPLATE
    assert Config(output="%(title)s.%(ext)s").output_template == "%(title)s.%(ext)s"


def test_sponsorblock_categories_property() -> None:
    assert Config(sponsorblock="sponsor, intro").sponsorblock_categories == [
        "sponsor",
        "intro",
    ]
    assert Config(sponsorblock=None).sponsorblock_categories == []


def test_load_missing_config_returns_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ytdl.config.default_config_paths", lambda cwd=None: [])
    config = load_config(cwd=tmp_path)
    assert config == Config()


def test_load_explicit_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        'format = "1080p"\nbitrate = 256\ndirectory = "media"\nconcurrent = 5\n',
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.format == "1080p"
    assert config.bitrate == 256
    assert config.directory == "media"
    assert config.concurrent == 5


def test_load_sectioned_config(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(
        "[download]\nformat = \"720p\"\n\n[paths]\ndirectory = \"media\"\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.format == "720p"
    assert config.directory == "media"


def test_explicit_missing_config_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.toml")


def test_load_config_accepts_a_utf8_bom(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(b"\xef\xbb\xbfformat = \"720p\"\n")
    assert load_config(path).format == "720p"


def test_invalid_toml_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("format = [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert "Invalid TOML" in str(excinfo.value)


def test_invalid_values_are_reported(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("bitrate = 9999\n", encoding="utf-8")
    with pytest.raises(ConfigError) as excinfo:
        load_config(path)
    assert "Invalid config value" in str(excinfo.value)


def test_inverted_playlist_range_is_rejected() -> None:
    with pytest.raises(ValueError):
        Config(playlist_start=10, playlist_end=5)


def test_bad_browser_is_rejected() -> None:
    with pytest.raises(ValueError):
        Config(cookies_from_browser="netscape")


def test_save_and_load_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "config.toml"
    original = Config(
        format="720p",
        bitrate=192,
        directory="media",
        concurrent=2,
        subtitle_langs="en,de",
        rate_limit="5M",
        keep_video=True,
    )
    saved = save_config(original, path)
    assert saved == path
    assert path.is_file()
    loaded = load_config(path)
    assert loaded.format == original.format
    assert loaded.bitrate == original.bitrate
    assert loaded.directory == original.directory
    assert loaded.concurrent == original.concurrent
    assert loaded.subtitle_langs == original.subtitle_langs
    assert loaded.rate_limit == original.rate_limit
    assert loaded.keep_video is True


def test_find_config_prefers_the_current_directory(tmp_path: Path) -> None:
    local = tmp_path / "ytdl-studio.toml"
    local.write_text('format = "480p"\n', encoding="utf-8")
    assert find_config(cwd=tmp_path) == local


def test_find_config_explicit(tmp_path: Path) -> None:
    path = tmp_path / "custom.toml"
    path.write_text("", encoding="utf-8")
    assert find_config(tmp_path, explicit=path) == path


def test_config_search_paths_include_the_current_directory(tmp_path: Path) -> None:
    paths = default_config_paths(tmp_path)
    assert paths[0] == tmp_path / "ytdl-studio.toml"
    assert len(paths) >= 2


def test_resolve_config_path_is_absolute_or_home_based(tmp_path: Path) -> None:
    path = resolve_config_path(tmp_path)
    assert path.name == "config.toml"
    assert path.is_absolute()
