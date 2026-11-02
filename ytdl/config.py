"""Configuration models and TOML loading/saving.

Precedence is intentionally simple:

1. built-in defaults,
2. ``./ytdl-studio.toml`` (or the per-user config file),
3. command-line flags.

The config file is optional.  Unknown keys are ignored so a config written for
a newer release remains usable, while invalid values produce an actionable
error instead of a traceback.
"""

from __future__ import annotations

import os
import platform
import tomllib
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .errors import ConfigError
from .paths import DEFAULT_DOWNLOAD_DIR, DEFAULT_MUSIC_TEMPLATE, DEFAULT_VIDEO_TEMPLATE

CONFIG_FILENAME: Final[str] = "ytdl-studio.toml"


class Config(BaseModel):
    """Validated application configuration."""

    model_config = ConfigDict(extra="ignore", validate_assignment=True, str_strip_whitespace=True)

    format: str = "best"
    quality: str | None = None
    bitrate: int = Field(default=320, ge=8, le=512)
    output: str | None = None
    directory: str = DEFAULT_DOWNLOAD_DIR
    music: bool = False

    subtitles: bool = False
    subtitle_langs: str = "en"
    thumbnail: bool = True
    metadata: bool = True
    chapters: bool = True
    sponsorblock: str | None = "sponsor,selfpromo"

    playlist_start: int | None = Field(default=None, ge=1)
    playlist_end: int | None = Field(default=None, ge=1)
    shuffle: bool = False
    reverse: bool = False

    concurrent: int = Field(default=3, ge=1)
    rate_limit: str | None = None
    retries: int = Field(default=10, ge=0, le=100)

    cookies_from_browser: str | None = None
    cookies: str | None = None
    proxy: str | None = None
    user_agent: str | None = None
    archive: str | None = None
    keep_video: bool = False
    use_aria2c: bool = True

    @field_validator("quality", mode="before")
    @classmethod
    def _quality_to_string(cls, value: Any) -> str | None:
        if value is None or value == "" or value is False:
            return None
        return str(value)

    @field_validator("playlist_start", "playlist_end", mode="before")
    @classmethod
    def _blank_int_to_none(cls, value: Any) -> Any:
        if value in (None, "", 0):
            return None
        return value

    @field_validator("cookies_from_browser")
    @classmethod
    def _validate_browser(cls, value: str | None) -> str | None:
        if value is None:
            return None
        allowed = {"chrome", "firefox", "edge", "brave", "safari", "chromium", "opera", "vivaldi", "whale"}
        cleaned = value.strip().lower()
        if cleaned not in allowed:
            raise ValueError(
                f"Unsupported browser {value!r}; use one of: {', '.join(sorted(allowed))}"
            )
        return cleaned

    @model_validator(mode="after")
    def _validate_ranges(self) -> Config:
        if (
            self.playlist_start is not None
            and self.playlist_end is not None
            and self.playlist_end < self.playlist_start
        ):
            raise ValueError("playlist_end must be greater than or equal to playlist_start")
        if self.rate_limit is not None and not str(self.rate_limit).strip():
            self.rate_limit = None
        return self

    @property
    def output_template(self) -> str:
        """The configured template, or a mode-aware default."""

        return self.output or (DEFAULT_MUSIC_TEMPLATE if self.music else DEFAULT_VIDEO_TEMPLATE)

    @property
    def sponsorblock_categories(self) -> list[str]:
        """Parse the comma-separated SponsorBlock category list."""

        if not self.sponsorblock:
            return []
        return [part.strip().lower() for part in self.sponsorblock.split(",") if part.strip()]

    def public_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly configuration without secrets."""

        data = self.model_dump(mode="json")
        for key in ("cookies", "cookies_from_browser", "proxy", "archive"):
            if data.get(key):
                data[key] = "configured"
        return data


def _flatten_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Accept a flat config and a few common sectioned layouts."""

    if not isinstance(raw, dict):
        raise ConfigError("The config file must contain a TOML table.")
    merged: dict[str, Any] = {}
    section_names = {
        "defaults",
        "download",
        "downloads",
        "paths",
        "output",
        "metadata",
        "playlist",
        "network",
        "authentication",
        "advanced",
        "ytdl",
        "ytdl-studio",
        "ytdl_studio",
    }
    for key, value in raw.items():
        if isinstance(value, dict) and key in section_names:
            merged.update(value)
        else:
            merged[key] = value

    aliases = {
        "download_dir": "directory",
        "download_directory": "directory",
        "dir": "directory",
        "outtmpl": "output",
        "output_template": "output",
        "subs": "subtitles",
        "embed_subs": "subtitles",
        "embed_thumbnail": "thumbnail",
        "write_metadata": "metadata",
        "embed_chapters": "chapters",
        "sponsorblock_categories": "sponsorblock",
        "parallel": "concurrent",
        "jobs": "concurrent",
        "concurrency": "concurrent",
        "ratelimit": "rate_limit",
        "cookie_file": "cookies",
        "browser_cookies": "cookies_from_browser",
        "subtitle_languages": "subtitle_langs",
    }
    for old, new in aliases.items():
        if old != new and old in merged and new not in merged:
            merged[new] = merged[old]
        if old != new:
            merged.pop(old, None)
    return merged


