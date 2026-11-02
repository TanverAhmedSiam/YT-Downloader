"""Typer command-line application for ytdl-studio."""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path

import typer

from . import __version__
from .config import Config, ConfigError, find_config, load_config
from .doctor import render_report, run_checks
from .downloader import Downloader, DownloadOptions
from .errors import InvalidOptionError, YtdlStudioError
from .paths import (
    DEFAULT_DOWNLOAD_DIR,
    format_bytes,
    parse_rate_limit,
    resolve_output_template,
)
from .presets import PRESETS, Preset, parse_height, resolve_preset
from .ui import UI

APP_NAME = "ytdl"
COMMANDS = {
    "download",
    "audio",
    "playlist",
    "batch",
    "info",
    "formats",
    "doctor",
    "gui",
}
SPONSORBLOCK_CATEGORIES = {
    "sponsor",
    "selfpromo",
    "interaction",
    "intro",
    "outro",
    "preview",
    "music_offtopic",
    "filler",
    "poi_highlight",
}
BROWSERS = {"chrome", "firefox", "edge", "brave", "safari", "chromium", "opera", "vivaldi", "whale"}
DEFAULT_SPONSORBLOCK = "sponsor,selfpromo"

app = typer.Typer(
    name=APP_NAME,
    add_completion=False,
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
    epilog=(
        "Run [cyan]ytdl <URL>[/] as shorthand for [cyan]ytdl download <URL>[/].\n"
        "Examples: [cyan]ytdl download URL -q 1080p[/] · "
        "[cyan]ytdl audio URL -f flac[/] · "
        "[cyan]ytdl playlist URL --playlist-start 1 --playlist-end 5[/]"
    ),
)


@app.callback()
def _root_options(
    version: bool = typer.Option(
        False,
        "--version",
        help="Show the version and exit.",
        is_eager=True,
    ),
) -> None:
    """Download, inspect, and convert YouTube media."""

    if version:
        typer.echo(f"ytdl-studio {__version__}")
        raise typer.Exit(0)


@dataclass(slots=True)
class ResolvedRun:
    options: DownloadOptions
    config: Config
    preset: Preset


# ---------------------------------------------------------------------------
# Option factories (kept in one place so every download command behaves alike)
# ---------------------------------------------------------------------------
def _format_opt() -> typer.Option:
    return typer.Option(
        None,
        "-f",
        "--format",
        help="Preset: best, 8k, 4k, 1440p, 1080p, 720p, 480p, mp4, mp4-h264, mp4-h265, webm, mkv, mov, gif, mp3, m4a, opus, ogg, flac, wav, alac.",
        rich_help_panel="Format",
    )


def _quality_opt() -> typer.Option:
    return typer.Option(
        None,
        "-q",
        "--quality",
        metavar="HEIGHT",
        help="Target video height (4320/2160/1440/1080/720/480/360 or 1080p).",
        rich_help_panel="Format",
    )


def _bitrate_opt() -> typer.Option:
    return typer.Option(
        None,
        "-b",
        "--bitrate",
        min=8,
        max=512,
        help="Audio bitrate in kbps for lossy formats (default: config/320).",
        rich_help_panel="Format",
    )


def _output_opt() -> typer.Option:
    return typer.Option(
        None,
        "-o",
        "--output",
        metavar="TEMPLATE",
        help="yt-dlp output template. Relative templates live under --dir.",
        rich_help_panel="Output",
    )


def _dir_opt() -> typer.Option:
    return typer.Option(
        None,
        "-d",
        "--dir",
        metavar="PATH",
        help="Base download directory (default: ./downloads).",
        rich_help_panel="Output",
    )


def _music_opt() -> typer.Option:
    return typer.Option(
        None,
        "--music/--no-music",
        help="YouTube Music mode: best audio with full tags and cover art.",
        rich_help_panel="Mode",
    )


def _subs_opt() -> typer.Option:
    return typer.Option(
        None,
        "--subs",
        metavar="[LANGS]",
        help="Download and embed subtitles. Languages are comma-separated (default: en).",
        rich_help_panel="Metadata",
    )


def _no_subs_opt() -> typer.Option:
    return typer.Option(False, "--no-subs", help="Disable subtitle downloading/embedding.", rich_help_panel="Metadata")


def _thumbnail_opt() -> typer.Option:
    return typer.Option(
        None,
        "--thumbnail/--no-thumbnail",
        help="Embed cover art / video thumbnail (default: on).",
        rich_help_panel="Metadata",
    )


def _metadata_opt() -> typer.Option:
    return typer.Option(
        None,
        "--metadata/--no-metadata",
        help="Write tags and .info.json sidecars (default: on).",
        rich_help_panel="Metadata",
    )


