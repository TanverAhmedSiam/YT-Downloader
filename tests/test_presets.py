"""Preset resolution, selectors, and audio encoder argument tests."""

from __future__ import annotations

import pytest

from ytdl.errors import InvalidPresetError
from ytdl.presets import (
    AUDIO_PRESETS,
    PRESETS,
    audio_bitrate_args,
    normalize_preset_name,
    parse_height,
    resolve_preset,
)

REQUIRED_VIDEO_PRESETS = {
    "best",
    "8k",
    "4k",
    "1440p",
    "1080p",
    "720p",
    "480p",
    "mp4",
    "mp4-h264",
    "mp4-h265",
    "mkv",
    "webm",
    "webm-vp9",
    "webm-av1",
    "mov",
    "gif",
}

REQUIRED_AUDIO_PRESETS = {"mp3", "m4a", "opus", "ogg", "flac", "wav", "alac"}


def test_required_presets_exist() -> None:
    assert set(PRESETS) >= REQUIRED_VIDEO_PRESETS
    assert set(PRESETS) >= REQUIRED_AUDIO_PRESETS
    assert REQUIRED_AUDIO_PRESETS <= AUDIO_PRESETS


def test_audio_and_video_presets_are_classified() -> None:
    for name in REQUIRED_AUDIO_PRESETS:
        assert PRESETS[name].is_audio
        assert not PRESETS[name].is_video
        assert PRESETS[name].selector == "bestaudio/best"
    for name in REQUIRED_VIDEO_PRESETS:
        assert PRESETS[name].is_video
        assert not PRESETS[name].is_audio


def test_video_containers_are_consistent() -> None:
    for name in ("best", "mp4", "mp4-h264", "mp4-h265"):
        preset = PRESETS[name]
        assert preset.extension == "mp4"
        assert preset.container == "mp4"
        assert preset.merge_output_format == "mp4"
    assert PRESETS["mkv"].merge_output_format == "mkv"
    assert PRESETS["webm"].merge_output_format == "webm"
    assert PRESETS["mov"].merge_output_format == "mov"
    assert PRESETS["gif"].extension == "gif"


def test_best_selector_merges_best_streams() -> None:
    assert PRESETS["best"].selector == "bv*+ba/b"


def test_mp4_selector_prefers_h264_and_aac() -> None:
    selector = PRESETS["mp4"].selector
    assert "vcodec^=avc1" in selector
    assert "acodec^=mp4a" in selector
    assert selector.endswith("bv*+ba/b")


def test_height_presets_cap_the_height() -> None:
    for name, height in (("8k", 4320), ("4k", 2160), ("1440p", 1440), ("1080p", 1080), ("720p", 720), ("480p", 480)):
        preset = PRESETS[name]
        assert preset.target_height == height
        assert f"height<={height}" in preset.selector


def test_low_height_presets_prefer_h264_aac() -> None:
    for name in ("1080p", "720p", "480p", "360p"):
        selector = PRESETS[name].selector
        assert "vcodec^=avc1" in selector
        assert "acodec^=mp4a" in selector


def test_high_height_presets_do_not_force_h264() -> None:
    # YouTube rarely publishes H.264 above 1080p; forcing it would silently
    # downgrade a 4K/8K request to 1080p.
    for name in ("1440p", "4k", "8k"):
        selector = PRESETS[name].selector
        assert "vcodec^=avc1" not in selector
        assert "acodec^=mp4a" in selector


def test_hls_video_selector_keeps_height_and_prefers_compatible_codecs() -> None:
    selector = PRESETS["1080p"].hls_selector()
    assert "height<=1080" in selector
    assert "protocol*=m3u8" in selector
    assert "vcodec^=avc1" in selector
    assert selector.endswith("b[height<=1080][protocol*=m3u8]")


def test_hls_video_selector_can_drop_codec_preferences() -> None:
    selector = PRESETS["1080p"].hls_selector(prefer_codec=False)
    assert "height<=1080" in selector
    assert "vcodec^=avc1" not in selector


def test_hls_audio_selector_uses_hls_only() -> None:
    assert PRESETS["mp3"].hls_selector() == "bestaudio[protocol*=m3u8]"


def test_webm_hls_selector_prefers_open_codecs() -> None:
    selector = PRESETS["webm"].hls_selector()
    assert "vcodec^=vp9" in selector
    assert "vcodec^=av01" in selector


def test_audio_preset_extensions_and_codecs() -> None:
    expected = {
        "mp3": ("mp3", "mp3"),
        "m4a": ("m4a", "m4a"),
        "aac": ("m4a", "aac"),
        "opus": ("opus", "opus"),
        "ogg": ("ogg", "vorbis"),
        "flac": ("flac", "flac"),
        "wav": ("wav", "wav"),
        "alac": ("m4a", "alac"),
    }
    for name, (extension, codec) in expected.items():
        preset = PRESETS[name]
        assert preset.extension == extension
        assert preset.audio_codec == codec


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, None),
        ("", None),
        ("best", None),
        ("max", None),
        ("0", None),
        (1080, 1080),
        ("1080", 1080),
        ("1080p", 1080),
        ("4K", 2160),
        ("8k", 4320),
        ("720P", 720),
        (480, 480),
    ],
)
def test_parse_height(value: object, expected: int | None) -> None:
    assert parse_height(value) == expected  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["potato", "-1", "abc1080", 0.5])