def default_config_paths(cwd: str | Path | None = None) -> list[Path]:
    """Config search order, highest priority first."""

    cwd_path = Path(cwd) if cwd is not None else Path.cwd()
    paths = [cwd_path / CONFIG_FILENAME]
    home = Path.home()
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            paths.append(Path(appdata) / "ytdl-studio" / "config.toml")
        paths.append(home / ".config" / "ytdl-studio" / "config.toml")
    elif platform.system() == "Darwin":
        paths.append(home / "Library" / "Application Support" / "ytdl-studio" / "config.toml")
        paths.append(home / ".config" / "ytdl-studio" / "config.toml")
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        if xdg:
            paths.append(Path(xdg) / "ytdl-studio" / "config.toml")
        paths.append(home / ".config" / "ytdl-studio" / "config.toml")
    return paths


def find_config(cwd: str | Path | None = None, explicit: str | Path | None = None) -> Path | None:
    """Find the active config file, or return ``None``."""

    if explicit is not None:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"Config file not found: {path}")
        return path
    for path in default_config_paths(cwd):
        if path.is_file():
            return path
    return None


def load_config(
    path: str | Path | None = None,
    *,
    cwd: str | Path | None = None,
) -> Config:
    """Load and validate a TOML config.

    When *path* is omitted the standard search order is used.  A missing config
    is not an error; built-in defaults are returned.
    """

    selected = find_config(cwd=cwd, explicit=path)
    if selected is None:
        return Config()
    try:
        raw = tomllib.loads(selected.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ConfigError(f"Could not read config {selected}: {exc}", hint="Check file permissions.") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(
            f"Invalid TOML in {selected}: {exc}",
            hint="Compare it with config.example.toml.",
        ) from exc
    try:
        return Config.model_validate(_flatten_config(raw))
    except Exception as exc:  # pydantic.ValidationError and friends
        raise ConfigError(
            f"Invalid config value in {selected}: {exc}",
            hint="Compare it with config.example.toml.",
        ) from exc


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    raise TypeError(f"Cannot serialize {type(value).__name__} to TOML")


def save_config(config: Config, path: str | Path) -> Path:
    """Write a readable TOML config and return its path."""

    destination = Path(path).expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# ytdl-studio configuration",
        "# CLI flags override these values.",
        "",
    ]
    for field, value in config.model_dump(mode="python").items():
        if value is None:
            continue
        lines.append(f"{field} = {_toml_value(value)}")
    lines.append("")
    destination.write_text("\n".join(lines), encoding="utf-8")
    return destination


def resolve_config_path(cwd: str | Path | None = None) -> Path:
    """Return the preferred path for a new per-user config."""

    return default_config_paths(cwd)[-1]