def _chapters_opt() -> typer.Option:
    return typer.Option(
        None,
        "--chapters/--no-chapters",
        help="Embed chapters when available (default: on).",
        rich_help_panel="Metadata",
    )


def _sponsorblock_opt() -> typer.Option:
    return typer.Option(
        None,
        "--sponsorblock",
        metavar="[CATS]",
        help="Remove SponsorBlock categories (default: sponsor,selfpromo).",
        rich_help_panel="Metadata",
    )


def _no_sponsorblock_opt() -> typer.Option:
    return typer.Option(False, "--no-sponsorblock", help="Disable SponsorBlock removal.", rich_help_panel="Metadata")


def _start_opt() -> typer.Option:
    return typer.Option(None, "--playlist-start", min=1, help="First playlist/channel item (1-based).", rich_help_panel="Playlist")


def _end_opt() -> typer.Option:
    return typer.Option(None, "--playlist-end", min=1, help="Last playlist/channel item (inclusive).", rich_help_panel="Playlist")


def _shuffle_opt() -> typer.Option:
    return typer.Option(None, "--shuffle/--no-shuffle", help="Shuffle playlist items after applying ranges.", rich_help_panel="Playlist")


def _reverse_opt() -> typer.Option:
    return typer.Option(None, "--reverse/--no-reverse", help="Reverse playlist items after applying ranges.", rich_help_panel="Playlist")


def _concurrent_opt() -> typer.Option:
    return typer.Option(None, "--concurrent", min=1, metavar="N", help="Parallel downloads (default: 3, no upper cap).", rich_help_panel="Performance")


def _rate_opt() -> typer.Option:
    return typer.Option(None, "--rate-limit", metavar="RATE", help="Optional limit, e.g. 10M; omitted means unlimited.", rich_help_panel="Performance")


def _retries_opt() -> typer.Option:
    return typer.Option(None, "--retries", min=0, max=100, metavar="N", help="Network retries (default: 10).", rich_help_panel="Performance")


def _browser_opt() -> typer.Option:
    return typer.Option(None, "--cookies-from-browser", metavar="BROWSER", help="chrome|firefox|edge|brave|safari.", rich_help_panel="Authentication")


def _cookies_opt() -> typer.Option:
    return typer.Option(None, "--cookies", metavar="FILE", help="Netscape cookies.txt file.", rich_help_panel="Authentication")


def _proxy_opt() -> typer.Option:
    return typer.Option(None, "--proxy", metavar="URL", help="HTTP/SOCKS proxy for yt-dlp.", rich_help_panel="Authentication")


def _archive_opt() -> typer.Option:
    return typer.Option(None, "--archive", metavar="FILE", help="Skip completed items on future runs.", rich_help_panel="Output")


def _keep_opt() -> typer.Option:
    return typer.Option(
        None,
        "--keep-video/--no-keep-video",
        help="Keep the source file after audio/GIF conversion.",
        rich_help_panel="Output",
    )


def _dry_opt() -> typer.Option:
    return typer.Option(False, "--dry-run", help="Resolve and show what would download; write nothing.", rich_help_panel="General")


def _json_opt() -> typer.Option:
    return typer.Option(False, "--json", help="Emit a machine-readable JSON summary on stdout.", rich_help_panel="General")


def _quiet_opt() -> typer.Option:
    return typer.Option(False, "--quiet", help="Hide banners and live progress.", rich_help_panel="General")


def _verbose_opt() -> typer.Option:
    return typer.Option(False, "--verbose", help="Show extractor and conversion details on stderr.", rich_help_panel="General")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
