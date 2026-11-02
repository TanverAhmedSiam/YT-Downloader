"""yt-dlp wrapper, playlist resolution, progress hooks, and concurrency."""

from __future__ import annotations

import contextlib
import glob
import random
import threading
import time
from collections.abc import Iterable, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError as YtDlpDownloadError

from . import converter, metadata
from .aria2 import find_aria2c
from .converter import MediaInfo
from .errors import (
    ArchiveReadError,
    DownloadCancelledError,
    DownloadError,
    YtdlStudioError,
    classify_error,
)
from .presets import Preset
from .ui import UI, ProgressDashboard

_MEDIA_SUFFIXES = {
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
    ".gif",
    ".mp3",
    ".m4a",
    ".aac",
    ".opus",
    ".ogg",
    ".oga",
    ".flac",
    ".wav",
    ".alac",
}
_SIDECAR_SUFFIXES = {".json", ".jpg", ".jpeg", ".png", ".webp", ".vtt", ".srt", ".ass"}
# yt-dlp's EmbedThumbnail postprocessor supports these video containers.
_EMBEDDABLE_VIDEO_CONTAINERS = frozenset({"mp4", "m4v", "mov", "mkv"})

_YOUTUBE_HOSTS = frozenset({"youtube.com", "m.youtube.com", "music.youtube.com", "youtube-nocookie.com"})


def _parsed_host(url: str) -> tuple[str, str, dict[str, list[str]]]:
    parsed = urlsplit(str(url).strip())
    host = parsed.netloc.lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    return host, parsed.path.rstrip("/"), parse_qs(parsed.query)


def is_single_video_url(url: str) -> bool:
    """True when a URL points at one video, even if it also carries a list id.

    YouTube watch links routinely include ``&list=...`` (albums, mixes, user
    playlists).  Pasting one of those should download that video, not the whole
    list, so the resolver treats them as single items unless asked otherwise.
    """

    try:
        host, path, query = _parsed_host(url)
    except ValueError:
        return False
    if host == "youtu.be":
        return bool(path.strip("/"))
    if host not in _YOUTUBE_HOSTS:
        return False
    if path in {"/watch", "/watch/"} or path.startswith("/watch/"):
        return bool(query.get("v"))
    return path.startswith(("/shorts/", "/embed/", "/live/", "/v/"))


def is_playlist_url(url: str) -> bool:
    """True for a YouTube playlist or album page that can be expanded.

    Watch links that merely carry a ``list=`` parameter are still single
    videos, so they are deliberately excluded from the picker's expand action.
    """

    try:
        host, path, query = _parsed_host(url)
    except ValueError:
        return False
    if host not in _YOUTUBE_HOSTS:
        return False
    if path.startswith(("/playlist", "/browse/")):
        return True
    if is_single_video_url(url):
        return False
    return bool(query.get("list"))


def youtube_list_id(url: str) -> str | None:
    """Return the ``list=`` id from a YouTube URL, if there is one."""

    try:
        host, _path, query = _parsed_host(url)
    except ValueError:
        return None
    if host not in _YOUTUBE_HOSTS:
        return None
    values = query.get("list") or []
    return values[0] if values and values[0] else None


def youtube_playlist_url(list_id: str) -> str:
    return f"https://www.youtube.com/playlist?list={list_id}"


_CHANNEL_TABS: dict[str, tuple[str, str]] = {
    "videos": ("Videos", "videos"),
    "shorts": ("Shorts", "shorts"),
    "streams": ("Live", "live"),
    "playlists": ("Playlists", "playlists"),
    "releases": ("Releases", "releases"),
    "podcasts": ("Podcasts", "podcasts"),
}
_CHANNEL_TAB_ORDER = {
    "youtube": ("videos", "shorts", "streams", "playlists", "releases", "podcasts"),
    "music": ("releases", "videos", "playlists", "streams", "shorts", "podcasts"),
}


def is_channel_url(url: str) -> bool:
    """True for a YouTube channel root or one of its tab pages."""

    try:
        host, path, _query = _parsed_host(url)
    except ValueError:
        return False
    if host not in _YOUTUBE_HOSTS:
        return False
    if not path or path.startswith(("/watch", "/shorts/", "/embed/", "/live/", "/v/", "/playlist", "/results", "/search")):
        return False
    if path.startswith(("/channel/", "/c/", "/user/", "/@")):
        return True
    return bool(path.startswith("/browse/") and path.split("/", 2)[-1].startswith("UC"))


def _channel_parts(url: str) -> tuple[str, str | None, str]:
    """Return ``(base_url, requested_tab, source)`` for a channel URL."""

    host, path, _query = _parsed_host(url)
    source = "music" if host == "music.youtube.com" else "youtube"
    parts = [part for part in path.split("/") if part]
    if parts and parts[0] == "browse" and len(parts) > 1 and parts[1].startswith("UC"):
        parts = ["channel", parts[1]]
        source = "music"
    if not parts:
        raise DownloadError("That channel link does not contain a channel id or handle.")
    if parts[0] in {"channel", "c", "user"} and len(parts) > 1:
        base_parts, rest = parts[:2], parts[2:]
    elif parts[0].startswith("@"):
        base_parts, rest = parts[:1], parts[1:]
    else:
        base_parts, rest = parts, []
    tab = rest[0] if rest and rest[0] in _CHANNEL_TABS else None
    return f"https://www.youtube.com/{'/'.join(base_parts)}", tab, source


def _channel_title(title: str, label: str) -> str:
    suffix = f" - {label}"
    cleaned = str(title or "").strip()
    if cleaned.lower().endswith(suffix.lower()):
        return cleaned[: -len(suffix)].strip()
    return cleaned


def _is_youtube_url(url: str) -> bool:
    try:
        host, _, _ = _parsed_host(url)
    except ValueError:
        return False
    return host == "youtu.be" or host in _YOUTUBE_HOSTS


def _is_youtube_media_failure(url: str, error: YtdlStudioError) -> bool:
    """True when another YouTube stream/client is worth trying."""

    if not isinstance(error, DownloadError) or not _is_youtube_url(url):
        return False
    message = error.message.lower()
    return (
        "http error 403" in message
        or "403: forbidden" in message
        or "requested format is not available" in message
        or "page needs to be reloaded" in message
    )


