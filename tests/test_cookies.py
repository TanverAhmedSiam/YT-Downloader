"""Cookie source selection must never turn a locked profile into a failure."""

from __future__ import annotations

from pathlib import Path

import pytest

from ytdl import cookies


class FakeCookie:
    def __init__(self, domain: str) -> None:
        self.domain = domain

    def is_expired(self) -> bool:
        return False


class FakeJar:
    def __init__(self, count: int = 1, domain: str = ".youtube.com") -> None:
        self.cookies = [FakeCookie(domain) for _ in range(count)]

    def __len__(self) -> int:
        return len(self.cookies)

    def __iter__(self):
        return iter(self.cookies)

    def save(self, filename: str, **kwargs: object) -> None:
        Path(filename).write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")


@pytest.fixture()
def cookie_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(cookies, "_COOKIE_DIR", tmp_path)
    monkeypatch.setattr(cookies, "_COOKIE_FILE", tmp_path / "cookies.txt")
    cookies.clear_cookie_cache()
    yield cookies
    cookies.clear_cookie_cache()


def test_quiet_logger_matches_ytdlp_protocol(cookie_env) -> None:
    for name in ("debug", "info", "warning", "error"):
        assert callable(getattr(cookie_env._QUIET, name))


def test_unreadable_browser_falls_back_to_firefox(
    cookie_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def extract(browser: str, **kwargs: object) -> FakeJar:
        calls.append(browser)
        if browser in {"edge", "chrome", "chromium"}:
            raise RuntimeError("Failed to decrypt with DPAPI")
        return FakeJar()

    monkeypatch.setattr(cookie_env, "extract_cookies_from_browser", extract)
    path = cookie_env.resolve_cookies(
        "edge",
        urls=["https://www.youtube.com/watch?v=abc"],
        force=True,
    )
    assert path is not None
    assert path.is_file()
    assert cookie_env.cookie_status()["browser"] == "firefox"
    assert calls[0] == "edge"
    assert "firefox" in calls


def test_no_readable_browser_returns_none(cookie_env, monkeypatch: pytest.MonkeyPatch) -> None:
    def extract(browser: str, **kwargs: object) -> FakeJar:
        raise RuntimeError("cookie database is locked")

    monkeypatch.setattr(cookie_env, "extract_cookies_from_browser", extract)
    assert (
        cookie_env.resolve_cookies(
            "auto",
            urls=["https://youtu.be/abc"],
            force=True,
        )
        is None
    )
    assert cookie_env.cookie_status()["browser"] is None


def test_existing_cookie_file_takes_priority(
    cookie_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    explicit = tmp_path / "exported-cookies.txt"
    explicit.write_text(
        "# Netscape HTTP Cookie File\n"
        ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\tfake-session\n",
        encoding="utf-8",
    )
    calls: list[str] = []

    def extract(browser: str, **kwargs: object) -> FakeJar:
        calls.append(browser)
        return FakeJar()

    monkeypatch.setattr(cookie_env, "extract_cookies_from_browser", extract)
    result = cookie_env.resolve_cookies(
        "chrome",
        cookie_file=explicit,
        urls=["https://youtu.be/abc"],
        force=True,
    )
    assert result == explicit
    assert calls == []


def test_missing_cookie_file_falls_back_to_browser(
    cookie_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        cookie_env,
        "extract_cookies_from_browser",
        lambda browser, **kwargs: FakeJar(),
    )
    result = cookie_env.resolve_cookies(
        "auto",
        cookie_file=tmp_path / "missing.txt",
        urls=["https://youtu.be/abc"],
        force=True,
    )
    assert result is not None
    assert result.is_file()


def test_malformed_cookie_file_falls_back_to_browser(
    cookie_env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    invalid = tmp_path / "not-cookies.txt"
    invalid.write_text("this is not a Netscape cookie file\n", encoding="utf-8")
    monkeypatch.setattr(
        cookie_env,
        "extract_cookies_from_browser",
        lambda browser, **kwargs: FakeJar(),
    )
    result = cookie_env.resolve_cookies(
        "auto",
        cookie_file=invalid,
        urls=["https://youtu.be/abc"],
        force=True,
    )
    assert result is not None
    assert result != invalid
    assert result.is_file()


def test_youtube_requires_youtube_cookies(
    cookie_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        cookie_env,
        "extract_cookies_from_browser",
        lambda browser, **kwargs: FakeJar(domain=".example.com"),
    )
    assert (
        cookie_env.resolve_cookies(
            "auto",
            urls=["https://example.com/video"],
            force=True,
        )
        is not None
    )
    assert (
        cookie_env.resolve_cookies(
            "auto",
            urls=["https://www.youtube.com/watch?v=abc"],
            force=True,
        )
        is None
    )


def test_cookie_none_mode_skips_browsers(cookie_env, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    def extract(browser: str, **kwargs: object) -> FakeJar:
        calls.append(browser)
        return FakeJar()

    monkeypatch.setattr(cookie_env, "extract_cookies_from_browser", extract)
    assert cookie_env.resolve_cookies("none", urls=["https://youtu.be/abc"]) is None
    assert calls == []


def test_service_replaces_browser_with_readable_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from ytdl.downloader import DownloadOptions
    from ytdl.gui import service
    from ytdl.presets import resolve_preset

    options = DownloadOptions(
        preset=resolve_preset("best"),
        output_template=str(tmp_path / "%(title)s.%(ext)s"),
        download_dir=tmp_path,
        cookies_from_browser="edge",
    )
    readable = tmp_path / "cookies.txt"
    readable.write_text("# Netscape HTTP Cookie File\n", encoding="utf-8")
    monkeypatch.setattr(service, "resolve_cookies", lambda *args, **kwargs: readable)
    service._apply_cookie_source(options, ["https://youtu.be/abc"])
    assert options.cookies == str(readable)
    assert options.cookies_from_browser is None

    monkeypatch.setattr(service, "resolve_cookies", lambda *args, **kwargs: None)
    service._apply_cookie_source(options, ["https://youtu.be/abc"])
    assert options.cookies is None
    assert options.cookies_from_browser is None