@app.command("download", help="Download videos using a preset. Playlists and channels are expanded.")
def download(
    urls: list[str] = typer.Argument(..., help="One or more YouTube / YouTube Music URLs."),
    fmt: str | None = _format_opt(),
    quality: str | None = _quality_opt(),
    bitrate: int | None = _bitrate_opt(),
    output: str | None = _output_opt(),
    directory: str | None = _dir_opt(),
    music: bool | None = _music_opt(),
    subs: str | None = _subs_opt(),
    no_subs: bool = _no_subs_opt(),
    thumbnail: bool | None = _thumbnail_opt(),
    metadata: bool | None = _metadata_opt(),
    chapters: bool | None = _chapters_opt(),
    sponsorblock: str | None = _sponsorblock_opt(),
    no_sponsorblock: bool = _no_sponsorblock_opt(),
    playlist_start: int | None = _start_opt(),
    playlist_end: int | None = _end_opt(),
    shuffle: bool | None = _shuffle_opt(),
    reverse: bool | None = _reverse_opt(),
    concurrent: int | None = _concurrent_opt(),
    rate_limit: str | None = _rate_opt(),
    retries: int | None = _retries_opt(),
    cookies_from_browser: str | None = _browser_opt(),
    cookies: str | None = _cookies_opt(),
    proxy: str | None = _proxy_opt(),
    archive: Path | None = _archive_opt(),
    keep_video: bool | None = _keep_opt(),
    dry_run: bool = _dry_opt(),
    json_output: bool = _json_opt(),
    quiet: bool = _quiet_opt(),
    verbose: bool = _verbose_opt(),
) -> None:
    """Download one or more videos, playlists, channels, or albums."""

    _run_download(
        urls,
        command="download",
        fmt=fmt,
        quality=quality,
        bitrate=bitrate,
        output=output,
        directory=directory,
        music=music,
        subs=subs,
        no_subs=no_subs,
        thumbnail=thumbnail,
        metadata=metadata,
        chapters=chapters,
        sponsorblock=sponsorblock,
        no_sponsorblock=no_sponsorblock,
        playlist_start=playlist_start,
        playlist_end=playlist_end,
        shuffle=shuffle,
        reverse=reverse,
        concurrent=concurrent,
        rate_limit=rate_limit,
        retries=retries,
        cookies_from_browser=cookies_from_browser,
        cookies=cookies,
        proxy=proxy,
        archive=archive,
        keep_video=keep_video,
        dry_run=dry_run,
        json_output=json_output,
        quiet=quiet,
        verbose=verbose,
    )


@app.command("audio", help="Extract audio only, with full tags and cover art.")
def audio(
    urls: list[str] = typer.Argument(..., help="One or more YouTube / YouTube Music URLs."),
    fmt: str | None = _format_opt(),
    quality: str | None = _quality_opt(),
    bitrate: int | None = _bitrate_opt(),
    output: str | None = _output_opt(),
    directory: str | None = _dir_opt(),
    music: bool | None = _music_opt(),
    subs: str | None = _subs_opt(),
    no_subs: bool = _no_subs_opt(),
    thumbnail: bool | None = _thumbnail_opt(),
    metadata: bool | None = _metadata_opt(),
    chapters: bool | None = _chapters_opt(),
    sponsorblock: str | None = _sponsorblock_opt(),
    no_sponsorblock: bool = _no_sponsorblock_opt(),
    playlist_start: int | None = _start_opt(),
    playlist_end: int | None = _end_opt(),
    shuffle: bool | None = _shuffle_opt(),
    reverse: bool | None = _reverse_opt(),
    concurrent: int | None = _concurrent_opt(),
    rate_limit: str | None = _rate_opt(),
    retries: int | None = _retries_opt(),
    cookies_from_browser: str | None = _browser_opt(),
    cookies: str | None = _cookies_opt(),
    proxy: str | None = _proxy_opt(),
    archive: Path | None = _archive_opt(),
    keep_video: bool | None = _keep_opt(),
    dry_run: bool = _dry_opt(),
    json_output: bool = _json_opt(),
    quiet: bool = _quiet_opt(),
    verbose: bool = _verbose_opt(),
) -> None:
    """Extract one or more audio tracks (MP3 by default)."""

    _run_download(
        urls,
        command="audio",
        fmt=fmt,
        quality=quality,
        bitrate=bitrate,
        output=output,
        directory=directory,
        music=True if music is None else music,
        subs=subs,
        no_subs=no_subs,
        thumbnail=thumbnail,
        metadata=metadata,
        chapters=chapters,
        sponsorblock=sponsorblock,
        no_sponsorblock=no_sponsorblock,
        playlist_start=playlist_start,
        playlist_end=playlist_end,
        shuffle=shuffle,
        reverse=reverse,
        concurrent=concurrent,
        rate_limit=rate_limit,
        retries=retries,
        cookies_from_browser=cookies_from_browser,
        cookies=cookies,
        proxy=proxy,
        archive=archive,
        keep_video=keep_video,
        dry_run=dry_run,
        json_output=json_output,
        quiet=quiet,
        verbose=verbose,
    )


