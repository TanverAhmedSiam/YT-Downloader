"""Tests for the graphical studio backend and format catalog."""

from __future__ import annotations

import http.client
import json
import threading
from http.cookies import SimpleCookie
from pathlib import Path

import pytest

import ytdl.gui
from ytdl.config import Config
from ytdl.errors import InvalidOptionError
from ytdl.gui.catalog import (
    build_download_request,
    build_info_options,
    catalog_payload,
    defaults_from_config,
    normalize_urls,
)
from ytdl.gui.server import StudioServer
from ytdl.gui.service import JobManager

STATIC_DIR = Path(ytdl.gui.__file__).resolve().parent / "web"


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
def test_catalog_payload_exposes_every_group() -> None:
    catalog = catalog_payload()
    assert {item["id"] for item in catalog["quality"]} >= {"best", "4k", "1080p", "360p"}
    assert {item["id"] for item in catalog["containers"]} >= {"auto", "h264", "mkv", "gif", "av1"}
    assert {item["id"] for item in catalog["audio"]} >= {"mp3", "m4a", "opus", "flac", "wav"}
    assert catalog["sponsorblock"]
    assert "chrome" in catalog["browsers"]
    assert "auto" in catalog["browsers"]


def test_defaults_from_config_are_browser_ready() -> None:
    defaults = defaults_from_config(Config())
    assert defaults["mode"] == "video"
    assert defaults["quality"] == "best"
    assert defaults["container"] == "auto"
    assert Path(defaults["directory"]).is_absolute()
    assert isinstance(defaults["sponsorblock"], list)
    assert defaults["cookies_from_browser"] == "auto"


def test_defaults_follow_music_config() -> None:
    defaults = defaults_from_config(Config(format="flac", music=True))
    assert defaults["mode"] == "audio"
    assert defaults["audio_format"] == "flac"
    assert defaults["music"] is True


def test_defaults_follow_container_config() -> None:
    defaults = defaults_from_config(Config(format="webm-vp9", quality="1080p"))
    assert defaults["mode"] == "video"
    assert defaults["container"] == "vp9"
    assert defaults["quality"] == "1080p"


def test_normalize_urls_splits_dedupes_and_upgrades() -> None:
    urls = normalize_urls("https://a.example/1\nwww.b.example/x\nhttps://a.example/1")
    assert urls == ["https://a.example/1", "https://www.b.example/x"]


def test_normalize_urls_rejects_junk() -> None:
    with pytest.raises(InvalidOptionError):
        normalize_urls("definitely not a link")


def test_studio_preferences_round_trip(tmp_path: Path) -> None:
    from ytdl.gui.service import load_studio_prefs, save_studio_prefs

    path = tmp_path / "studio.json"
    saved = save_studio_prefs(
        {
            "directory": "D:/Media",
            "browser": "firefox",
            "cookies": "",
            "proxy": "http://127.0.0.1:8080",
            "concurrent": 99,
            "retries": -3,
            "audioBitrate": 999,
            "unknown": "ignored",
        },
        path,
    )
    assert saved == {
        "directory": "D:/Media",
        "browser": "firefox",
        "cookies": "",
        "proxy": "http://127.0.0.1:8080",
        "concurrent": 8,
        "retries": 0,
    }
    assert load_studio_prefs(path) == saved
    assert "unknown" not in load_studio_prefs(path)

    save_studio_prefs({"audioBitrate": 256}, path)
    assert load_studio_prefs(path)["audioBitrate"] == 256

    path.write_text("not json", encoding="utf-8")
    assert load_studio_prefs(path) == {}


# ---------------------------------------------------------------------------
# Request building
# ---------------------------------------------------------------------------
def test_build_video_request_defaults() -> None:
    urls, options, meta = build_download_request(
        {"urls": "https://youtu.be/abc", "mode": "video"}, Config()
    )
    assert urls == ["https://youtu.be/abc"]
    assert options.preset.is_video
    assert meta["preset"] == "best"
    assert meta["mode"] == "video"
    assert Path(meta["destination"]).is_absolute()
    assert options.output_template.startswith(meta["destination"])


def test_missing_cookie_file_falls_back_instead_of_failing() -> None:
    _, options, _ = build_download_request(
        {
            "urls": "https://youtu.be/abc",
            "mode": "video",
            "cookies": "Z:/this/file/does/not/exist/cookies.txt",
        },
        Config(),
    )
    assert options.cookies is None


