"""Static catalog and request validation for the graphical studio.

The GUI deliberately reuses the same presets, templates, and validation rules as
the CLI.  Keeping the catalog here means the web client never has to guess which
formats exist or how a combination maps to a yt-dlp selector.
"""

from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any, Final

from ..config import Config
from ..downloader import DownloadOptions
from ..errors import InvalidOptionError
from ..paths import (
    DEFAULT_MUSIC_TEMPLATE,
    DEFAULT_VIDEO_TEMPLATE,
    parse_rate_limit,
    resolve_output_template,
)
from ..presets import AUDIO_PRESETS, MAX_HEIGHT, PRESETS, parse_height, resolve_preset

QUALITY_OPTIONS: Final[list[dict[str, Any]]] = [
    {"id": "best", "label": "Best", "sub": "No cap", "badge": "AUTO"},
    {"id": "8k", "label": "8K", "sub": "4320p", "badge": "MAX"},
    {"id": "4k", "label": "4K", "sub": "2160p", "badge": "UHD"},
    {"id": "1440p", "label": "1440p", "sub": "QHD", "badge": "2K"},
    {"id": "1080p", "label": "1080p", "sub": "Full HD", "badge": "FHD"},
    {"id": "720p", "label": "720p", "sub": "HD", "badge": "HD"},
    {"id": "480p", "label": "480p", "sub": "SD", "badge": "SD"},
    {"id": "360p", "label": "360p", "sub": "Small", "badge": "LOW"},
]

CONTAINER_OPTIONS: Final[list[dict[str, Any]]] = [
    {
        "id": "auto",
        "label": "MP4 · Auto",
        "sub": "Best compatibility",
        "ext": "mp4",
        "icon": "film",
        "preset": "mp4",
    },
    {
        "id": "h264",
        "label": "MP4 · H.264",
        "sub": "Plays everywhere",
        "ext": "mp4",
        "icon": "film",
        "preset": "mp4-h264",
    },
    {
        "id": "h265",
        "label": "MP4 · H.265",
        "sub": "Smaller, HEVC",
        "ext": "mp4",
        "icon": "layers",
        "preset": "mp4-h265",
    },
    {
        "id": "mkv",
        "label": "MKV",
        "sub": "Original streams",
        "ext": "mkv",
        "icon": "box",
        "preset": "mkv",
    },
    {
        "id": "webm",
        "label": "WebM",
        "sub": "Open formats",
        "ext": "webm",
        "icon": "globe",
        "preset": "webm",
    },
    {
        "id": "vp9",
        "label": "WebM · VP9",
        "sub": "Efficient open video",
        "ext": "webm",
        "icon": "globe",
        "preset": "webm-vp9",
    },
    {
        "id": "av1",
        "label": "WebM · AV1",
        "sub": "Next-gen codec",
        "ext": "webm",
        "icon": "sparkles",
        "preset": "webm-av1",
    },
    {
        "id": "mov",
        "label": "MOV",
        "sub": "QuickTime",
        "ext": "mov",
        "icon": "video",
        "preset": "mov",
    },
    {
        "id": "gif",
        "label": "GIF",
        "sub": "Looping animation",
        "ext": "gif",
        "icon": "wand",
        "preset": "gif",
    },
]

AUDIO_OPTIONS: Final[list[dict[str, Any]]] = [
    {
        "id": "mp3",
        "label": "MP3",
        "sub": "Universal · LAME",
        "ext": "mp3",
        "lossy": True,
        "icon": "music",
    },
    {
        "id": "m4a",
        "label": "M4A",
        "sub": "AAC · Apple friendly",
        "ext": "m4a",
        "lossy": True,
        "icon": "music",
    },
    {
        "id": "aac",
        "label": "AAC",
        "sub": "Raw AAC in M4A",
        "ext": "m4a",
        "lossy": True,
        "icon": "music",
    },
    {
        "id": "opus",
        "label": "Opus",
        "sub": "Modern & efficient",
        "ext": "opus",
        "lossy": True,
        "icon": "wave",
    },
    {
        "id": "ogg",
        "label": "OGG",
        "sub": "Vorbis",
        "ext": "ogg",
        "lossy": True,
        "icon": "wave",
    },
    {
        "id": "flac",
        "label": "FLAC",
        "sub": "Lossless",
        "ext": "flac",
        "lossy": False,
        "icon": "layers",
    },
    {
        "id": "wav",
        "label": "WAV",
        "sub": "PCM · Studio",
        "ext": "wav",
        "lossy": False,
        "icon": "wave",
    },
    {
        "id": "alac",
        "label": "ALAC",
        "sub": "Lossless · Apple",
        "ext": "m4a",
        "lossy": False,
        "icon": "layers",
    },
]

