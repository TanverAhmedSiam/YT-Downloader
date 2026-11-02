"""Background jobs, progress events, and local actions for the studio UI."""

from __future__ import annotations

import contextlib
import json
import os
import queue
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import Config, ConfigError, default_config_paths, find_config, load_config
from ..cookies import resolve_cookies
from ..doctor import run_checks
from ..downloader import (
    Downloader,
    DownloadOptions,
    DownloadResult,
    is_channel_url,
    is_single_video_url,
    youtube_list_id,
    youtube_playlist_url,
)
from ..errors import DownloadCancelledError, InvalidOptionError, YtdlStudioError
from ..ui import UI, ProgressDashboard
from .catalog import build_download_request, build_info_options


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_number(value: object) -> float | None:
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def _apply_cookie_source(options: DownloadOptions, urls: list[str]) -> None:
    """Swap a browser name for a cookie file that yt-dlp can actually read.

    If no browser works, cookies are cleared instead of letting a locked or
    encrypted profile fail the whole inspect/download operation.
    """

    try:
        path = resolve_cookies(
            options.cookies_from_browser,
            cookie_file=options.cookies,
            urls=urls,
        )
    except Exception:
        path = None
    options.cookies = str(path) if path is not None else None
    options.cookies_from_browser = None


def _result_dict(result: DownloadResult) -> dict[str, Any]:
    return {
        "title": result.title,
        "format": result.format,
        "size": result.size,
        "duration": result.duration,
        "path": result.path,
        "status": result.status,
        "error": result.error,
        "url": result.url,
        "id": result.id,
        "elapsed": round(result.elapsed, 2),
    }