def test_build_video_request_with_container_and_quality() -> None:
    _, options, meta = build_download_request(
        {"urls": "https://youtu.be/abc", "mode": "video", "quality": "1080p", "container": "h264"},
        Config(),
    )
    assert options.preset.name == "mp4-h264"
    assert options.preset.target_height == 1080
    assert meta["container"] == "h264"


def test_build_video_request_accepts_the_source_height() -> None:
    _, options, meta = build_download_request(
        {"urls": "https://youtu.be/abc", "mode": "video", "quality": "144p"},
        Config(),
    )
    assert options.preset.target_height == 144
    assert "height<=144" in options.preset.selector
    assert meta["quality"] == "144p"
    assert meta["format_label"] == "144p · MP4"


def test_title_template_names_the_file_after_the_title() -> None:
    _, options, meta = build_download_request(
        {"urls": "https://youtu.be/abc", "template_mode": "title"},
        Config(),
    )
    assert options.output_template.endswith("%(title)s.%(ext)s")
    assert "%(id)s" not in options.output_template
    assert meta["template_label"] == "title"


def test_gui_downloads_skip_the_info_json_sidecar() -> None:
    _, options, _ = build_download_request({"urls": "https://youtu.be/abc"}, Config())
    assert options.info_json is False
    _, options, _ = build_download_request(
        {"urls": "https://youtu.be/abc", "info_json": True}, Config()
    )
    assert options.info_json is True


def test_build_audio_request_uses_music_template() -> None:
    _, options, meta = build_download_request(
        {"urls": "https://youtu.be/abc", "mode": "audio", "audio_format": "flac", "music": True},
        Config(),
    )
    assert options.preset.is_audio
    assert options.music is True
    assert meta["audio_format"] == "flac"


def test_bitrate_out_of_range_is_rejected() -> None:
    with pytest.raises(InvalidOptionError):
        build_download_request(
            {"urls": "https://youtu.be/abc", "mode": "audio", "bitrate": 9000}, Config()
        )


def test_custom_template_requires_content() -> None:
    with pytest.raises(InvalidOptionError):
        build_download_request(
            {"urls": "https://youtu.be/abc", "template_mode": "custom", "template": "  "},
            Config(),
        )


def test_playlist_range_validation() -> None:
    with pytest.raises(InvalidOptionError):
        build_download_request(
            {"urls": "https://youtu.be/abc", "playlist_start": 5, "playlist_end": 2}, Config()
        )


def test_playlist_mode_defaults_to_auto() -> None:
    _, options, meta = build_download_request(
        {"urls": "https://www.youtube.com/watch?v=abc&list=PLxyz"}, Config()
    )
    assert options.playlist_mode == "auto"
    assert options.playlist_items is None
    assert meta["playlist_mode"] == "auto"
    assert meta["playlist_items"] == []


def test_unknown_playlist_mode_is_rejected() -> None:
    with pytest.raises(InvalidOptionError):
        build_download_request({"urls": "https://youtu.be/abc", "playlist_mode": "sometimes"}, Config())


def test_selected_items_force_expansion() -> None:
    _, options, meta = build_download_request(
        {"urls": "https://youtu.be/abc", "playlist_mode": "auto", "playlist_items": [3, 1, 3]},
        Config(),
    )
    assert options.playlist_mode == "always"
    assert options.playlist_items == (1, 3)
    assert meta["count"] == 2
    assert meta["playlist_items"] == [1, 3]


def test_playlist_items_must_be_numbers() -> None:
    with pytest.raises(InvalidOptionError):
        build_download_request({"urls": "https://youtu.be/abc", "playlist_items": ["x"]}, Config())
    with pytest.raises(InvalidOptionError):
        build_download_request({"urls": "https://youtu.be/abc", "playlist_items": 4}, Config())
    with pytest.raises(InvalidOptionError):
        build_download_request({"urls": "https://youtu.be/abc", "playlist_items": [0]}, Config())


def test_unknown_sponsorblock_category_is_rejected() -> None:
    with pytest.raises(InvalidOptionError):
        build_download_request(
            {"urls": "https://youtu.be/abc", "sponsorblock": ["not_a_category"]}, Config()
        )


def test_bad_rate_limit_is_rejected() -> None:
    with pytest.raises(InvalidOptionError):
        build_download_request({"urls": "https://youtu.be/abc", "rate_limit": "fast"}, Config())


