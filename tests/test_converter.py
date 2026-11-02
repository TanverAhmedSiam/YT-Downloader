"""FFmpeg argument construction and conversion-decision tests.

These tests never run FFmpeg or touch the network; they exercise the pure
builders and decision helpers that the downloader relies on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ytdl.converter import (
    MediaInfo,
    build_audio_args,
    build_gif_args,
    build_video_args,
    choose_output_path,
    format_media_summary,
    needs_conversion,
)
from ytdl.errors import FFmpegError


def make_media(
    path: str,
    *,
    video: str | None = "h264",
    audio: str | None = "aac",
    width: int | None = 1920,
    height: int | None = 1080,
    has_cover: bool = False,
    duration: float | None = 60.0,
    size: int = 1024,
) -> MediaInfo:
    return MediaInfo(
        path=Path(path),
        duration=duration,
        size=size,
        format_name=Path(path).suffix.lstrip("."),
        video_codec=video,
        audio_codec=audio,
        width=width,
        height=height,
        fps=30.0,
        bit_rate=2_000_000,
        audio_bit_rate=128_000,
        has_cover=has_cover,
    )


# ---------------------------------------------------------------------------
# Audio arguments
# ---------------------------------------------------------------------------
def test_mp3_args_use_lame_and_id3v2() -> None:
    args = build_audio_args("in.webm", "out.mp3", "mp3", 320)
    assert args[:9] == [
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        "in.webm",
        "-map",
        "0:a:0",
    ]
    assert "-vn" in args
    assert args[args.index("-c:a") + 1] == "libmp3lame"
    assert args[args.index("-b:a") + 1] == "320k"
    assert args[args.index("-map_metadata") + 1] == "0"
    assert args[args.index("-id3v2_version") + 1] == "3"


@pytest.mark.parametrize(
    ("codec", "encoder"),
    [
        ("aac", "aac"),
        ("m4a", "aac"),
        ("opus", "libopus"),
        ("vorbis", "libvorbis"),
        ("flac", "flac"),
        ("wav", "pcm_s16le"),
        ("alac", "alac"),
    ],
)
def test_audio_encoders(codec: str, encoder: str) -> None:
    args = build_audio_args("in.m4a", "out", codec, 192)
    assert args[args.index("-c:a") + 1] == encoder
    assert "-vn" in args


def test_lossless_args_have_no_bitrate_flag() -> None:
    for codec in ("flac", "wav", "alac"):
        args = build_audio_args("in.webm", "out", codec, 320)
        assert "-b:a" not in args


def test_keep_metadata_false_omits_metadata_mapping() -> None:
    args = build_audio_args("in.webm", "out.mp3", "mp3", 192, keep_metadata=False)
    assert "-map_metadata" not in args
    assert "-id3v2_version" not in args


# ---------------------------------------------------------------------------
# Video arguments
# ---------------------------------------------------------------------------
def test_h264_mp4_args_transcode_video_and_audio() -> None:
    args = build_video_args(
        "in.webm",
        "out.mp4",
        container="mp4",
        video_codec="h264",
        audio_codec="aac",
        audio_bitrate=192,
        height=1080,
    )
    assert args[args.index("-c:v") + 1] == "libx264"
    assert "-crf" in args
    assert "yuv420p" in args
    assert args[args.index("-vf") + 1] == "scale=-2:1080"
    assert args[args.index("-c:a") + 1] == "aac"
    assert args[args.index("-b:a") + 1] == "192k"
    assert args[args.index("-c:s") + 1] == "mov_text"
    assert "+faststart" in args


def test_h265_mp4_uses_hevc_tag() -> None:
    args = build_video_args("in.mkv", "out.mp4", container="mp4", video_codec="h265")
    assert args[args.index("-c:v") + 1] == "libx265"
    assert args[args.index("-tag:v") + 1] == "hvc1"


def test_copy_remux_uses_stream_copy() -> None:
    args = build_video_args(
        "in.mkv",
        "out.mkv",
        container="mkv",
        video_codec=None,
        audio_codec="aac",
        copy_audio=True,
    )
    assert args[args.index("-c:v") + 1] == "copy"
    assert args[args.index("-c:a") + 1] == "copy"
    assert args[args.index("-c:s") + 1] == "copy"


def test_webm_uses_vp9_opus_and_webvtt() -> None:
    args = build_video_args(
        "in.mp4",
        "out.webm",
        container="webm",
        video_codec="vp9",
        audio_codec="opus",
        audio_bitrate=160,
        copy_audio=False,
    )
    assert args[args.index("-c:v") + 1] == "libvpx-vp9"
    assert args[args.index("-c:a") + 1] == "libopus"
    assert args[args.index("-b:a") + 1] == "160k"
    assert args[args.index("-c:s") + 1] == "webvtt"


def test_mov_uses_mov_text_and_faststart() -> None:
    args = build_video_args(
        "in.mkv", "out.mov", container="mov", video_codec="h264", audio_codec="aac"
    )
    assert args[args.index("-c:s") + 1] == "mov_text"
    assert "+faststart" in args


def test_unknown_video_codec_is_rejected() -> None:
    with pytest.raises(FFmpegError):
        build_video_args("in.mp4", "out.mp4", container="mp4", video_codec="mpeg2")


# ---------------------------------------------------------------------------
# GIF arguments
# ---------------------------------------------------------------------------
def test_gif_args_use_palette_pipeline() -> None:
    args = build_gif_args("in.mp4", "out.gif", height=480, fps=12)
    filters = args[args.index("-filter_complex") + 1]
    assert "fps=12" in filters
    assert "scale=-2:480" in filters
    assert "palettegen" in filters
    assert "paletteuse" in filters
    assert "-an" in args
    assert args[args.index("-loop") + 1] == "0"


def test_gif_args_can_use_width_or_default_width() -> None:
    width_args = build_gif_args("in.mp4", "out.gif", width=640)
    assert "scale=640:-2" in width_args[width_args.index("-filter_complex") + 1]
    default_args = build_gif_args("in.mp4", "out.gif")
    assert "min(1280,iw)" in default_args[default_args.index("-filter_complex") + 1]


@pytest.mark.parametrize("fps", [0, 61, -1])
def test_gif_fps_is_validated(fps: int) -> None:
    with pytest.raises(ValueError):
        build_gif_args("in.mp4", "out.gif", fps=fps)


# ---------------------------------------------------------------------------
# Conversion decisions
# ---------------------------------------------------------------------------
def test_no_conversion_when_container_and_codec_match() -> None:
    media = make_media("video.mp4", video="h264")
    assert not needs_conversion(media, container="mp4", video_codec="h264")


def test_container_change_requires_conversion() -> None:
    media = make_media("video.webm", video="vp9")
    assert needs_conversion(media, container="mp4")


def test_codec_change_requires_conversion() -> None:
    media = make_media("video.mp4", video="h264")
    assert needs_conversion(media, container="mp4", video_codec="h265")


def test_h265_matches_hevc_probe_name() -> None:
    media = make_media("video.mp4", video="hevc")
    assert not needs_conversion(media, container="mp4", video_codec="h265")


def test_av1_matches_av01_probe_name() -> None:
    media = make_media("video.webm", video="av1")
    assert not needs_conversion(media, container="webm", video_codec="av1")


def test_force_always_converts() -> None:
    media = make_media("video.mp4", video="h264")
    assert needs_conversion(media, container="mp4", video_codec="h264", force=True)


def test_gif_conversion_decision() -> None:
    assert not needs_conversion(make_media("anim.gif", video="gif"), container="gif")
    assert needs_conversion(make_media("movie.mp4", video="h264"), container="gif")


# ---------------------------------------------------------------------------
# Output paths
# ---------------------------------------------------------------------------
def test_choose_output_path_changes_the_extension(tmp_path: Path) -> None:
    source = tmp_path / "movie.mp4"
    source.write_text("x", encoding="utf-8")
    assert choose_output_path(source, "mp3") == tmp_path / "movie.mp3"


def test_choose_output_path_avoids_existing_files(tmp_path: Path) -> None:
    source = tmp_path / "movie.mp4"
    source.write_text("x", encoding="utf-8")
    (tmp_path / "movie.mp3").write_text("existing", encoding="utf-8")
    assert choose_output_path(source, "mp3") == tmp_path / "movie (1).mp3"


def test_choose_output_path_handles_same_extension(tmp_path: Path) -> None:
    source = tmp_path / "song.mp3"
    source.write_text("x", encoding="utf-8")
    assert choose_output_path(source, "mp3") == tmp_path / "song (converted).mp3"


def test_choose_output_path_respects_directory(tmp_path: Path) -> None:
    source = tmp_path / "movie.mp4"
    source.write_text("x", encoding="utf-8")
    other = tmp_path / "converted"
    other.mkdir()
    assert choose_output_path(source, "mkv", directory=other) == other / "movie.mkv"


# ---------------------------------------------------------------------------
# MediaInfo
# ---------------------------------------------------------------------------
def test_media_info_classification() -> None:
    video = make_media("movie.mp4", video="h264", audio="aac")
    assert video.is_video
    assert not video.is_audio
    assert video.resolution == "1920x1080"

    audio = make_media("song.mp3", video=None, audio="mp3", width=None, height=None)
    assert audio.is_audio
    assert not audio.is_video
    assert audio.resolution == "—"

    with_cover = make_media("song.m4a", video=None, audio="aac", has_cover=True)
    assert with_cover.is_audio
    assert not with_cover.is_video


def test_format_media_summary() -> None:
    summary = format_media_summary(make_media("movie.mp4", size=1536))
    assert "video h264" in summary
    assert "1920x1080" in summary
    assert "1.50 KiB" in summary
