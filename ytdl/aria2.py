"""Find or provision a local aria2c binary.

aria2c is optional: when it is available yt-dlp can use it for parallel
connections, and when it is not the native downloader is used.  On Windows we
can download the official static build into the app's data directory instead
of requiring a package manager.
"""

from __future__ import annotations

import hashlib
import io
import os
import platform
import tempfile
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx

from .converter import find_executable
from .errors import MissingDependencyError

_Progress = Callable[[str], None]

_RELEASE_API = "https://api.github.com/repos/aria2/aria2/releases/latest"
_FALLBACK_ASSET = (
    "https://github.com/aria2/aria2/releases/download/"
    "release-1.37.0/aria2-1.37.0-win-64bit-build1.zip"
)


def _data_dir() -> Path:
    if os.name == "nt":
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        base = Path(root) if root else Path.home() / "AppData" / "Local"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "ytdl-studio"


def _binary_name() -> str:
    return "aria2c.exe" if os.name == "nt" else "aria2c"


def find_aria2c() -> Path | None:
    """Return the managed aria2c binary, otherwise one from PATH."""

    bundled = _data_dir() / "bin" / _binary_name()
    if bundled.is_file():
        return bundled
    return find_executable("aria2c")


def _asset_from_release(client: httpx.Client) -> tuple[str, str | None]:
    try:
        response = client.get(_RELEASE_API)
        response.raise_for_status()
        assets = response.json().get("assets") or []
    except (httpx.HTTPError, ValueError, TypeError):
        return _FALLBACK_ASSET, None
    for asset in assets:
        name = str(asset.get("name") or "")
        if "win-64bit" in name.lower() and name.lower().endswith(".zip"):
            digest = asset.get("digest")
            return str(asset.get("browser_download_url") or _FALLBACK_ASSET), (
                str(digest) if digest else None
            )
    return _FALLBACK_ASSET, None


def _extract_aria2c(archive: bytes, target: Path) -> None:
    with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
        member = next(
            (
                name
                for name in bundle.namelist()
                if Path(name).name.lower() == "aria2c.exe"
            ),
            None,
        )
        if member is None:
            raise MissingDependencyError(
                "The aria2c archive did not contain aria2c.exe.",
                hint="Install aria2 manually with `winget install aria2.aria2`.",
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=".aria2c-",
            suffix=".exe.tmp",
            dir=target.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            with bundle.open(member) as source:
                while chunk := source.read(1024 * 1024):
                    handle.write(chunk)
        temporary.replace(target)


def install_aria2c(progress: _Progress | None = None) -> Path:
    """Download the official Windows build into the app data directory.

    Returns the existing binary when one is already available.  Automatic
    provisioning is intentionally Windows-only; macOS and Linux should use
    their package manager.
    """

    existing = find_aria2c()
    if existing is not None:
        return existing
    if os.name != "nt":
        raise MissingDependencyError(
            "Automatic aria2c download is only supported on Windows.",
            hint="Install aria2 with your package manager (brew, apt, or pacman).",
        )
    machine = platform.machine().lower()
    if machine not in {"amd64", "x86_64"}:
        raise MissingDependencyError(
            f"Automatic aria2c download is not available for {platform.machine()}.",
            hint="Install aria2 manually from https://aria2.github.io/.",
        )

    target = _data_dir() / "bin" / _binary_name()
    if progress:
        progress("Looking up the latest aria2 release…")
    with httpx.Client(
        timeout=httpx.Timeout(90.0),
        follow_redirects=True,
        headers={"User-Agent": "ytdl-studio aria2 installer"},
    ) as client:
        url, digest = _asset_from_release(client)
        if progress:
            progress("Downloading aria2c…")
        response = client.get(url)
        response.raise_for_status()
        payload = response.content
    if digest and digest.lower().startswith("sha256:"):
        actual = hashlib.sha256(payload).hexdigest()
        if actual.lower() != digest.split(":", 1)[1].strip().lower():
            raise MissingDependencyError(
                "The aria2c download failed its checksum.",
                hint="Try again, or install aria2 manually.",
            )
    if progress:
        progress("Extracting aria2c…")
    _extract_aria2c(payload, target)
    if not target.is_file():
        raise MissingDependencyError(
            "aria2c could not be installed.",
            hint="Install aria2 manually with `winget install aria2.aria2`.",
        )
    return target