def test_build_info_options_uses_first_url() -> None:
    url, options = build_info_options({"url": "www.youtube.com/watch?v=abc"}, Config())
    assert url == "https://www.youtube.com/watch?v=abc"
    assert options.preset.name == "best"


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
@pytest.fixture()
def studio():
    service = JobManager()
    server = StudioServer(("127.0.0.1", 0), service, "test-token", STATIC_DIR)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def http_request(server, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
    payload = json.dumps(body) if body is not None else None
    connection.request(method, path, body=payload, headers=headers or {})
    response = connection.getresponse()
    data = response.read()
    headers_out = dict(response.getheaders())
    connection.close()
    return response.status, headers_out, data


def auth_cookie(server) -> str:
    status, headers, _ = http_request(server, "GET", "/?token=test-token")
    assert status == 200
    jar = SimpleCookie()
    jar.load(headers["Set-Cookie"])
    return jar["ytdl_studio_token"].value


def test_index_requires_token(studio) -> None:
    status, _, body = http_request(studio, "GET", "/")
    assert status == 401
    assert b"Studio is locked" in body


def test_index_with_token_sets_cookie(studio) -> None:
    status, headers, body = http_request(studio, "GET", "/?token=test-token")
    assert status == 200
    assert "ytdl_studio_token" in headers["Set-Cookie"]
    assert b"ytdl.studio" in body


def test_static_assets_are_served(studio) -> None:
    for path in ("/styles.css", "/app.js", "/favicon.svg", "/fonts/instrument-latin.woff2"):
        status, _, body = http_request(studio, "GET", path)
        assert status == 200, path
        assert body


def test_bootstrap_returns_catalog(studio) -> None:
    token = auth_cookie(studio)
    status, _, body = http_request(
        studio, "GET", "/api/bootstrap", headers={"Cookie": f"ytdl_studio_token={token}"}
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["ok"] is True
    assert payload["catalog"]["quality"]
    assert isinstance(payload["prefs"], dict)


def test_preferences_endpoint_persists(studio, monkeypatch, tmp_path) -> None:
    from ytdl.gui import server as server_module
    from ytdl.gui.service import load_studio_prefs, save_studio_prefs

    path = tmp_path / "studio.json"
    monkeypatch.setattr(
        server_module,
        "save_studio_prefs",
        lambda payload: save_studio_prefs(payload, path),
    )
    monkeypatch.setattr(server_module, "load_studio_prefs", lambda: load_studio_prefs(path))

    token = auth_cookie(studio)
    headers = {
        "Cookie": f"ytdl_studio_token={token}",
        "X-Ytdl-Request": "studio",
        "Content-Type": "application/json",
    }
    status, _, body = http_request(
        studio,
        "POST",
        "/api/prefs",
        body={
            "directory": "D:/Media",
            "browser": "firefox",
            "concurrent": 4,
            "retries": 12,
            "audioBitrate": 256,
        },
        headers=headers,
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["prefs"]["directory"] == "D:/Media"
    assert load_studio_prefs(path) == payload["prefs"]

    status, _, body = http_request(
        studio, "GET", "/api/bootstrap", headers={"Cookie": f"ytdl_studio_token={token}"}
    )
    assert json.loads(body)["prefs"]["browser"] == "firefox"


def test_api_requires_authentication(studio) -> None:
    status, _, _ = http_request(studio, "GET", "/api/jobs")
    assert status == 401


def test_post_requires_studio_header(studio) -> None:
    token = auth_cookie(studio)
    status, _, _ = http_request(
        studio,
        "POST",
        "/api/download",
        body={"urls": "https://youtu.be/abc"},
        headers={"Cookie": f"ytdl_studio_token={token}", "Content-Type": "application/json"},
    )
    assert status == 403


def test_download_validates_input(studio) -> None:
    token = auth_cookie(studio)
    headers = {
        "Cookie": f"ytdl_studio_token={token}",
        "X-Ytdl-Request": "studio",
        "Content-Type": "application/json",
    }
    status, _, _ = http_request(studio, "POST", "/api/download", body={"urls": "nonsense"}, headers=headers)
    assert status == 400


def test_aria2_install_endpoint(studio, monkeypatch) -> None:
    monkeypatch.setattr(
        studio.service,
        "install_aria2c",
        lambda: {"path": "C:/aria2c.exe", "report": {"checks": []}},
    )
    token = auth_cookie(studio)
    headers = {
        "Cookie": f"ytdl_studio_token={token}",
        "X-Ytdl-Request": "studio",
        "Content-Type": "application/json",
    }
    status, _, body = http_request(
        studio, "POST", "/api/aria2/install", body={}, headers=headers
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["ok"] is True
    assert payload["path"] == "C:/aria2c.exe"


def test_expand_endpoint_lists_collection_tracks(studio, monkeypatch) -> None:
    monkeypatch.setattr(
        studio.service,
        "expand",
        lambda payload: {
            "title": "Album",
            "count": 1,
            "entries": [
                {
                    "index": 1,
                    "title": "Track",
                    "url": "https://www.youtube.com/watch?v=t1",
                    "expandable": False,
                }
            ],
        },
    )
    token = auth_cookie(studio)
    headers = {
        "Cookie": f"ytdl_studio_token={token}",
        "X-Ytdl-Request": "studio",
        "Content-Type": "application/json",
    }
    status, _, body = http_request(
        studio,
        "POST",
        "/api/expand",
        body={"url": "https://www.youtube.com/playlist?list=OLAK5uy_album"},
        headers=headers,
    )
    assert status == 200
    payload = json.loads(body)
    assert payload["ok"] is True
    assert payload["title"] == "Album"
    assert payload["entries"][0]["title"] == "Track"


def test_jobs_endpoint_is_empty_initially(studio) -> None:
    token = auth_cookie(studio)
    status, _, body = http_request(
        studio, "GET", "/api/jobs", headers={"Cookie": f"ytdl_studio_token={token}"}
    )
    assert status == 200
    assert json.loads(body)["jobs"] == []


# ---------------------------------------------------------------------------
# Inspection
# ---------------------------------------------------------------------------
class FakeInspectDownloader:
    calls: list[tuple] = []

    def __init__(self, options, ui) -> None:
        self.options = options

    def get_info(self, url: str, playlist: bool = True) -> dict:
        FakeInspectDownloader.calls.append(("get_info", url, playlist))
        return {
            "id": "song1",
            "title": "One Song",
            "channel": "Artist",
            "duration": 180,
            "formats": [
                {
                    "format_id": "18",
                    "ext": "mp4",
                    "vcodec": "avc1.42001E",
                    "acodec": "mp4a.40.2",
                    "height": 360,
                    "tbr": 500,
                }
            ],
        }

    def list_playlist(self, url: str, limit: int = 500) -> dict:
        FakeInspectDownloader.calls.append(("list_playlist", url))
        return {
            "title": "Greatest Hits",
            "id": "PL1",
            "kind": "playlist",
            "count": 2,
            "truncated": False,
            "entries": [
                {"index": 1, "id": "e1", "title": "Track 1", "duration": 60, "uploader": "A", "url": "u1", "thumbnail": ""},
                {"index": 2, "id": "e2", "title": "Track 2", "duration": 90, "uploader": "B", "url": "u2", "thumbnail": ""},
            ],
        }

    def list_channel(self, url: str, limit: int = 500) -> dict:
        FakeInspectDownloader.calls.append(("list_channel", url))
        entries = [
            {
                "index": 1,
                "id": "v1",
                "title": "Video 1",
                "duration": 60,
                "uploader": "Artist",
                "url": "https://www.youtube.com/watch?v=v1",
                "thumbnail": "",
                "section": "videos",
                "section_title": "Videos",
                "key": "videos:1",
                "position": 1,
            },
            {
                "index": 1,
                "id": "OLAK1",
                "title": "Album 1",
                "duration": None,
                "uploader": "Artist",
                "url": "https://www.youtube.com/playlist?list=OLAK1",
                "thumbnail": "",
                "section": "albums",
                "section_title": "Albums",
                "key": "albums:1",
                "position": 2,
            },
        ]
        return {
            "title": "Artist",
            "id": "UC1",
            "kind": "channel",
            "source": "youtube-music",
            "count": 2,
            "truncated": False,
            "url": "https://www.youtube.com/channel/UC1",
            "thumbnail": "",
            "sections": [
                {"id": "videos", "title": "Videos", "kind": "videos", "count": 1, "entries": [entries[0]]},
                {"id": "albums", "title": "Albums", "kind": "albums", "count": 1, "entries": [entries[1]]},
            ],
            "entries": entries,
        }


@pytest.fixture()
def fake_inspect(monkeypatch: pytest.MonkeyPatch) -> type[FakeInspectDownloader]:
    from ytdl.gui import service as service_module

    FakeInspectDownloader.calls = []
    monkeypatch.setattr(service_module, "Downloader", FakeInspectDownloader)
    monkeypatch.setattr(service_module, "load_config", lambda *args, **kwargs: Config())
    monkeypatch.setattr(service_module, "resolve_cookies", lambda *args, **kwargs: None)
    return FakeInspectDownloader


def test_inspect_watch_link_also_lists_the_playlist(fake_inspect) -> None:
    result = JobManager().inspect(
        {"url": "https://www.youtube.com/watch?v=abc&list=PLxyz"}
    )
    assert result["media"]["title"] == "One Song"
    assert result["playlist"]["title"] == "Greatest Hits"
    assert result["playlist"]["count"] == 2
    assert result["media"]["playlist_id"] == "PLxyz"
    assert ("get_info", "https://www.youtube.com/watch?v=abc&list=PLxyz", False) in fake_inspect.calls
    assert ("list_playlist", "https://www.youtube.com/playlist?list=PLxyz") in fake_inspect.calls


def test_inspect_collection_returns_pickable_entries(fake_inspect) -> None:
    result = JobManager().inspect({"url": "https://www.youtube.com/playlist?list=PLxyz"})
    assert result["media"]["title"] == "Greatest Hits"
    assert result["media"]["playlist_kind"] == "playlist"
    assert result["formats"] == []
    assert [row["index"] for row in result["playlist"]["entries"]] == [1, 2]
    assert [row["height"] for row in result["qualities"]] == [360]
    assert result["max_height"] == 360
    assert result["has_video"] is True
    assert result["has_audio"] is True
    assert ("get_info", "u1", False) in fake_inspect.calls


def test_expand_collection_returns_tracks(fake_inspect) -> None:
    result = JobManager().expand(
        {"url": "https://www.youtube.com/playlist?list=OLAK5uy_album"}
    )
    assert result["title"] == "Greatest Hits"
    assert result["count"] == 2
    assert [row["title"] for row in result["entries"]] == ["Track 1", "Track 2"]
    assert (
        "list_playlist",
        "https://www.youtube.com/playlist?list=OLAK5uy_album",
    ) in fake_inspect.calls


def test_inspect_channel_returns_sections(fake_inspect) -> None:
    result = JobManager().inspect(
        {"url": "https://music.youtube.com/channel/UC1"}
    )
    assert result["media"]["playlist_kind"] == "channel"
    assert result["media"]["channel_source"] == "youtube-music"
    assert result["media"]["playlist_sections"] == 2
    assert [section["title"] for section in result["playlist"]["sections"]] == [
        "Videos",
        "Albums",
    ]
    assert [row["key"] for row in result["playlist"]["entries"]] == [
        "videos:1",
        "albums:1",
    ]
    assert ("list_channel", "https://music.youtube.com/channel/UC1") in fake_inspect.calls


def test_inspect_keeps_the_video_when_its_list_is_unreadable(fake_inspect, monkeypatch) -> None:
    def explode(self, url: str, limit: int = 500) -> dict:
        raise InvalidOptionError("This playlist type is unviewable.")

    monkeypatch.setattr(FakeInspectDownloader, "list_playlist", explode)
    result = JobManager().inspect({"url": "https://www.youtube.com/watch?v=abc&list=RDabc"})
    assert result["media"]["title"] == "One Song"
    assert "playlist" not in result
    assert "unviewable" in result["playlist_error"]


def test_inspect_payload_builds_the_source_quality_ladder() -> None:
    from ytdl.gui.service import _inspect_payload

    payload = _inspect_payload(
        {
            "id": "x",
            "title": "Clip",
            "duration": 100,
            "formats": [
                {
                    "format_id": "18",
                    "vcodec": "avc1.42001E",
                    "acodec": "mp4a.40.2",
                    "height": 360,
                    "tbr": 500,
                    "filesize": 1000,
                },
                {
                    "format_id": "137",
                    "vcodec": "avc1.640028",
                    "acodec": "none",
                    "height": 1080,
                    "tbr": 4000,
                },
                {
                    "format_id": "140",
                    "vcodec": "none",
                    "acodec": "mp4a.40.2",
                    "abr": 128,
                },
            ],
        }
    )
    assert [row["height"] for row in payload["qualities"]] == [360, 1080]
    assert payload["qualities"][0]["size"] == 1000
    assert payload["qualities"][1]["size"] == 50_000_000
    assert payload["max_height"] == 1080
    assert payload["has_video"] is True
    assert payload["has_audio"] is True


def test_inspect_payload_marks_audio_only_sources() -> None:
    from ytdl.gui.service import _inspect_payload

    payload = _inspect_payload(
        {
            "title": "Track",
            "duration": 30,
            "formats": [{"vcodec": "none", "acodec": "opus", "abr": 160}],
        }
    )
    assert payload["qualities"] == []
    assert payload["has_video"] is False
    assert payload["has_audio"] is True
