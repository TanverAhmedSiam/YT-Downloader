"""Safe FFmpeg/ffprobe wrappers used for conversion and verification.

Every command is passed as an argv list with ``shell=False``.  Conversions are
written to a temporary file in the destination directory and atomically
replaced only after FFmpeg exits successfully.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from .errors import FFmpegError, MissingFFmpegError, ProbeError
from .paths import format_bytes, format_duration, unique_path
from .presets import audio_bitrate_args

_AUDIO_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".mp3", ".m4a", ".aac", ".opus", ".ogg", ".oga", ".flac", ".wav", ".alac", ".aiff"}
)
_VIDEO_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".mp4", ".mkv", ".webm", ".mov", ".gif", ".avi", ".flv", ".ts", ".m4v"}
)


@dataclass(slots=True)
class MediaInfo:
    """A compact subset of ffprobe output."""

    path: Path
    duration: float | None
    size: int
    format_name: str
    video_codec: str | None = None
    audio_codec: str | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    bit_rate: int | None = None
    audio_bit_rate: int | None = None
    subtitle_count: int = 0
    has_cover: bool = False

    @property
    def is_audio(self) -> bool:
        return self.audio_codec is not None and (self.video_codec is None or self.has_cover)

    @property
    def is_video(self) -> bool:
        return self.video_codec is not None and not self.has_cover

    @property
    def resolution(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return "—"

    def summary(self) -> str:
        return (
            f"{self.path.name} · {self.resolution} · "
            f"{format_duration(self.duration)} · {format_bytes(self.size)}"
        )


@dataclass(slots=True)
class ConversionResult:
    """Result of one FFmpeg operation."""

    source: Path
    output: Path
    size: int
    duration: float | None
    media: MediaInfo

    @property
    def human_size(self) -> str:
        return format_bytes(self.size)


@dataclass(slots=True)
class ToolPair:
    ffmpeg: Path
    ffprobe: Path

    @property
    def directory(self) -> Path:
        return self.ffmpeg.parent


def _windows_candidates(name: str) -> Iterable[Path]:
    executable = f"{name}.exe"
    local = os.environ.get("LOCALAPPDATA")
    if local:
        yield Path(local) / "Microsoft" / "WinGet" / "Links" / executable
        packages = Path(local) / "Microsoft" / "WinGet" / "Packages"
        if packages.is_dir():
            with contextlib.suppress(OSError):
                yield from packages.glob(f"*FFmpeg*/**/bin/{executable}")
        yield Path(local) / "Programs" / "ffmpeg" / "bin" / executable
    for root in (
        Path("C:/ffmpeg/bin"),
        Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "ffmpeg" / "bin",
        Path(os.environ.get("PROGRAMFILES(X86)", "C:/Program Files (x86)")) / "ffmpeg" / "bin",
    ):
        yield root / executable


def find_executable(name: str, extra_paths: Iterable[str | Path] = ()) -> Path | None:
    """Locate *name* on PATH or in common package-manager locations.

    Environment overrides: ``FFMPEG_BINARY`` and ``FFPROBE_BINARY``.
    """

    env_name = f"{name.upper()}_BINARY"
    override = os.environ.get(env_name)
    if override:
        candidate = Path(override).expanduser()
        if candidate.is_file():
            return candidate
    found = shutil.which(name)
    if found:
        return Path(found)
    for extra in extra_paths:
        candidate = Path(extra).expanduser()
        if candidate.is_dir():
            candidate = candidate / (f"{name}.exe" if os.name == "nt" else name)
        if candidate.is_file():
            return candidate
    if os.name == "nt":
        for candidate in _windows_candidates(name):
            if candidate.is_file():
                return candidate
    else:
        for path in (
            Path("/opt/homebrew/bin") / name,
            Path("/usr/local/bin") / name,
            Path("/usr/bin") / name,
            Path("/snap/bin") / name,
        ):
            if path.is_file():
                return path
    return None


def find_tools() -> ToolPair | None:
    """Return both FFmpeg binaries, or ``None`` if either is missing."""

    ffmpeg = find_executable("ffmpeg")
    ffprobe = find_executable("ffprobe")
    if ffmpeg is None or ffprobe is None:
        return None
    return ToolPair(ffmpeg=ffmpeg, ffprobe=ffprobe)


def require_tools() -> ToolPair:
    """Return FFmpeg tools or raise a friendly missing-dependency error."""

    tools = find_tools()
    if tools is None:
        raise MissingFFmpegError(
            "FFmpeg and ffprobe are required for merging, conversion, and tagging.",
            hint=(
                "Install FFmpeg and ensure both `ffmpeg` and `ffprobe` are on PATH. "
                "Run `ytdl doctor` for instructions for your platform."
            ),
        )
    return tools


def ffmpeg_location() -> str | None:
    """Return the directory to pass to yt-dlp's ``ffmpeg_location``."""

    tools = find_tools()
    return str(tools.directory) if tools else None