SPONSORBLOCK_OPTIONS: Final[list[dict[str, str]]] = [
    {"id": "sponsor", "label": "Sponsor"},
    {"id": "selfpromo", "label": "Self-promo"},
    {"id": "interaction", "label": "Interaction"},
    {"id": "intro", "label": "Intro"},
    {"id": "outro", "label": "Outro"},
    {"id": "preview", "label": "Preview"},
    {"id": "music_offtopic", "label": "Off-topic"},
    {"id": "filler", "label": "Filler"},
]

BROWSER_OPTIONS: Final[list[str]] = [
    "auto",
    "firefox",
    "chrome",
    "chromium",
    "edge",
    "brave",
    "opera",
    "vivaldi",
    "whale",
    "safari",
]

SPONSORBLOCK_IDS: Final[frozenset[str]] = frozenset(
    item["id"] for item in SPONSORBLOCK_OPTIONS
)
BROWSER_IDS: Final[frozenset[str]] = frozenset(BROWSER_OPTIONS)
QUALITY_IDS: Final[frozenset[str]] = frozenset(item["id"] for item in QUALITY_OPTIONS)
CONTAINER_IDS: Final[frozenset[str]] = frozenset(item["id"] for item in CONTAINER_OPTIONS)
AUDIO_IDS: Final[frozenset[str]] = frozenset(item["id"] for item in AUDIO_OPTIONS)

_CONTAINER_BY_PRESET: Final[dict[str, str]] = {
    "mp4": "auto",
    "mp4-h264": "h264",
    "mp4-h265": "h265",
    "mkv": "mkv",
    "webm": "webm",
    "webm-vp9": "vp9",
    "webm-av1": "av1",
    "mov": "mov",
    "gif": "gif",
}
_HEIGHT_TO_QUALITY: Final[dict[int, str]] = {
    4320: "8k",
    2160: "4k",
    1440: "1440p",
    1080: "1080p",
    720: "720p",
    480: "480p",
    360: "360p",
}
_URL_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:https?://|www\.|[\w-]+(?:\.[\w-]+)+/|youtu\.be/|youtube\.com/)", re.I
)


def catalog_payload() -> dict[str, Any]:
    """Everything the client needs to render the format picker."""

    return {
        "quality": QUALITY_OPTIONS,
        "containers": CONTAINER_OPTIONS,
        "audio": AUDIO_OPTIONS,
        "sponsorblock": SPONSORBLOCK_OPTIONS,
        "browsers": BROWSER_OPTIONS,
    }


def _absolute_directory(value: str | None, fallback: str) -> str:
    raw = (value or fallback or "downloads").strip()
    raw = os.path.expandvars(os.path.expanduser(raw))
    path = Path(raw)
    if not path.is_absolute():
        path = Path.cwd() / path
    try:
        return str(path.resolve())
    except OSError:
        return str(path.absolute())


def _default_archive(directory: str) -> str:
    return str(Path(directory) / ".ytdl-archive.txt")


def defaults_from_config(config: Config) -> dict[str, Any]:
    """Map a TOML config onto the GUI's controls."""

    mode = "audio" if config.music else "video"
    audio_format = "mp3"
    quality = "best"
    container = "auto"

    configured = (config.format or "best").strip().lower()
    if configured in AUDIO_PRESETS:
        mode = "audio"
        audio_format = "ogg" if configured == "vorbis" else configured
    elif configured in QUALITY_IDS:
        quality = configured
    elif configured in _CONTAINER_BY_PRESET:
        container = _CONTAINER_BY_PRESET[configured]
    else:
        try:
            preset = resolve_preset(configured)
        except Exception:
            preset = PRESETS["best"]
        if preset.is_audio:
            mode = "audio"
            audio_format = preset.name if preset.name in AUDIO_IDS else "mp3"
        else:
            container = _CONTAINER_BY_PRESET.get(preset.name, "auto")
            if preset.target_height in _HEIGHT_TO_QUALITY:
                quality = _HEIGHT_TO_QUALITY[preset.target_height]

    if config.quality:
        try:
            height = parse_height(config.quality)
        except Exception:
            height = None
        if height in _HEIGHT_TO_QUALITY:
            quality = _HEIGHT_TO_QUALITY[height]
        elif height is not None:
            quality = "best"

    directory = _absolute_directory(config.directory, "downloads")
    archive_path = (
        str(Path(os.path.expanduser(config.archive)))
        if config.archive
        else _default_archive(directory)
    )

    return {
        "mode": mode,
        "quality": quality,
        "container": container,
        "audio_format": audio_format,
        "bitrate": config.bitrate,
        "music": config.music if mode == "audio" else False,
        "directory": directory,
        "template_mode": "custom" if config.output else "default",
        "template": config.output or "",
        "subtitles": config.subtitles,
        "subtitle_langs": config.subtitle_langs,
        "thumbnail": config.thumbnail,
        "metadata": config.metadata,
        "chapters": config.chapters,
        "sponsorblock": [
            part.strip()
            for part in (config.sponsorblock or "").split(",")
            if part.strip() in SPONSORBLOCK_IDS
        ],
        "playlist_start": config.playlist_start,
        "playlist_end": config.playlist_end,
        "shuffle": config.shuffle,
        "reverse": config.reverse,
        "concurrent": config.concurrent,
        "rate_limit": config.rate_limit or "",
        "retries": config.retries,
        "cookies_from_browser": config.cookies_from_browser or "auto",
        "cookies": config.cookies or "",
        "proxy": config.proxy or "",
        "user_agent": config.user_agent or "",
        "archive": bool(config.archive),
        "archive_path": archive_path,
        "keep_video": config.keep_video,
        "use_aria2c": config.use_aria2c,
        "dry_run": False,
    }


