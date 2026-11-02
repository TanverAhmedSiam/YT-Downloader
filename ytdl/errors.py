"""Typed exceptions and friendly diagnostics for ytdl-studio.

The downloader deliberates keeps yt-dlp's exceptions at its boundary and maps
them to errors with actionable hints.  The CLI is then free to render a short
message instead of a wall of extractor traceback.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable


@dataclass(slots=True)
class ErrorInfo:
    """A user-facing error description."""

    title: str
    message: str
    hint: str | None = None


class YtdlStudioError(Exception):
    """Base class for expected ytdl-studio failures."""

    exit_code = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def info(self) -> ErrorInfo:
        return ErrorInfo(title=self.__class__.__name__, message=self.message, hint=self.hint)


class ConfigError(YtdlStudioError):
    """The configuration file could not be read or validated."""


class InvalidPresetError(YtdlStudioError):
    """The requested format preset does not exist."""


class InvalidOptionError(YtdlStudioError):
    """A command-line or config option is invalid."""


class MissingDependencyError(YtdlStudioError):
    """A required external executable is missing."""


class MissingFFmpegError(MissingDependencyError):
    """FFmpeg or ffprobe is not available."""


class FFmpegError(YtdlStudioError):
    """FFmpeg failed to process a file."""


class ProbeError(YtdlStudioError):
    """ffprobe could not inspect a media file."""


class MetadataError(YtdlStudioError):
    """Tags, artwork, chapters, or subtitles could not be written."""


class DownloadError(YtdlStudioError):
    """A download failed."""


class PrivateVideoError(DownloadError):
    """The video is private."""


class AgeRestrictedError(DownloadError):
    """The video requires an age-verified account."""


class GeoBlockedError(DownloadError):
    """The video is unavailable in this region."""


class PremiumRequiredError(DownloadError):
    """The media requires a paid membership."""


class UnavailableError(DownloadError):
    """The video was removed or is otherwise unavailable."""


class RateLimitedError(DownloadError):
    """YouTube is throttling requests."""


class NetworkError(DownloadError):
    """A network request failed."""


class DiskSpaceError(YtdlStudioError):
    """There is not enough free disk space."""


class FormatUnavailableError(DownloadError):
    """No suitable video/audio stream was found."""


class ArchiveReadError(YtdlStudioError):
    """The archive file could not be read or written."""


class DownloadCancelledError(YtdlStudioError):
    """The user cancelled a running download."""


_INSTALL_HINTS = {
    "cookies": (
        "Sign in to YouTube in your browser, then choose Automatic browser cookies "
        "in the studio. On the CLI, use `--cookies-from-browser firefox`; a "
        "cookies.txt file also works with `--cookies FILE`."
    ),
    "network": "Check your internet connection, VPN/proxy, and try again.",
    "ffmpeg": (
        "Install FFmpeg and make sure `ffmpeg` and `ffprobe` are on PATH. "
        "See `ytdl doctor` for platform-specific instructions."
    ),
    "disk": "Free some disk space or choose another directory with `--dir`.",
    "format": "Run `ytdl formats URL` to inspect available stream IDs.",
    "retry": "Wait a moment and retry. Resume will continue partial downloads.",
}


def _text(value: object) -> str:
    return str(value).strip()


def classify_error(error: BaseException | str) -> YtdlStudioError:
    """Translate a yt-dlp/ffmpeg error into a typed, friendly exception.

    This intentionally uses conservative substring matching.  Unknown errors
    are returned as :class:`DownloadError` with the original message preserved.
    """

    raw = _text(error)
    lowered = raw.lower()

    if "ffmpeg" in lowered and ("not found" in lowered or "not installed" in lowered):
        return MissingFFmpegError(
            "FFmpeg is required but was not found.",
            hint=_INSTALL_HINTS["ffmpeg"],
        )
    if "no space left" in lowered or "disk full" in lowered or "not enough space" in lowered:
        return DiskSpaceError(
            "The download ran out of disk space.",
            hint=_INSTALL_HINTS["disk"],
        )
    if any(
        phrase in lowered
        for phrase in (
            "dpapi",
            "failed to decrypt",
            "cookie database",
            "could not copy chrome cookie",
        )
    ):
        return DownloadError(
            "The browser cookie store could not be read.",
            hint=(
                "Choose Automatic browser cookies in the studio, or use a "
                "cookies.txt file. Firefox is the most reliable source."
            ),
        )
    if "private video" in lowered or "this video is private" in lowered:
        return PrivateVideoError(
            "This video is private and cannot be downloaded.",
            hint=_INSTALL_HINTS["cookies"],
        )
    if "age" in lowered and any(
        phrase in lowered
        for phrase in ("confirm your age", "age-restricted", "age restricted", "inappropriate for some users")
    ):
        return AgeRestrictedError(
            "This video is age-restricted.",
            hint=_INSTALL_HINTS["cookies"],
        )
    if any(
        phrase in lowered
        for phrase in (
            "not available in your country",
            "blocked it in your country",
            "geo restricted",
            "geo-restricted",
            "not made this video available in your country",
        )
    ):
        return GeoBlockedError(
            "This video is geo-blocked in your region.",
            hint="Try a VPN or a proxy with `--proxy URL`.",
        )
    if any(
        phrase in lowered
        for phrase in (
            "music premium",
            "premium members",
            "paid membership",
            "channel members",
            "members-only",
        )
    ):
        return PremiumRequiredError(
            "This media requires a paid membership.",
            hint=_INSTALL_HINTS["cookies"],
        )
    if "http error 429" in lowered or "too many requests" in lowered:
        return RateLimitedError(
            "YouTube is rate-limiting this connection (HTTP 429).",
            hint="Wait a few minutes, use `--rate-limit`, or try a different network/proxy.",
        )
    if "requested format is not available" in lowered or "no video formats found" in lowered:
        return FormatUnavailableError(
            "No matching format is available for this media.",
            hint=_INSTALL_HINTS["format"],
        )
    if any(
        phrase in lowered
        for phrase in (
            "video unavailable",
            "this video is unavailable",
            "has been removed",
            "does not exist",
            "is not a valid url",
            "unsupported url",
        )
    ):
        return UnavailableError(
            "This video is unavailable or the URL is not supported.",
            hint="Double-check the URL, or run `ytdl info URL` to inspect it.",
        )
    if any(
        phrase in lowered
        for phrase in (
            "timed out",
            "timeout",
            "temporary failure",
            "connection reset",
            "connection aborted",
            "network is unreachable",
            "getaddrinfo",
            "ssl:",
            "http error 5",
            "unable to download webpage",
            "failed to resolve",
        )
    ):
        return NetworkError(
            "A network error interrupted the download.",
            hint=_INSTALL_HINTS["network"],
        )
    if "login required" in lowered or "sign in" in lowered or "cookies" in lowered:
        return DownloadError(
            "YouTube requires a signed-in session for this video.",
            hint=_INSTALL_HINTS["cookies"],
        )
    return DownloadError(raw or "The download failed for an unknown reason.")


def combine_errors(errors: Iterable[BaseException]) -> list[str]:
    """Return de-duplicated messages from an error iterable."""

    seen: set[str] = set()
    result: list[str] = []
    for error in errors:
        message = str(error).strip()
        if message and message not in seen:
            seen.add(message)
            result.append(message)
    return result
