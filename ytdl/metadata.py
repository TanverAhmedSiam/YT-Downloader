"""Metadata, artwork, chapters, subtitles, and sidecar files.

Audio files are tagged with Mutagen (works even when yt-dlp's FFmpeg metadata
postprocessor is not used).  Video tags are handled by yt-dlp's FFmpeg
postprocessors; the helpers below can also re-embed chapters/subtitles/artwork
after a conversion when needed.
"""

from __future__ import annotations

import base64
import contextlib
import json
import mimetypes
import os
import subprocess
import tempfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import httpx

from .converter import find_tools, require_tools
from .errors import MetadataError
from .paths import format_bytes

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}
_MAX_COVER_BYTES = 30 * 1024 * 1024


def _first(info: dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        value = info.get(key)
        if value not in (None, "", [], {}, False):
            return str(value)
    return default


def _year(info: dict[str, Any]) -> str:
    value = info.get("release_year") or info.get("upload_year")
    if value:
        return str(value)
    date = str(info.get("release_date") or info.get("upload_date") or "")
    return date[:4] if len(date) >= 4 and date[:4].isdigit() else ""


def _track(info: dict[str, Any]) -> tuple[int | None, int | None]:
    number = info.get("track_number") or info.get("playlist_index")
    total = info.get("track_count") or info.get("n_entries")
    try:
        return (int(number), int(total)) if number is not None else (None, int(total) if total else None)
    except (TypeError, ValueError):
        return None, None


def _int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _is_square_source(info: dict[str, Any]) -> bool:
    """True when the source itself is square.

    YouTube Music often has a 1:1 video with a 16:9 thumbnail.  In that case the
    thumbnail contains coloured pillarboxes; cropping it to the source's square
    aspect ratio gives the artwork the user actually sees in the player.
    """

    width, height = _int(info.get("width")), _int(info.get("height"))
    if not width or not height:
        return False
    longest = max(width, height)
    return abs(width - height) <= max(2, round(longest * 0.02))


def _date(info: dict[str, Any]) -> str:
    return _first(info, "release_date", "upload_date", default="") or _year(info)


def _joined(value: object) -> str:
    if isinstance(value, (list, tuple, set)):
        return ", ".join(str(item) for item in value if item not in (None, "", [], {}))
    return str(value or "")


def _extra_tags(info: dict[str, Any]) -> dict[str, str]:
    """Best-effort source details that do not fit the small core tag set."""

    extras = {
        "date": _date(info),
        "description": _first(info, "description", "synopsis", default=""),
        "copyright": _first(info, "copyright", default=""),
        "license": _first(info, "license", default=""),
        "publisher": _first(info, "publisher", "studio", default=""),
        "language": _first(info, "language", default=""),
        "grouping": _first(info, "grouping", "series", default=""),
        "lyricist": _first(info, "lyricist", "writer", default=""),
        "conductor": _first(info, "conductor", default=""),
        "performer": _first(info, "performer", default=""),
        "remixer": _first(info, "remixer", default=""),
        "encoded_by": _first(info, "encoded_by", "encoder", default=""),
        "source": _first(info, "webpage_url", "original_url", default=""),
        "webpage_url": _first(info, "webpage_url", default=""),
        "channel": _first(info, "channel", "uploader", default=""),
        "channel_id": _first(info, "channel_id", default=""),
        "channel_url": _first(info, "channel_url", "uploader_url", default=""),
        "uploader": _first(info, "uploader", default=""),
        "uploader_id": _first(info, "uploader_id", default=""),
        "creator": _first(info, "creator", default=""),
        "album_artist": _first(info, "album_artist", default=""),
        "release_year": _first(info, "release_year", default=""),
        "playlist": _first(info, "playlist_title", "playlist", default=""),
        "playlist_index": _first(info, "playlist_index", default=""),
        "extractor": _first(info, "extractor", "extractor_key", default=""),
        "view_count": _first(info, "view_count", default=""),
        "like_count": _first(info, "like_count", default=""),
        "comment_count": _first(info, "comment_count", default=""),
        "age_limit": _first(info, "age_limit", default=""),
        "is_live": _first(info, "is_live", default=""),
        "was_live": _first(info, "was_live", default=""),
        "live_status": _first(info, "live_status", default=""),
        "availability": _first(info, "availability", default=""),
        "tags": _joined(info.get("tags")),
    }
    return {key: value for key, value in extras.items() if value not in ("", None)}


def _tags(info: dict[str, Any]) -> dict[str, str]:
    title = _first(info, "track", "title", default="Unknown title")
    artist = _first(info, "artist", "creator", "channel", "uploader", default="Unknown artist")
    album_artist = _first(info, "album_artist", "artist", "channel", "uploader", default=artist)
    album = _first(info, "album", default="Unknown album")
    genre = _first(info, "genre", default="")
    comment = _first(info, "description", "webpage_url", default="")
    if len(comment) > 2000:
        comment = comment[:1997] + "…"
    number, total = _track(info)
    return {
        "title": title,
        "artist": artist,
        "albumartist": album_artist,
        "album": album,
        "year": _year(info),
        "genre": genre,
        "comment": comment,
        "track": str(number) if number is not None else "",
        "track_total": str(total) if total is not None else "",
        "disc": str(info.get("disc_number") or ""),
        "disc_total": str(info.get("disc_count") or ""),
        "composer": _first(info, "composer", default=""),
        "url": _first(info, "webpage_url", default=""),
        "extra": _extra_tags(info),
    }


def _image_mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(path.name)
    if mime and mime.startswith("image/"):
        return mime
    header = path.read_bytes()[:12]
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith(b"\x89PNG"):
        return "image/png"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _as_cover(path: Path, *, square: bool = False) -> tuple[Path, bool]:
    """Return an embeddable JPEG/PNG path and whether it is temporary.

    ``square`` center-crops the artwork to 1:1 instead of adding or retaining
    pillarboxes.  This is what makes a square YouTube Music canvas stay square
    in players that display album art as a square.
    """

    if not square and path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
        return path, False
    tools = find_tools()
    if tools is None:
        raise MetadataError(
            "FFmpeg is required to convert the thumbnail into embeddable cover art.",
            hint="Run `ytdl doctor` to install/verify FFmpeg.",
        )
    temp = path.with_name(f".{path.stem}.cover.jpg")
    command = [
        str(tools.ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(path),
    ]
    if square:
        # FFmpeg centres the crop by default, so side padding is removed and
        # the actual source artwork is kept.
        command.extend(["-vf", "crop=min(iw\\,ih):min(iw\\,ih)"])
    command.extend(
        [
            "-frames:v",
            "1",
            "-q:v",
            "2",
            str(temp),
        ]
    )
    proc = subprocess.run(
        command,
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if proc.returncode != 0 or not temp.is_file() or temp.stat().st_size == 0:
        if temp.exists():
            temp.unlink(missing_ok=True)
        raise MetadataError(f"Could not convert cover art: {(proc.stderr or '').strip()[-500:]}")
    return temp, True


def _flac_picture(cover: Path) -> Any:
    from mutagen.flac import Picture

    data = cover.read_bytes()
    if len(data) > _MAX_COVER_BYTES:
        raise MetadataError("Cover art is unexpectedly large.")
    picture = Picture()
    picture.type = 3  # front cover
    picture.mime = _image_mime(cover)
    picture.desc = "Cover"
    picture.data = data
    picture.width = 0
    picture.height = 0
    picture.depth = 0
    return picture


def tag_audio(
    path: str | Path,
    info: dict[str, Any],
    *,
    cover_path: str | Path | None = None,
) -> Path:
    """Write title/artist/album/year/track tags and optional cover art.

    Supports MP3, M4A/AAC/ALAC, FLAC, Ogg Vorbis, Opus, and WAV.  Existing tags
    are merged rather than erased.  The function is idempotent.
    """

    audio_path = Path(path)
    if not audio_path.is_file():
        raise MetadataError(f"Audio file not found: {audio_path}")
    if audio_path.stat().st_size == 0:
        raise MetadataError(f"Audio file is empty: {audio_path.name}")

    cover = Path(cover_path) if cover_path else None
    temporary_cover: Path | None = None
    try:
        if cover and cover.is_file():
            cover, is_temporary = _as_cover(cover, square=_is_square_source(info))
            if is_temporary:
                temporary_cover = cover
        else:
            cover = None
        tags = _tags(info)
        suffix = audio_path.suffix.lower()
        if suffix == ".mp3":
            _tag_id3(audio_path, tags, cover)
        elif suffix in {".m4a", ".mp4", ".alac"}:
            _tag_mp4(audio_path, tags, cover)
        elif suffix == ".flac":
            _tag_flac(audio_path, tags, cover)
        elif suffix in {".ogg", ".oga", ".opus"}:
            _tag_ogg(audio_path, tags, cover)
        elif suffix == ".wav":
            _tag_wav(audio_path, tags, cover)
        else:
            raise MetadataError(f"Unsupported audio container for tagging: {suffix or 'unknown'}")
    except MetadataError:
        raise
    except Exception as exc:
        raise MetadataError(f"Could not write tags to {audio_path.name}: {exc}") from exc
    finally:
        if temporary_cover and temporary_cover.exists():
            temporary_cover.unlink(missing_ok=True)
    return audio_path


def _id3_extra_frames(frames: Any, tags: dict[str, Any]) -> None:
    """Add standard ID3 frames plus TXXX fields for source details."""

    from mutagen.id3 import (
        TCOP,
        TENC,
        TEXT,
        TIT1,
        TLAN,
        TPE3,
        TPE4,
        TPUB,
        TXXX,
        WOAS,
        WXXX,
    )

    extra = tags.get("extra") or {}
    standards = {
        "copyright": (TCOP, "TCOP"),
        "publisher": (TPUB, "TPUB"),
        "encoded_by": (TENC, "TENC"),
        "language": (TLAN, "TLAN"),
        "lyricist": (TEXT, "TEXT"),
        "conductor": (TPE3, "TPE3"),
        "remixer": (TPE4, "TPE4"),
        "grouping": (TIT1, "TIT1"),
    }
    for key, (frame_type, frame_id) in standards.items():
        value = extra.get(key)
        if value:
            frames.setall(frame_id, [frame_type(encoding=3, text=value)])
    if extra.get("date"):
        from mutagen.id3 import TDRC

        frames.setall("TDRC", [TDRC(encoding=3, text=extra["date"])])
    if extra.get("source"):
        frames.setall("WOAS", [WOAS(url=extra["source"])])
    if extra.get("webpage_url"):
        frames.setall(
            "WXXX",
            [WXXX(encoding=3, desc="Source", url=extra["webpage_url"])],
        )
    for key, value in extra.items():
        if key in standards or key in {"date", "source", "webpage_url"}:
            continue
        frames.setall(
            f"TXXX:{key}",
            [TXXX(encoding=3, desc=key, text=str(value))],
        )


def _tag_id3(path: Path, tags: dict[str, Any], cover: Path | None) -> None:
    from mutagen.id3 import (
        APIC,
        COMM,
        ID3,
        TALB,
        TCOM,
        TCON,
        TDRC,
        TIT2,
        TPE1,
        TPE2,
        TPOS,
        TRCK,
        ID3NoHeaderError,
    )

    try:
        frames = ID3(path)
    except ID3NoHeaderError:
        frames = ID3()
    frames.setall("TIT2", [TIT2(encoding=3, text=tags["title"])])
    frames.setall("TPE1", [TPE1(encoding=3, text=tags["artist"])])
    frames.setall("TPE2", [TPE2(encoding=3, text=tags["albumartist"])])
    frames.setall("TALB", [TALB(encoding=3, text=tags["album"])])
    if tags["year"]:
        frames.setall("TDRC", [TDRC(encoding=3, text=tags["year"])])
    if tags["track"]:
        track = tags["track"]
        if tags["track_total"]:
            track = f"{track}/{tags['track_total']}"
        frames.setall("TRCK", [TRCK(encoding=3, text=track)])
    if tags["disc"]:
        disc = tags["disc"]
        if tags.get("disc_total"):
            disc = f"{disc}/{tags['disc_total']}"
        frames.setall("TPOS", [TPOS(encoding=3, text=disc)])
    if tags["genre"]:
        frames.setall("TCON", [TCON(encoding=3, text=tags["genre"])])
    if tags["composer"]:
        frames.setall("TCOM", [TCOM(encoding=3, text=tags["composer"])])
    if tags["comment"]:
        frames.setall("COMM", [COMM(encoding=3, lang="eng", desc="", text=tags["comment"])])
    _id3_extra_frames(frames, tags)
    if cover:
        data = cover.read_bytes()
        frames.setall(
            "APIC",
            [APIC(encoding=3, mime=_image_mime(cover), type=3, desc="Cover", data=data)],
        )
    frames.save(path, v2_version=3)


def _tag_mp4(path: Path, tags: dict[str, Any], cover: Path | None) -> None:
    from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

    audio = MP4(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    extra = tags.get("extra") or {}
    audio.tags["\xa9nam"] = [tags["title"]]
    audio.tags["\xa9ART"] = [tags["artist"]]
    audio.tags["aART"] = [tags["albumartist"]]
    audio.tags["\xa9alb"] = [tags["album"]]
    date = extra.get("date") or tags["year"]
    if date:
        audio.tags["\xa9day"] = [date]
    if tags["genre"]:
        audio.tags["\xa9gen"] = [tags["genre"]]
    if tags["composer"]:
        audio.tags["\xa9wrt"] = [tags["composer"]]
    if tags["comment"]:
        audio.tags["\xa9cmt"] = [tags["comment"]]
    if tags["track"]:
        number = int(tags["track"])
        total = int(tags["track_total"]) if tags["track_total"] else 0
        audio.tags["trkn"] = [(number, total)]
    if tags["disc"]:
        with contextlib.suppress(ValueError):
            total = int(tags["disc_total"]) if tags.get("disc_total") else 0
            audio.tags["disk"] = [(int(tags["disc"]), total)]
    for key, value in extra.items():
        if key in {"date"} or value in ("", None):
            continue
        atom = f"----:com.apple.iTunes:{key}"
        audio.tags[atom] = [
            MP4FreeForm(str(value).encode("utf-8"), dataformat=MP4FreeForm.FORMAT_TEXT)
        ]
    if cover:
        data = cover.read_bytes()
        fmt = MP4Cover.FORMAT_PNG if cover.suffix.lower() == ".png" else MP4Cover.FORMAT_JPEG
        audio.tags["covr"] = [MP4Cover(data, imageformat=fmt)]
    audio.save()


def _tag_flac(path: Path, tags: dict[str, Any], cover: Path | None) -> None:
    from mutagen.flac import FLAC

    audio = FLAC(path)
    audio["title"] = tags["title"]
    audio["artist"] = tags["artist"]
    audio["albumartist"] = tags["albumartist"]
    audio["album"] = tags["album"]
    if tags["year"]:
        audio["date"] = tags["year"]
    if tags["track"]:
        audio["tracknumber"] = tags["track"]
    if tags.get("track_total"):
        audio["tracktotal"] = tags["track_total"]
    if tags["disc"]:
        audio["discnumber"] = tags["disc"]
    if tags.get("disc_total"):
        audio["disctotal"] = tags["disc_total"]
    if tags["genre"]:
        audio["genre"] = tags["genre"]
    if tags["composer"]:
        audio["composer"] = tags["composer"]
    if tags["comment"]:
        audio["comment"] = tags["comment"]
    for key, value in (tags.get("extra") or {}).items():
        if value not in ("", None):
            audio[key] = str(value)
    if cover:
        audio.clear_pictures()
        audio.add_picture(_flac_picture(cover))
    audio.save()


def _tag_ogg(path: Path, tags: dict[str, Any], cover: Path | None) -> None:
    from mutagen.oggopus import OggOpus
    from mutagen.oggvorbis import OggVorbis

    opener = OggOpus if path.suffix.lower() == ".opus" else OggVorbis
    audio = opener(path)
    audio["title"] = [tags["title"]]
    audio["artist"] = [tags["artist"]]
    audio["albumartist"] = [tags["albumartist"]]
    audio["album"] = [tags["album"]]
    if tags["year"]:
        audio["date"] = [tags["year"]]
    if tags["track"]:
        audio["tracknumber"] = [tags["track"]]
    if tags.get("track_total"):
        audio["tracktotal"] = [tags["track_total"]]
    if tags["disc"]:
        audio["discnumber"] = [tags["disc"]]
    if tags.get("disc_total"):
        audio["disctotal"] = [tags["disc_total"]]
    if tags["genre"]:
        audio["genre"] = [tags["genre"]]
    if tags["composer"]:
        audio["composer"] = [tags["composer"]]
    if tags["comment"]:
        audio["comment"] = [tags["comment"]]
    for key, value in (tags.get("extra") or {}).items():
        if value not in ("", None):
            audio[key] = [str(value)]
    if cover:
        audio.pop("metadata_block_picture", None)
        picture = _flac_picture(cover)
        encoded = base64.b64encode(picture.write()).decode("ascii")
        audio["metadata_block_picture"] = [encoded]
    audio.save()


def _tag_wav(path: Path, tags: dict[str, Any], cover: Path | None) -> None:
    from mutagen.id3 import APIC, TALB, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK
    from mutagen.wave import WAVE

    audio = WAVE(path)
    if audio.tags is None:
        audio.add_tags()
    assert audio.tags is not None
    audio.tags.setall("TIT2", [TIT2(encoding=3, text=tags["title"])])
    audio.tags.setall("TPE1", [TPE1(encoding=3, text=tags["artist"])])
    audio.tags.setall("TPE2", [TPE2(encoding=3, text=tags["albumartist"])])
    audio.tags.setall("TALB", [TALB(encoding=3, text=tags["album"])])
    if tags["year"]:
        audio.tags.setall("TDRC", [TDRC(encoding=3, text=tags["year"])])
    if tags["track"]:
        track = tags["track"]
        if tags.get("track_total"):
            track = f"{track}/{tags['track_total']}"
        audio.tags.setall("TRCK", [TRCK(encoding=3, text=track)])
    if tags["disc"]:
        disc = tags["disc"]
        if tags.get("disc_total"):
            disc = f"{disc}/{tags['disc_total']}"
        audio.tags.setall("TPOS", [TPOS(encoding=3, text=disc)])
    _id3_extra_frames(audio.tags, tags)
    if cover:
        audio.tags.setall(
            "APIC",
            [APIC(encoding=3, mime=_image_mime(cover), type=3, desc="Cover", data=cover.read_bytes())],
        )
    audio.save()


def write_info_json(media_path: str | Path, info: dict[str, Any]) -> Path:
    """Write a UTF-8 ``.info.json`` sidecar next to the final media file."""

    path = Path(media_path)
    sidecar = path.with_name(path.name + ".info.json")
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    try:
        sidecar.write_text(
            json.dumps(info, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
    except OSError as exc:
        raise MetadataError(f"Could not write {sidecar.name}: {exc}") from exc
    return sidecar


def fetch_thumbnail(url: str | None, destination: str | Path, *, timeout: float = 30.0) -> Path | None:
    """Download a thumbnail with httpx, enforcing a sane size limit."""

    if not url:
        return None
    dest = Path(destination)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        with (
            httpx.Client(follow_redirects=True, timeout=timeout) as client,
            client.stream("GET", url) as response,
        ):
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            if content_type and not content_type.lower().startswith("image/"):
                return None
            written = 0
            with dest.open("wb") as handle:
                for chunk in response.iter_bytes(64 * 1024):
                    written += len(chunk)
                    if written > _MAX_COVER_BYTES:
                        raise MetadataError("Thumbnail exceeds the 30 MiB safety limit.")
                    handle.write(chunk)
        if dest.stat().st_size == 0:
            dest.unlink(missing_ok=True)
            return None
        return dest
    except (httpx.HTTPError, OSError) as exc:
        dest.unlink(missing_ok=True)
        raise MetadataError(f"Could not download thumbnail: {exc}") from exc


def find_thumbnail(media_path: str | Path) -> Path | None:
    """Find a yt-dlp thumbnail sitting beside a media file."""

    path = Path(media_path)
    for suffix in (".jpg", ".jpeg", ".png", ".webp"):
        candidate = path.with_suffix(suffix)
        if candidate.is_file():
            return candidate
    # yt-dlp sometimes keeps the source extension in the thumbnail stem when a
    # postprocessor changes the final extension.
    for candidate in path.parent.glob(path.stem + ".*"):
        if candidate.suffix.lower() in _IMAGE_SUFFIXES:
            return candidate
    return None


def _ffmetadata(info: dict[str, Any], chapters: Iterable[dict[str, Any]]) -> str:
    def esc(value: object) -> str:
        text = str(value or "")
        for char in ("\\", "=", ";", "#", "\n"):
            text = text.replace(char, f"\\{char}")
        return text

    tags = _tags(info)
    lines = [";FFMETADATA1"]
    for key, value in (
        ("title", tags["title"]),
        ("artist", tags["artist"]),
        ("album", tags["album"]),
        ("album_artist", tags["albumartist"]),
        ("date", tags["year"]),
        ("genre", tags["genre"]),
        ("comment", tags["comment"]),
    ):
        if value:
            lines.append(f"{key}={esc(value)}")
    for chapter in chapters:
        start = int(float(chapter.get("start_time") or 0) * 1000)
        end = int(float(chapter.get("end_time") or 0) * 1000)
        if end <= start:
            continue
        lines.extend(
            [
                "",
                "[CHAPTER]",
                "TIMEBASE=1/1000",
                f"START={start}",
                f"END={end}",
                f"title={esc(chapter.get('title') or 'Chapter')}",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def embed_chapters(
    media_path: str | Path,
    chapters: Iterable[dict[str, Any]],
    info: dict[str, Any] | None = None,
) -> Path:
    """Embed chapters and common tags into a video by remuxing with FFmpeg."""

    path = Path(media_path)
    chapters = list(chapters)
    if not path.is_file() or not chapters:
        return path
    tools = require_tools()
    metadata_text = _ffmetadata(info or {}, chapters)
    with tempfile.NamedTemporaryFile(
        "w", suffix=".ffmetadata", delete=False, encoding="utf-8", dir=path.parent
    ) as handle:
        handle.write(metadata_text)
        metadata_path = Path(handle.name)
    temp_out = path.with_name(f".{path.stem}.chapters.tmp{path.suffix}")
    try:
        proc = subprocess.run(
            [
                str(tools.ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(path),
                "-i",
                str(metadata_path),
                "-map",
                "0",
                "-map_metadata",
                "1",
                "-codec",
                "copy",
                str(temp_out),
            ],
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if proc.returncode != 0 or not temp_out.is_file():
            raise MetadataError(f"Could not embed chapters: {proc.stderr[-800:]}")
        os.replace(temp_out, path)
    except OSError as exc:
        raise MetadataError(f"Could not embed chapters: {exc}") from exc
    finally:
        metadata_path.unlink(missing_ok=True)
        if temp_out.exists():
            temp_out.unlink(missing_ok=True)
    return path


def embed_subtitles(
    media_path: str | Path,
    subtitle_files: Iterable[str | Path],
    *,
    language: str = "eng",
) -> Path:
    """Embed subtitle files into a video, remuxing only the streams."""

    path = Path(media_path)
    subs = [Path(item) for item in subtitle_files if Path(item).is_file()]
    if not path.is_file() or not subs:
        return path
    tools = require_tools()
    temp_out = path.with_name(f".{path.stem}.subs.tmp{path.suffix}")
    suffix = path.suffix.lower()
    codec = "mov_text" if suffix in {".mp4", ".mov"} else "srt"
    argv = [
        str(tools.ffmpeg),
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-y",
        "-i",
        str(path),
    ]
    for subtitle in subs:
        argv.extend(["-i", str(subtitle)])
    argv.extend(["-map", "0"])
    for index in range(len(subs)):
        argv.extend(["-map", str(index + 1)])
    argv.extend(["-c", "copy", "-c:s", codec])
    for index in range(len(subs)):
        argv.extend([f"-metadata:s:s:{index}", f"language={language}"])
    argv.append(str(temp_out))
    try:
        proc = subprocess.run(
            argv,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if proc.returncode != 0 or not temp_out.is_file():
            raise MetadataError(f"Could not embed subtitles: {proc.stderr[-800:]}")
        os.replace(temp_out, path)
    finally:
        if temp_out.exists():
            temp_out.unlink(missing_ok=True)
    return path


def embed_cover_video(media_path: str | Path, cover_path: str | Path) -> Path:
    """Attach cover art to an MP4/MKV video without re-encoding."""

    path = Path(media_path)
    cover = Path(cover_path)
    if not path.is_file() or not cover.is_file():
        return path
    if path.suffix.lower() not in {".mp4", ".m4v", ".mkv", ".mov"}:
        raise MetadataError(f"Cover art is not supported for {path.suffix}.")
    tools = require_tools()
    temp_cover, is_temp = _as_cover(cover)
    temp_out = path.with_name(f".{path.stem}.cover.tmp{path.suffix}")
    try:
        proc = subprocess.run(
            [
                str(tools.ffmpeg),
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-i",
                str(path),
                "-i",
                str(temp_cover),
                "-map",
                "0",
                "-map",
                "1",
                "-c",
                "copy",
                "-disposition:v:1",
                "attached_pic",
                str(temp_out),
            ],
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if proc.returncode != 0 or not temp_out.is_file():
            raise MetadataError(f"Could not attach cover art: {proc.stderr[-800:]}")
        os.replace(temp_out, path)
    finally:
        if is_temp and temp_cover.exists():
            temp_cover.unlink(missing_ok=True)
        if temp_out.exists():
            temp_out.unlink(missing_ok=True)
    return path


def metadata_summary(path: str | Path, info: dict[str, Any]) -> str:
    """A short line for verbose logs."""

    size = Path(path).stat().st_size if Path(path).is_file() else 0
    return f"{_first(info, 'title', default=Path(path).name)} · {format_bytes(size)}"
