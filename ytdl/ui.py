"""Rich-based terminal UI: banners, live progress, tables, and summaries."""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich import box
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TaskProgressColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Column, Table
from rich.text import Text

from . import __app_name__, __version__
from .paths import format_bytes, format_duration, truncate_middle

if TYPE_CHECKING:
    from .downloader import DownloadResult
    from .errors import YtdlStudioError


STATUS_STYLES = {
    "completed": "green",
    "downloaded": "green",
    "done": "green",
    "skipped": "yellow",
    "planned": "cyan",
    "failed": "red",
    "error": "red",
    "queued": "dim",
    "metadata": "cyan",
    "downloading": "cyan",
    "converting": "magenta",
    "tagging": "blue",
    "starting": "cyan",
    "cancelled": "yellow",
}

STATUS_SYMBOLS = {
    "completed": "✓",
    "done": "✓",
    "skipped": "•",
    "planned": "→",
    "failed": "✗",
    "error": "✗",
    "queued": "·",
    "cancelled": "•",
}


@dataclass(slots=True)
class SummaryRow:
    """Duck-typed summary row shared with the downloader."""

    title: str
    format: str
    size: int | None
    duration: float | None
    path: str
    status: str
    error: str | None = None
    url: str = ""


def make_console(*, quiet: bool = False, json_mode: bool = False, verbose: bool = False) -> Console:
    """Create a console that behaves well in pipes and on Windows terminals."""

    return Console(
        stderr=json_mode,
        quiet=quiet and not json_mode,
        highlight=False,
        emoji=False,
        soft_wrap=False,
        force_terminal=None,
        color_system="auto" if not json_mode else None,
        _environ={"TERM": "dumb"} if json_mode else None,
        legacy_windows=False,
        width=None,
    )