@dataclass(slots=True)
class DownloadOptions:
    """Everything the downloader needs, independent of the CLI layer."""

    preset: Preset
    output_template: str
    download_dir: Path
    music: bool = False
    subtitles: bool = False
    subtitle_langs: str = "en"
    thumbnail: bool = True
    metadata: bool = True
    info_json: bool = True
    chapters: bool = True
    sponsorblock: tuple[str, ...] = ()
    playlist_start: int | None = None
    playlist_end: int | None = None
    playlist_mode: str = "auto"
    playlist_items: tuple[int, ...] | None = None
    shuffle: bool = False
    reverse: bool = False
    concurrent: int = 3
    rate_limit: int | None = None
    retries: int = 10
    cookies_from_browser: str | None = None
    cookies: str | None = None
    proxy: str | None = None
    user_agent: str | None = None
    archive: Path | None = None
    keep_video: bool = False
    dry_run: bool = False
    use_aria2c: bool = True
    bitrate: int = 320
    verbose: bool = False
    cancel_event: threading.Event | None = None


@dataclass(slots=True)
class MediaEntry:
    """A single video/audio item resolved from one or more URLs."""

    url: str
    title: str
    id: str | None = None
    duration: float | None = None
    extractor: str = "generic"
    webpage_url: str = ""
    playlist_title: str | None = None
    playlist_index: int | None = None
    info: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class DownloadResult:
    """Outcome of one media item, used by the summary and JSON output."""

    title: str
    format: str
    size: int | None
    duration: float | None
    path: str
    status: str
    error: str | None = None
    url: str = ""
    id: str | None = None
    elapsed: float = 0.0
    info: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PlannedItem:
    """Dry-run row."""

    title: str
    url: str
    format: str
    duration: float | None


class Archive:
    """A small, thread-safe yt-dlp-compatible archive implementation.

    We write the archive only after post-processing succeeds.  That prevents a
    failed MP3 conversion from marking an item as "done" forever.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self._lock = threading.Lock()
        self._ids: set[str] = set()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    self._remember(line.strip())
        except OSError as exc:
            raise ArchiveReadError(f"Could not read archive {self.path}: {exc}") from exc

    def _remember(self, line: str) -> None:
        if not line:
            return
        self._ids.add(line.lower())
        parts = line.rsplit(None, 1)
        if len(parts) == 2:
            self._ids.add(parts[1].lower())
        elif len(parts) == 1:
            self._ids.add(parts[0].lower())

    def contains(self, identifier: str | None) -> bool:
        return bool(identifier and identifier.lower() in self._ids)

    def add(self, extractor: str, identifier: str | None) -> None:
        if not identifier:
            return
        line = f"{extractor or 'generic'} {identifier}".strip()
        with self._lock:
            if self.contains(identifier):
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            try:
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
                    handle.flush()
            except OSError as exc:
                raise ArchiveReadError(f"Could not write archive {self.path}: {exc}") from exc
            self._remember(line)


class _YdlLogger:
    """Route yt-dlp's console noise into our state and verbose log."""

    def __init__(self, state: dict[str, Any], ui: UI, entry: MediaEntry) -> None:
        self.state = state
        self.ui = ui
        self.entry = entry

    def debug(self, message: str) -> None:
        text = str(message).strip()
        if not text:
            return
        lowered = text.lower()
        if "has already been downloaded" in lowered or "already downloaded" in lowered:
            self.state["already"] = True
        if self.ui.verbose:
            self.ui.verbose_log(f"[{self.entry.id or 'ytdl'}] {text}")

    def info(self, message: str) -> None:
        self.debug(message)

    def warning(self, message: str) -> None:
        text = str(message).strip()
        if text:
            self.state.setdefault("warnings", []).append(text)
            self.ui.verbose_log(f"warning: {text}")

    def error(self, message: str) -> None:
        text = str(message).strip()
        if text:
            self.state.setdefault("errors", []).append(text)


