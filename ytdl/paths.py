"""Output path templates, safe filenames, and collision handling."""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path
from typing import Final

from .errors import InvalidOptionError

DEFAULT_VIDEO_TEMPLATE: Final[str] = (
    "%(channel,uploader|Unknown)s/"
    "%(upload_date>%Y,release_date>%Y|Unknown)s/"
    "%(title)s [%(id)s].%(ext)s"
)
DEFAULT_MUSIC_TEMPLATE: Final[str] = (
    "%(artist,creator,uploader|Unknown Artist)s/"
    "%(album|Unknown Album)s/"
    "%(track_number,playlist_index|0)02d - %(title)s.%(ext)s"
)
DEFAULT_DOWNLOAD_DIR: Final[str] = "downloads"

_WINDOWS_INVALID = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_RATE_RE = re.compile(r"^\s*(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>[kmgt]?i?b?(?:/s)?)?\s*$", re.I)


def sanitize_filename(name: str, *, max_length: int = 180, replacement: str = "_") -> str:
    """Return a Windows-safe, cross-platform-safe filename.

    The function is conservative: invalid characters become *replacement*,
    control characters disappear, Unicode is normalized to NFC, trailing dots
    and spaces are removed, reserved DOS device names are prefixed, and the
    result is trimmed without leaving a broken extension.  It works on a file
    *name*, not a path; separators are sanitized rather than creating folders.
    """

    if max_length < 8:
        raise ValueError("max_length must be at least 8")
    if replacement == "":
        replacement = "_"

    normalized = unicodedata.normalize("NFC", str(name))
    normalized = normalized.replace("\r", " ").replace("\n", " ")
    cleaned = _WINDOWS_INVALID.sub(replacement, normalized)
    cleaned = cleaned.replace(replacement + replacement, replacement)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned or not cleaned.strip(replacement + " ."):
        cleaned = "untitled"

    stem, suffix = _split_filename(cleaned)
    if stem.upper() in _WINDOWS_RESERVED:
        stem = f"_{stem}"
    if not stem:
        stem = "untitled"

    # Preserve the extension where possible when trimming.
    if len(stem) + len(suffix) > max_length:
        allowed_stem = max(1, max_length - len(suffix))
        stem = stem[:allowed_stem].rstrip(" .")
        if not stem:
            stem = "untitled"
    result = f"{stem}{suffix}".strip(" .")
    if not result:
        result = "untitled"
    return result


def _split_filename(name: str) -> tuple[str, str]:
    """Split a filename into stem and suffix without treating dotfiles oddly."""

    if name.startswith(".") and name.count(".") == 1:
        return name, ""
    pos = name.rfind(".")
    if pos <= 0 or pos == len(name) - 1:
        return name, ""
    suffix = name[pos:]
    if len(suffix) > 16 or "/" in suffix or "\\" in suffix:
        return name, ""
    return name[:pos], suffix


def safe_path_component(component: str, *, max_length: int = 180) -> str:
    """Sanitize one directory or filename component from a yt-dlp template."""

    return sanitize_filename(component, max_length=max_length)


def unique_path(path: str | Path, *, exists: object = None) -> Path:
    """Return *path* or the first free ``name (1).ext`` / ``dir (1)``.

    ``exists`` can be a callable for tests or a preloaded set of paths.
    """

    candidate = Path(path)
    if exists is None:

        def predicate(p: Path) -> bool:
            return p.exists()

    elif callable(exists):

        def predicate(p: Path) -> bool:
            return bool(exists(p))

    else:
        existing = {str(Path(p).resolve()).lower() for p in exists}  # type: ignore[union-attr]

        def predicate(p: Path) -> bool:
            return str(p.resolve()).lower() in existing

    if not predicate(candidate):
        return candidate

    if candidate.suffix:
        stem = candidate.stem
        suffix = candidate.suffix
        for index in range(1, 100_000):
            alternative = candidate.with_name(f"{stem} ({index}){suffix}")
            if not predicate(alternative):
                return alternative
    else:
        for index in range(1, 100_000):
            alternative = candidate.with_name(f"{candidate.name} ({index})")
            if not predicate(alternative):
                return alternative
    raise OSError(f"Could not find a free filename for {candidate}")