class UI:
    """The presentation layer used by the CLI and downloader."""

    def __init__(
        self,
        *,
        quiet: bool = False,
        verbose: bool = False,
        json_mode: bool = False,
    ) -> None:
        self.quiet = quiet
        self.verbose = verbose
        self.json_mode = json_mode
        self.console = make_console(quiet=quiet, json_mode=json_mode, verbose=verbose)
        self.err_console = Console(
            stderr=True,
            highlight=False,
            emoji=False,
            legacy_windows=False,
            safe_box=True,
        )
        self._dashboard: ProgressDashboard | None = None

    # ------------------------------------------------------------------
    # Basic output
    # ------------------------------------------------------------------
    def print(self, *objects: object, **kwargs: Any) -> None:
        if self.quiet or self.json_mode:
            return
        self.console.print(*objects, **kwargs)

    def notice(self, message: str, *, detail: str | None = None) -> None:
        if self.quiet or self.json_mode:
            return
        body = Text(message, style="yellow")
        if detail:
            body.append(f"\n{detail}", style="dim")
        self.console.print(Panel(body, border_style="yellow", box=box.ROUNDED, padding=(0, 1)))

    def error(self, error: str | YtdlStudioError, *, hint: str | None = None) -> None:
        if self.json_mode:
            return
        title = "Error"
        message = str(error)
        if not isinstance(error, str):
            info = error.info()
            title = info.title.replace("Error", "").strip() or "Error"
            message = info.message
            hint = info.hint or hint
        body = Text(message, style="red")
        if hint:
            body.append(f"\nHint: {hint}", style="dim")
        self.err_console.print(Panel(body, title=title, border_style="red", box=box.ROUNDED))

    def warning(self, message: str) -> None:
        if self.quiet or self.json_mode:
            return
        self.console.print(f"[yellow]![/] {message}")

    def success(self, message: str) -> None:
        if self.quiet or self.json_mode:
            return
        self.console.print(f"[green]✓[/] {message}")

    def verbose_log(self, message: str) -> None:
        if not self.verbose or self.json_mode:
            return
        self.err_console.print(f"[dim]{message}[/]")

    def emit_json(self, payload: dict[str, Any]) -> None:
        """Write one JSON document to stdout, with no Rich decoration."""

        sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
        sys.stdout.flush()

    # ------------------------------------------------------------------
    # Banner/settings
    # ------------------------------------------------------------------
    def banner(self, settings: dict[str, Any]) -> None:
        if self.quiet or self.json_mode:
            return
        title = Text()
        title.append(__app_name__, style="bold cyan")
        title.append(f"  v{__version__}", style="dim")
        subtitle = Text("Unlimited YouTube & YouTube Music downloader + converter", style="dim")
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="dim", no_wrap=True)
        grid.add_column(style="white")
        rows = [
            ("Format", settings.get("preset", "best")),
            ("Output", str(settings.get("directory", "downloads"))),
            ("Template", settings.get("output_template", "default")),
            ("Concurrency", str(settings.get("concurrent", 3))),
            ("Rate limit", str(settings.get("rate_limit") or "unlimited")),
            ("Options", settings.get("options", "defaults")),
        ]
        for label, value in rows:
            grid.add_row(f"{label:<12}", str(value))
        panel = Panel(
            Group(subtitle, Text(""), grid),
            title=title,
            title_align="left",
            border_style="cyan",
            box=box.ROUNDED,
            padding=(1, 2),
        )
        self.console.print(panel)
        self.console.print()

    # ------------------------------------------------------------------
    # Live progress
    # ------------------------------------------------------------------
    def start_progress(self, *, total_files: int | None = None) -> ProgressDashboard:
        if self._dashboard is None:
            self._dashboard = ProgressDashboard(self.console, total_files=total_files, enabled=not (self.quiet or self.json_mode))
            self._dashboard.start()
        return self._dashboard

    def stop_progress(self) -> None:
        if self._dashboard is not None:
            self._dashboard.stop()
            self._dashboard = None

    def progress(self) -> ProgressDashboard | None:
        return self._dashboard

    # ------------------------------------------------------------------
    # Info and formats
    # ------------------------------------------------------------------
    def render_info(self, info: dict[str, Any], formats: Sequence[dict[str, Any]] | None = None) -> None:
        if self.json_mode:
            self.emit_json(_json_safe_info(info))
            return
        fields = [
            ("Title", info.get("title") or "Unknown"),
            ("Channel", info.get("channel") or info.get("uploader") or "Unknown"),
            ("Artist", info.get("artist") or "—"),
            ("Album", info.get("album") or "—"),
            ("Duration", format_duration(info.get("duration"))),
            ("Uploaded", _format_date(info.get("upload_date"))),
            ("Views", _format_number(info.get("view_count"))),
            ("ID", info.get("id") or "—"),
            ("URL", info.get("webpage_url") or info.get("original_url") or "—"),
            ("Formats", str(len(formats if formats is not None else info.get("formats") or []))),
        ]
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="dim", no_wrap=True)
        grid.add_column(style="white", overflow="fold")
        for label, value in fields:
            grid.add_row(label, str(value))
        self.console.print(
            Panel(grid, title="Media information", border_style="cyan", box=box.ROUNDED, padding=(1, 2))
        )
        if formats:
            self._formats_table(formats, title="Available formats", limit=None)
        snippet = info.get("description")
        if snippet:
            text = str(snippet).strip()
            if len(text) > 700:
                text = text[:697] + "…"
            self.console.print(Panel(Text(text, style="dim"), title="Description", border_style="dim", box=box.ROUNDED))

    def render_formats(self, info: dict[str, Any], formats: Sequence[dict[str, Any]] | None = None) -> None:
        if self.json_mode:
            self.emit_json(
                {
                    "id": info.get("id"),
                    "title": info.get("title"),
                    "formats": [_format_row(fmt) for fmt in (formats or info.get("formats") or [])],
                }
            )
            return
        self.console.print(
            f"[bold]{info.get('title') or 'Media'}[/]  [dim]({info.get('id') or 'unknown id'})[/]"
        )
        self._formats_table(formats or info.get("formats") or [], title="Every selectable format", limit=None)

    def _formats_table(
        self,
        formats: Sequence[dict[str, Any]],
        *,
        title: str,
        limit: int | None = 40,
    ) -> None:
        table = Table(
            title=title,
            box=box.SIMPLE_HEAVY,
            header_style="bold cyan",
            show_lines=False,
            padding=(0, 1),
        )
        table.add_column("ID", style="bold", no_wrap=True, width=7)
        table.add_column("Ext", no_wrap=True, width=5)
        table.add_column("Resolution", no_wrap=True, width=14)
        table.add_column("Codec", no_wrap=True, width=20)
        table.add_column("Size", justify="right", no_wrap=True, width=10)
        rows = _sort_formats(list(formats))
        if limit is not None and len(rows) > limit:
            rows = rows[:limit]
        for fmt in rows:
            codec = _codec_label(fmt)
            resolution = _resolution(fmt)
            fps = fmt.get("fps")
            if fps and resolution not in {"audio", "—"}:
                resolution = f"{resolution}@{float(fps):.0f}"
            table.add_row(
                str(fmt.get("format_id") or "—"),
                str(fmt.get("ext") or "—"),
                resolution,
                codec[:20],
                format_bytes(fmt.get("filesize") or fmt.get("filesize_approx")),
            )
        self.console.print(table)
        if limit is not None and len(formats) > limit:
            self.console.print(
                f"[dim]Showing {limit} of {len(formats)} formats; use `ytdl formats URL` for all.[/]"
            )

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    def render_summary(
        self,
        results: Sequence[DownloadResult],
        *,
        elapsed: float,
        title: str = "Download summary",
    ) -> int:
        """Render the final table and return the desired exit code."""

        failures = [row for row in results if row.status == "failed"]
        if self.json_mode:
            payload = {
                "ok": not failures,
                "elapsed_seconds": round(elapsed, 3),
                "total_size": sum(row.size or 0 for row in results),
                "completed": sum(row.status in {"completed", "downloaded"} for row in results),
                "skipped": sum(row.status == "skipped" for row in results),
                "failed": len(failures),
                "items": [
                    {
                        "title": row.title,
                        "format": row.format,
                        "size": row.size,
                        "duration": row.duration,
                        "path": row.path,
                        "status": row.status,
                        "error": row.error,
                        "url": row.url,
                    }
                    for row in results
                ],
            }
            self.emit_json(payload)
            return 1 if failures else 0

        table = Table(
            title=title,
            box=box.SIMPLE_HEAVY,
            header_style="bold cyan",
            show_lines=False,
            padding=(0, 1),
            expand=True,
        )
        table.add_column("", width=3, no_wrap=True)
        table.add_column(
            "Title",
            style="white",
            overflow="ellipsis",
            ratio=3,
            min_width=14,
            no_wrap=True,
        )
        table.add_column("Format", no_wrap=True, width=8)
        table.add_column("Size", justify="right", no_wrap=True, width=9)
        table.add_column("Duration", justify="right", no_wrap=True, width=8)
        table.add_column(
            "Path",
            style="dim",
            overflow="ellipsis",
            ratio=2,
            min_width=10,
            no_wrap=True,
        )
        total_size = sum(row.size or 0 for row in results)
        for row in results:
            style = STATUS_STYLES.get(row.status, "white")
            symbol = STATUS_SYMBOLS.get(row.status, "·")
            table.add_row(
                Text(symbol, style=style),
                row.title,
                row.format,
                format_bytes(row.size),
                format_duration(row.duration),
                _display_path(row.path),
            )
        self.console.print(table)
        completed = sum(row.status in {"completed", "downloaded"} for row in results)
        skipped = sum(row.status == "skipped" for row in results)
        summary = Text()
        summary.append(f"{completed} completed", style="green")
        summary.append(f" · {skipped} skipped", style="yellow")
        summary.append(f" · {len(failures)} failed", style="red" if failures else "dim")
        summary.append(f" · {format_bytes(total_size)} · {format_duration(elapsed)}", style="dim")
        self.console.print(summary)
        if failures:
            self.console.print()
            for row in failures:
                message = row.error or "Download failed."
                self.error(message[:500], hint=None)
        return 1 if failures else 0

    def render_dry_run(self, rows: Sequence[Any]) -> None:
        if self.json_mode:
            self.emit_json(
                {
                    "ok": True,
                    "dry_run": True,
                    "items": [
                        {
                            "title": getattr(row, "title", ""),
                            "url": getattr(row, "url", ""),
                            "format": getattr(row, "format", ""),
                            "duration": getattr(row, "duration", None),
                        }
                        for row in rows
                    ],
                }
            )
            return
        table = Table(
            title="Dry run — nothing will be written",
            box=box.SIMPLE_HEAVY,
            header_style="bold cyan",
            expand=True,
        )
        table.add_column("#", justify="right", style="dim", width=4, no_wrap=True)
        table.add_column(
            "Title",
            overflow="ellipsis",
            ratio=3,
            min_width=14,
            no_wrap=True,
        )
        table.add_column("Format", width=8, no_wrap=True)
        table.add_column("Duration", justify="right", width=8, no_wrap=True)
        table.add_column(
            "URL",
            style="dim",
            overflow="ellipsis",
            ratio=2,
            min_width=10,
            no_wrap=True,
        )
        for index, row in enumerate(rows, start=1):
            table.add_row(
                str(index),
                getattr(row, "title", "") or "Unknown",
                getattr(row, "format", ""),
                format_duration(getattr(row, "duration", None)),
                getattr(row, "url", ""),
            )
        self.console.print(table)
        self.console.print(f"[dim]Planned {len(rows)} item(s).[/]")