class Downloader:
    """High-level orchestration around yt-dlp."""

    def __init__(self, options: DownloadOptions, ui: UI) -> None:
        self.options = options
        self.ui = ui
        self.archive = Archive(options.archive) if options.archive else None
        self._aria2c = find_aria2c() if options.use_aria2c else None

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------
    def run(self, urls: Sequence[str]) -> list[DownloadResult]:
        """Resolve, download, and return one result per item."""

        if not urls:
            raise DownloadError("No URLs were provided.")
        if not self.options.dry_run:
            converter.require_tools()
            self.options.download_dir.mkdir(parents=True, exist_ok=True)
        entries = self.resolve_entries(urls)
        if not entries:
            self.ui.notice("No downloadable items were found.")
            return []
        if self.options.dry_run:
            planned = [
                PlannedItem(
                    title=entry.title,
                    url=entry.url,
                    format=self.options.preset.name,
                    duration=entry.duration,
                )
                for entry in entries
            ]
            self.ui.render_dry_run(planned)
            return [
                DownloadResult(
                    title=item.title,
                    format=item.format,
                    size=None,
                    duration=item.duration,
                    path="",
                    status="planned",
                    url=item.url,
                )
                for item in planned
            ]

        dashboard = self.ui.start_progress(total_files=len(entries))
        results: list[DownloadResult] = []
        try:
            results = self._process_bounded(entries, dashboard)
        finally:
            self.ui.stop_progress()
        return results

    def resolve_entries(self, urls: Sequence[str]) -> list[MediaEntry]:
        """Expand collection URLs into individual download items.

        ``playlist_mode`` decides what counts as a collection: ``auto`` expands
        only URLs that point at a playlist/album/channel (a watch link that
        merely carries ``&list=`` stays a single video), ``always`` expands
        everything, and ``never`` keeps one item per URL.
        """

        all_entries: list[MediaEntry] = []
        for url in urls:
            self.ui.verbose_log(f"resolving {url}")
            options = self._flat_options(expand=self._should_expand(url))
            try:
                with YoutubeDL(options) as ydl:
                    info = ydl.extract_info(url, download=False)
            except YtDlpDownloadError as exc:
                raise classify_error(exc) from exc
            except Exception as exc:
                raise classify_error(exc) from exc
            if not info:
                raise DownloadError(f"No metadata returned for {url}")
            raw_entries = self._playlist_items(info)
            if not raw_entries:
                raise DownloadError(f"No downloadable entries found at {url}")
            playlist_title = info.get("title") if info.get("_type") == "playlist" else None
            pairs = list(enumerate(raw_entries, start=1))
            if self.options.playlist_items:
                wanted = set(self.options.playlist_items)
                pairs = [(index, item) for index, item in pairs if index in wanted]
            else:
                start = (self.options.playlist_start or 1) - 1
                end = self.options.playlist_end
                pairs = pairs[start:end]
            resolved = [
                self._entry_from_info(item, playlist_title=playlist_title, index=index)
                for index, item in pairs
                if item
            ]
            resolved = [entry for entry in resolved if entry.url]
            if self.options.reverse:
                resolved.reverse()
            if self.options.shuffle:
                random.SystemRandom().shuffle(resolved)
            all_entries.extend(resolved)
        return self._dedupe(all_entries)

    def _should_expand(self, url: str) -> bool:
        mode = str(self.options.playlist_mode or "auto").strip().lower()
        if mode == "always":
            return True
        if mode == "never":
            return False
        return not is_single_video_url(url)

    def get_info(self, url: str, *, playlist: bool = False) -> dict[str, Any]:
        """Fetch full metadata without downloading."""

        options = self._base_options()
        options.update(
            {
                "noplaylist": not playlist,
                "skip_download": True,
                "extract_flat": False,
                "writethumbnail": False,
                "writeinfojson": False,
                "postprocessors": [],
                "quiet": True,
                "no_warnings": True,
            }
        )
        with YoutubeDL(options) as ydl:
            try:
                info = ydl.extract_info(url, download=False)
            except YtDlpDownloadError as exc:
                raise classify_error(exc) from exc
            except Exception as exc:
                raise classify_error(exc) from exc
        if not info:
            raise DownloadError(f"No metadata returned for {url}")
        return info

    def list_playlist(self, url: str, *, limit: int = 500) -> dict[str, Any]:
        """Return the flat contents of a playlist/album/channel without downloading."""

        options = self._flat_options(expand=True)
        if limit:
            options["playlistend"] = limit
        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
        except YtDlpDownloadError as exc:
            raise classify_error(exc) from exc
        except Exception as exc:
            raise classify_error(exc) from exc
        if not info:
            raise DownloadError(f"No metadata returned for {url}")
        raw_entries = self._playlist_items(info)
        entries: list[dict[str, Any]] = []
        for index, item in enumerate(raw_entries, start=1):
            if not item:
                continue
            parsed = self._entry_from_info(item, playlist_title=info.get("title"), index=index)
            entries.append(
                {
                    "index": index,
                    "id": parsed.id or "",
                    "title": parsed.title,
                    "duration": parsed.duration,
                    "uploader": str(
                        item.get("uploader")
                        or item.get("channel")
                        or item.get("creator")
                        or ""
                    ),
                    "url": parsed.url,
                    "thumbnail": _entry_thumbnail(item),
                    "is_live": bool(item.get("is_live")),
                    "expandable": is_playlist_url(parsed.url),
                }
            )
        return {
            "title": str(info.get("title") or ""),
            "id": str(info.get("id") or ""),
            "kind": str(info.get("_type") or "playlist"),
            "count": len(entries),
            "truncated": bool(limit and len(raw_entries) >= limit),
            "entries": entries,
        }

    def list_channel(self, url: str, *, limit: int = 500) -> dict[str, Any]:
        """List a channel as separate, pickable sections.

        YouTube channels expose videos, shorts, live streams, playlists, and
        releases.  YouTube Music artist pages use the same release tab; its
        albums and singles/EPs are split using their track counts so the picker
        can present them as distinct sections.
        """

        base, requested_tab, source = _channel_parts(url)
        tab_ids = [requested_tab] if requested_tab else list(_CHANNEL_TAB_ORDER[source])
        sections: list[dict[str, Any]] = []
        errors: list[str] = []
        channel_title = ""

        for tab in tab_ids:
            if tab not in _CHANNEL_TABS:
                continue
            label, kind = _CHANNEL_TABS[tab]
            section_url = f"{base}/{tab}"
            try:
                listing = self.list_playlist(section_url, limit=limit)
            except YtdlStudioError as exc:
                errors.append(exc.message)
                continue
            entries = listing.get("entries") or []
            if not entries:
                continue
            raw_title = str(listing.get("title") or "")
            if not channel_title:
                channel_title = _channel_title(raw_title, label)
            if tab == "releases":
                release_sections = self._release_sections(
                    entries,
                    section_url=section_url,
                )
                sections.extend(release_sections)
                continue
            sections.append(
                {
                    "id": tab,
                    "title": label,
                    "kind": kind,
                    "count": len(entries),
                    "truncated": bool(listing.get("truncated")),
                    "url": section_url,
                    "entries": entries,
                }
            )

        if not sections:
            # Topic channels have no tab pages, but yt-dlp resolves the root to
            # their uploads playlist.  Keep that useful fallback.
            try:
                listing = self.list_playlist(base, limit=limit)
            except YtdlStudioError as exc:
                errors.append(exc.message)
                listing = {}
            entries = listing.get("entries") or []
            if entries:
                raw_title = str(listing.get("title") or "")
                channel_title = _channel_title(raw_title, "Uploads") or channel_title
                sections.append(
                    {
                        "id": "uploads",
                        "title": "Uploads",
                        "kind": "uploads",
                        "count": len(entries),
                        "truncated": bool(listing.get("truncated")),
                        "url": base,
                        "entries": entries,
                    }
                )

        if not sections:
            detail = errors[-1] if errors else "No channel sections were found."
            raise DownloadError(f"Could not read this channel: {detail}")

        flat: list[dict[str, Any]] = []
        for section in sections:
            # Number each tab independently (1..n) so a channel's playlists do
            # not continue the videos' count.  Keep the global order separately
            # for the flattened download list.
            for position, entry in enumerate(section["entries"], start=1):
                entry["section"] = section["id"]
                entry["section_title"] = section["title"]
                entry["key"] = f"{section['id']}:{entry.get('index')}"
                entry["position"] = position
                entry["global_position"] = len(flat) + 1
                flat.append(entry)

        if not channel_title:
            channel_title = base.rstrip("/").rsplit("/", 1)[-1]
        channel_id = ""
        for section in sections:
            for entry in section["entries"]:
                value = str(entry.get("id") or "")
                if value.startswith("UC"):
                    channel_id = value
                    break
            if channel_id:
                break
        if not channel_id and "/channel/" in base:
            channel_id = base.rstrip("/").rsplit("/", 1)[-1]

        thumbnail = next(
            (entry.get("thumbnail") for entry in flat if entry.get("thumbnail")),
            "",
        )
        return {
            "title": channel_title,
            "id": channel_id,
            "kind": "channel",
            "source": "youtube-music" if source == "music" else "youtube",
            "count": len(flat),
            "truncated": any(bool(section.get("truncated")) for section in sections),
            "url": base,
            "thumbnail": thumbnail,
            "sections": sections,
            "entries": flat,
        }

    def _playlist_count(self, url: str) -> int | None:
        """Return a release's track count without fetching its entries."""

        options = self._flat_options(expand=True)
        options.update(
            {
                "extract_flat": False,
                "playlistend": 0,
                "skip_download": True,
                "quiet": True,
                "no_warnings": True,
            }
        )
        try:
            with YoutubeDL(options) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception:
            return None
        try:
            value = int((info or {}).get("playlist_count") or 0)
        except (TypeError, ValueError):
            return None
        return value or None

    def _release_sections(
        self,
        entries: list[dict[str, Any]],
        *,
        section_url: str,
    ) -> list[dict[str, Any]]:
        """Split albums and singles/EPs using release track counts."""

        urls = [str(entry.get("url") or "") for entry in entries]
        counts: list[int | None] = []
        if len(urls) <= 80:
            workers = max(1, min(4, len(urls)))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ytdl-releases") as pool:
                counts = list(pool.map(self._playlist_count, urls))
        else:
            counts = [None] * len(urls)

        albums: list[dict[str, Any]] = []
        singles: list[dict[str, Any]] = []
        unknown: list[dict[str, Any]] = []
        for entry, count in zip(entries, counts, strict=False):
            entry["track_count"] = count
            if count is None:
                unknown.append(entry)
            elif count >= 7:
                albums.append(entry)
            else:
                singles.append(entry)

        sections: list[dict[str, Any]] = []
        if albums:
            sections.append(
                {
                    "id": "albums",
                    "title": "Albums",
                    "kind": "albums",
                    "count": len(albums),
                    "truncated": False,
                    "url": section_url,
                    "entries": albums,
                }
            )
        if singles:
            sections.append(
                {
                    "id": "singles",
                    "title": "Singles & EPs",
                    "kind": "singles",
                    "count": len(singles),
                    "truncated": False,
                    "url": section_url,
                    "entries": singles,
                }
            )
        if unknown or not sections:
            extra = entries if not sections else unknown
            sections.append(
                {
                    "id": "releases",
                    "title": "Releases",
                    "kind": "releases",
                    "count": len(extra),
                    "truncated": False,
                    "url": section_url,
                    "entries": extra,
                }
            )
        return sections

    # ------------------------------------------------------------------
    # Scheduling
    # ------------------------------------------------------------------
    def _process_bounded(
        self,
        entries: Sequence[MediaEntry],
        dashboard: ProgressDashboard,
    ) -> list[DownloadResult]:
        results: list[tuple[int, DownloadResult]] = []
        workers = max(1, self.options.concurrent)
        iterator = iter(enumerate(entries))
        pending: dict[Future[DownloadResult], int] = {}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ytdl") as pool:
            # Keep only a small window of work queued so huge playlists don't
            # allocate one future per item.
            for _ in range(min(workers * 2, len(entries))):
                try:
                    index, entry = next(iterator)
                except StopIteration:
                    break
                pending[pool.submit(self._process_entry, entry, dashboard)] = index
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    index = pending.pop(future)
                    try:
                        result = future.result()
                    except YtdlStudioError as exc:
                        result = DownloadResult(
                            title=entries[index].title,
                            format=self.options.preset.name,
                            size=None,
                            duration=entries[index].duration,
                            path="",
                            status="failed",
                            error=exc.message,
                            url=entries[index].url,
                            id=entries[index].id,
                        )
                    except Exception as exc:  # defence in depth for worker threads
                        result = DownloadResult(
                            title=entries[index].title,
                            format=self.options.preset.name,
                            size=None,
                            duration=entries[index].duration,
                            path="",
                            status="failed",
                            error=str(exc),
                            url=entries[index].url,
                            id=entries[index].id,
                        )
                    results.append((index, result))
                    try:
                        index, entry = next(iterator)
                    except StopIteration:
                        continue
                    pending[pool.submit(self._process_entry, entry, dashboard)] = index
        results.sort(key=lambda item: item[0])
        return [result for _, result in results]

    # ------------------------------------------------------------------
    # One item
    # ------------------------------------------------------------------
    def _process_entry(self, entry: MediaEntry, dashboard: ProgressDashboard) -> DownloadResult:
        started = time.monotonic()
        if self._cancelled():
            task_id = dashboard.add_task(entry.title, stage="cancelled")
            dashboard.finish(task_id, status="cancelled", detail="cancelled")
            return DownloadResult(
                title=entry.title,
                format=self.options.preset.name,
                size=None,
                duration=entry.duration,
                path="",
                status="cancelled",
                error="Cancelled by user.",
                url=entry.url,
                id=entry.id,
                elapsed=time.monotonic() - started,
            )
        if self._is_archived(entry):
            task_id = dashboard.add_task(entry.title, stage="skipped")
            dashboard.finish(task_id, status="skipped", detail="archived")
            return DownloadResult(
                title=entry.title,
                format=self.options.preset.name,
                size=None,
                duration=entry.duration,
                path="",
                status="skipped",
                url=entry.url,
                id=entry.id,
                elapsed=time.monotonic() - started,
            )

        task_id = dashboard.add_task(entry.title, stage="metadata")
        last_error: YtdlStudioError | None = None
        attempts = max(1, min(self.options.retries, 3))
        preset = self.options.preset
        fallbacks: list[tuple[str | None, tuple[str, ...] | None, str | None]] = []
        if _is_youtube_url(entry.url):
            if preset.is_audio:
                # Some videos publish DASH audio URLs that YouTube rejects with
                # a 403, while their HLS audio still downloads.  Try that before
                # falling back to a client whose best audio is progressive.
                fallbacks = [
                    (preset.hls_selector(), None, None),
                    ("bestaudio/best", ("android",), None),
                ]
            else:
                # HLS also carries separate video streams at the full ladder.
                # Merge into MKV for WebM targets because HLS audio is AAC;
                # the final converter turns that into the requested container.
                merge_format = "mkv" if preset.container == "webm" else preset.merge_output_format
                hls_selectors = list(
                    dict.fromkeys(
                        (
                            preset.hls_selector(),
                            preset.hls_selector(prefer_codec=False),
                        )
                    )
                )
                fallbacks = [
                    (selector, None, merge_format) for selector in hls_selectors
                ]
                # Android only exposes a progressive stream, but it is a useful
                # last resort when HLS has no matching format.
                fallbacks.append((preset.selector, ("android",), None))
        format_override: str | None = None
        player_client: tuple[str, ...] | None = None
        merge_format_override: str | None = None
        fallback_index = 0
        network_attempt = 0
        while True:
            network_attempt += 1
            state: dict[str, Any] = {}
            try:
                result = self._download_attempt(
                    entry,
                    task_id,
                    dashboard,
                    state,
                    started,
                    format_override=format_override,
                    player_client=player_client,
                    merge_format_override=merge_format_override,
                )
                if result.status == "completed" and self.archive:
                    self.archive.add(entry.extractor, entry.id)
                dashboard.finish(task_id, status=result.status)
                return result
            except DownloadCancelledError:
                dashboard.finish(task_id, status="cancelled", detail="cancelled")
                return DownloadResult(
                    title=entry.title,
                    format=self.options.preset.name,
                    size=None,
                    duration=entry.duration,
                    path="",
                    status="cancelled",
                    error="Cancelled by user.",
                    url=entry.url,
                    id=entry.id,
                    elapsed=time.monotonic() - started,
                )
            except YtdlStudioError as exc:
                last_error = exc
                if fallback_index < len(fallbacks) and _is_youtube_media_failure(entry.url, exc):
                    format_override, player_client, merge_format_override = fallbacks[fallback_index]
                    fallback_index += 1
                    self._discard_partial(state)
                    dashboard.stage(task_id, "retrying")
                    self.ui.verbose_log(
                        f"{entry.title}: retrying with a compatible YouTube stream "
                        f"after: {exc.message}"
                    )
                    continue
                retryable = isinstance(exc, DownloadError) and exc.__class__.__name__ in {
                    "NetworkError",
                    "RateLimitedError",
                }
                if retryable and network_attempt < attempts:
                    delay = 2**network_attempt
                    dashboard.stage(task_id, "retrying")
                    self.ui.warning(
                        f"{entry.title}: {exc.message} Retrying in {delay}s "
                        f"({network_attempt}/{attempts - 1})."
                    )
                    time.sleep(delay)
                    continue
                break
            except Exception as exc:
                last_error = classify_error(exc)
                break
        error = last_error or DownloadError("The download failed for an unknown reason.")
        dashboard.finish(task_id, status="failed", detail="failed")
        return DownloadResult(
            title=entry.title,
            format=self.options.preset.name,
            size=None,
            duration=entry.duration,
            path="",
            status="failed",
            error=error.message,
            url=entry.url,
            id=entry.id,
            elapsed=time.monotonic() - started,
        )

    def _download_attempt(
        self,
        entry: MediaEntry,
        task_id: int,
        dashboard: ProgressDashboard,
        state: dict[str, Any],
        started: float,
        *,
        format_override: str | None = None,
        player_client: tuple[str, ...] | None = None,
        merge_format_override: str | None = None,
    ) -> DownloadResult:
        if self._cancelled():
            raise DownloadCancelledError("Cancelled by user.")
        if not entry.url:
            raise DownloadError("The item has no downloadable URL.")
        ydl_options = self._download_options(
            task_id,
            dashboard,
            state,
            entry,
            format_override=format_override,
            player_client=player_client,
            merge_format_override=merge_format_override,
        )
        with YoutubeDL(ydl_options) as ydl:
            try:
                info = ydl.extract_info(entry.url, download=True)
            except YtDlpDownloadError as exc:
                raise classify_error(exc) from exc
        if not info:
            raise DownloadError("yt-dlp returned no information for this item.")
        if info.get("_type") == "playlist":
            nested = self._playlist_items(info)
            if not nested:
                raise DownloadError("This playlist has no downloadable items.")
            info = nested[0]
        # HLS/Android fallback extraction may omit the source geometry even
        # though the initial resolve pass had it.  Carry it forward so audio
        # cover art can still be cropped to the source's square aspect ratio.
        info = _merge_source_details(info, entry.info)
        title = str(info.get("title") or entry.title or "Unknown")
        dashboard.set_title(task_id, title)
        duration = _float(info.get("duration")) or entry.duration
        identifier = info.get("id") or entry.id
        extractor = info.get("extractor_key") or info.get("extractor") or entry.extractor
        webpage = info.get("webpage_url") or entry.webpage_url or entry.url

        if state.get("already") and not state.get("paths") and not self.options.preset.is_audio:
            existing = self._existing_path(info, ydl_options, state)
            if existing is not None:
                dashboard.finish(task_id, status="skipped", detail="already downloaded")
                return DownloadResult(
                    title=title,
                    format=self.options.preset.name,
                    size=existing.stat().st_size if existing.exists() else None,
                    duration=duration,
                    path=str(existing),
                    status="skipped",
                    url=webpage,
                    id=str(identifier) if identifier else None,
                    elapsed=time.monotonic() - started,
                    info=info,
                )

        source = self._find_downloaded_file(info, ydl_options, state)
        if source is None:
            raise DownloadError(
                "The download finished but no media file was found.",
                hint="Check the output directory and template.",
            )
        self.ui.verbose_log(f"downloaded {source}")

        if self.options.preset.is_audio:
            final, media = self._finalize_audio(source, info, task_id, dashboard)
        else:
            final, media = self._finalize_video(source, info, task_id, dashboard)

        if self.options.metadata or self.options.music:
            info = dict(info)
            info["filepath"] = str(final)
            if self.options.metadata and self.options.info_json:
                metadata.write_info_json(final, info)

        if self.archive:
            self.archive.add(str(extractor or "generic"), str(identifier) if identifier else None)

        return DownloadResult(
            title=title,
            format=self.options.preset.name,
            size=media.size if media else (final.stat().st_size if final.exists() else None),
            duration=media.duration if media else duration,
            path=str(final),
            status="completed",
            url=webpage,
            id=str(identifier) if identifier else None,
            elapsed=time.monotonic() - started,
            info=info,
        )

    # ------------------------------------------------------------------
    # Audio/video finalisation
    # ------------------------------------------------------------------
    def _finalize_audio(
        self,
        source: Path,
        info: dict[str, Any],
        task_id: int,
        dashboard: ProgressDashboard,
    ) -> tuple[Path, MediaInfo | None]:
        codec = self.options.preset.audio_codec or "mp3"
        destination = converter.choose_output_path(source, self.options.preset.extension, directory=source.parent)
        dashboard.stage(task_id, "converting")
        self.ui.verbose_log(f"converting {source.name} -> {destination.name} ({codec}, {self.options.bitrate}k)")
        result = converter.extract_audio(
            source,
            destination,
            codec=codec,
            bitrate=self.options.bitrate,
        )
        final = result.output
        media = result.media
        dashboard.stage(task_id, "tagging")
        cover = metadata.find_thumbnail(source) if self.options.thumbnail else None
        temp_cover: Path | None = None
        if cover is None and self.options.thumbnail:
            thumbnail_url = info.get("thumbnail")
            if thumbnail_url:
                suffix = Path(str(thumbnail_url).split("?")[0]).suffix or ".jpg"
                temp_cover = source.with_name(f".{source.stem}.cover{suffix}")
                try:
                    cover = metadata.fetch_thumbnail(str(thumbnail_url), temp_cover)
                except YtdlStudioError as exc:
                    self.ui.warning(f"Could not fetch cover art for {source.name}: {exc.message}")
                    cover = None
        if self.options.metadata:
            try:
                metadata.tag_audio(final, info, cover_path=cover)
            except YtdlStudioError as exc:
                self.ui.warning(f"Could not tag {final.name}: {exc.message}")
        if temp_cover and temp_cover.exists():
            temp_cover.unlink(missing_ok=True)

        if not self.options.keep_video:
            self._remove_source(source)
        return final, media

    def _finalize_video(
        self,
        source: Path,
        info: dict[str, Any],
        task_id: int,
        dashboard: ProgressDashboard,
    ) -> tuple[Path, MediaInfo | None]:
        preset = self.options.preset
        try:
            media = converter.probe(source)
        except YtdlStudioError:
            media = None
        target_container = preset.container or preset.extension
        conversion_video_codec = preset.video_codec
        if (
            preset.container == "webm"
            and media is not None
            and media.video_codec not in {"vp8", "vp9", "av1"}
        ):
            # A fallback source may be H.264; WebM cannot contain it.
            conversion_video_codec = "vp9"
        need = converter.needs_conversion(
            media,
            container=target_container,
            video_codec=conversion_video_codec,
            force=preset.force_transcode,
        ) if media else True

        final = source
        if need:
            dashboard.stage(task_id, "converting")
            destination = converter.choose_output_path(source, preset.extension, directory=source.parent)
            self.ui.verbose_log(
                f"converting {source.name} -> {destination.name} "
                f"({preset.container or preset.extension}, {preset.video_codec or 'copy'})"
            )
            if preset.postprocess == "gif" or preset.container == "gif":
                result = converter.convert_to_gif(
                    source,
                    destination,
                    height=preset.target_height,
                )
            else:
                scale_height = None
                if (
                    conversion_video_codec is not None
                    and preset.target_height
                    and media is not None
                    and media.height
                    and media.height > preset.target_height
                ):
                    scale_height = preset.target_height
                copy_audio = (
                    conversion_video_codec is None
                    and (
                        preset.container != "webm"
                        or (media is not None and media.audio_codec in {"opus", "vorbis"})
                    )
                )
                result = converter.convert_video(
                    source,
                    destination,
                    container=preset.container,
                    video_codec=conversion_video_codec,
                    audio_codec=preset.audio_codec or ("opus" if preset.container == "webm" else "aac"),
                    audio_bitrate=self.options.bitrate,
                    height=scale_height,
                    copy_audio=copy_audio,
                )
            final = result.output
            media = result.media
            if not self.options.keep_video:
                self._remove_source(source)
        elif media is None:
            try:
                media = converter.probe(final)
            except YtdlStudioError:
                media = None

        if preset.target_height and media and media.height and media.height < preset.target_height:
            self.ui.warning(
                f"{info.get('title') or source.name}: requested {preset.target_height}p, "
                f"best available is {media.height}p."
            )
        self._cleanup_sidecars_if_needed(source, final)
        return final, media

    def _discard_partial(self, state: dict[str, Any]) -> None:
        """Remove a failed stream's partial file before retrying another one."""

        raw = state.get("last_progress_file")
        if not raw:
            return
        path = Path(str(raw))
        candidates = {path}
        if path.name.endswith(".part"):
            base = path.with_name(path.name[:-5])
            candidates.add(base)
            candidates.add(base.with_name(base.name + ".ytdl"))
        else:
            candidates.add(path.with_name(path.name + ".part"))
            candidates.add(path.with_name(path.name + ".ytdl"))
        for candidate in candidates:
            if not candidate.name.endswith((".part", ".ytdl")):
                continue
            with contextlib.suppress(OSError):
                candidate.unlink(missing_ok=True)

    def _remove_source(self, source: Path) -> None:
        if source.exists():
            try:
                source.unlink()
            except OSError as exc:
                self.ui.warning(f"Could not remove source file {source.name}: {exc}")
        for sidecar in (
            source.with_name(source.name + ".info.json"),
            source.with_suffix(".info.json"),
        ):
            sidecar.unlink(missing_ok=True)
        thumb = metadata.find_thumbnail(source)
        if thumb and thumb.exists():
            thumb.unlink(missing_ok=True)

    def _cleanup_sidecars_if_needed(self, source: Path, final: Path) -> None:
        if source == final:
            return
        if self.options.keep_video:
            return
        # The source is removed by the converter branch; this covers a desired
        # conversion that turned out to be a no-op.
        return

    # ------------------------------------------------------------------
    # Output discovery
    # ------------------------------------------------------------------
    def _find_downloaded_file(
        self,
        info: dict[str, Any],
        ydl_options: dict[str, Any],
        state: dict[str, Any],
    ) -> Path | None:
        candidates: list[Path] = []
        for key in ("filepath", "_filename", "filename"):
            value = info.get(key)
            if value:
                candidates.append(Path(str(value)))
        requested = info.get("requested_downloads") or []
        if isinstance(requested, list):
            for item in requested:
                if isinstance(item, dict):
                    for key in ("filepath", "_filename", "filename"):
                        if item.get(key):
                            candidates.append(Path(str(item[key])))
        candidates.extend(state.get("paths", []))
        try:
            with YoutubeDL({**ydl_options, "quiet": True, "skip_download": True}) as ydl:
                prepared = ydl.prepare_filename(info)
                if prepared:
                    candidates.append(Path(prepared))
        except Exception:
            pass

        wanted = self.options.preset.extension.lower()
        existing = [candidate for candidate in candidates if candidate.is_file()]
        existing = [candidate for candidate in existing if candidate.suffix.lower() in _MEDIA_SUFFIXES]
        if not existing:
            # Last resort: look beside the expected filename for a file modified
            # in the last few minutes. This is essential after some yt-dlp
            # postprocessors rename their output.
            for candidate in candidates:
                parent = candidate.parent
                stem = candidate.stem
                if not parent.is_dir():
                    continue
                for match in parent.glob(glob.escape(stem) + ".*"):
                    if (
                        match.is_file()
                        and match.suffix.lower() in _MEDIA_SUFFIXES
                        and match.stat().st_mtime > time.time() - 600
                    ):
                        existing.append(match)
        if not existing:
            return None
        existing = list(dict.fromkeys(existing))
        # Prefer the final container, then the newest file.
        existing.sort(
            key=lambda path: (path.suffix.lower() == f".{wanted}", path.stat().st_mtime),
            reverse=True,
        )
        return existing[0]

    def _existing_path(
        self,
        info: dict[str, Any],
        ydl_options: dict[str, Any],
        state: dict[str, Any],
    ) -> Path | None:
        return self._find_downloaded_file(info, ydl_options, state)

    # ------------------------------------------------------------------
    # yt-dlp option construction
    # ------------------------------------------------------------------
    def _base_options(self) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "no_color": True,
            "socket_timeout": 30,
            "retries": self.options.retries,
            "fragment_retries": self.options.retries,
            "file_access_retries": max(2, min(self.options.retries, 5)),
            "extractor_retries": max(1, min(self.options.retries, 5)),
            "continuedl": True,
            "nopart": False,
            "overwrites": False,
            "windowsfilenames": True,
            "restrictfilenames": False,
            "trim_file_name": 180,
            "outtmpl": self.options.output_template,
            "ratelimit": self.options.rate_limit,
            "ffmpeg_location": converter.ffmpeg_location(),
            "concurrent_fragment_downloads": max(1, min(self.options.concurrent, 16)),
        }
        if self.options.cookies:
            opts["cookiefile"] = str(Path(self.options.cookies).expanduser())
        elif self.options.cookies_from_browser:
            opts["cookiesfrombrowser"] = (self.options.cookies_from_browser,)
        if self.options.proxy:
            opts["proxy"] = self.options.proxy
        if self.options.user_agent:
            opts["http_headers"] = {"User-Agent": self.options.user_agent}
        return opts

    def _flat_options(self, *, expand: bool = True) -> dict[str, Any]:
        opts = self._base_options()
        opts.update(
            {
                "skip_download": True,
                "extract_flat": True,
                "noplaylist": not expand,
                "writethumbnail": False,
                "writeinfojson": False,
                "postprocessors": [],
            }
        )
        if expand and not self.options.playlist_items and self.options.playlist_end:
            opts["playlistend"] = self.options.playlist_end
        return opts

    def _download_options(
        self,
        task_id: int,
        dashboard: ProgressDashboard,
        state: dict[str, Any],
        entry: MediaEntry,
        *,
        format_override: str | None = None,
        player_client: tuple[str, ...] | None = None,
        merge_format_override: str | None = None,
    ) -> dict[str, Any]:
        opts = self._base_options()
        thumbnail = self.options.thumbnail and _needs_thumbnail(self.options.preset)
        opts.update(
            {
                "format": self.options.preset.selector,
                "merge_output_format": self.options.preset.merge_output_format,
                "noplaylist": True,
                "writethumbnail": thumbnail,
                "writeinfojson": False,
                "writesubtitles": self.options.subtitles and self.options.preset.is_video,
                "writeautomaticsub": self.options.subtitles and self.options.preset.is_video,
                "subtitleslangs": _split_langs(self.options.subtitle_langs),
                "subtitlesformat": "srt/best",
                "logger": _YdlLogger(state, self.ui, entry),
                "progress_hooks": [self._progress_hook(task_id, dashboard, state)],
                "postprocessor_hooks": [self._postprocessor_hook(task_id, dashboard, state)],
                "postprocessors": self._postprocessors(),
            }
        )
        if self.options.preset.is_audio:
            opts["format"] = "bestaudio/best"
            opts["merge_output_format"] = None
        if format_override:
            opts["format"] = format_override
        if player_client:
            opts["extractor_args"] = {"youtube": {"player_client": list(player_client)}}
        if merge_format_override is not None:
            opts["merge_output_format"] = merge_format_override
        # YouTube's signed media URLs reject aria2c's parallel range requests
        # with HTTP 403.  Keep the native downloader there and use aria2c for
        # sources where it is a real speed-up.
        if self._aria2c and self.options.use_aria2c and not _is_youtube_url(entry.url):
            opts["external_downloader"] = {"default": str(self._aria2c)}
            opts["external_downloader_args"] = {
                "aria2c": ["-x", "16", "-s", "16", "-k", "1M", "--file-allocation=none"]
            }
        return opts

    def _postprocessors(self) -> list[dict[str, Any]]:
        pps: list[dict[str, Any]] = []
        if self.options.sponsorblock:
            pps.append(
                {
                    "key": "SponsorBlock",
                    "categories": list(self.options.sponsorblock),
                    "when": "after_filter",
                }
            )
        if self.options.preset.is_video:
            if self.options.metadata or self.options.chapters:
                pps.append(
                    {
                        "key": "FFmpegMetadata",
                        "add_metadata": self.options.metadata,
                        "add_chapters": self.options.chapters,
                        "add_infojson": "if_exists",
                    }
                )
            if self.options.thumbnail and _supports_embedded_thumbnail(self.options.preset):
                pps.append({"key": "EmbedThumbnail"})
            if self.options.subtitles:
                pps.append({"key": "FFmpegEmbedSubtitle", "already_have_subtitle": False})
        return pps

    def _cancelled(self) -> bool:
        return bool(self.options.cancel_event and self.options.cancel_event.is_set())

    def _progress_hook(self, task_id: int, dashboard: ProgressDashboard, state: dict[str, Any]):
        def hook(data: dict[str, Any]) -> None:
            if self._cancelled():
                raise DownloadCancelledError("Cancelled by user.")
            status = data.get("status")
            if status == "downloading":
                total = _int(data.get("total_bytes")) or _int(data.get("total_bytes_estimate"))
                downloaded = _int(data.get("downloaded_bytes"))
                speed = _float(data.get("speed"))
                eta = _float(data.get("eta"))
                dashboard.download_update(
                    task_id,
                    downloaded=downloaded,
                    total=total,
                    speed=speed,
                    eta=eta,
                )
                filename = data.get("filename")
                if filename:
                    state["last_progress_file"] = str(filename)
            elif status == "finished":
                filename = data.get("filename")
                if filename:
                    state.setdefault("paths", []).append(Path(str(filename)))
                dashboard.stage(task_id, "processing")
            elif status == "error":
                state["progress_error"] = True

        return hook

    def _postprocessor_hook(
        self,
        task_id: int,
        dashboard: ProgressDashboard,
        state: dict[str, Any],
    ):
        def hook(data: dict[str, Any]) -> None:
            info = data.get("info_dict") or {}
            for key in ("filepath", "_filename"):
                if info.get(key):
                    path = Path(str(info[key]))
                    if path.suffix.lower() in _MEDIA_SUFFIXES:
                        state.setdefault("paths", []).append(path)
            if data.get("status") == "started":
                name = str(data.get("postprocessor") or "processing")
                stage = "tagging" if any(
                    word in name.lower() for word in ("metadata", "thumbnail", "subtitle", "sponsor")
                ) else "converting"
                dashboard.stage(task_id, stage)
            elif data.get("status") == "finished":
                dashboard.stage(task_id, "tagging")

        return hook

    # ------------------------------------------------------------------
    # Entry parsing
    # ------------------------------------------------------------------
    def _playlist_items(self, info: dict[str, Any]) -> list[dict[str, Any]]:
        entries = info.get("entries")
        if entries is None:
            return [info]
        if not isinstance(entries, (list, tuple)):
            # yt-dlp may return a lazy iterator for enormous channels.
            entries = list(entries)
        return [entry for entry in entries if entry]

    def _entry_from_info(
        self,
        info: dict[str, Any],
        *,
        playlist_title: str | None,
        index: int,
    ) -> MediaEntry:
        url = (
            info.get("webpage_url")
            or info.get("original_url")
            or info.get("url")
            or ""
        )
        if url and not str(url).startswith(("http://", "https://")):
            identifier = info.get("id")
            ie_key = str(info.get("ie_key") or info.get("extractor_key") or "")
            if identifier and "youtube" in ie_key.lower():
                url = f"https://www.youtube.com/watch?v={identifier}"
            else:
                url = ""
        if not url and info.get("id"):
            ie_key = str(info.get("ie_key") or info.get("extractor_key") or "")
            if "youtube" in ie_key.lower():
                url = f"https://www.youtube.com/watch?v={info['id']}"
        return MediaEntry(
            url=str(url),
            title=str(info.get("title") or info.get("id") or "Unknown"),
            id=str(info["id"]) if info.get("id") else None,
            duration=_float(info.get("duration")),
            extractor=str(info.get("extractor_key") or info.get("extractor") or info.get("ie_key") or "generic"),
            webpage_url=str(info.get("webpage_url") or url),
            playlist_title=playlist_title,
            playlist_index=_int(info.get("playlist_index")) or index,
            info=info,
        )

    def _dedupe(self, entries: Iterable[MediaEntry]) -> list[MediaEntry]:
        seen: set[str] = set()
        result: list[MediaEntry] = []
        for entry in entries:
            key = entry.id or entry.url
            if key in seen:
                continue
            seen.add(key)
            result.append(entry)
        return result

    def _is_archived(self, entry: MediaEntry) -> bool:
        return bool(self.archive and self.archive.contains(entry.id))