def resolve_output_template(
    base_dir: str | Path,
    template: str | None = None,
    *,
    music: bool = False,
) -> str:
    """Combine the base directory with a yt-dlp output template.

    Absolute templates are respected.  Relative templates live below
    *base_dir*, which is created on demand by the downloader.  The returned
    value is always a string suitable for yt-dlp's ``outtmpl`` option.
    """

    chosen = template or (DEFAULT_MUSIC_TEMPLATE if music else DEFAULT_VIDEO_TEMPLATE)
    chosen = chosen.strip()
    if not chosen:
        raise InvalidOptionError("Output template cannot be empty.")

    expanded = Path(os.path.expandvars(os.path.expanduser(chosen)))
    if expanded.is_absolute() or Path(chosen).drive:
        return str(expanded)

    base = Path(os.path.expandvars(os.path.expanduser(str(base_dir)))).expanduser()
    # Do not resolve symlinks here; users may intentionally use a symlinked
    # media directory.  joinpath also preserves yt-dlp's %(...)s syntax.
    return str(base / chosen)


def parse_rate_limit(value: str | int | None) -> int | None:
    """Convert ``10M``/``1.5MiB``/``500k`` to bytes per second.

    ``None``, ``0`` and an empty string mean unlimited.
    """

    if value is None:
        return None
    if isinstance(value, int):
        if value < 0:
            raise InvalidOptionError("Rate limit cannot be negative.")
        return value or None
    cleaned = value.strip()
    if not cleaned or cleaned.lower() in {"0", "none", "unlimited", "off"}:
        return None
    match = _RATE_RE.match(cleaned)
    if not match:
        raise InvalidOptionError(
            f"Invalid rate limit {value!r}. Examples: 500K, 5M, 1.5M, 10MiB."
        )
    number = float(match.group("number"))
    unit = (match.group("unit") or "").lower().removesuffix("/s").replace("b", "")
    multiplier = {
        "": 1,
        "k": 1024,
        "ki": 1024,
        "m": 1024**2,
        "mi": 1024**2,
        "g": 1024**3,
        "gi": 1024**3,
        "t": 1024**4,
        "ti": 1024**4,
    }.get(unit)
    if multiplier is None:
        raise InvalidOptionError(f"Unknown rate-limit unit in {value!r}.")
    result = int(number * multiplier)
    return result or None


def format_bytes(size: float | int | None) -> str:
    """Human-readable IEC byte size."""

    if size is None:
        return "—"
    value = float(size)
    if value < 0:
        return "—"
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    index = 0
    while value >= 1024 and index < len(units) - 1:
        value /= 1024
        index += 1
    if index == 0:
        return f"{int(value)} B"
    return f"{value:.2f} {units[index]}"


def format_duration(seconds: float | int | None) -> str:
    """Format seconds as ``1:02:03`` or ``2:03``."""

    if seconds is None:
        return "—"
    total = max(0, int(round(float(seconds))))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def format_speed(speed: float | int | None) -> str:
    if not speed:
        return "—"
    return f"{format_bytes(speed)}/s"


def format_eta(seconds: float | int | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    return format_duration(seconds)


def truncate_middle(text: str, width: int = 48) -> str:
    """Shorten a long title for a progress row."""

    if width <= 3 or len(text) <= width:
        return text
    left = (width - 1) // 2
    right = width - left - 1
    return f"{text[:left]}…{text[-right:]}"


def strip_media_suffix(path: str | Path) -> Path:
    """Remove a media suffix, including common double suffixes."""

    candidate = Path(path)
    if candidate.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov", ".gif", ".mp3", ".m4a", ".opus", ".ogg", ".flac", ".wav", ".aac"}:
        return candidate.with_suffix("")
    return candidate
