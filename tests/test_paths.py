"""Filename safety, collision handling, templates, and formatting tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from ytdl.errors import InvalidOptionError
from ytdl.paths import (
    DEFAULT_MUSIC_TEMPLATE,
    DEFAULT_VIDEO_TEMPLATE,
    format_bytes,
    format_duration,
    format_eta,
    format_speed,
    parse_rate_limit,
    resolve_output_template,
    safe_path_component,
    sanitize_filename,
    strip_media_suffix,
    truncate_middle,
    unique_path,
)


# ---------------------------------------------------------------------------
# Filename sanitization
# ---------------------------------------------------------------------------
def test_invalid_windows_characters_are_replaced() -> None:
    cleaned = sanitize_filename('a<b>c:d"e|f?g*h.txt')
    assert "<" not in cleaned
    assert ">" not in cleaned
    assert ":" not in cleaned
    assert '"' not in cleaned
    assert "|" not in cleaned
    assert "?" not in cleaned
    assert "*" not in cleaned
    assert cleaned.endswith(".txt")
    assert cleaned.startswith("a_b_c_d_e_f_g_h")


def test_path_separators_do_not_create_directories() -> None:
    cleaned = sanitize_filename("folder/subfolder\\file.mp3")
    assert "/" not in cleaned
    assert "\\" not in cleaned


def test_control_characters_are_neutralized() -> None:
    cleaned = sanitize_filename("bad\x00name\x1f.mp4")
    assert "\x00" not in cleaned
    assert "\x1f" not in cleaned


@pytest.mark.parametrize("name", ["CON", "con.txt", "NUL", "COM1.mp3", "LPT9"])
def test_reserved_dos_names_are_prefixed(name: str) -> None:
    cleaned = sanitize_filename(name)
    assert cleaned.upper().startswith("_")


def test_trailing_dots_and_spaces_are_removed() -> None:
    assert sanitize_filename("report...   ") == "report"
    assert sanitize_filename("video.mp4. ") == "video.mp4"


def test_blank_names_fall_back_to_untitled() -> None:
    assert sanitize_filename("   ") == "untitled"
    assert sanitize_filename("<>") == "untitled"


def test_long_names_keep_their_extension() -> None:
    cleaned = sanitize_filename("x" * 400 + ".mp3", max_length=40)
    assert len(cleaned) <= 40
    assert cleaned.endswith(".mp3")
    assert cleaned.startswith("x")


def test_unicode_is_normalized_and_preserved() -> None:
    # e + combining acute accent -> precomposed é
    cleaned = sanitize_filename("Cafe\u0301.mp3")
    assert cleaned == "Café.mp3"


def test_max_length_must_be_sensible() -> None:
    with pytest.raises(ValueError):
        sanitize_filename("x", max_length=4)


def test_safe_path_component_matches_filename_rules() -> None:
    assert safe_path_component("a:b") == "a_b"


# ---------------------------------------------------------------------------
# Collisions
# ---------------------------------------------------------------------------
def test_unique_path_returns_the_original_when_free(tmp_path: Path) -> None:
    target = tmp_path / "song.mp3"
    assert unique_path(target) == target


def test_unique_path_adds_a_number_for_files(tmp_path: Path) -> None:
    target = tmp_path / "song.mp3"
    target.write_text("first", encoding="utf-8")
    (tmp_path / "song (1).mp3").write_text("second", encoding="utf-8")
    assert unique_path(target) == tmp_path / "song (2).mp3"


def test_unique_path_handles_directories(tmp_path: Path) -> None:
    target = tmp_path / "album"
    target.mkdir()
    assert unique_path(target) == tmp_path / "album (1)"


def test_unique_path_accepts_a_callable(tmp_path: Path) -> None:
    taken = {tmp_path / "a.txt"}

    def exists(path: Path) -> bool:
        return path in taken

    assert unique_path(tmp_path / "a.txt", exists=exists) == tmp_path / "a (1).txt"


def test_unique_path_accepts_a_collection(tmp_path: Path) -> None:
    existing = tmp_path / "b.txt"
    existing.write_text("x", encoding="utf-8")
    assert unique_path(tmp_path / "b.txt", exists={str(existing)}) == tmp_path / "b (1).txt"


# ---------------------------------------------------------------------------
# Output templates
# ---------------------------------------------------------------------------
def test_default_video_template_matches_documentation() -> None:
    assert DEFAULT_VIDEO_TEMPLATE == (
        "%(channel,uploader|Unknown)s/"
        "%(upload_date>%Y,release_date>%Y|Unknown)s/"
        "%(title)s [%(id)s].%(ext)s"
    )


def test_default_music_template_matches_documentation() -> None:
    assert DEFAULT_MUSIC_TEMPLATE == (
        "%(artist,creator,uploader|Unknown Artist)s/"
        "%(album|Unknown Album)s/"
        "%(track_number,playlist_index|0)02d - %(title)s.%(ext)s"
    )


def test_relative_templates_live_under_the_base_directory(tmp_path: Path) -> None:
    result = resolve_output_template(tmp_path, "%(title)s.%(ext)s")
    assert result == str(tmp_path / "%(title)s.%(ext)s")


def test_relative_default_uses_video_template(tmp_path: Path) -> None:
    result = resolve_output_template(tmp_path)
    assert result == str(tmp_path / DEFAULT_VIDEO_TEMPLATE)


def test_music_mode_uses_the_music_template(tmp_path: Path) -> None:
    result = resolve_output_template(tmp_path, music=True)
    assert result == str(tmp_path / DEFAULT_MUSIC_TEMPLATE)


def test_absolute_templates_are_not_rebased(tmp_path: Path) -> None:
    absolute = tmp_path / "absolute" / "%(title)s.%(ext)s"
    assert resolve_output_template(tmp_path / "base", str(absolute)) == str(absolute)


def test_empty_template_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(InvalidOptionError):
        resolve_output_template(tmp_path, "   ")


# ---------------------------------------------------------------------------
# Rate limits and formatting
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        (0, None),
        ("0", None),
        ("", None),
        ("unlimited", None),
        ("none", None),
        (1024, 1024),
        ("500K", 500 * 1024),
        ("500k", 500 * 1024),
        ("10M", 10 * 1024**2),
        ("10M/s", 10 * 1024**2),
        ("1.5MiB", int(1.5 * 1024**2)),
        ("2GiB", 2 * 1024**3),
    ],
)
def test_parse_rate_limit(value: object, expected: int | None) -> None:
    assert parse_rate_limit(value) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["12XB", "fast", "10MBs", "-5M"])
def test_parse_rate_limit_rejects_garbage(value: str) -> None:
    with pytest.raises(InvalidOptionError):
        parse_rate_limit(value)


def test_parse_rate_limit_rejects_negative_integers() -> None:
    with pytest.raises(InvalidOptionError):
        parse_rate_limit(-1)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "—"),
        (-1, "—"),
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1.00 KiB"),
        (1536, "1.50 KiB"),
        (1024**2, "1.00 MiB"),
        (int(1.25 * 1024**3), "1.25 GiB"),
    ],
)
def test_format_bytes(value: int | None, expected: str) -> None:
    assert format_bytes(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, "—"),
        (-10, "0:00"),
        (0, "0:00"),
        (59, "0:59"),
        (60, "1:00"),
        (3599, "59:59"),
        (3600, "1:00:00"),
        (3661, "1:01:01"),
        (213.4, "3:33"),
    ],
)
def test_format_duration(value: float | None, expected: str) -> None:
    assert format_duration(value) == expected


def test_format_speed_and_eta() -> None:
    assert format_speed(None) == "—"
    assert format_speed(1024) == "1.00 KiB/s"
    assert format_eta(None) == "—"
    assert format_eta(-1) == "—"
    assert format_eta(61) == "1:01"


def test_truncate_middle() -> None:
    assert truncate_middle("short", 20) == "short"
    assert truncate_middle("abcdefghij", 5) == "ab…ij"
    assert len(truncate_middle("x" * 100, 11)) == 11


def test_strip_media_suffix() -> None:
    assert strip_media_suffix("video.mp4") == Path("video")
    assert strip_media_suffix("song.flac") == Path("song")
    assert strip_media_suffix("notes.txt") == Path("notes.txt")
