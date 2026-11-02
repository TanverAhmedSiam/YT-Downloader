"""Environment diagnostics for ``ytdl doctor``."""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import httpx
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import __version__
from .aria2 import find_aria2c
from .converter import ffmpeg_version, find_executable, find_tools
from .paths import format_bytes

Status = Literal["ok", "warn", "fail", "optional"]


@dataclass(slots=True)
class Check:
    name: str
    status: Status
    detail: str
    install: str | None = None


@dataclass(slots=True)
class DoctorReport:
    checks: list[Check] = field(default_factory=list)
    version: str = __version__
    platform_name: str = ""
    config_path: str | None = None
    download_dir: str = ""

    @property
    def ok(self) -> bool:
        return not any(check.status == "fail" for check in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "version": self.version,
            "platform": self.platform_name,
            "config_path": self.config_path,
            "download_dir": self.download_dir,
            "checks": [
                {
                    "name": check.name,
                    "status": check.status,
                    "detail": check.detail,
                    "install": check.install,
                }
                for check in self.checks
            ],
        }


def _run_version(argv: list[str]) -> str | None:
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    output = (proc.stdout or proc.stderr or "").strip()
    return output.splitlines()[0] if output else None


def _disk_details(path: Path) -> tuple[Status, str]:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    try:
        usage = shutil.disk_usage(candidate)
    except OSError as exc:
        return "warn", f"Could not read disk space: {exc}"
    free = usage.free
    status: Status = "ok" if free >= 1024**3 else "warn"
    detail = f"{format_bytes(free)} free on {candidate.drive or candidate}"
    if free < 100 * 1024**2:
        status = "fail"
        detail += " — less than 100 MiB available"
    return status, detail


def _network_check(proxy: str | None = None) -> tuple[Status, str]:
    try:
        with httpx.Client(
            timeout=httpx.Timeout(15.0),
            follow_redirects=True,
            headers={"User-Agent": "ytdl-studio doctor"},
            proxy=proxy,
        ) as client:
            response = client.get("https://www.youtube.com/")
        if response.status_code < 400:
            ctype = response.headers.get("content-type", "")
            return "ok", f"Reached youtube.com (HTTP {response.status_code}, {ctype.split(';')[0]})"
        return "warn", f"YouTube responded with HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        return "fail", f"Network request failed: {exc.__class__.__name__}: {exc}"


def _install_hint(component: str) -> str:
    system = platform.system()
    if system == "Windows":
        return {
            "ffmpeg": "winget install Gyan.FFmpeg   (or: choco install ffmpeg)",
            "aria2c": "winget install aria2.aria2   (or: choco install aria2)",
            "python": "winget install Python.Python.3.12",
            "yt-dlp": "pip install -U yt-dlp",
        }.get(component, "")
    if system == "Darwin":
        return {
            "ffmpeg": "brew install ffmpeg",
            "aria2c": "brew install aria2",
            "python": "brew install python@3.12",
            "yt-dlp": "pip install -U yt-dlp",
        }.get(component, "")
    return {
        "ffmpeg": "sudo apt install ffmpeg   (Debian/Ubuntu) or use your distro package manager",
        "aria2c": "sudo apt install aria2",
        "python": "sudo apt install python3.12 python3.12-venv",
        "yt-dlp": "pip install -U yt-dlp",
    }.get(component, "")


