"""Playlist policy: song links stay songs, collections can be picked apart."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ytdl import downloader as downloader_module
from ytdl.downloader import (
    Downloader,
    DownloadOptions,
    is_channel_url,
    is_playlist_url,
    is_single_video_url,
    youtube_list_id,
    youtube_playlist_url,
)
from ytdl.presets import resolve_preset
from ytdl.ui import UI

ENTRIES: list[dict[str, Any]] = [
    {
        "id": f"e{index}",
        "title": f"Track {index}",
        "duration": 60 * index,
        "uploader": "Some Artist",
        "ie_key": "Youtube",
        "thumbnails": [{"url": f"https://img.example/{index}.jpg"}],
    }
    for index in range(1, 5)
]


class FakeYDL:
    """Stand-in for YoutubeDL that records the options we resolve with."""

    calls: list[tuple[str, dict[str, Any]]] = []

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options

    def __enter__(self) -> FakeYDL:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def extract_info(self, url: str, download: bool = False) -> dict[str, Any]:
        FakeYDL.calls.append((url, dict(self.options)))
        collection = "/playlist?list=" in url or "/channel/" in url
        expands = collection or ("list=" in url and not self.options.get("noplaylist"))
        if expands:
            limit = self.options.get("playlistend")
            entries = ENTRIES[:limit] if limit else ENTRIES
            return {"_type": "playlist", "id": "PL1", "title": "Greatest Hits", "entries": entries}
        return {
            "id": "song1",
            "title": "One Song",
            "webpage_url": url,
            "formats": [],
        }


@pytest.fixture()
def fake_ydl(monkeypatch: pytest.MonkeyPatch) -> type[FakeYDL]:
    FakeYDL.calls = []
    monkeypatch.setattr(downloader_module, "YoutubeDL", FakeYDL)
    return FakeYDL


def make_downloader(**overrides: Any) -> Downloader:
    options = DownloadOptions(
        preset=resolve_preset("best"),
        output_template="%(title)s.%(ext)s",
        download_dir=Path("downloads"),
        **overrides,
    )
    return Downloader(options, UI(quiet=True, json_mode=True))


# ---------------------------------------------------------------------------
# URL classification
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc123",
        "https://www.youtube.com/watch?v=abc123&list=PLabc&index=2",
        "https://music.youtube.com/watch?v=abc123&list=OLAK5uy_abc",
        "https://youtu.be/abc123?list=PLabc",
        "https://www.youtube.com/shorts/abc123",
    ],
)
def test_single_video_urls(url: str) -> None:
    assert is_single_video_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/playlist?list=PLabc",
        "https://music.youtube.com/playlist?list=OLAK5uy_abc",
        "https://www.youtube.com/@someartist",
        "https://www.youtube.com/channel/UCabc",
        "https://vimeo.com/12345",
    ],
)
def test_collection_urls(url: str) -> None:
    assert is_single_video_url(url) is False


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/@someartist",
        "https://www.youtube.com/@someartist/videos",
        "https://www.youtube.com/channel/UCabc",
        "https://www.youtube.com/channel/UCabc/playlists",
        "https://music.youtube.com/channel/UCabc",
        "https://music.youtube.com/browse/UCabc",
    ],
)
def test_channel_urls(url: str) -> None:
    assert is_channel_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abc123",
        "https://www.youtube.com/playlist?list=PLabc",
        "https://music.youtube.com/playlist?list=OLAK5uy_abc",
        "https://www.youtube.com/browse/MPREb_album",
        "https://vimeo.com/12345",
    ],
)
def test_non_channel_urls(url: str) -> None:
    assert is_channel_url(url) is False


def test_list_helpers() -> None:
    assert youtube_list_id("https://www.youtube.com/watch?v=abc&list=PLxyz") == "PLxyz"
    assert youtube_list_id("https://www.youtube.com/watch?v=abc") is None
    assert youtube_list_id("https://vimeo.com/1?list=PLxyz") is None
    assert youtube_playlist_url("PLxyz") == "https://www.youtube.com/playlist?list=PLxyz"


def test_expandable_collection_urls() -> None:
    assert is_playlist_url("https://www.youtube.com/playlist?list=PLabc") is True
    assert is_playlist_url("https://music.youtube.com/playlist?list=OLAK5uy_abc") is True
    assert is_playlist_url("https://www.youtube.com/browse/MPREb_album") is True
    assert is_playlist_url("https://www.youtube.com/watch?v=abc&list=PLabc") is False
    assert is_playlist_url("https://vimeo.com/12345") is False


# ---------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------
def test_watch_link_with_list_resolves_as_one_video(fake_ydl: type[FakeYDL]) -> None:
    downloader = make_downloader(playlist_mode="auto")
    entries = downloader.resolve_entries(
        ["https://www.youtube.com/watch?v=abc123&list=PLabc"]
    )
    assert [entry.id for entry in entries] == ["song1"]
    assert fake_ydl.calls[0][1]["noplaylist"] is True


def test_playlist_link_expands_in_auto_mode(fake_ydl: type[FakeYDL]) -> None:
    downloader = make_downloader(playlist_mode="auto")
    entries = downloader.resolve_entries(["https://www.youtube.com/playlist?list=PLabc"])
    assert [entry.id for entry in entries] == ["e1", "e2", "e3", "e4"]
    assert fake_ydl.calls[0][1]["noplaylist"] is False


def test_playlist_mode_always_expands_a_watch_link(fake_ydl: type[FakeYDL]) -> None:
    downloader = make_downloader(playlist_mode="always")
    entries = downloader.resolve_entries(
        ["https://www.youtube.com/watch?v=abc123&list=PLabc"]
    )
    assert len(entries) == 4
    assert fake_ydl.calls[0][1]["noplaylist"] is False


def test_playlist_mode_never_stays_single(fake_ydl: type[FakeYDL]) -> None:
    downloader = make_downloader(playlist_mode="never")
    entries = downloader.resolve_entries(
        ["https://www.youtube.com/watch?v=abc123&list=PLabc"]
    )
    assert len(entries) == 1
    assert fake_ydl.calls[0][1]["noplaylist"] is True


def test_selected_items_use_original_positions(fake_ydl: type[FakeYDL]) -> None:
    downloader = make_downloader(playlist_mode="always", playlist_items=(2, 4))
    entries = downloader.resolve_entries(
        ["https://www.youtube.com/watch?v=abc123&list=PLabc"]
    )
    assert [entry.id for entry in entries] == ["e2", "e4"]
    assert [entry.playlist_index for entry in entries] == [2, 4]
    assert all(entry.playlist_title == "Greatest Hits" for entry in entries)


def test_start_and_end_still_slice_the_list(fake_ydl: type[FakeYDL]) -> None:
    downloader = make_downloader(playlist_mode="auto", playlist_start=2, playlist_end=3)
    entries = downloader.resolve_entries(["https://www.youtube.com/playlist?list=PLabc"])
    assert [entry.id for entry in entries] == ["e2", "e3"]
    assert [entry.playlist_index for entry in entries] == [2, 3]


def test_list_playlist_returns_pickable_rows(fake_ydl: type[FakeYDL]) -> None:
    downloader = make_downloader()
    listing = downloader.list_playlist("https://www.youtube.com/playlist?list=PLabc", limit=2)
    assert listing["title"] == "Greatest Hits"
    assert listing["count"] == 2
    assert listing["truncated"] is True
    first = listing["entries"][0]
    assert first["index"] == 1
    assert first["title"] == "Track 1"
    assert first["duration"] == 60
    assert first["thumbnail"] == "https://img.example/1.jpg"
    assert first["url"] == "https://www.youtube.com/watch?v=e1"
    assert first["expandable"] is False


def test_channel_sections_number_from_one(monkeypatch: pytest.MonkeyPatch) -> None:
    downloader = make_downloader()

    def fake_listing(url: str, *, limit: int = 500) -> dict[str, Any]:
        if url.endswith("/videos"):
            entries = [
                {"index": 1, "title": "Video 1", "url": "https://www.youtube.com/watch?v=v1"},
                {"index": 2, "title": "Video 2", "url": "https://www.youtube.com/watch?v=v2"},
            ]
        elif url.endswith("/playlists"):
            entries = [
                {"index": 1, "title": "List 1", "url": "https://www.youtube.com/playlist?list=PL1"},
                {"index": 2, "title": "List 2", "url": "https://www.youtube.com/playlist?list=PL2"},
            ]
        else:
            entries = []
        return {"title": "Artist", "count": len(entries), "entries": entries}

    monkeypatch.setattr(downloader, "list_playlist", fake_listing)
    listing = downloader.list_channel("https://www.youtube.com/channel/UCabc")

    assert [
        (entry["section"], entry["position"], entry["global_position"])
        for entry in listing["entries"]
    ] == [
        ("videos", 1, 1),
        ("videos", 2, 2),
        ("playlists", 1, 3),
        ("playlists", 2, 4),
    ]