def test_parse_height_rejects_garbage(value: object) -> None:
    with pytest.raises(InvalidPresetError):
        parse_height(value)  # type: ignore[arg-type]


def test_parse_height_rejects_absurd_heights() -> None:
    with pytest.raises(InvalidPresetError):
        parse_height(9000)


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("1080", "1080p"),
        ("fullhd", "1080p"),
        ("720", "720p"),
        ("2160p", "4k"),
        ("h264", "mp4-h264"),
        ("hevc", "mp4-h265"),
        ("vp9", "webm-vp9"),
        ("av1", "webm-av1"),
        ("matroska", "mkv"),
        ("quicktime", "mov"),
        ("audio", "mp3"),
    ],
)
def test_aliases_normalize(alias: str, canonical: str) -> None:
    assert normalize_preset_name(alias) == canonical


def test_resolve_preset_alias_and_quality() -> None:
    preset = resolve_preset("1080")
    assert preset.name == "1080p"
    assert preset.target_height == 1080
    assert "height<=1080" in preset.selector


def test_quality_override_applies_to_best() -> None:
    preset = resolve_preset("best", "720p")
    assert preset.target_height == 720
    assert "height<=720" in preset.selector
    # MP4 height caps stay compatibility-first.
    assert "vcodec^=avc1" in preset.selector
    assert "acodec^=mp4a" in preset.selector


def test_quality_override_on_mp4_keeps_compatibility_preferences() -> None:
    preset = resolve_preset("mp4", 1080)
    assert preset.target_height == 1080
    assert "height<=1080" in preset.selector
    assert "vcodec^=avc1" in preset.selector
    assert "acodec^=mp4a" in preset.selector


@pytest.mark.parametrize(
    ("name", "container", "height"),
    [
        ("mp4-1080p", "mp4", 1080),
        ("1080p-mp4", "mp4", 1080),
        ("mp4-h264-1440p", "mp4-h264", 1440),
        ("1440p-mp4-h264", "mp4-h264", 1440),
        ("4k-mkv", "mkv", 2160),
        ("webm-720p", "webm", 720),
    ],
)
def test_combined_preset_names(name: str, container: str, height: int) -> None:
    preset = resolve_preset(name)
    assert preset.name == container
    assert preset.target_height == height
    assert f"height<={height}" in preset.selector


def test_explicit_quality_wins_over_combined_name() -> None:
    assert resolve_preset("mp4-1080p", "480p").target_height == 480


def test_music_defaults_to_mp3() -> None:
    assert resolve_preset(None, music=True).name == "mp3"
    assert resolve_preset(None, music=False).name == "best"


def test_audio_preset_ignores_quality() -> None:
    preset = resolve_preset("flac", 1080)
    assert preset.is_audio
    assert preset.target_height is None


def test_unknown_preset_is_actionable() -> None:
    with pytest.raises(InvalidPresetError) as excinfo:
        resolve_preset("not-a-preset")
    assert "Unknown format preset" in str(excinfo.value)
    assert "mp3" in str(excinfo.value)


def test_with_quality_returns_a_copy() -> None:
    base = PRESETS["mp4"]
    capped = base.with_quality(1080)
    assert capped is not base
    assert base.target_height is None
    assert "height<=1080" in capped.selector


@pytest.mark.parametrize(
    ("codec", "expected"),
    [
        ("mp3", ["-c:a", "libmp3lame", "-b:a", "320k"]),
        ("aac", ["-c:a", "aac", "-b:a", "320k"]),
        ("m4a", ["-c:a", "aac", "-b:a", "320k"]),
        ("opus", ["-c:a", "libopus", "-b:a", "320k", "-vbr", "on"]),
        ("vorbis", ["-c:a", "libvorbis", "-b:a", "320k"]),
        ("flac", ["-c:a", "flac", "-compression_level", "8"]),
        ("wav", ["-c:a", "pcm_s16le"]),
        ("alac", ["-c:a", "alac"]),
    ],
)
def test_audio_bitrate_args(codec: str, expected: list[str]) -> None:
    assert audio_bitrate_args(codec, 320) == expected


def test_lossless_args_ignore_bitrate() -> None:
    assert audio_bitrate_args("flac", 64) == audio_bitrate_args("flac", 512)


def test_audio_bitrate_args_rejects_bad_input() -> None:
    with pytest.raises(ValueError):
        audio_bitrate_args("mp3", 0)
    with pytest.raises(InvalidPresetError):
        audio_bitrate_args("bogus", 320)