@app.command("playlist", help="Download a full playlist, album, channel, or mix with optional ranges.")
def playlist(
    url: str = typer.Argument(..., help="Playlist, album, channel, or mix URL."),
    fmt: str | None = _format_opt(),
    quality: str | None = _quality_opt(),
    bitrate: int | None = _bitrate_opt(),
    output: str | None = _output_opt(),
    directory: str | None = _dir_opt(),
    music: bool | None = _music_opt(),
    subs: str | None = _subs_opt(),
    no_subs: bool = _no_subs_opt(),
    thumbnail: bool | None = _thumbnail_opt(),
    metadata: bool | None = _metadata_opt(),
    chapters: bool | None = _chapters_opt(),
    sponsorblock: str | None = _sponsorblock_opt(),
    no_sponsorblock: bool = _no_sponsorblock_opt(),
    playlist_start: int | None = _start_opt(),
    playlist_end: int | None = _end_opt(),
    shuffle: bool | None = _shuffle_opt(),
    reverse: bool | None = _reverse_opt(),
    concurrent: int | None = _concurrent_opt(),
    rate_limit: str | None = _rate_opt(),
    retries: int | None = _retries_opt(),
    cookies_from_browser: str | None = _browser_opt(),
    cookies: str | None = _cookies_opt(),
    proxy: str | None = _proxy_opt(),
    archive: Path | None = _archive_opt(),
    keep_video: bool | None = _keep_opt(),
    dry_run: bool = _dry_opt(),
    json_output: bool = _json_opt(),
    quiet: bool = _quiet_opt(),
    verbose: bool = _verbose_opt(),
) -> None:
    """Download a playlist with ranges, shuffle, and reverse support."""

    _run_download(
        [url],
        command="playlist",
        fmt=fmt,
        quality=quality,
        bitrate=bitrate,
        output=output,
        directory=directory,
        music=music,
        subs=subs,
        no_subs=no_subs,
        thumbnail=thumbnail,
        metadata=metadata,
        chapters=chapters,
        sponsorblock=sponsorblock,
        no_sponsorblock=no_sponsorblock,
        playlist_start=playlist_start,
        playlist_end=playlist_end,
        shuffle=shuffle,
        reverse=reverse,
        concurrent=concurrent,
        rate_limit=rate_limit,
        retries=retries,
        cookies_from_browser=cookies_from_browser,
        cookies=cookies,
        proxy=proxy,
        archive=archive,
        keep_video=keep_video,
        dry_run=dry_run,
        json_output=json_output,
        quiet=quiet,
        verbose=verbose,
    )


@app.command("batch", help="Download many URLs from a file, one per line.")
def batch(
    file: Path = typer.Argument(..., exists=True, readable=True, dir_okay=False, help="Text file with one URL per line."),
    fmt: str | None = _format_opt(),
    quality: str | None = _quality_opt(),
    bitrate: int | None = _bitrate_opt(),
    output: str | None = _output_opt(),
    directory: str | None = _dir_opt(),
    music: bool | None = _music_opt(),
    subs: str | None = _subs_opt(),
    no_subs: bool = _no_subs_opt(),
    thumbnail: bool | None = _thumbnail_opt(),
    metadata: bool | None = _metadata_opt(),
    chapters: bool | None = _chapters_opt(),
    sponsorblock: str | None = _sponsorblock_opt(),
    no_sponsorblock: bool = _no_sponsorblock_opt(),
    playlist_start: int | None = _start_opt(),
    playlist_end: int | None = _end_opt(),
    shuffle: bool | None = _shuffle_opt(),
    reverse: bool | None = _reverse_opt(),
    concurrent: int | None = _concurrent_opt(),
    rate_limit: str | None = _rate_opt(),
    retries: int | None = _retries_opt(),
    cookies_from_browser: str | None = _browser_opt(),
    cookies: str | None = _cookies_opt(),
    proxy: str | None = _proxy_opt(),
    archive: Path | None = _archive_opt(),
    keep_video: bool | None = _keep_opt(),
    dry_run: bool = _dry_opt(),
    json_output: bool = _json_opt(),
    quiet: bool = _quiet_opt(),
    verbose: bool = _verbose_opt(),
) -> None:
    """Process a batch file concurrently. Blank lines and # comments are ignored."""

    try:
        lines = file.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        ui = UI(quiet=quiet, verbose=verbose, json_mode=json_output)
        ui.error(f"Could not read batch file {file}: {exc}")
        raise typer.Exit(1) from exc
    urls = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    if not urls:
        ui = UI(quiet=quiet, verbose=verbose, json_mode=json_output)
        ui.error(f"Batch file contains no URLs: {file}")
        raise typer.Exit(1)
    _run_download(
        urls,
        command="batch",
        fmt=fmt,
        quality=quality,
        bitrate=bitrate,
        output=output,
        directory=directory,
        music=music,
        subs=subs,
        no_subs=no_subs,
        thumbnail=thumbnail,
        metadata=metadata,
        chapters=chapters,
        sponsorblock=sponsorblock,
        no_sponsorblock=no_sponsorblock,
        playlist_start=playlist_start,
        playlist_end=playlist_end,
        shuffle=shuffle,
        reverse=reverse,
        concurrent=concurrent,
        rate_limit=rate_limit,
        retries=retries,
        cookies_from_browser=cookies_from_browser,
        cookies=cookies,
        proxy=proxy,
        archive=archive,
        keep_video=keep_video,
        dry_run=dry_run,
        json_output=json_output,
        quiet=quiet,
        verbose=verbose,
    )


