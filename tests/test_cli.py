"""Argument normalization and option validation tests for the CLI."""

from __future__ import annotations

import pytest

from ytdl.cli import (
    DEFAULT_SPONSORBLOCK,
    _looks_like_url,
    _validate_browser,
    _validate_sponsorblock,
    normalize_argv,
)
from ytdl.errors import InvalidOptionError


def test_plain_url_gets_the_implicit_download_command() -> None:
    assert normalize_argv(["https://youtu.be/abc"]) == [
        "download",
        "https://youtu.be/abc",
    ]


def test_multiple_urls_keep_their_order() -> None:
    urls = ["https://youtu.be/a", "https://youtu.be/b"]
    assert normalize_argv(urls) == ["download", *urls]


@pytest.mark.parametrize(
    "command",
    ["download", "audio", "playlist", "batch", "info", "formats", "doctor"],
)
def test_explicit_commands_are_preserved(command: str) -> None:
    argv = [command, "value"]
    assert normalize_argv(argv) == argv


def test_help_and_version_are_not_given_a_command() -> None:
    assert normalize_argv(["--help"]) == ["--help"]
    assert normalize_argv(["--version"]) == ["--version"]
    assert normalize_argv(["-h"]) == ["-h"]


def test_bare_subs_flag_becomes_english() -> None:
    assert normalize_argv(["URL", "--subs"]) == ["download", "URL", "--subs=en"]


def test_bare_subs_before_a_url_is_not_consumed_by_the_flag() -> None:
    assert normalize_argv(["--subs", "https://youtu.be/abc"]) == [
        "download",
        "--subs=en",
        "https://youtu.be/abc",
    ]


def test_subs_with_explicit_languages_is_untouched() -> None:
    assert normalize_argv(["URL", "--subs", "en,de"]) == [
        "download",
        "URL",
        "--subs",
        "en,de",
    ]


def test_bare_sponsorblock_uses_the_default_categories() -> None:
    assert normalize_argv(["URL", "--sponsorblock"]) == [
        "download",
        "URL",
        f"--sponsorblock={DEFAULT_SPONSORBLOCK}",
    ]


def test_sponsorblock_with_explicit_categories_is_untouched() -> None:
    assert normalize_argv(["URL", "--sponsorblock", "intro,outro"]) == [
        "download",
        "URL",
        "--sponsorblock",
        "intro,outro",
    ]


def test_sponsorblock_before_a_url_uses_the_default() -> None:
    assert normalize_argv(["--sponsorblock", "https://youtu.be/abc"]) == [
        "download",
        f"--sponsorblock={DEFAULT_SPONSORBLOCK}",
        "https://youtu.be/abc",
    ]


@pytest.mark.parametrize(
    "value",
    [
        "https://www.youtube.com/watch?v=x",
        "http://example.com",
        "www.youtube.com/watch?v=x",
        "youtu.be/x",
        "youtube.com/watch?v=x",
        "music.youtube.com/watch?v=x",
        "example.com/path",
    ],
)
def test_url_detection(value: str) -> None:
    assert _looks_like_url(value)


@pytest.mark.parametrize("value", ["en", "intro,outro", "not a url", "file"])
def test_non_url_detection(value: str) -> None:
    assert not _looks_like_url(value)


def test_sponsorblock_validation() -> None:
    assert _validate_sponsorblock("sponsor,selfpromo") == ("sponsor", "selfpromo")
    assert _validate_sponsorblock(" sponsor , selfpromo , sponsor ") == (
        "sponsor",
        "selfpromo",
    )
    assert _validate_sponsorblock(None) == ()
    assert _validate_sponsorblock("") == ()


def test_sponsorblock_all_expands_to_every_category() -> None:
    categories = _validate_sponsorblock("all")
    assert "sponsor" in categories
    assert "poi_highlight" in categories
    assert len(categories) > 5


def test_unknown_sponsorblock_category_is_rejected() -> None:
    with pytest.raises(InvalidOptionError) as excinfo:
        _validate_sponsorblock("sponsor,bogus")
    assert "bogus" in str(excinfo.value)


def test_browser_validation() -> None:
    assert _validate_browser("Chrome") == "chrome"
    assert _validate_browser("  edge ") == "edge"
    assert _validate_browser(None) is None
    assert _validate_browser("") is None
    with pytest.raises(InvalidOptionError):
        _validate_browser("netscape")
