"""Artwork and source metadata helpers."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from ytdl.converter import find_tools
from ytdl.downloader import _merge_source_details
from ytdl.metadata import _as_cover, _is_square_source, _tags


def test_square_source_detection() -> None:
    assert _is_square_source({"width": 1080, "height": 1080}) is True
    assert _is_square_source({"width": 1080, "height": 1081}) is True
    assert _is_square_source({"width": 1920, "height": 1080}) is False
    assert _is_square_source({"width": 1080, "height": 1920}) is False
    assert _is_square_source({}) is False


def test_fallback_extraction_keeps_source_geometry() -> None:
    fallback = {
        "thumbnail": "https://img.example/fallback.webp",
        "width": 1080,
        "height": 1080,
        "aspect_ratio": 1.0,
    }
    merged = _merge_source_details({"thumbnail": "https://img.example/fallback.webp"}, fallback)
    assert merged["width"] == 1080
    assert merged["height"] == 1080
    assert _is_square_source(merged) is True


def test_source_details_are_kept_as_extra_tags() -> None:
    tags = _tags(
        {
            "title": "Track",
            "channel": "Channel",
            "channel_id": "UC1",
            "uploader": "Uploader",
            "creator": "Creator",
            "description": "Description",
            "release_date": "2024-03-02",
            "copyright": "Copyright",
            "license": "License",
            "tags": ["one", "two"],
        }
    )
    assert tags["extra"]["channel"] == "Channel"
    assert tags["extra"]["channel_id"] == "UC1"
    assert tags["extra"]["creator"] == "Creator"
    assert tags["extra"]["date"] == "2024-03-02"
    assert tags["extra"]["copyright"] == "Copyright"
    assert tags["extra"]["tags"] == "one, two"


def test_square_cover_is_center_cropped(tmp_path: Path) -> None:
    tools = find_tools()
    if tools is None:
        pytest.skip("FFmpeg is required for cover conversion")
    source = tmp_path / "wide.jpg"
    subprocess.run(
        [
            str(tools.ffmpeg),
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=200x100",
            "-frames:v",
            "1",
            str(source),
        ],
        check=True,
    )
    cover, temporary = _as_cover(source, square=True)
    try:
        proc = subprocess.run(
            [
                str(tools.ffprobe),
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "csv=p=0",
                str(cover),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert proc.stdout.strip() == "100,100"
    finally:
        if temporary:
            cover.unlink(missing_ok=True)