@app.command("info", help="Show media metadata and a readable formats table.")
def info(
    url: str = typer.Argument(..., help="Video or audio URL."),
    cookies_from_browser: str | None = _browser_opt(),
    cookies: str | None = _cookies_opt(),
    proxy: str | None = _proxy_opt(),
    json_output: bool = _json_opt(),
    quiet: bool = _quiet_opt(),
    verbose: bool = _verbose_opt(),
) -> None:
    """Inspect a URL without downloading anything."""

    _run_info(url, formats_only=False, cookies_from_browser=cookies_from_browser, cookies=cookies, proxy=proxy, json_output=json_output, quiet=quiet, verbose=verbose)


@app.command("formats", help="List every selectable format id for a URL.")
def formats(
    url: str = typer.Argument(..., help="Video or audio URL."),
    cookies_from_browser: str | None = _browser_opt(),
    cookies: str | None = _cookies_opt(),
    proxy: str | None = _proxy_opt(),
    json_output: bool = _json_opt(),
    quiet: bool = _quiet_opt(),
    verbose: bool = _verbose_opt(),
) -> None:
    """List all yt-dlp format IDs, including video-only and audio-only streams."""

    _run_info(url, formats_only=True, cookies_from_browser=cookies_from_browser, cookies=cookies, proxy=proxy, json_output=json_output, quiet=quiet, verbose=verbose)


@app.command("doctor", help="Verify Python, FFmpeg, yt-dlp, aria2c, disk, and network.")
def doctor(
    json_output: bool = _json_opt(),
    quiet: bool = _quiet_opt(),
    verbose: bool = _verbose_opt(),
    install_aria2c: bool = typer.Option(
        False,
        "--install-aria2c",
        help="Download the managed Windows aria2c build when it is missing.",
    ),
) -> None:
    """Run environment diagnostics and print install instructions."""

    ui = UI(quiet=quiet, verbose=verbose, json_mode=json_output)
    if install_aria2c:
        from .aria2 import install_aria2c as download_aria2c

        try:
            path = download_aria2c(progress=ui.notice)
            ui.notice(f"aria2c ready at {path}")
        except YtdlStudioError as exc:
            ui.warning(exc.message)
            raise typer.Exit(1) from exc
    try:
        config = load_config()
        config_path = find_config()
        proxy = config.proxy
    except ConfigError:
        config = Config()
        config_path = find_config()
        proxy = None
    report = run_checks(download_dir=config.directory, config_path=config_path, proxy=proxy)
    if json_output:
        ui.emit_json(report.as_dict())
    else:
        render_report(report, ui.console)
    raise typer.Exit(0 if report.ok else 1)


@app.command("gui", help="Launch the dark-purple graphical studio in your browser.")
def gui(
    port: int = typer.Option(
        0,
        "--port",
        metavar="PORT",
        help="Fixed port to listen on (default: a random free port).",
    ),
    no_browser: bool = typer.Option(
        False,
        "--no-browser",
        help="Print the access URL without opening a browser.",
    ),
) -> None:
    """Serve a local, token-protected studio on 127.0.0.1."""

    if not 0 <= port <= 65535:
        raise typer.BadParameter("--port must be between 0 and 65535")
    from .gui import run_gui

    run_gui(port=port, open_browser=not no_browser)