def run_checks(
    *,
    download_dir: str | Path = "downloads",
    config_path: str | Path | None = None,
    proxy: str | None = None,
) -> DoctorReport:
    """Run all diagnostics and return a structured report."""

    report = DoctorReport(
        platform_name=f"{platform.system()} {platform.release()} ({platform.machine()})",
        config_path=str(config_path) if config_path else None,
        download_dir=str(download_dir),
    )

    # Python -----------------------------------------------------------------
    py_ok = sys.version_info >= (3, 11)
    report.checks.append(
        Check(
            "Python",
            "ok" if py_ok else "fail",
            f"{platform.python_version()} at {sys.executable}",
            None if py_ok else _install_hint("python"),
        )
    )

    # yt-dlp -----------------------------------------------------------------
    try:
        import yt_dlp
        import yt_dlp.version

        ytdlp_version = getattr(yt_dlp.version, "__version__", "unknown")
        report.checks.append(
            Check(
                "yt-dlp",
                "ok",
                f"{ytdlp_version} (Python API {Path(yt_dlp.__file__).name})",
            )
        )
    except Exception as exc:
        report.checks.append(
            Check("yt-dlp", "fail", f"Import failed: {exc}", _install_hint("yt-dlp"))
        )

    # FFmpeg / ffprobe -------------------------------------------------------
    tools = find_tools()
    if tools is None:
        ffmpeg = find_executable("ffmpeg")
        ffprobe = find_executable("ffprobe")
        detail_parts = []
        if ffmpeg is None:
            detail_parts.append("ffmpeg not found")
        if ffprobe is None:
            detail_parts.append("ffprobe not found")
        report.checks.append(
            Check("FFmpeg", "fail", "; ".join(detail_parts), _install_hint("ffmpeg"))
        )
        report.checks.append(
            Check("ffprobe", "fail", "Required companion to FFmpeg", _install_hint("ffmpeg"))
        )
    else:
        version = ffmpeg_version() or _run_version([str(tools.ffmpeg), "-version"]) or "unknown"
        report.checks.append(Check("FFmpeg", "ok", f"{version} at {tools.ffmpeg}"))
        probe_version = _run_version([str(tools.ffprobe), "-version"]) or "unknown"
        report.checks.append(Check("ffprobe", "ok", f"{probe_version} at {tools.ffprobe}"))

    # aria2c -----------------------------------------------------------------
    aria2c = find_aria2c()
    if aria2c is None:
        report.checks.append(
            Check(
                "aria2c",
                "optional",
                "Not installed; native yt-dlp downloader will be used",
                _install_hint("aria2c"),
            )
        )
    else:
        version = _run_version([str(aria2c), "--version"]) or "installed"
        report.checks.append(Check("aria2c", "ok", f"{version} at {aria2c}"))

    # Disk -------------------------------------------------------------------
    status, detail = _disk_details(Path(download_dir))
    report.checks.append(Check("Disk space", status, detail))

    # Network ----------------------------------------------------------------
    status, detail = _network_check(proxy=proxy)
    report.checks.append(
        Check(
            "Network",
            status,
            detail,
            "Check your connection, DNS, firewall, VPN, or use `--proxy URL`." if status != "ok" else None,
        )
    )

    return report


def render_report(report: DoctorReport, console: Console | None = None) -> None:
    """Print the doctor report with installation instructions."""

    console = console or Console(highlight=False, emoji=False, legacy_windows=False)
    table = Table(
        title="ytdl doctor",
        box=box.SIMPLE_HEAVY,
        header_style="bold cyan",
        show_lines=False,
    )
    table.add_column("", width=2, no_wrap=True)
    table.add_column("Check", style="bold", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Details", overflow="fold")
    styles = {
        "ok": ("✓", "green"),
        "warn": ("!", "yellow"),
        "fail": ("✗", "red"),
        "optional": ("–", "dim"),
    }
    for check in report.checks:
        symbol, style = styles[check.status]
        table.add_row(
            f"[{style}]{symbol}[/]",
            check.name,
            f"[{style}]{check.status}[/]",
            check.detail,
        )
    console.print(table)
    console.print(
        f"[dim]ytdl-studio {report.version} · {report.platform_name} · output: {report.download_dir}[/]"
    )
    if report.config_path:
        console.print(f"[dim]config: {report.config_path}[/]")

    missing = [check for check in report.checks if check.status == "fail"]
    instructions = [(check.name, check.install) for check in missing if check.install]
    if instructions:
        body = []
        for name, command in instructions:
            body.append(f"[bold]{name}[/]: [cyan]{command}[/]")
        console.print(
            Panel(
                "\n".join(body),
                title="Install missing dependencies",
                border_style="yellow",
                box=box.ROUNDED,
            )
        )
    if report.ok:
        console.print("[green]All core tools are ready.[/]")
    else:
        console.print("[red]One or more core checks failed. Fix them and rerun `ytdl doctor`.[/]")