class EventBus:
    """Fan-out queue used by the SSE endpoint."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()
        self._sequence = 0

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        channel: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=600)
        with self._lock:
            self._subscribers.add(channel)
        return channel

    def unsubscribe(self, channel: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(channel)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._sequence += 1
            event = {"seq": self._sequence, **event}
            subscribers = tuple(self._subscribers)
        for channel in subscribers:
            try:
                channel.put_nowait(event)
            except queue.Full:
                with contextlib.suppress(queue.Empty):
                    channel.get_nowait()
                with contextlib.suppress(queue.Full):
                    channel.put_nowait(event)


@dataclass
class Job:
    id: str
    urls: list[str]
    meta: dict[str, Any]
    options: DownloadOptions
    status: str = "queued"
    created_at: str = field(default_factory=_now)
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None
    tasks: dict[int, dict[str, Any]] = field(default_factory=dict)
    task_order: list[int] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    _next_task: int = 0
    _lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    def add_task(self, title: str, stage: str = "queued") -> dict[str, Any]:
        with self._lock:
            self._next_task += 1
            task_id = self._next_task
            task = {
                "id": task_id,
                "title": title or "Unknown",
                "stage": stage,
                "status": "active",
                "downloaded": None,
                "total": None,
                "speed": None,
                "eta": None,
                "detail": None,
                "path": "",
                "size": None,
                "duration": None,
                "error": None,
            }
            self.tasks[task_id] = task
            self.task_order.append(task_id)
            return dict(task)

    def update_task(self, task_id: int, **fields: Any) -> dict[str, Any]:
        with self._lock:
            task = self.tasks.get(task_id)
            if task is None:
                raise KeyError(f"Unknown task {task_id}")
            for key, value in fields.items():
                if key in {"speed", "eta", "downloaded", "total", "duration", "size"}:
                    number = _safe_number(value)
                    if number is not None:
                        task[key] = number
                    continue
                task[key] = value
            return dict(task)

    def task(self, task_id: int) -> dict[str, Any] | None:
        with self._lock:
            task = self.tasks.get(task_id)
            return dict(task) if task else None

    def add_log(self, message: str) -> None:
        with self._lock:
            self.logs.append(message)
            if len(self.logs) > 250:
                del self.logs[:100]

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            tasks = [dict(self.tasks[task_id]) for task_id in self.task_order if task_id in self.tasks]
            results = [dict(item) for item in self.results]
            logs = list(self.logs[-80:])
            summary = self._summary_locked()
            return {
                "id": self.id,
                "urls": list(self.urls),
                "status": self.status,
                "created_at": self.created_at,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "error": self.error,
                "mode": self.meta.get("mode", "video"),
                "format": self.meta.get("format_label", self.options.preset.name),
                "preset": self.meta.get("preset", self.options.preset.name),
                "destination": self.meta.get("destination", str(self.options.download_dir)),
                "template": self.meta.get("template", ""),
                "template_label": self.meta.get("template_label", "smart"),
                "count": self.meta.get("count", len(self.urls)),
                "playlist_mode": self.meta.get("playlist_mode", self.options.playlist_mode),
                "playlist_items": self.meta.get(
                    "playlist_items", list(self.options.playlist_items or ())
                ),
                "dry_run": bool(self.meta.get("dry_run")),
                "music": bool(self.meta.get("music")),
                "bitrate": self.meta.get("bitrate"),
                "subtitles": bool(self.meta.get("subtitles")),
                "subtitle_langs": self.meta.get("subtitle_langs", ""),
                "archive": self.meta.get("archive", ""),
                "tasks": tasks,
                "results": results,
                "summary": summary,
                "logs": logs,
            }

    def decorate_results(self, results: list[DownloadResult]) -> None:
        """Attach result paths and final states to dashboard task rows."""

        unmatched = list(self.task_order)
        for result in results:
            target = None
            for task_id in unmatched:
                task = self.tasks.get(task_id)
                if task and task.get("title") == result.title:
                    target = task
                    unmatched.remove(task_id)
                    break
            if target is None and unmatched:
                target = self.tasks.get(unmatched.pop(0))
            if target is None:
                task = self.add_task(result.title, stage=result.status)
                target = self.tasks.get(int(task["id"]))
            if target is None:
                continue
            target.update(
                {
                    "title": result.title or target.get("title"),
                    "status": result.status,
                    "stage": result.status,
                    "path": result.path,
                    "size": result.size,
                    "duration": result.duration,
                    "error": result.error,
                    "downloaded": result.size,
                    "total": result.size,
                }
            )
        for task_id in unmatched:
            task = self.tasks.get(task_id)
            if task and task.get("status") == "active":
                task.update(
                    {
                        "status": "cancelled",
                        "stage": "cancelled",
                        "error": "Cancelled by user.",
                    }
                )

    def _summary_locked(self) -> dict[str, Any]:
        tasks = list(self.tasks.values())
        completed = sum(1 for task in tasks if task.get("status") == "completed")
        failed = sum(1 for task in tasks if task.get("status") == "failed")
        skipped = sum(1 for task in tasks if task.get("status") == "skipped")
        cancelled = sum(1 for task in tasks if task.get("status") == "cancelled")
        total_size = sum(
            int(task.get("size") or 0)
            for task in tasks
            if task.get("status") == "completed"
        )
        if not total_size:
            total_size = sum(
                int(item.get("size") or 0)
                for item in self.results
                if item.get("status") == "completed"
            )
        elapsed: float | None = None
        if self.started_at:
            try:
                start = datetime.fromisoformat(self.started_at).timestamp()
                end = (
                    datetime.fromisoformat(self.finished_at).timestamp()
                    if self.finished_at
                    else time.time()
                )
                elapsed = max(0.0, end - start)
            except ValueError:
                elapsed = None
        return {
            "total": len(tasks) or int(self.meta.get("count") or 0),
            "completed": completed,
            "failed": failed,
            "skipped": skipped,
            "cancelled": cancelled,
            "size": total_size,
            "elapsed": elapsed,
        }


class JobManager:
    """Serial job queue with one active download batch at a time."""

    def __init__(self) -> None:
        self.bus = EventBus()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._lock = threading.RLock()
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._worker = threading.Thread(
            target=self._work_loop, name="ytdl-gui-worker", daemon=True
        )
        self._worker.start()

    # ------------------------------------------------------------------
    # Public API used by the HTTP layer
    # ------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            jobs = [
                self._jobs[job_id].snapshot()
                for job_id in self._order
                if job_id in self._jobs
            ]
        return {"jobs": jobs}

    def start_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = load_config()
        urls, options, meta = build_download_request(payload, config)
        _apply_cookie_source(options, urls)
        job = Job(
            id=secrets.token_hex(4),
            urls=urls,
            meta=meta,
            options=options,
            cancel_event=options.cancel_event or threading.Event(),
        )
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
        self.bus.publish({"type": "job_created", "job": job.snapshot()})
        self._queue.put(job.id)
        return job.snapshot()

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise InvalidOptionError("That download is no longer in this session.")
        job.cancel_event.set()
        if job.status == "queued":
            job.status = "cancelled"
            job.finished_at = _now()
            self.bus.publish({"type": "job_finished", "job": job.snapshot()})
        elif job.status in {"running", "cancelling"}:
            job.status = "cancelling"
            self.bus.publish({"type": "job_state", "job": job.snapshot()})
        return job.snapshot()

    def clear_finished(self) -> dict[str, Any]:
        terminal = {"completed", "partial", "failed", "cancelled"}
        removed: list[str] = []
        with self._lock:
            for job_id in list(self._order):
                job = self._jobs.get(job_id)
                if job is not None and job.status in terminal:
                    self._order.remove(job_id)
                    self._jobs.pop(job_id, None)
                    removed.append(job_id)
        if removed:
            self.bus.publish({"type": "jobs_removed", "ids": removed})
        return self.snapshot()

    def get_job(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def inspect(self, payload: dict[str, Any]) -> dict[str, Any]:
        config = load_config()
        url, options = build_info_options(payload, config)
        _apply_cookie_source(options, [url])
        ui = UI(quiet=True, json_mode=True)
        downloader = Downloader(options, ui)
        if is_single_video_url(url):
            info = downloader.get_info(url, playlist=False)
            result = _inspect_payload(info)
            list_id = youtube_list_id(url)
            if list_id:
                try:
                    result["playlist"] = downloader.list_playlist(youtube_playlist_url(list_id))
                except YtdlStudioError as exc:
                    result["playlist_error"] = exc.message
                    return result
                media = result["media"]
                media["playlist_title"] = result["playlist"]["title"] or None
                media["playlist_count"] = result["playlist"]["count"]
                media["playlist_id"] = list_id
            return result

        listing = (
            downloader.list_channel(url)
            if is_channel_url(url)
            else downloader.list_playlist(url)
        )
        return _collection_inspection(listing, url, downloader)

    def expand(self, payload: dict[str, Any]) -> dict[str, Any]:
        """List the tracks inside an album or playlist without probing media."""

        config = load_config()
        url, options = build_info_options(payload, config)
        _apply_cookie_source(options, [url])
        ui = UI(quiet=True, json_mode=True)
        downloader = Downloader(options, ui)
        return downloader.list_playlist(url)

    def doctor(self) -> dict[str, Any]:
        try:
            config = load_config()
            config_path = find_config()
        except ConfigError:
            config = Config()
            config_path = find_config()
        report = run_checks(
            download_dir=config.directory,
            config_path=config_path,
            proxy=config.proxy,
        )
        return report.as_dict()

    def install_aria2c(self) -> dict[str, Any]:
        from ..aria2 import install_aria2c

        path = install_aria2c()
        return {"path": str(path), "report": self.doctor()}

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------
    def _work_loop(self) -> None:
        while True:
            job_id = self._queue.get()
            if job_id is None:
                self._queue.task_done()
                return
            try:
                job = self.get_job(job_id)
                if job is None or job.status == "cancelled":
                    continue
                self._run_job(job)
            finally:
                self._queue.task_done()

    def _run_job(self, job: Job) -> None:
        if job.cancel_event.is_set():
            job.status = "cancelled"
            job.finished_at = _now()
            self.bus.publish({"type": "job_finished", "job": job.snapshot()})
            return

        job.status = "running"
        job.started_at = _now()
        self.bus.publish({"type": "job_state", "job": job.snapshot()})
        ui = StudioUI(job, self.bus)
        results: list[DownloadResult] = []
        error_message: str | None = None
        try:
            results = Downloader(job.options, ui).run(job.urls)
        except DownloadCancelledError:
            job.cancel_event.set()
        except YtdlStudioError as exc:
            error_message = exc.message
        except Exception as exc:  # pragma: no cover - defensive boundary
            error_message = str(exc) or exc.__class__.__name__

        with job._lock:
            job.results = [_result_dict(result) for result in results]
            job.decorate_results(results)
            if error_message:
                job.error = error_message
            if job.cancel_event.is_set():
                job.status = "cancelled"
            elif error_message:
                job.status = "failed"
            elif any(item.get("status") == "failed" for item in job.results):
                completed = any(
                    item.get("status") in {"completed", "skipped"} for item in job.results
                )
                job.status = "partial" if completed else "failed"
            elif job.results and all(
                item.get("status") == "cancelled" for item in job.results
            ):
                job.status = "cancelled"
            else:
                job.status = "completed"
            job.finished_at = _now()
            snapshot = job.snapshot()
        self.bus.publish({"type": "job_finished", "job": snapshot})


class StudioUI(UI):
    """A quiet UI adapter that turns downloader messages into browser events."""

    def __init__(self, job: Job, bus: EventBus) -> None:
        super().__init__(quiet=True, json_mode=True)
        self.job = job
        self.bus = bus

    def start_progress(self, *, total_files: int | None = None) -> ProgressDashboard:
        return StudioDashboard(self.job, self.bus)  # type: ignore[return-value]

    def stop_progress(self) -> None:
        return None

    def notice(self, message: str, *, detail: str | None = None) -> None:
        self._toast("info", message, detail)

    def warning(self, message: str) -> None:
        self._toast("warning", message)

    def success(self, message: str) -> None:
        self._toast("success", message)

    def error(self, error: str | YtdlStudioError, *, hint: str | None = None) -> None:
        message = str(error)
        detail = hint
        if isinstance(error, YtdlStudioError):
            message = error.message
            detail = error.hint or hint
        self._toast("error", message, detail)

    def verbose_log(self, message: str) -> None:
        self.job.add_log(str(message))

    def banner(self, settings: dict[str, Any]) -> None:
        return None

    def render_dry_run(self, rows: Any) -> None:
        planned = [
            {
                "title": getattr(row, "title", ""),
                "format": getattr(row, "format", ""),
                "duration": getattr(row, "duration", None),
                "url": getattr(row, "url", ""),
            }
            for row in rows
        ]
        self.bus.publish(
            {"type": "dry_run", "job_id": self.job.id, "planned": planned}
        )

    def _toast(self, level: str, message: str, detail: str | None = None) -> None:
        self.bus.publish(
            {
                "type": "toast",
                "job_id": self.job.id,
                "level": level,
                "message": str(message),
                "detail": detail,
            }
        )


class StudioDashboard(ProgressDashboard):
    """Progress sink compatible with :class:`Downloader`."""

    def __init__(self, job: Job, bus: EventBus) -> None:
        self.job = job
        self.bus = bus
        self._lock = threading.RLock()

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

    def add_task(self, title: str, *, stage: str = "queued") -> int:
        task = self.job.add_task(title, stage=stage)
        self.bus.publish(
            {
                "type": "task_added",
                "job_id": self.job.id,
                "task": task,
                "summary": self.job._summary_locked(),
            }
        )
        return int(task["id"])

    def set_title(self, task_id: int, title: str) -> None:
        self._update(task_id, title=title)

    def update(self, task_id: int, **fields: Any) -> None:
        self._update(task_id, **fields)

    def download_update(
        self,
        task_id: int,
        *,
        downloaded: int | None = None,
        total: int | None = None,
        speed: float | None = None,
        eta: float | None = None,
    ) -> None:
        if self.job.cancel_event.is_set():
            raise DownloadCancelledError("Cancelled by user.")
        self._update(
            task_id,
            stage="downloading",
            downloaded=downloaded,
            total=total,
            speed=speed,
            eta=eta,
        )

    def stage(self, task_id: int, stage: str, *, total: float | None = None) -> None:
        fields: dict[str, Any] = {"stage": stage}
        if total is not None:
            fields["total"] = total
            fields["downloaded"] = 0
        self._update(task_id, **fields)

    def finish(
        self,
        task_id: int,
        *,
        status: str = "completed",
        detail: str | None = None,
    ) -> None:
        self._update(task_id, stage=detail or status, status=status, detail=detail)

    def _update(self, task_id: int, **fields: Any) -> None:
        task = self.job.update_task(task_id, **fields)
        self.bus.publish(
            {
                "type": "task",
                "job_id": self.job.id,
                "task": task,
                "summary": self.job._summary_locked(),
            }
        )


def _collection_inspection(
    listing: dict[str, Any],
    url: str,
    downloader: Downloader,
) -> dict[str, Any]:
    entries = listing.get("entries") or []
    first = entries[0] if entries else {}
    kind = str(listing.get("kind") or "playlist")
    sections = listing.get("sections") or []
    title = str(listing.get("title") or ("Channel" if kind == "channel" else "Playlist"))
    media = {
        "id": listing.get("id") or None,
        "title": title,
        "channel": title if kind == "channel" else None,
        "artist": None,
        "album": None,
        "duration": None,
        "upload_date": None,
        "view_count": None,
        "thumbnail": listing.get("thumbnail") or first.get("thumbnail") or None,
        "webpage_url": url,
        "description": "",
        "playlist_title": title,
        "playlist_count": listing.get("count") or len(entries) or None,
        "playlist_kind": kind,
        "playlist_sections": len(sections),
        "channel_source": listing.get("source"),
        "chapter_count": 0,
        "is_live": False,
        "extractor": "channel" if kind == "channel" else "playlist",
    }
    result: dict[str, Any] = {
        "media": media,
        "formats": [],
        "qualities": [],
        "has_video": True,
        "has_audio": True,
        "max_height": None,
        "playlist": listing,
    }

    # Probe the first real video (not the first album/playlist) for the
    # resolution ladder the user can pick.
    sample_url = next(
        (
            str(entry.get("url") or "")
            for entry in entries
            if is_single_video_url(str(entry.get("url") or ""))
        ),
        "",
    )
    if not sample_url and entries:
        sample_url = str(entries[0].get("url") or "")
    if sample_url:
        try:
            sample = downloader.get_info(sample_url, playlist=False)
        except YtdlStudioError:
            sample = None
        if sample:
            probe = _inspect_payload(sample)
            result["qualities"] = probe.get("qualities") or []
            result["has_video"] = bool(probe.get("has_video"))
            result["has_audio"] = bool(probe.get("has_audio"))
            result["max_height"] = probe.get("max_height")
            if not media["thumbnail"] and sample.get("thumbnail"):
                media["thumbnail"] = sample["thumbnail"]
    return result


def _inspect_payload(info: dict[str, Any]) -> dict[str, Any]:
    playlist_title = None
    playlist_count: int | None = None
    formats = info.get("formats") or []
    entries: list[dict[str, Any]] = []

    if info.get("_type") == "playlist":
        playlist_title = str(info.get("title") or "")
        raw_entries = info.get("entries") or []
        try:
            entries = [entry for entry in raw_entries if entry]
        except TypeError:
            entries = []
        playlist_count = len(entries) if entries else None
        if not formats and entries:
            first = entries[0]
            formats = first.get("formats") or []
            if not info.get("thumbnail") and first.get("thumbnail"):
                info = {**info, "thumbnail": first.get("thumbnail")}

    description = str(info.get("description") or "").strip()
    if len(description) > 900:
        description = description[:897] + "…"

    media = {
        "id": info.get("id"),
        "title": info.get("title") or (entries[0].get("title") if entries else None) or "Unknown media",
        "channel": info.get("channel") or info.get("uploader") or info.get("creator"),
        "artist": info.get("artist"),
        "album": info.get("album"),
        "duration": _safe_number(info.get("duration")),
        "upload_date": info.get("upload_date"),
        "view_count": info.get("view_count"),
        "thumbnail": info.get("thumbnail"),
        "webpage_url": info.get("webpage_url") or info.get("original_url"),
        "description": description,
        "playlist_title": playlist_title,
        "playlist_count": playlist_count,
        "chapter_count": len(info.get("chapters") or []),
        "is_live": bool(info.get("is_live")),
        "extractor": info.get("extractor_key") or info.get("extractor"),
    }
    qualities = _quality_ladder(formats, media["duration"])
    if not qualities:
        height = _safe_number(info.get("height"))
        if height:
            qualities = [
                {
                    "id": f"{int(height)}p",
                    "label": f"{int(height)}p",
                    "height": int(height),
                    "size": None,
                    "fps": _safe_number(info.get("fps")),
                }
            ]
    if formats:
        has_audio = any(
            isinstance(item, dict)
            and str(item.get("acodec") or "none") not in {"none", ""}
            for item in formats
        )
    else:
        has_audio = True
    return {
        "media": media,
        "formats": _format_rows(formats),
        "qualities": qualities,
        "max_height": qualities[-1]["height"] if qualities else None,
        "has_video": bool(qualities),
        "has_audio": has_audio,
    }


def _quality_ladder(
    formats: list[dict[str, Any]],
    duration: float | None,
) -> list[dict[str, Any]]:
    """Build the resolution rows that a single source can actually deliver.

    Sizes are estimates: real file sizes are used when the extractor knows
    them, otherwise they come from the stream bitrate and the duration.
    """

    best: dict[int, dict[str, Any]] = {}
    for item in formats:
        if not isinstance(item, dict):
            continue
        if str(item.get("vcodec") or "none") in {"none", ""}:
            continue
        height = _safe_number(item.get("height"))
        if not height or height <= 0 or height > 4320:
            continue
        key = int(height)
        size = _safe_number(item.get("filesize") or item.get("filesize_approx"))
        if not size:
            tbr = _safe_number(item.get("tbr") or item.get("vbr"))
            if tbr and duration:
                size = tbr * 1000 / 8 * duration
        size_value = int(size) if size and size > 0 else None
        fps = _safe_number(item.get("fps"))
        row = best.get(key)
        if row is None:
            best[key] = {"height": key, "size": size_value, "fps": fps}
            continue
        if size_value and (not row["size"] or size_value > row["size"]):
            row["size"] = size_value
        if fps and (not row["fps"] or fps > row["fps"]):
            row["fps"] = fps

    ladder: list[dict[str, Any]] = []
    for height in sorted(best):
        row = best[height]
        ladder.append(
            {
                "id": f"{height}p",
                "label": f"{height}p",
                "height": height,
                "size": row["size"],
                "fps": row["fps"],
            }
        )
    return ladder


def _format_rows(formats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in formats:
        if not isinstance(item, dict):
            continue
        vcodec = str(item.get("vcodec") or "none")
        acodec = str(item.get("acodec") or "none")
        has_video = vcodec not in {"none", ""}
        has_audio = acodec not in {"none", ""}
        if not has_video and not has_audio:
            continue
        kind = "video" if has_video else "audio"
        height = _safe_number(item.get("height"))
        width = _safe_number(item.get("width"))
        if has_video and height:
            resolution = f"{int(height)}p"
            if width:
                resolution = f"{int(width)}×{int(height)}"
        elif kind == "audio":
            resolution = "Audio"
        else:
            resolution = "—"
        fps = _safe_number(item.get("fps"))
        codec_parts: list[str] = []
        if has_video:
            codec_parts.append(vcodec.split(".")[0].upper())
        if has_audio:
            codec_parts.append(acodec.split(".")[0].upper())
        rows.append(
            {
                "format_id": str(item.get("format_id") or ""),
                "kind": kind,
                "ext": str(item.get("ext") or ""),
                "resolution": resolution,
                "height": int(height) if height else 0,
                "codec": " / ".join(codec_parts) or "—",
                "fps": fps,
                "bitrate": _safe_number(item.get("tbr") or item.get("abr") or item.get("vbr")),
                "size": int(item.get("filesize") or item.get("filesize_approx") or 0) or None,
                "note": str(item.get("format_note") or ""),
                "dynamic_range": str(item.get("dynamic_range") or ""),
                "audio_channels": item.get("audio_channels"),
            }
        )

    def sort_key(row: dict[str, Any]) -> tuple[int, int, float]:
        return (
            0 if row["kind"] == "video" else 1,
            -int(row.get("height") or 0),
            -float(row.get("bitrate") or 0),
        )

    rows.sort(key=sort_key)
    return rows


# ---------------------------------------------------------------------------
# Durable studio preferences
# ---------------------------------------------------------------------------
_AUDIO_BITRATES = {128, 192, 256, 320}
_PREF_TEXT_KEYS = ("directory", "browser", "cookies", "proxy")
_PREF_SAVE_LOCK = threading.Lock()


def studio_prefs_path() -> Path:
    """Per-user preferences file, next to the optional config file.

    The studio's port is random, so browser storage is scoped to a different
    origin each launch.  This file keeps the destination and network choices
    stable across launches.
    """

    candidates = default_config_paths()
    base = candidates[1] if len(candidates) > 1 else candidates[0]
    return base.parent / "studio.json"


def _clean_studio_prefs(payload: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key in _PREF_TEXT_KEYS:
        value = payload.get(key)
        if isinstance(value, str):
            clean[key] = value.strip()[:4096]
    for key, low, high in (("concurrent", 1, 8), ("retries", 0, 100)):
        if key in payload:
            try:
                clean[key] = max(low, min(high, int(payload[key])))
            except (TypeError, ValueError):
                continue
    if "audioBitrate" in payload:
        try:
            bitrate = int(payload["audioBitrate"])
        except (TypeError, ValueError):
            bitrate = 0
        if bitrate in _AUDIO_BITRATES:
            clean["audioBitrate"] = bitrate
    return clean


def load_studio_prefs(path: str | Path | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else studio_prefs_path()
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return _clean_studio_prefs(raw)


def save_studio_prefs(
    payload: dict[str, Any],
    path: str | Path | None = None,
) -> dict[str, Any]:
    clean = _clean_studio_prefs(payload)
    target = Path(path) if path is not None else studio_prefs_path()
    with _PREF_SAVE_LOCK:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.tmp")
        temporary.write_text(
            json.dumps(clean, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, target)
    return clean


# ---------------------------------------------------------------------------
# Local OS actions
# ---------------------------------------------------------------------------
def open_path(path_value: str, *, select: bool = False) -> None:
    path = Path(path_value).expanduser()
    if not path.exists():
        raise InvalidOptionError("That path is no longer available.")
    if os.name == "nt":
        if path.is_dir():
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["explorer", f"/select,{path}"], close_fds=True)
    elif sys.platform == "darwin":
        subprocess.Popen(["open", "-R" if select and path.is_file() else str(path)])
    else:
        target = path.parent if select and path.is_file() else path
        subprocess.Popen(["xdg-open", str(target)])
    return None


def choose_folder(initial: str = "", *, title: str = "Choose a download folder") -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:  # pragma: no cover - depends on platform Tk
        raise InvalidOptionError(
            "This Python build has no graphical file picker; type the folder path instead."
        ) from exc

    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        selected = filedialog.askdirectory(
            parent=root,
            initialdir=initial or str(Path.home()),
            title=title,
            mustexist=False,
        )
        root.destroy()
    except Exception as exc:  # pragma: no cover - window manager specific
        raise InvalidOptionError(
            "Could not open the folder picker; type the folder path instead.",
            hint=str(exc),
        ) from exc
    return str(Path(selected)) if selected else ""


def choose_file(
    initial: str = "",
    *,
    title: str = "Choose a file",
    filetypes: list[tuple[str, str]] | None = None,
) -> str:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:  # pragma: no cover - depends on platform Tk
        raise InvalidOptionError(
            "This Python build has no graphical file picker; type the path instead."
        ) from exc

    start = Path(initial).expanduser() if initial else Path.home()
    directory = start if start.is_dir() else start.parent
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        selected = filedialog.askopenfilename(
            parent=root,
            initialdir=str(directory),
            title=title,
            filetypes=filetypes
            or [
                ("Cookie exports", "*.txt"),
                ("Netscape cookies", "cookies.txt"),
                ("All files", "*.*"),
            ],
        )
        root.destroy()
    except Exception as exc:  # pragma: no cover - window manager specific
        raise InvalidOptionError(
            "Could not open the file picker; type the path instead.",
            hint=str(exc),
        ) from exc
    return str(Path(selected)) if selected else ""