# ---------------------------------------------------------------------------
# Shared execution helpers
# ---------------------------------------------------------------------------
def _run_download(
    urls: list[str],
    *,
    command: str,
    fmt: str | None,
    quality: str | None,
    bitrate: int | None,
    output: str | None,
    directory: str | None,
    music: bool | None,
    subs: str | None,
    no_subs: bool,
    thumbnail: bool | None,
    metadata: bool | None,
    chapters: bool | None,
    sponsorblock: str | None,
    no_sponsorblock: bool,
    playlist_start: int | None,
    playlist_end: int | None,
    shuffle: bool | None,
    reverse: bool | None,
    concurrent: int | None,
    rate_limit: str | None,
    retries: int | None,
    cookies_from_browser: str | None,
    cookies: str | None,
    proxy: str | None,
    archive: Path | None,
    keep_video: bool | None,
    dry_run: bool,
    json_output: bool,
    quiet: bool,
    verbose: bool,
) -> None:
    ui = UI(quiet=quiet, verbose=verbose, json_mode=json_output)
    try:
        if quiet and verbose:
            raise InvalidOptionError("--quiet and --verbose cannot be used together.")
        config = load_config()
        resolved = _resolve_options(
            config,
            command=command,
            fmt=fmt,
            quality=quality,
            bitrate=bitrate,
            output=output,
            directory=directory,
            music=music,
            subs=subs,
            no_subs=no_subs,
            thumbnail=thumbnail,
            metadata=metadata,
            chapters=chapters,
            sponsorblock=sponsorblock,
            no_sponsorblock=no_sponsorblock,
            playlist_start=playlist_start,
            playlist_end=playlist_end,
            shuffle=shuffle,
            reverse=reverse,
            concurrent=concurrent,
            rate_limit=rate_limit,
            retries=retries,
            cookies_from_browser=cookies_from_browser,
            cookies=cookies,
            proxy=proxy,
            archive=archive,
            keep_video=keep_video,
            dry_run=dry_run,
            quiet=quiet,
            verbose=verbose,
        )
    except YtdlStudioError as exc:
        _fail(ui, exc, json_output=json_output)
        return
    except Exception as exc:
        _fail(ui, InvalidOptionError(str(exc)), json_output=json_output)
        return

    _print_banner(ui, resolved)
    started = time.monotonic()
    try:
        results = Downloader(resolved.options, ui).run(urls)
    except KeyboardInterrupt:
        ui.stop_progress()
        raise
    except YtdlStudioError as exc:
        ui.stop_progress()
        _fail(ui, exc, json_output=json_output)
        return
    except Exception as exc:
        ui.stop_progress()
        _fail(ui, InvalidOptionError(f"Unexpected failure: {exc}"), json_output=json_output, verbose=verbose)
        return
    finally:
        ui.stop_progress()

    if resolved.options.dry_run:
        return
    code = ui.render_summary(results, elapsed=time.monotonic() - started)
    if code:
        raise typer.Exit(code)


def _run_info(
    url: str,
    *,
    formats_only: bool,
    cookies_from_browser: str | None,
    cookies: str | None,
    proxy: str | None,
    json_output: bool,
    quiet: bool,
    verbose: bool,
) -> None:
    ui = UI(quiet=quiet, verbose=verbose, json_mode=json_output)
    try:
        config = load_config()
        if quiet and verbose:
            raise InvalidOptionError("--quiet and --verbose cannot be used together.")
        browser = _validate_browser(cookies_from_browser or config.cookies_from_browser)
        cookie_file = _validate_cookie_file(cookies or config.cookies)
        options = DownloadOptions(
            preset=PRESETS["best"],
            output_template=resolve_output_template(config.directory, config.output),
            download_dir=Path(config.directory).expanduser(),
            cookies_from_browser=browser,
            cookies=cookie_file,
            proxy=proxy or config.proxy,
            user_agent=config.user_agent,
            verbose=verbose,
        )
        ui.banner(
            {
                "preset": "info",
                "directory": config.directory,
                "output_template": "—",
                "concurrent": 1,
                "rate_limit": None,
                "options": "metadata inspection only",
            }
        )
        info_data = Downloader(options, ui).get_info(url)
        formats_list = info_data.get("formats") or []
        if formats_only:
            ui.render_formats(info_data, formats_list)
        else:
            ui.render_info(info_data, formats_list)
    except YtdlStudioError as exc:
        _fail(ui, exc, json_output=json_output)
        return
    except Exception as exc:
        _fail(ui, InvalidOptionError(f"Unexpected failure: {exc}"), json_output=json_output, verbose=verbose)
        return