def _text(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _bool(value: object, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _optional_int(value: object, label: str) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise InvalidOptionError(f"{label} must be a whole number.") from exc
    if number < 1:
        raise InvalidOptionError(f"{label} must be at least 1.")
    return number


def _int_in_range(value: object, default: int, *, label: str, low: int, high: int) -> int:
    if value is None or str(value).strip() == "":
        return default
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise InvalidOptionError(f"{label} must be a whole number.") from exc
    if not low <= number <= high:
        raise InvalidOptionError(f"{label} must be between {low} and {high}.")
    return number


def normalize_urls(value: object) -> list[str]:
    """Split a textarea or URL list into clean, unique URLs."""

    if isinstance(value, list):
        raw_items = [str(item) for item in value]
    else:
        raw_items = re.split(r"[\r\n\s]+", _text(value))
    urls: list[str] = []
    seen: set[str] = set()
    for raw in raw_items:
        cleaned = raw.strip().strip("\"'")
        if not cleaned or cleaned.startswith("#"):
            continue
        if not _URL_RE.match(cleaned):
            raise InvalidOptionError(
                f"{cleaned[:80]!r} does not look like a supported URL.",
                hint="Paste a YouTube, YouTube Music, playlist, or channel link.",
            )
        if not re.match(r"^https?://", cleaned, re.I):
            cleaned = "https://" + cleaned
        if cleaned in seen:
            continue
        seen.add(cleaned)
        urls.append(cleaned)
    if not urls:
        raise InvalidOptionError("Add at least one link to download.")
    return urls


def _validate_browser(value: str) -> str | None:
    cleaned = value.strip().lower()
    if not cleaned:
        return None
    if cleaned not in BROWSER_IDS:
        raise InvalidOptionError(
            f"Unsupported browser {value!r}. Choose one of: {', '.join(BROWSER_OPTIONS)}."
        )
    return cleaned


def _sponsorblock(value: object, fallback: str | None) -> tuple[str, ...]:
    if value is None:
        value = fallback or ""
    if isinstance(value, list):
        parts = [str(item).strip().lower() for item in value]
    else:
        parts = [part.strip().lower() for part in _text(value).split(",")]
    parts = [part for part in parts if part]
    if "all" in parts:
        return tuple(sorted(SPONSORBLOCK_IDS))
    invalid = [part for part in parts if part not in SPONSORBLOCK_IDS]
    if invalid:
        raise InvalidOptionError(
            f"Unknown SponsorBlock categor{'y' if len(invalid) == 1 else 'ies'}: "
            f"{', '.join(invalid)}."
        )
    return tuple(dict.fromkeys(parts))


_CUSTOM_HEIGHT_RE: Final = re.compile(r"^(\d{2,4})p$")


def _resolve_video_preset(quality: str, container: str):
    custom_height: int | None = None
    if quality not in QUALITY_IDS:
        match = _CUSTOM_HEIGHT_RE.match(quality.strip().lower())
        if match:
            custom_height = min(int(match.group(1)), MAX_HEIGHT)
            quality = f"{custom_height}p"
        else:
            quality = "best"
    if container not in CONTAINER_IDS:
        container = "auto"
    if custom_height is not None:
        base = (
            "best"
            if container == "auto"
            else next(
                item["preset"] for item in CONTAINER_OPTIONS if item["id"] == container
            )
        )
        return resolve_preset(base).with_quality(custom_height), quality, container
    if container == "auto":
        name = "best" if quality == "best" else quality
    else:
        base = next(
            item["preset"] for item in CONTAINER_OPTIONS if item["id"] == container
        )
        name = base if quality == "best" else f"{base}-{quality}"
    return resolve_preset(name), quality, container


def _format_label(mode: str, preset, quality: str, container: str, bitrate: int) -> str:
    if mode == "audio":
        suffix = "" if preset.lossless else f" · {bitrate} kbps"
        return f"{preset.name.upper()}{suffix}"
    custom = _CUSTOM_HEIGHT_RE.match(quality)
    quality_label = (
        f"{int(custom.group(1))}p"
        if custom
        else next(
            (item["label"] for item in QUALITY_OPTIONS if item["id"] == quality), "Best"
        )
    )
    container_label = next(
        (item["label"] for item in CONTAINER_OPTIONS if item["id"] == container), "MP4"
    )
    if container == "auto":
        if quality == "best":
            return "Best quality · MP4"
        return f"{quality_label} · MP4"
    return f"{quality_label} · {container_label}"


def build_download_request(
    payload: dict[str, Any],
    config: Config,
) -> tuple[list[str], DownloadOptions, dict[str, Any]]:
    """Validate a browser request and produce downloader options."""

    urls = normalize_urls(payload.get("urls"))
    mode = "audio" if _text(payload.get("mode")).lower() == "audio" else "video"
    quality = _text(payload.get("quality")) or "best"
    container = _text(payload.get("container")) or "auto"
    audio_format = _text(payload.get("audio_format")) or "mp3"
    if audio_format not in AUDIO_IDS:
        audio_format = "mp3"

    bitrate = _int_in_range(
        payload.get("bitrate"),
        config.bitrate,
        label="Bitrate",
        low=8,
        high=512,
    )

    if mode == "audio":
        preset = resolve_preset(audio_format)
        music = _bool(payload.get("music"), True)
        quality = "best"
        container = "auto"
    else:
        preset, quality, container = _resolve_video_preset(quality, container)
        music = False

    directory = _absolute_directory(_text(payload.get("directory")), config.directory)
    template_mode = _text(payload.get("template_mode")).lower() or "default"
    template: str | None = None
    if template_mode == "title":
        # The studio names every file exactly like the source title.
        template = "%(title)s.%(ext)s"
    elif template_mode == "custom":
        template = _text(payload.get("template"))
        if not template:
            raise InvalidOptionError("Enter an output template, or choose the smart template.")
    resolved_template = resolve_output_template(directory, template, music=music)

    subtitles = _bool(payload.get("subtitles"), config.subtitles) and preset.is_video
    subtitle_langs = _text(payload.get("subtitle_langs")) or config.subtitle_langs or "en"

    start = _optional_int(payload.get("playlist_start"), "Playlist start")
    end = _optional_int(payload.get("playlist_end"), "Playlist end")
    if start is not None and end is not None and end < start:
        raise InvalidOptionError("Playlist end must be greater than or equal to start.")

    playlist_mode = _text(payload.get("playlist_mode")).lower() or "auto"
    if playlist_mode not in {"auto", "always", "never"}:
        raise InvalidOptionError("Playlist mode must be auto, always, or never.")

    playlist_items: tuple[int, ...] | None = None
    raw_items = payload.get("playlist_items")
    if raw_items:
        if not isinstance(raw_items, list):
            raise InvalidOptionError("Selected playlist items must be a list of item numbers.")
        chosen: set[int] = set()
        for value in raw_items:
            try:
                number = int(value)
            except (TypeError, ValueError) as exc:
                raise InvalidOptionError("Selected playlist items must be item numbers.") from exc
            if number < 1:
                raise InvalidOptionError("Selected playlist items start at 1.")
            chosen.add(number)
        if len(chosen) > 5000:
            raise InvalidOptionError("Select at most 5000 playlist items per download.")
        playlist_items = tuple(sorted(chosen))
        playlist_mode = "always"

    concurrent = _int_in_range(
        payload.get("concurrent"),
        config.concurrent,
        label="Concurrent downloads",
        low=1,
        high=16,
    )
    retries = _int_in_range(
        payload.get("retries"), config.retries, label="Retries", low=0, high=100
    )

    rate_value = _text(payload.get("rate_limit")) or config.rate_limit
    try:
        rate_limit = parse_rate_limit(rate_value)
    except Exception as exc:
        raise InvalidOptionError(str(exc)) from exc

    browser = _validate_browser(
        _text(payload.get("cookies_from_browser")) or (config.cookies_from_browser or "")
    )
    cookie_value = _text(payload.get("cookies")) or (config.cookies or "")
    cookies: str | None = None
    if cookie_value:
        # A stale or deleted file should not block a download.  The service
        # layer falls back to automatic browser cookies in that case.
        cookie_path = Path(os.path.expanduser(cookie_value))
        if cookie_path.is_file():
            cookies = str(cookie_path)

    archive_enabled = _bool(payload.get("archive"))
    archive_path = _text(payload.get("archive_path"))
    if archive_enabled:
        if not archive_path:
            archive_path = config.archive or _default_archive(directory)
        archive_candidate = Path(os.path.expandvars(os.path.expanduser(archive_path)))
        if not archive_candidate.is_absolute():
            archive_candidate = Path.cwd() / archive_candidate
        archive_candidate = archive_candidate.resolve()
    else:
        archive_candidate = None

    options = DownloadOptions(
        preset=preset,
        output_template=resolved_template,
        download_dir=Path(directory),
        music=music,
        subtitles=subtitles,
        subtitle_langs=subtitle_langs,
        thumbnail=_bool(payload.get("thumbnail"), config.thumbnail),
        metadata=_bool(payload.get("metadata"), config.metadata),
        info_json=_bool(payload.get("info_json"), False),
        chapters=_bool(payload.get("chapters"), config.chapters),
        sponsorblock=_sponsorblock(payload.get("sponsorblock"), config.sponsorblock),
        playlist_start=start,
        playlist_end=end,
        playlist_mode=playlist_mode,
        playlist_items=playlist_items,
        shuffle=_bool(payload.get("shuffle"), config.shuffle),
        reverse=_bool(payload.get("reverse"), config.reverse),
        concurrent=concurrent,
        rate_limit=rate_limit,
        retries=retries,
        cookies_from_browser=browser,
        cookies=cookies,
        proxy=_text(payload.get("proxy")) or config.proxy,
        user_agent=_text(payload.get("user_agent")) or config.user_agent,
        archive=archive_candidate,
        keep_video=_bool(payload.get("keep_video"), config.keep_video),
        dry_run=_bool(payload.get("dry_run"), False),
        use_aria2c=_bool(payload.get("use_aria2c"), config.use_aria2c),
        bitrate=bitrate,
        verbose=False,
        cancel_event=threading.Event(),
    )

    meta = {
        "mode": mode,
        "preset": preset.name,
        "format_label": _format_label(mode, preset, quality, container, bitrate),
        "quality": quality,
        "container": container,
        "audio_format": audio_format,
        "bitrate": bitrate,
        "music": music,
        "destination": directory,
        "template": resolved_template,
        "template_label": "title" if template_mode == "title" else ("custom" if template else "smart"),
        "count": len(playlist_items) if playlist_items else len(urls),
        "playlist_mode": playlist_mode,
        "playlist_items": list(playlist_items or ()),
        "subtitles": subtitles,
        "subtitle_langs": subtitle_langs if subtitles else "",
        "archive": str(archive_candidate) if archive_candidate else "",
        "dry_run": options.dry_run,
    }
    return urls, options, meta


def build_info_options(
    payload: dict[str, Any],
    config: Config,
) -> tuple[str, DownloadOptions]:
    """Options for a metadata-only inspection request."""

    urls = normalize_urls(payload.get("url"))
    url = urls[0]
    directory = _absolute_directory(_text(payload.get("directory")), config.directory)

    browser = _validate_browser(
        _text(payload.get("cookies_from_browser")) or (config.cookies_from_browser or "")
    )
    cookie_value = _text(payload.get("cookies")) or (config.cookies or "")
    cookies: str | None = None
    if cookie_value:
        cookie_path = Path(os.path.expanduser(cookie_value))
        if cookie_path.is_file():
            cookies = str(cookie_path)

    options = DownloadOptions(
        preset=PRESETS["best"],
        output_template=resolve_output_template(directory, None),
        download_dir=Path(directory),
        cookies_from_browser=browser,
        cookies=cookies,
        proxy=_text(payload.get("proxy")) or config.proxy,
        user_agent=_text(payload.get("user_agent")) or config.user_agent,
        retries=1,
        use_aria2c=False,
        verbose=False,
    )
    return url, options


def template_examples() -> dict[str, str]:
    """Human-readable template previews used in the settings panel."""

    return {
        "video": DEFAULT_VIDEO_TEMPLATE,
        "music": DEFAULT_MUSIC_TEMPLATE,
    }