class ProgressDashboard:
    """A thread-safe Rich live table with one progress row per item."""

    def __init__(self, console: Console, *, total_files: int | None, enabled: bool = True) -> None:
        self.console = console
        self.total_files = total_files
        self.enabled = enabled
        self.progress = Progress(
            TextColumn("{task.fields[symbol]:>2}", justify="right", table_column=Column(width=2)),
            TextColumn(
                "[bold]{task.fields[title]}",
                table_column=Column(no_wrap=True, overflow="ellipsis", ratio=2, min_width=16),
            ),
            BarColumn(bar_width=None, table_column=Column(ratio=3, min_width=8)),
            TaskProgressColumn(table_column=Column(width=5, no_wrap=True)),
            DownloadColumn(table_column=Column(width=13, no_wrap=True)),
            TransferSpeedColumn(table_column=Column(width=11, no_wrap=True)),
            TimeRemainingColumn(compact=True, table_column=Column(width=6, no_wrap=True)),
            TextColumn(
                "[cyan]{task.fields[stage]}",
                table_column=Column(width=11, no_wrap=True, overflow="ellipsis"),
            ),
            console=console,
            expand=True,
            transient=True,
            disable=not enabled,
        )
        self.live: Live | None = None
        self._lock = threading.RLock()
        self._started_at = time.monotonic()

    def start(self) -> None:
        if not self.enabled or self.live is not None:
            return
        self.live = Live(
            self._render(),
            console=self.console,
            refresh_per_second=8,
            transient=True,
            vertical_overflow="visible",
        )
        self.live.start(refresh=True)

    def stop(self) -> None:
        if self.live is not None:
            self.live.update(self._render())
            self.live.stop()
            self.live = None

    def _render(self) -> RenderableType:
        elapsed = format_duration(time.monotonic() - self._started_at)
        completed = sum(task.finished for task in self.progress.tasks)
        total = self.total_files if self.total_files is not None else len(self.progress.tasks)
        header = Text()
        header.append("  Downloading", style="bold cyan")
        header.append(f"  {completed}/{total} finished", style="dim")
        header.append(f"  ·  {elapsed}", style="dim")
        return Group(header, self.progress)

    def add_task(self, title: str, *, stage: str = "queued") -> int:
        with self._lock:
            return self.progress.add_task(
                "",
                total=None,
                title=truncate_middle(title or "Unknown", 40),
                stage=stage,
                symbol=STATUS_SYMBOLS.get(stage, "·"),
            )

    def set_title(self, task_id: int, title: str) -> None:
        self.update(task_id, title=truncate_middle(title or "Unknown", 40))

    def update(self, task_id: int, **fields: Any) -> None:
        if not self.enabled:
            return
        with self._lock:
            if "stage" in fields:
                fields.setdefault("symbol", STATUS_SYMBOLS.get(str(fields["stage"]), "·"))
            self.progress.update(task_id, **fields)
            if self.live is not None:
                self.live.update(self._render(), refresh=True)

    def download_update(
        self,
        task_id: int,
        *,
        downloaded: int | None = None,
        total: int | None = None,
        speed: float | None = None,
        eta: float | None = None,
    ) -> None:
        if not self.enabled:
            return
        with self._lock:
            task = self.progress.tasks[task_id]
            fields: dict[str, Any] = {"stage": "downloading", "symbol": "↓"}
            if total and total > 0:
                fields["total"] = total
            elif task.total is None:
                fields["total"] = None
            if downloaded is not None:
                if total and total > 0:
                    fields["completed"] = min(downloaded, total)
                else:
                    fields["completed"] = downloaded
            self.progress.update(task_id, **fields)
            if self.live is not None:
                self.live.update(self._render(), refresh=True)

    def stage(self, task_id: int, stage: str, *, total: float | None = None) -> None:
        if not self.enabled:
            return
        with self._lock:
            fields: dict[str, Any] = {"stage": stage, "symbol": STATUS_SYMBOLS.get(stage, "·")}
            if total is not None:
                fields["total"] = total
                fields["completed"] = 0
            self.progress.update(task_id, **fields)
            if self.live is not None:
                self.live.update(self._render(), refresh=True)

    def finish(self, task_id: int, *, status: str = "completed", detail: str | None = None) -> None:
        if not self.enabled:
            return
        with self._lock:
            task = self.progress.tasks[task_id]
            total = task.total if task.total not in (None, 0) else 1
            stage = detail or status
            self.progress.update(
                task_id,
                stage=stage,
                symbol=STATUS_SYMBOLS.get(status, "·"),
                total=total,
                completed=total,
            )
            if self.live is not None:
                self.live.update(self._render(), refresh=True)