def _resolve_options(
    config: Config,
    *,
    command: str,
    fmt: str | None,
    quality: str | None,
    bitrate: int | None,
    output: str | None,
    directory: str | None,
    music: bool | None,
    subs: str | None,
    no_subs: bool,
    thumbnail: bool | None,
    metadata: bool | None,
    chapters: bool | None,
    sponsorblock: str | None,
    no_sponsorblock: bool,
    playlist_start: int | None,
    playlist_end: int | None,
    shuffle: bool | None,
    reverse: bool | None,
    concurrent: int | None,
    rate_limit: str | None,
    retries: int | None,
    cookies_from_browser: str | None,
    cookies: str | None,
    proxy: str | None,
    archive: Path | None,
    keep_video: bool | None,
    dry_run: bool,
    quiet: bool,
    verbose: bool,
) -> ResolvedRun:
    effective_format: str
    music_mode = (config.music if music is None else music) or command == "audio"

    if command == "audio":
        effective_format = fmt or "mp3"
    elif music_mode:
        candidate = fmt or config.format
        if candidate in {"best", "bestvideo", "max", "source"}:
            effective_format = "mp3"
        else:
            effective_format = candidate
    else:
        effective_format = fmt or config.format

    effective_quality = quality if quality is not None else config.quality
    quality_height = parse_height(effective_quality) if effective_quality else None
    # The implicit "best" preset with a height cap merges to MP4; prefer the
    # most compatible stream profile in that case. Explicit height presets keep
    # their documented generic selector.
    implicit_mp4 = False
    if (
        not music_mode
        and quality_height is not None
        and effective_format in {"best", "bestvideo", "max", "source"}
    ):
        effective_format = "mp4"
        implicit_mp4 = True
    preset = resolve_preset(effective_format, effective_quality if not music_mode else None)
    if implicit_mp4 and preset.target_height:
        # Keep the familiar "1080p" name in the banner and summary while using
        # the compatibility-first MP4 selector under the hood.
        preset = replace(preset, name=f"{preset.target_height}p")
    if music_mode and preset.is_video:
        raise InvalidOptionError(
            f"Music mode needs an audio preset, but {preset.name!r} produces video. "
            "Use -f mp3, m4a, opus, ogg, flac, wav or alac."
        )
    if command == "audio" and preset.is_video:
        raise InvalidOptionError(
            f"The audio command needs an audio preset, not {preset.name!r}."
        )

    music_mode = music_mode or preset.is_audio
    bitrate_value = bitrate if bitrate is not None else config.bitrate
    if not 8 <= bitrate_value <= 512:
        raise InvalidOptionError("--bitrate must be between 8 and 512 kbps.")

    base_dir = directory or config.directory or DEFAULT_DOWNLOAD_DIR
    template = output if output is not None else config.output
    resolved_template = resolve_output_template(base_dir, template, music=music_mode)

    if subs is not None:
        subtitles = True
        subtitle_langs = subs or "en"
    else:
        subtitles = config.subtitles
        subtitle_langs = config.subtitle_langs
    if no_subs:
        subtitles = False

    if no_sponsorblock:
        sb_categories: tuple[str, ...] = ()
    elif sponsorblock is not None:
        sb_categories = _validate_sponsorblock(sponsorblock)
    else:
        sb_categories = _validate_sponsorblock(config.sponsorblock)

    start = playlist_start if playlist_start is not None else config.playlist_start
    end = playlist_end if playlist_end is not None else config.playlist_end
    if start is not None and end is not None and end < start:
        raise InvalidOptionError("--playlist-end must be greater than or equal to --playlist-start.")

    rate = parse_rate_limit(rate_limit if rate_limit is not None else config.rate_limit)
    concurrency = concurrent if concurrent is not None else config.concurrent
    if concurrency < 1:
        raise InvalidOptionError("--concurrent must be at least 1.")
    retries_value = retries if retries is not None else config.retries
    if retries_value < 0:
        raise InvalidOptionError("--retries cannot be negative.")
    browser = _validate_browser(cookies_from_browser or config.cookies_from_browser)
    cookie_file = _validate_cookie_file(cookies or config.cookies)
    archive_path = archive if archive is not None else (Path(config.archive).expanduser() if config.archive else None)
    keep = config.keep_video if keep_video is None else keep_video
    thumb_value = config.thumbnail if thumbnail is None else thumbnail
    metadata_value = config.metadata if metadata is None else metadata
    chapters_value = config.chapters if chapters is None else chapters
    shuffle_value = config.shuffle if shuffle is None else shuffle
    reverse_value = config.reverse if reverse is None else reverse

    options = DownloadOptions(
        preset=preset,
        output_template=resolved_template,
        download_dir=Path(base_dir).expanduser(),
        music=music_mode,
        subtitles=subtitles and preset.is_video,
        subtitle_langs=subtitle_langs,
        thumbnail=thumb_value,
        metadata=metadata_value,
        chapters=chapters_value,
        sponsorblock=sb_categories,
        playlist_start=start,
        playlist_end=end,
        playlist_mode="always" if command == "playlist" else "auto",
        shuffle=shuffle_value,
        reverse=reverse_value,
        concurrent=concurrency,
        rate_limit=rate,
        retries=retries_value,
        cookies_from_browser=browser,
        cookies=cookie_file,
        proxy=proxy or config.proxy,
        user_agent=config.user_agent,
        archive=archive_path,
        keep_video=keep,
        dry_run=dry_run,
        use_aria2c=config.use_aria2c,
        bitrate=bitrate_value,
        verbose=verbose,
    )
    return ResolvedRun(options=options, config=config, preset=preset)