def ffmpeg_version() -> str | None:
    """Return a short FFmpeg version string if installed."""

    tools = find_tools()
    if tools is None:
        return None
    try:
        proc = subprocess.run(
            [str(tools.ffmpeg), "-version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    first = (proc.stdout or "").splitlines()[0] if proc.stdout else ""
    if first.lower().startswith("ffmpeg version "):
        return first.split()[2]
    return first or None


def _run(argv: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise MissingFFmpegError(
            f"Executable not found: {argv[0]}",
            hint="Run `ytdl doctor` to verify your FFmpeg installation.",
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(f"FFmpeg timed out after {timeout} seconds.") from exc


def _tail(stderr: str | None, lines: int = 18) -> str:
    if not stderr:
        return "FFmpeg exited without an error message."
    parts = [line.strip() for line in stderr.strip().splitlines() if line.strip()]
    return "\n".join(parts[-lines:])


def _temporary_destination(destination: Path) -> Path:
    import uuid

    return destination.with_name(f".{destination.stem}.{uuid.uuid4().hex[:8]}.tmp{destination.suffix}")


def _atomic_ffmpeg(
    base_argv: list[str],
    destination: Path,
    *,
    timeout: float | None = None,
    verify: bool = True,
) -> ConversionResult:
    tools = require_tools()
    destination = destination.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = _temporary_destination(destination)
    argv = [str(tools.ffmpeg), *base_argv, str(temp)]
    try:
        proc = _run(argv, timeout=timeout)
        if proc.returncode != 0:
            raise FFmpegError(
                f"FFmpeg failed ({proc.returncode}): {_tail(proc.stderr)}",
                hint="The source may use an unsupported codec, or the destination may be in use.",
            )
        if not temp.exists() or temp.stat().st_size == 0:
            raise FFmpegError("FFmpeg reported success but produced no output.")
        os.replace(temp, destination)
    finally:
        if temp.exists():
            with contextlib.suppress(OSError):
                temp.unlink()
    media = probe(destination) if verify else MediaInfo(
        path=destination,
        duration=None,
        size=destination.stat().st_size,
        format_name=destination.suffix.lstrip("."),
    )
    return ConversionResult(
        source=Path(base_argv[base_argv.index("-i") + 1]),
        output=destination,
        size=destination.stat().st_size,
        duration=media.duration,
        media=media,
    )


def probe(path: str | Path) -> MediaInfo:
    """Inspect a media file with ffprobe."""

    tools = require_tools()
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        raise ProbeError(f"Media file not found: {candidate}")
    argv = [
        str(tools.ffprobe),
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(candidate),
    ]
    proc = _run(argv, timeout=60)
    if proc.returncode != 0:
        raise ProbeError(f"ffprobe could not inspect {candidate.name}: {_tail(proc.stderr)}")
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise ProbeError(f"ffprobe returned invalid JSON for {candidate.name}.") from exc

    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    video = next(
        (
            stream
            for stream in streams
            if stream.get("codec_type") == "video"
            and not (stream.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    cover = next(
        (
            stream
            for stream in streams
            if stream.get("codec_type") == "video"
            and (stream.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    subtitles = [stream for stream in streams if stream.get("codec_type") == "subtitle"]
    fps = _fraction_to_float(video.get("avg_frame_rate") or video.get("r_frame_rate")) if video else None
    audio_bit_rate = _safe_int(audio.get("bit_rate")) if audio else None
    return MediaInfo(
        path=candidate,
        duration=_safe_float(fmt.get("duration")),
        size=candidate.stat().st_size,
        format_name=str(fmt.get("format_name") or candidate.suffix.lstrip(".")),
        video_codec=video.get("codec_name") if video else None,
        audio_codec=audio.get("codec_name") if audio else None,
        width=_safe_int(video.get("width")) if video else None,
        height=_safe_int(video.get("height")) if video else None,
        fps=fps,
        bit_rate=_safe_int(fmt.get("bit_rate")),
        audio_bit_rate=audio_bit_rate,
        subtitle_count=len(subtitles),
        has_cover=cover is not None,
    )


def _safe_int(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _safe_float(value: object) -> float | None:
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _fraction_to_float(value: object) -> float | None:
    if value in (None, "", "0/0"):
        return None
    text = str(value)
    if "/" in text:
        numerator, denominator = text.split("/", 1)
        try:
            return float(numerator) / float(denominator) if float(denominator) else None
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def build_audio_args(
    source: str | Path,
    destination: str | Path,
    codec: str,
    bitrate: int = 320,
    *,
    keep_metadata: bool = True,
) -> list[str]:
    """Build argv (without the final destination) for audio extraction."""

    del destination  # kept in the signature for a readable, symmetrical API
    args = [
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        *audio_bitrate_args(codec, bitrate),
    ]
    if keep_metadata:
        if codec == "mp3":
            args.extend(["-map_metadata", "0", "-id3v2_version", "3"])
        elif codec in {"m4a", "aac", "alac", "flac", "opus", "vorbis"}:
            args.extend(["-map_metadata", "0"])
    return args


def extract_audio(
    source: str | Path,
    destination: str | Path,
    *,
    codec: str,
    bitrate: int = 320,
    keep_metadata: bool = True,
) -> ConversionResult:
    """Extract one audio stream and re-encode it to the requested codec."""

    src = Path(source)
    dest = Path(destination)
    if not src.is_file():
        raise FFmpegError(f"Source file not found: {src}")
    if src.resolve() == dest.resolve():
        raise FFmpegError("Source and destination are the same file.")
    args = build_audio_args(src, dest, codec, bitrate, keep_metadata=keep_metadata)
    return _atomic_ffmpeg(args, dest)


def _video_encoder_args(video_codec: str | None) -> list[str]:
    if video_codec is None or video_codec in {"copy", "none"}:
        return ["-c:v", "copy"]
    if video_codec == "h264":
        return ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
    if video_codec == "h265":
        return ["-c:v", "libx265", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-tag:v", "hvc1"]
    if video_codec == "vp9":
        return ["-c:v", "libvpx-vp9", "-crf", "30", "-b:v", "0", "-row-mt", "1"]
    if video_codec == "av1":
        encoder = _choose_av1_encoder()
        if encoder == "libsvtav1":
            return ["-c:v", encoder, "-preset", "6", "-crf", "30"]
        if encoder == "libaom-av1":
            return ["-c:v", encoder, "-crf", "30", "-b:v", "0", "-cpu-used", "6", "-row-mt", "1"]
        return ["-c:v", "libaom-av1", "-crf", "30", "-b:v", "0", "-cpu-used", "6", "-row-mt", "1"]
    raise FFmpegError(f"Unsupported video codec: {video_codec}")


_encoder_cache: set[str] | None = None


def _available_encoders() -> set[str]:
    global _encoder_cache
    if _encoder_cache is not None:
        return _encoder_cache
    tools = require_tools()
    proc = _run([str(tools.ffmpeg), "-hide_banner", "-encoders"], timeout=30)
    encoders: set[str] = set()
    for line in (proc.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            encoders.add(parts[1])
    _encoder_cache = encoders
    return encoders


def _choose_av1_encoder() -> str:
    encoders = _available_encoders()
    for candidate in ("libsvtav1", "libaom-av1", "librav1e"):
        if candidate in encoders:
            return candidate
    return "libaom-av1"


def build_video_args(
    source: str | Path,
    destination: str | Path,
    *,
    container: str,
    video_codec: str | None = None,
    audio_codec: str | None = "aac",
    audio_bitrate: int = 320,
    height: int | None = None,
    copy_audio: bool = True,
) -> list[str]:
    """Build argv for a video remux/transcode."""

    del destination
    args = [
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-map",
        "0",
        "-map_metadata",
        "0",
        "-map_chapters",
        "0",
        *_video_encoder_args(video_codec),
    ]
    if height:
        args.extend(["-vf", f"scale=-2:{height}"])
    if copy_audio and video_codec in {None, "copy", "none"}:
        args.extend(["-c:a", "copy"])
    elif container == "webm" or audio_codec == "opus":
        args.extend(["-c:a", "libopus", "-b:a", f"{min(audio_bitrate, 512)}k"])
    else:
        args.extend(["-c:a", "aac", "-b:a", f"{audio_bitrate}k"])
    if container in {"mp4", "mov"}:
        args.extend(["-c:s", "mov_text", "-movflags", "+faststart"])
    elif container == "webm":
        args.extend(["-c:s", "webvtt"])
    else:
        args.extend(["-c:s", "copy"])
    return args


def convert_video(
    source: str | Path,
    destination: str | Path,
    *,
    container: str | None = None,
    video_codec: str | None = None,
    audio_codec: str | None = "aac",
    audio_bitrate: int = 320,
    height: int | None = None,
    copy_audio: bool = True,
) -> ConversionResult:
    """Remux or transcode a video to the requested container/codec."""

    src = Path(source)
    dest = Path(destination)
    if not src.is_file():
        raise FFmpegError(f"Source file not found: {src}")
    if src.resolve() == dest.resolve():
        raise FFmpegError("Source and destination are the same file.")
    target_container = (container or dest.suffix.lstrip(".")).lower()
    if target_container == "gif":
        return convert_to_gif(src, dest, height=height)
    args = build_video_args(
        src,
        dest,
        container=target_container,
        video_codec=video_codec,
        audio_codec=audio_codec,
        audio_bitrate=audio_bitrate,
        height=height,
        copy_audio=copy_audio,
    )
    return _atomic_ffmpeg(args, dest)


def build_gif_args(
    source: str | Path,
    destination: str | Path,
    *,
    height: int | None = None,
    width: int | None = None,
    fps: int = 15,
) -> list[str]:
    """Build a high-quality two-pass-style palette GIF command."""

    del destination
    if fps < 1 or fps > 60:
        raise ValueError("GIF fps must be between 1 and 60")
    if height and width:
        scale = f"scale={width}:{height}:force_original_aspect_ratio=decrease"
    elif height:
        scale = f"scale=-2:{height}"
    elif width:
        scale = f"scale={width}:-2"
    else:
        scale = "scale='min(1280,iw)':-2"
    filters = (
        f"fps={fps},{scale}:flags=lanczos,split[s0][s1];"
        "[s0]palettegen=max_colors=256:stats_mode=diff[p];"
        "[s1][p]paletteuse=dither=sierra2_4a:diff_mode=rectangle"
    )
    return [
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(source),
        "-filter_complex",
        filters,
        "-loop",
        "0",
        "-an",
    ]


def convert_to_gif(
    source: str | Path,
    destination: str | Path,
    *,
    height: int | None = None,
    width: int | None = None,
    fps: int = 15,
) -> ConversionResult:
    """Convert a video to an optimized animated GIF."""

    src = Path(source)
    dest = Path(destination)
    if not src.is_file():
        raise FFmpegError(f"Source file not found: {src}")
    if src.resolve() == dest.resolve():
        raise FFmpegError("Source and destination are the same file.")
    args = build_gif_args(src, dest, height=height, width=width, fps=fps)
    return _atomic_ffmpeg(args, dest)


def needs_conversion(
    media: MediaInfo,
    *,
    container: str | None,
    video_codec: str | None = None,
    force: bool = False,
) -> bool:
    """Whether a downloaded file must be remuxed/transcoded for a preset."""

    if force:
        return True
    if container == "gif":
        return media.path.suffix.lower() != ".gif"
    if container and media.path.suffix.lower() != f".{container.lower()}":
        return True
    if video_codec:
        normalized = {
            "h264": {"h264"},
            "h265": {"hevc"},
            "vp9": {"vp9"},
            "av1": {"av1"},
        }.get(video_codec, {video_codec})
        return media.video_codec not in normalized
    return False


def choose_output_path(
    source: str | Path,
    extension: str,
    *,
    directory: str | Path | None = None,
) -> Path:
    """Choose a collision-free output path with a new extension."""

    src = Path(source)
    folder = Path(directory) if directory else src.parent
    candidate = folder / f"{src.stem}.{extension.lstrip('.')}"
    if candidate.resolve() == src.resolve():
        candidate = folder / f"{src.stem} (converted).{extension.lstrip('.')}"
    return unique_path(candidate)


def verify_output(path: str | Path, *, expect_audio: bool = False) -> MediaInfo:
    """Probe an output and ensure it contains the expected stream type."""

    info = probe(path)
    if expect_audio and info.audio_codec is None:
        raise ProbeError(f"Converted file has no audio stream: {Path(path).name}")
    if info.size <= 0:
        raise ProbeError(f"Converted file is empty: {Path(path).name}")
    return info


def format_media_summary(info: MediaInfo) -> str:
    """Small human-readable summary used by conversion results."""

    kind = "audio" if info.is_audio else "video"
    codec = info.audio_codec if info.is_audio else info.video_codec
    return (
        f"{kind} {codec or 'unknown'} · {info.resolution} · "
        f"{format_duration(info.duration)} · {format_bytes(info.size)}"
    )