def _sort_formats(formats: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort formats the way a human reads them.

    Highest resolution first; within a resolution, combined audio+video streams
    come before video-only and audio-only streams.  Ties keep the highest frame
    rate and bitrate at the top.  The original order is preserved for equal
    keys, which keeps yt-dlp's preference intact.
    """

    def group(fmt: dict[str, Any]) -> int:
        video = _has_video(fmt)
        audio = _has_audio(fmt)
        if video and audio:
            return 0
        if video:
            return 1
        if audio:
            return 2
        return 3

    def height(fmt: dict[str, Any]) -> int:
        try:
            return int(fmt.get("height") or 0)
        except (TypeError, ValueError):
            return 0

    def numeric(value: object) -> float:
        try:
            return float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 0.0

    return sorted(
        formats,
        key=lambda fmt: (
            -height(fmt),
            group(fmt),
            -numeric(fmt.get("fps")),
            -numeric(fmt.get("tbr") or fmt.get("abr")),
            str(fmt.get("format_id") or ""),
        ),
    )


def _has_video(fmt: dict[str, Any]) -> bool:
    codec = fmt.get("vcodec")
    return bool(codec) and str(codec).lower() != "none"


def _has_audio(fmt: dict[str, Any]) -> bool:
    codec = fmt.get("acodec")
    return bool(codec) and str(codec).lower() != "none"


_CODEC_LABELS = {
    "avc1": "H.264",
    "avc3": "H.264",
    "h264": "H.264",
    "hev1": "H.265",
    "hvc1": "H.265",
    "h265": "H.265",
    "vp9": "VP9",
    "vp09": "VP9",
    "av01": "AV1",
    "av1": "AV1",
    "mp4a": "AAC",
    "aac": "AAC",
    "opus": "Opus",
    "vorbis": "Vorbis",
    "flac": "FLAC",
    "mp3": "MP3",
    "ac3": "AC-3",
    "ec-3": "E-AC-3",
    "dtse": "DTS",
}


def _codec_label(fmt: dict[str, Any]) -> str:
    """Return a short, readable codec summary for one format row."""

    labels: list[str] = []
    if _has_video(fmt):
        labels.append(_codec_family(str(fmt.get("vcodec")), kind="video"))
    if _has_audio(fmt):
        labels.append(_codec_family(str(fmt.get("acodec")), kind="audio"))
    return " + ".join(labels) or "—"


def _codec_family(codec: str, *, kind: str) -> str:
    prefix = codec.split(".")[0].lower()
    if prefix in _CODEC_LABELS:
        return _CODEC_LABELS[prefix]
    return codec[:18]


def _resolution(fmt: dict[str, Any]) -> str:
    resolution = fmt.get("resolution")
    if resolution and str(resolution).lower() != "audio only":
        return str(resolution)
    width, height = fmt.get("width"), fmt.get("height")
    if width and height:
        return f"{width}x{height}"
    if _has_audio(fmt) and not _has_video(fmt):
        return "audio"
    return "—"


def _format_date(value: object) -> str:
    text = str(value or "")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text or "—"


def _format_number(value: object) -> str:
    try:
        return f"{int(str(value)):,}"
    except (TypeError, ValueError):
        return "—"


def _display_path(value: str | None) -> str:
    if not value:
        return "—"
    path = Path(value)
    try:
        return str(path.relative_to(Path.cwd()))
    except (ValueError, OSError):
        return str(path)


def _format_row(fmt: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": fmt.get("format_id"),
        "ext": fmt.get("ext"),
        "resolution": _resolution(fmt),
        "fps": fmt.get("fps"),
        "vcodec": fmt.get("vcodec"),
        "acodec": fmt.get("acodec"),
        "filesize": fmt.get("filesize") or fmt.get("filesize_approx"),
        "note": fmt.get("format_note"),
    }


def _json_safe_info(info: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "title",
        "description",
        "channel",
        "uploader",
        "artist",
        "album",
        "duration",
        "upload_date",
        "release_date",
        "view_count",
        "like_count",
        "webpage_url",
        "original_url",
        "thumbnail",
        "formats",
    )
    return {key: info.get(key) for key in keys if key in info}
