"""Failure-tolerant browser cookie import for the graphical studio.

The studio should never fail because a browser's cookie store is locked,
encrypted with a key that cannot be read, or simply not installed.  This module
tries the browsers in a practical order, materializes the first usable cookie
jar into a private temporary file, and otherwise returns ``None`` so a download
can still proceed anonymously.

The resolved file is a normal Netscape ``cookies.txt`` that yt-dlp can read, so
individual workers never have to touch a browser profile themselves.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from yt_dlp.cookies import YoutubeDLCookieJar, extract_cookies_from_browser

AUTO = "auto"
NONE = "none"

# Firefox first: it is readable without DPAPI and is the most reliable source
# on Windows.  Edge is deliberately after Chrome because its encrypted store
# fails on some Windows profiles.
_BROWSERS: tuple[str, ...] = (
    "firefox",
    "chrome",
    "chromium",
    "edge",
    "brave",
    "opera",
    "vivaldi",
    "whale",
    "safari",
)
_YOUTUBE_HOSTS = (
    "youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "googlevideo.com",
)
_CACHE_TTL = 300.0
_COOKIE_DIR = Path(tempfile.gettempdir()) / "ytdl-studio"
_COOKIE_FILE = _COOKIE_DIR / "cookies.txt"
_LOCK = threading.RLock()
_CACHE: dict[str, Any] = {}


class _QuietLogger:
    """Swallow yt-dlp's cookie diagnostic output."""

    def debug(self, message: str) -> None:
        return None

    def info(self, message: str) -> None:
        return None

    def warning(self, message: str) -> None:
        return None

    def error(self, message: str) -> None:
        return None


_QUIET = _QuietLogger()


def _is_youtube(url: str) -> bool:
    host = (urlparse(str(url)).hostname or "").lower()
    return any(host == item or host.endswith(f".{item}") for item in _YOUTUBE_HOSTS)


def _is_live(cookie: Any) -> bool:
    try:
        return not cookie.is_expired()
    except (AttributeError, TypeError, ValueError):
        return True


def _cookie_score(jar: Any, *, youtube: bool) -> int:
    total = 0
    matching = 0
    for cookie in jar:
        if not _is_live(cookie):
            continue
        total += 1
        domain = str(getattr(cookie, "domain", "") or "").lower()
        if "youtube.com" in domain or domain.endswith("google.com"):
            matching += 1
    return matching if youtube else total


def _browser_order(preferred: str | None) -> list[str]:
    explicit = preferred if preferred and preferred not in {AUTO, NONE} else None
    if explicit and explicit in _BROWSERS:
        return [explicit, *[item for item in _BROWSERS if item != explicit]]
    if explicit:
        return [explicit, *_BROWSERS]
    return list(_BROWSERS)


def _load_jar(browser: str) -> Any | None:
    try:
        jar = extract_cookies_from_browser(browser, logger=_QUIET)
    except Exception:
        # Missing profile, locked database, unsupported encryption, keyring
        # failure: all of these simply mean "try the next browser".
        return None
    try:
        if len(jar) <= 0:
            return None
    except TypeError:
        return None
    return jar


def _pick(preferred: str | None, *, youtube: bool) -> tuple[str, Any, int] | None:
    explicit = preferred if preferred and preferred not in {AUTO, NONE} else None
    best: tuple[str, Any, int] | None = None
    for browser in _browser_order(preferred):
        jar = _load_jar(browser)
        if jar is None:
            continue
        score = _cookie_score(jar, youtube=youtube)
        if best is None or score > best[2]:
            best = (browser, jar, score)
        if explicit and browser == explicit and score > 0:
            return best
        if not explicit and score > 0:
            return best
    if best is None:
        return None
    if youtube and best[2] <= 0:
        return None
    return best


def _valid_cookie_file(path: Path) -> bool:
    try:
        jar = YoutubeDLCookieJar(str(path))
        jar.load(ignore_discard=True, ignore_expires=True)
    except Exception:
        return False
    return len(jar) > 0


def _save_jar(jar: Any) -> None:
    _COOKIE_DIR.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(_COOKIE_DIR, 0o700)
    temporary = _COOKIE_FILE.with_name(
        f".{_COOKIE_FILE.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        jar.save(str(temporary), ignore_discard=True, ignore_expires=True)
        with contextlib.suppress(OSError):
            os.chmod(temporary, 0o600)
        os.replace(temporary, _COOKIE_FILE)
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)


def resolve_cookies(
    preferred: str | None = None,
    *,
    cookie_file: str | Path | None = None,
    urls: Any = (),
    force: bool = False,
) -> Path | None:
    """Return a yt-dlp cookie file, or ``None`` when cookies are unavailable.

    ``preferred`` may be ``auto``, a browser name, or ``none``.  An existing
    explicit cookie file always wins.  A missing one is ignored rather than
    reported, and an unreadable browser is skipped.
    """

    if cookie_file:
        try:
            candidate = Path(os.path.expanduser(str(cookie_file)))
            if candidate.is_file() and _valid_cookie_file(candidate):
                return candidate
        except OSError:
            pass

    mode = str(preferred or AUTO).strip().lower()
    if mode == NONE:
        return None

    if isinstance(urls, (str, Path)):
        urls = [str(urls)]
    youtube = any(_is_youtube(str(url)) for url in urls)
    cache_key = f"{mode}:{'youtube' if youtube else 'any'}"
    now = time.monotonic()
    with _LOCK:
        cached = (
            _CACHE.get("key") == cache_key
            and now - float(_CACHE.get("stamp") or 0.0) < _CACHE_TTL
            and _COOKIE_FILE.is_file()
        )
        if cached and not force:
            return _COOKIE_FILE

        picked = _pick(mode, youtube=youtube)
        if picked is None:
            _CACHE.clear()
            return None

        browser, jar, score = picked
        try:
            _save_jar(jar)
        except OSError:
            _CACHE.clear()
            return None
        _CACHE.update(
            key=cache_key,
            stamp=now,
            browser=browser,
            count=score,
            youtube=youtube,
        )
        return _COOKIE_FILE


def cookie_status() -> dict[str, Any]:
    """Describe the most recently resolved cookie source (for diagnostics)."""

    with _LOCK:
        return {
            "browser": _CACHE.get("browser"),
            "count": int(_CACHE.get("count") or 0),
            "file": str(_COOKIE_FILE) if _COOKIE_FILE.is_file() else None,
        }


def clear_cookie_cache() -> None:
    """Forget the in-memory result and remove the temporary cookie file."""

    with _LOCK:
        _CACHE.clear()
        with contextlib.suppress(OSError):
            _COOKIE_FILE.unlink(missing_ok=True)


def _cleanup() -> None:
    clear_cookie_cache()
    with contextlib.suppress(OSError):
        _COOKIE_DIR.rmdir()


atexit.register(_cleanup)
