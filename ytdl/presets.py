"""Named format presets and the yt-dlp selectors behind them.

A preset is deliberately more than a string: the downloader needs to know
whether it is handling video or audio, which container to merge into, whether
a final FFmpeg conversion is required, and which audio codec/bitrate to use.

The public API is intentionally small:

``resolve_preset(name, quality=None)``
    Return an immutable :class:`Preset` for a CLI name.

``PRESETS``
    The base presets, useful for help text and tests.

``parse_height(value)``
    Turn ``1080``, ``1080p``, ``4k`` or ``best`` into a height or ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Final

from .errors import InvalidPresetError

MAX_HEIGHT: Final[int] = 8640  # 16K, beyond YouTube's current maximum of 8K.
DEFAULT_AUDIO_BITRATE: Final[int] = 320


class PresetKind(StrEnum):
    """Whether a preset produces a video or an audio file."""

    VIDEO = "video"
    AUDIO = "audio"


@dataclass(frozen=True, slots=True)
class Preset:
    """A fully resolved output preset."""

    name: str
    kind: PresetKind
    selector: str
    extension: str
    container: str | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    merge_output_format: str | None = None
    target_height: int | None = None
    lossless: bool = False
    force_transcode: bool = False
    postprocess: str | None = None
    description: str = ""

    @property
    def is_audio(self) -> bool:
        return self.kind is PresetKind.AUDIO

    @property
    def is_video(self) -> bool:
        return self.kind is PresetKind.VIDEO

    @property
    def audio_only(self) -> bool:
        return self.is_audio

    def with_quality(self, height: int | None) -> Preset:
        """Return a copy of this video preset capped at *height*."""

        if self.is_audio or height is None:
            return self
        codecs = _codec_group(self.video_codec)
        preferred_audio = self.audio_codec
        if not codecs and self.container == "mp4":
            # Height-capped MP4 presets should remain playable everywhere.
            # Above 1080p H.264 is rarely published, so prefer the best
            # available video codec but keep AAC audio when possible.
            if height <= 1080:
                codecs = ("avc1",)
            preferred_audio = preferred_audio or "aac"
        selector = _video_selector(
            height,
            codecs=codecs,
            preferred_audio=preferred_audio,
        )
        return replace(self, selector=selector, target_height=height)

    def hls_selector(self, *, prefer_codec: bool = True) -> str:
        """Selector for YouTube's HLS streams, used when DASH URLs return 403.

        HLS keeps the requested resolution and, unlike a progressive fallback,
        does not force a 360p download.  The returned selector still prefers
        the preset's codec/audio choices, but falls back to any HLS streams
        when those codecs are not published over HLS.
        """

        if self.is_audio:
            return "bestaudio[protocol*=m3u8]"
        codecs = _codec_group(self.video_codec) if prefer_codec else ()
        if prefer_codec and not codecs:
            if self.container == "webm":
                codecs = ("vp9", "av01")
            elif self.container in {"mp4", "mov"} and (
                self.target_height is not None and self.target_height <= 1080
            ):
                codecs = ("avc1",)
        return _hls_video_selector(self.target_height, codecs=codecs)


def _selector_with_height(base: str, height: int | None) -> str:
    if height is None:
        return base
    return f"{base}[height<={height}]"


def _video_selector(
    height: int | None,
    *,
    codecs: tuple[str, ...] = (),
    preferred_audio: str | None = None,
) -> str:
    """Build a best-video + best-audio selector with a hard height cap.

    The first alternative prefers the requested codec for maximum player
    compatibility.  The fallback still honours the height cap, so a missing
    codec never silently upgrades to an unwanted resolution.
    """

    cap = f"[height<={height}]" if height else ""
    audio_filter = ""
    if preferred_audio == "aac":
        audio_filter = "[acodec^=mp4a]"
    elif preferred_audio == "opus":
        audio_filter = "[acodec^=opus]"

    alternatives: list[str] = []
    for codec in codecs:
        alternatives.append(f"bv*{cap}[vcodec^={codec}]+ba{audio_filter}")
    if audio_filter:
        alternatives.append(f"bv*{cap}+ba{audio_filter}")
    alternatives.append(f"bv*{cap}+ba")
    # A progressive format can rescue very old videos that have no DASH audio.
    alternatives.append(f"b{cap}")
    return "/".join(alternatives)


def _hls_video_selector(
    height: int | None,
    *,
    codecs: tuple[str, ...] = (),
) -> str:
    """Build a best-video + best-audio selector restricted to HLS."""

    cap = f"[height<={height}]" if height else ""
    protocol = "[protocol*=m3u8]"

    alternatives: list[str] = []
    for codec in codecs:
        alternatives.append(f"bv*{cap}[vcodec^={codec}]{protocol}+ba{protocol}")
    alternatives.append(f"bv*{cap}{protocol}+ba{protocol}")
    # Some videos only publish a muxed HLS stream.
    alternatives.append(f"b{cap}{protocol}")
    return "/".join(alternatives)


def _height_selector(height: int) -> str:
    """Selector for a height preset.

    H.264 is preferred up to 1080p for broad compatibility.  Above that,
    YouTube rarely publishes H.264, so the best available codec wins while
    AAC audio is still preferred when present.
    """

    codecs = ("avc1",) if height <= 1080 else ()
    return _video_selector(height, codecs=codecs, preferred_audio="aac")


def _codec_group(codec: str | None) -> tuple[str, ...]:
    return {
        "h264": ("avc1",),
        "h265": ("hev1", "hvc1"),
        "vp9": ("vp9",),
        "av1": ("av01",),
    }.get(codec or "", ())


_HEIGHT_NAMES: Final[dict[str, int]] = {
    "8k": 4320,
    "4320p": 4320,
    "4k": 2160,
    "2160p": 2160,
    "1440p": 1440,
    "1080p": 1080,
    "fullhd": 1080,
    "full-hd": 1080,
    "720p": 720,
    "hd": 720,
    "480p": 480,
    "360p": 360,
    "240p": 240,
    "144p": 144,
}


def parse_height(value: int | str | None) -> int | None:
    """Parse a CLI quality value.

    Raises :class:`InvalidPresetError` for anything that is not a sensible
    vertical resolution.  ``None``, ``"best"``, ``"max"`` and ``"0"`` mean no
    cap.
    """

    if value is None:
        return None
    if isinstance(value, int):
        if value <= 0:
            return None
        if value > MAX_HEIGHT:
            raise InvalidPresetError(f"Quality {value} is too large (maximum {MAX_HEIGHT}).")
        return value
    if not isinstance(value, str):
        raise InvalidPresetError(
            f"Invalid quality {value!r}. Use a height such as 2160, 1440, 1080, 720, 480 or 360."
        )
    cleaned = value.strip().lower().removesuffix("p")
    if cleaned in {"", "best", "max", "source", "highest", "0"}:
        return None
    if cleaned in _HEIGHT_NAMES:
        return _HEIGHT_NAMES[cleaned]
    if cleaned.isdigit():
        height = int(cleaned)
        if 0 < height <= MAX_HEIGHT:
            return height
    raise InvalidPresetError(
        f"Invalid quality {value!r}. Use a height such as 2160, 1440, 1080, 720, 480 or 360."
    )


def _audio_preset(
    name: str,
    *,
    extension: str,
    codec: str,
    lossless: bool = False,
    aliases: tuple[str, ...] = (),
    description: str = "",
) -> Preset:
    del aliases  # aliases are stored separately in PRESET_ALIASES
    return Preset(
        name=name,
        kind=PresetKind.AUDIO,
        selector="bestaudio/best",
        extension=extension,
        container=extension,
        audio_codec=codec,
        lossless=lossless,
        description=description,
    )


# ---------------------------------------------------------------------------
# Base presets
# ---------------------------------------------------------------------------

PRESETS: Final[dict[str, Preset]] = {
    "best": Preset(
        name="best",
        kind=PresetKind.VIDEO,
        selector="bv*+ba/b",
        extension="mp4",
        container="mp4",
        merge_output_format="mp4",
        description="Best video + best audio, no resolution cap",
    ),
    "8k": Preset(
        name="8k",
        kind=PresetKind.VIDEO,
        selector=_height_selector(4320),
        extension="mp4",
        container="mp4",
        merge_output_format="mp4",
        target_height=4320,
        description="Up to 4320p (8K)",
    ),
    "4k": Preset(
        name="4k",
        kind=PresetKind.VIDEO,
        selector=_height_selector(2160),
        extension="mp4",
        container="mp4",
        merge_output_format="mp4",
        target_height=2160,
        description="Up to 2160p (4K)",
    ),
    "1440p": Preset(
        name="1440p",
        kind=PresetKind.VIDEO,
        selector=_height_selector(1440),
        extension="mp4",
        container="mp4",
        merge_output_format="mp4",
        target_height=1440,
        description="Up to 1440p",
    ),
    "1080p": Preset(
        name="1080p",
        kind=PresetKind.VIDEO,
        selector=_height_selector(1080),
        extension="mp4",
        container="mp4",
        merge_output_format="mp4",
        target_height=1080,
        description="Up to 1080p",
    ),
    "720p": Preset(
        name="720p",
        kind=PresetKind.VIDEO,
        selector=_height_selector(720),
        extension="mp4",
        container="mp4",
        merge_output_format="mp4",
        target_height=720,
        description="Up to 720p",
    ),
    "480p": Preset(
        name="480p",
        kind=PresetKind.VIDEO,
        selector=_height_selector(480),
        extension="mp4",
        container="mp4",
        merge_output_format="mp4",
        target_height=480,
        description="Up to 480p",
    ),
    "360p": Preset(
        name="360p",
        kind=PresetKind.VIDEO,
        selector=_height_selector(360),
        extension="mp4",
        container="mp4",
        merge_output_format="mp4",
        target_height=360,
        description="Up to 360p",
    ),
    "mp4": Preset(
        name="mp4",
        kind=PresetKind.VIDEO,
        selector=_video_selector(None, codecs=("avc1",), preferred_audio="aac"),
        extension="mp4",
        container="mp4",
        video_codec="h264",
        audio_codec="aac",
        merge_output_format="mp4",
        description="MP4, preferring H.264 + AAC",
    ),
    "mp4-h264": Preset(
        name="mp4-h264",
        kind=PresetKind.VIDEO,
        selector=_video_selector(None, codecs=("avc1",), preferred_audio="aac"),
        extension="mp4",
        container="mp4",
        video_codec="h264",
        audio_codec="aac",
        merge_output_format="mp4",
        description="MP4 with H.264 video and AAC audio",
    ),
    "mp4-h265": Preset(
        name="mp4-h265",
        kind=PresetKind.VIDEO,
        selector=_video_selector(None, codecs=("hev1", "hvc1"), preferred_audio="aac"),
        extension="mp4",
        container="mp4",
        video_codec="h265",
        audio_codec="aac",
        merge_output_format="mp4",
        description="MP4 with H.265/HEVC video",
    ),
    "webm": Preset(
        name="webm",
        kind=PresetKind.VIDEO,
        selector=_video_selector(None, codecs=("vp9", "av01"), preferred_audio="opus"),
        extension="webm",
        container="webm",
        merge_output_format="webm",
        description="WebM, preferring VP9/AV1 + Opus",
    ),
    "webm-vp9": Preset(
        name="webm-vp9",
        kind=PresetKind.VIDEO,
        selector=_video_selector(None, codecs=("vp9",), preferred_audio="opus"),
        extension="webm",
        container="webm",
        video_codec="vp9",
        audio_codec="opus",
        merge_output_format="webm",
        description="WebM with VP9 video and Opus audio",
    ),
    "webm-av1": Preset(
        name="webm-av1",
        kind=PresetKind.VIDEO,
        selector=_video_selector(None, codecs=("av01",), preferred_audio="opus"),
        extension="webm",
        container="webm",
        video_codec="av1",
        audio_codec="opus",
        merge_output_format="webm",
        description="WebM with AV1 video and Opus audio",
    ),
    "mkv": Preset(
        name="mkv",
        kind=PresetKind.VIDEO,
        selector="bv*+ba/b",
        extension="mkv",
        container="mkv",
        merge_output_format="mkv",
        description="Matroska with the original streams",
    ),
    "mov": Preset(
        name="mov",
        kind=PresetKind.VIDEO,
        selector=_video_selector(None, codecs=("avc1",), preferred_audio="aac"),
        extension="mov",
        container="mov",
        video_codec="h264",
        audio_codec="aac",
        merge_output_format="mov",
        description="QuickTime MOV, preferring H.264 + AAC",
    ),
    "gif": Preset(
        name="gif",
        kind=PresetKind.VIDEO,
        selector="bv*+ba/b",
        extension="gif",
        container="gif",
        merge_output_format="mp4",
        force_transcode=True,
        postprocess="gif",
        description="Animated GIF (15 fps, palette optimised)",
    ),
    "mp3": _audio_preset("mp3", extension="mp3", codec="mp3", description="MP3 (LAME)"),
    "m4a": _audio_preset("m4a", extension="m4a", codec="m4a", description="M4A/AAC audio"),
    "aac": _audio_preset("aac", extension="m4a", codec="aac", description="Raw AAC in M4A"),
    "opus": _audio_preset("opus", extension="opus", codec="opus", description="Opus audio"),
    "ogg": _audio_preset("ogg", extension="ogg", codec="vorbis", description="Ogg Vorbis"),
    "vorbis": _audio_preset("vorbis", extension="ogg", codec="vorbis", description="Ogg Vorbis"),
    "flac": _audio_preset("flac", extension="flac", codec="flac", lossless=True, description="FLAC lossless"),
    "wav": _audio_preset("wav", extension="wav", codec="wav", lossless=True, description="WAV PCM"),
    "alac": _audio_preset("alac", extension="m4a", codec="alac", lossless=True, description="ALAC lossless"),
}

AUDIO_PRESETS: Final[frozenset[str]] = frozenset(
    name for name, preset in PRESETS.items() if preset.is_audio
)
VIDEO_HEIGHT_PRESETS: Final[frozenset[str]] = frozenset(_HEIGHT_NAMES)

PRESET_ALIASES: Final[dict[str, str]] = {
    "bestvideo": "best",
    "max": "best",
    "source": "best",
    "2160": "4k",
    "2160p": "4k",
    "1440": "1440p",
    "1080": "1080p",
    "fullhd": "1080p",
    "full-hd": "1080p",
    "720": "720p",
    "hd": "720p",
    "480": "480p",
    "360": "360p",
    "8k": "8k",
    "4320p": "8k",
    "h264": "mp4-h264",
    "avc": "mp4-h264",
    "mp4-h264": "mp4-h264",
    "h265": "mp4-h265",
    "hevc": "mp4-h265",
    "mp4-h265": "mp4-h265",
    "vp9": "webm-vp9",
    "webm-vp9": "webm-vp9",
    "av1": "webm-av1",
    "webm-av1": "webm-av1",
    "matroska": "mkv",
    "quicktime": "mov",
    "vorbis": "ogg",
    "aac": "m4a",
    "m4a": "m4a",
    "audio": "mp3",
}

def _parse_combined(name: str) -> tuple[str, int] | None:
    """Split names such as ``mp4-1080p``, ``1080p-mp4`` or ``mp4-h264-1440p``.

    Returns ``(preset_name, height)`` or ``None``.  Both orders are accepted so
    users do not have to remember which side the container goes on.
    """

    parts = [part for part in name.replace("_", "-").lower().split("-") if part]
    if len(parts) < 2:
        return None
    for index in range(1, len(parts)):
        left = "-".join(parts[:index])
        right = "-".join(parts[index:])
        for preset_part, height_part in ((left, right), (right, left)):
            preset_name = PRESET_ALIASES.get(preset_part, preset_part)
            preset = PRESETS.get(preset_name)
            if preset is None or not preset.is_video:
                continue
            try:
                height = parse_height(height_part)
            except InvalidPresetError:
                continue
            if height is not None:
                return preset_name, height
    return None


def normalize_preset_name(name: str) -> str:
    """Normalize aliases and common spellings."""

    cleaned = name.strip().lower().replace("_", "-")
    if cleaned in PRESETS:
        return cleaned
    if cleaned in PRESET_ALIASES:
        return PRESET_ALIASES[cleaned]
    combined = _parse_combined(cleaned)
    if combined is not None:
        # The height is applied by resolve_preset(); this function only
        # identifies the container/codec policy.
        return combined[0]
    raise InvalidPresetError(
        f"Unknown format preset {name!r}. "
        "Try best, 4k, 1440p, 1080p, 720p, 480p, mp4, webm, mkv, mov, gif, "
        "mp3, m4a, aac, opus, ogg, flac, wav or alac."
    )


def resolve_preset(
    name: str | None = None,
    quality: int | str | None = None,
    *,
    music: bool = False,
) -> Preset:
    """Resolve a CLI preset name and optional height override.

    ``music=True`` changes the implicit preset to MP3, but never overrides an
    explicit video preset.  That lets ``--music -f flac`` do the obvious thing
    while ``--music`` alone produces tagged MP3 files.

    A height can be embedded in the name (``mp4-1080p``) or supplied through
    *quality*; an explicit *quality* wins.
    """

    combined = _parse_combined(name) if name else None
    if combined is not None:
        base_name, combined_height = combined
        normalized = base_name
        height = parse_height(quality) if quality not in (None, "") else combined_height
    else:
        normalized = normalize_preset_name(name or ("mp3" if music else "best"))
        height = parse_height(quality)
    base = PRESETS[normalized]
    if base.is_audio:
        return base
    if height is not None:
        base = base.with_quality(height)
        if base.postprocess == "gif":
            base = replace(base, selector=f"bv*[height<={height}]+ba/b[height<={height}]")
    if base.name in {"4k", "8k"} and base.target_height is None:
        base = base.with_quality(2160 if base.name == "4k" else 4320)
    return base


def preset_names(include_aliases: bool = False) -> list[str]:
    """Return sorted user-facing preset names."""

    names = sorted(set(PRESETS) | (set(PRESET_ALIASES) if include_aliases else set()))
    return names


def is_audio_preset(name: str) -> bool:
    """Return whether *name* resolves to an audio-only preset."""

    try:
        return resolve_preset(name).is_audio
    except InvalidPresetError:
        return False


def audio_bitrate_args(codec: str, bitrate: int) -> list[str]:
    """Return FFmpeg encoder arguments for an audio codec.

    Lossless formats deliberately ignore the bitrate.  This helper is shared
    by the converter and by tests.
    """

    if bitrate <= 0:
        raise ValueError("Audio bitrate must be greater than zero")
    if codec in {"mp3", "aac", "m4a"}:
        return ["-c:a", {"aac": "aac", "m4a": "aac"}.get(codec, "libmp3lame"), "-b:a", f"{bitrate}k"]
    if codec == "opus":
        return ["-c:a", "libopus", "-b:a", f"{bitrate}k", "-vbr", "on"]
    if codec == "vorbis":
        # Vorbis quality levels top out at q10; a bitrate target is more

        # predictable for users and matches the CLI's --bitrate contract.
        return ["-c:a", "libvorbis", "-b:a", f"{bitrate}k"]
    if codec == "flac":
        return ["-c:a", "flac", "-compression_level", "8"]
    if codec == "wav":
        return ["-c:a", "pcm_s16le"]
    if codec == "alac":
        return ["-c:a", "alac"]
    raise InvalidPresetError(f"Unsupported audio codec: {codec}")