def _supports_embedded_thumbnail(preset: Preset) -> bool:
    """Whether yt-dlp can embed cover art in this video preset's container."""
    if not preset.is_video:
        return False
    container = (preset.container or preset.extension or "").lower()
    return container in _EMBEDDABLE_VIDEO_CONTAINERS


def _needs_thumbnail(preset: Preset) -> bool:
    """Audio covers are tagged with Mutagen; video covers only when embeddable."""
    return preset.is_audio or _supports_embedded_thumbnail(preset)


def _split_langs(value: str) -> list[str]:
    parts = [part.strip() for part in value.replace(";", ",").split(",") if part.strip()]
    return parts or ["en"]


def _int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


_SOURCE_DETAIL_KEYS = ("width", "height", "resolution", "aspect_ratio", "thumbnail")


def _merge_source_details(info: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    """Fill extraction gaps from the entry's original flat/full metadata."""

    merged = dict(info)
    for key in _SOURCE_DETAIL_KEYS:
        if merged.get(key) in (None, "") and fallback.get(key) not in (None, ""):
            merged[key] = fallback[key]
    return merged


def _entry_thumbnail(item: dict[str, Any]) -> str:
    thumb = item.get("thumbnail")
    if thumb:
        return str(thumb)
    thumbs = item.get("thumbnails")
    if isinstance(thumbs, list) and thumbs:
        last = thumbs[-1]
        if isinstance(last, dict) and last.get("url"):
            return str(last["url"])
    return ""


def _float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