def _validate_browser(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    cleaned = str(value).strip().lower()
    if cleaned not in BROWSERS:
        raise InvalidOptionError(
            f"Unsupported browser {value!r}. Choose one of: {', '.join(sorted(BROWSERS))}."
        )
    return cleaned


def _validate_cookie_file(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    path = Path(str(value)).expanduser()
    if not path.is_file():
        raise InvalidOptionError(f"Cookie file not found: {path}")
    return str(path)


def _validate_sponsorblock(value: str | None) -> tuple[str, ...]:
    if value is None or not str(value).strip():
        return ()
    parts = [part.strip().lower() for part in str(value).split(",") if part.strip()]
    if "all" in parts:
        return tuple(sorted(SPONSORBLOCK_CATEGORIES))
    invalid = [part for part in parts if part not in SPONSORBLOCK_CATEGORIES]
    if invalid:
        raise InvalidOptionError(
            f"Unknown SponsorBlock categor{'y' if len(invalid) == 1 else 'ies'}: "
            f"{', '.join(invalid)}. Valid: {', '.join(sorted(SPONSORBLOCK_CATEGORIES))}."
        )
    return tuple(dict.fromkeys(parts))


def _print_banner(ui: UI, resolved: ResolvedRun) -> None:
    options = resolved.options
    flags: list[str] = []
    if options.music:
        flags.append("music")
    if options.subtitles:
        flags.append(f"subs:{options.subtitle_langs}")
    if options.metadata:
        flags.append("tags")
    if options.thumbnail:
        flags.append("cover")
    if options.chapters:
        flags.append("chapters")
    if options.sponsorblock:
        flags.append("sponsorblock")
    if options.archive:
        flags.append("archive")
    if options.keep_video:
        flags.append("keep-source")
    if options.dry_run:
        flags.append("dry-run")
    if not flags:
        flags.append("defaults")
    ui.banner(
        {
            "preset": f"{resolved.preset.name} ({options.preset.extension})",
            "directory": str(options.download_dir),
            "output_template": "custom" if resolved.config.output else "default",
            "concurrent": options.concurrent,
            "rate_limit": format_bytes(options.rate_limit) + "/s" if options.rate_limit else None,
            "options": ", ".join(flags),
        }
    )


def _fail(ui: UI, exc: YtdlStudioError, *, json_output: bool, verbose: bool = False) -> None:
    if verbose and exc.__cause__ is not None:
        ui.verbose_log(f"caused by: {exc.__cause__!r}")
    if json_output:
        ui.emit_json(
            {
                "ok": False,
                "error": exc.message,
                "type": exc.__class__.__name__,
                "hint": exc.hint,
            }
        )
    else:
        ui.error(exc)
    raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Argument preprocessing and entry point
# ---------------------------------------------------------------------------
def _looks_like_url(value: str) -> bool:
    lowered = value.lower()
    return (
        lowered.startswith(("http://", "https://", "www.", "youtu.be/", "youtube.com/", "music.youtube.com/"))
        or bool(re.match(r"^(?:[\w-]+\.)+[a-z]{2,}(?:/|$)", lowered))
    )


def _normalize_optional_flags(argv: list[str]) -> list[str]:
    """Allow bare ``--subs`` and ``--sponsorblock`` despite optional values."""

    result: list[str] = []
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--subs":
            following = argv[index + 1] if index + 1 < len(argv) else None
            if following is None or following.startswith("-") or _looks_like_url(following):
                result.append("--subs=en")
            else:
                result.append(token)
        elif token == "--sponsorblock":
            following = argv[index + 1] if index + 1 < len(argv) else None
            if following is None or following.startswith("-") or _looks_like_url(following):
                result.append(f"--sponsorblock={DEFAULT_SPONSORBLOCK}")
            else:
                result.append(token)
        else:
            result.append(token)
        index += 1
    return result


def normalize_argv(argv: list[str]) -> list[str]:
    """Insert the implicit ``download`` command and normalize optional flags."""

    if not argv:
        return argv
    argv = _normalize_optional_flags(list(argv))
    if any(token in COMMANDS for token in argv):
        return argv
    if all(token in {"-h", "--help", "--version"} for token in argv):
        return argv
    return ["download", *argv]


def main() -> None:
    """Console-script entry point."""

    argv = sys.argv[1:]
    if "--version" in argv:
        typer.echo(f"ytdl-studio {__version__}")
        return
    normalized = normalize_argv(argv)
    try:
        app(args=normalized, prog_name=APP_NAME)
    except KeyboardInterrupt:
        typer.echo("\nInterrupted.", err=True)
        raise SystemExit(130) from None


if __name__ == "__main__":  # pragma: no cover
    main()
