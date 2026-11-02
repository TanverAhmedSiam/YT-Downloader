"""Download fallbacks for YouTube media URLs that reject the default stream."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ytdl.downloader import (
    Downloader,
    DownloadOptions,
    DownloadResult,
    MediaEntry,
    _is_youtube_media_failure,
)
from ytdl.errors import DownloadError
from ytdl.presets import resolve_preset
from ytdl.ui import UI

URL = "https://www.youtube.com/watch?v=j_dPwLT2mg0"


def make_downloader(preset_name: str = "mp3", **overrides: Any) -> Downloader:
    options = DownloadOptions(
        preset=resolve_preset(preset_name),
        output_template="%(title)s.%(ext)s",
        download_dir=Path("downloads"),
        **overrides,
    )
    return Downloader(options, UI(quiet=True, json_mode=True))


class FakeDashboard:
    def add_task(self, *args: object, **kwargs: object) -> int:
        return 1

    def finish(self, *args: object, **kwargs: object) -> None:
        pass

    def stage(self, *args: object, **kwargs: object) -> None:
        pass


def entry() -> MediaEntry:
    return MediaEntry(url=URL, title="Test song", id="j_dPwLT2mg0", extractor="youtube")


def test_aria2c_is_skipped_for_youtube_signed_urls() -> None:
    downloader = make_downloader(use_aria2c=True)
    downloader._aria2c = Path("aria2c")
    youtube_options = downloader._download_options(1, FakeDashboard(), {}, entry())
    assert "external_downloader" not in youtube_options

    other = MediaEntry(url="https://vimeo.com/123", title="Other", extractor="vimeo")
    other_options = downloader._download_options(1, FakeDashboard(), {}, other)
    assert other_options["external_downloader"]["default"] == "aria2c"


def result() -> DownloadResult:
    return DownloadResult(
        title="Test song",
        format="mp3",
        size=1,
        duration=1,
        path="out.mp3",
        status="completed",
        url=URL,
        id="j_dPwLT2mg0",
    )


def test_youtube_media_failure_detection() -> None:
    error = DownloadError("unable to download video data: HTTP Error 403: Forbidden")
    assert _is_youtube_media_failure(URL, error) is True
    assert _is_youtube_media_failure("https://vimeo.com/1", error) is False
    assert _is_youtube_media_failure(URL, DownloadError("disk full")) is False


def test_audio_fallback_options_override_format_and_client(tmp_path: Path) -> None:
    downloader = make_downloader()
    options = downloader._download_options(
        1,
        None,  # type: ignore[arg-type]
        {},
        entry(),
        format_override="bestaudio[protocol*=m3u8]",
        player_client=("android",),
        merge_format_override="mkv",
    )
    assert options["format"] == "bestaudio[protocol*=m3u8]"
    assert options["extractor_args"] == {"youtube": {"player_client": ["android"]}}
    assert options["merge_output_format"] == "mkv"


class RetryDownloader(Downloader):
    def __init__(self, *args: Any, failures: list[DownloadError], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.failures = failures
        self.calls: list[tuple[str | None, tuple[str, ...] | None, str | None]] = []

    def _download_attempt(
        self,
        entry: MediaEntry,
        task_id: int,
        dashboard: Any,
        state: dict[str, Any],
        started: float,
        *,
        format_override: str | None = None,
        player_client: tuple[str, ...] | None = None,
        merge_format_override: str | None = None,
    ) -> DownloadResult:
        self.calls.append((format_override, player_client, merge_format_override))
        if self.failures:
            state["last_progress_file"] = str(Path("partial.webm.part"))
            raise self.failures.pop(0)
        return result()


def test_retry_uses_hls_audio_after_403() -> None:
    downloader = RetryDownloader(
        make_downloader().options,
        UI(quiet=True, json_mode=True),
        failures=[DownloadError("unable to download video data: HTTP Error 403: Forbidden")],
    )
    outcome = downloader._process_entry(entry(), FakeDashboard())  # type: ignore[arg-type]
    assert outcome.status == "completed"
    assert downloader.calls == [
        (None, None, None),
        ("bestaudio[protocol*=m3u8]", None, None),
    ]


def test_retry_uses_android_when_hls_is_unavailable() -> None:
    downloader = RetryDownloader(
        make_downloader().options,
        UI(quiet=True, json_mode=True),
        failures=[
            DownloadError("unable to download video data: HTTP Error 403: Forbidden"),
            DownloadError("Requested format is not available"),
        ],
    )
    outcome = downloader._process_entry(entry(), FakeDashboard())  # type: ignore[arg-type]
    assert outcome.status == "completed"
    assert downloader.calls == [
        (None, None, None),
        ("bestaudio[protocol*=m3u8]", None, None),
        ("bestaudio/best", ("android",), None),
    ]


def test_retry_uses_hls_video_with_height_cap() -> None:
    downloader = make_downloader("360p")
    retrying = RetryDownloader(
        downloader.options,
        UI(quiet=True, json_mode=True),
        failures=[DownloadError("unable to download video data: HTTP Error 403: Forbidden")],
    )
    outcome = retrying._process_entry(entry(), FakeDashboard())  # type: ignore[arg-type]
    assert outcome.status == "completed"
    hls_selector = resolve_preset("360p").hls_selector()
    assert "height<=360" in hls_selector
    assert "vcodec^=avc1" in hls_selector
    assert retrying.calls == [
        (None, None, None),
        (hls_selector, None, "mp4"),
    ]


def test_retry_uses_mkv_for_hls_webm_fallback() -> None:
    downloader = make_downloader("webm")
    retrying = RetryDownloader(
        downloader.options,
        UI(quiet=True, json_mode=True),
        failures=[DownloadError("unable to download video data: HTTP Error 403: Forbidden")],
    )
    outcome = retrying._process_entry(entry(), FakeDashboard())  # type: ignore[arg-type]
    assert outcome.status == "completed"
    assert retrying.calls[1] == (
        resolve_preset("webm").hls_selector(),
        None,
        "mkv",
    )
