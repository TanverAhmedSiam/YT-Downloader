"""Tiny localhost HTTP server and API for the ytdl studio web interface."""

from __future__ import annotations

import argparse
import contextlib
import hmac
import json
import mimetypes
import os
import platform
import queue
import secrets
import sys
import threading
import webbrowser
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from .. import __app_name__, __version__
from ..config import Config, ConfigError, find_config, load_config
from ..errors import InvalidOptionError, YtdlStudioError
from .catalog import catalog_payload, defaults_from_config, template_examples
from .service import (
    JobManager,
    choose_file,
    choose_folder,
    load_studio_prefs,
    open_path,
    save_studio_prefs,
)

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
}
_COOKIE_NAME = "ytdl_studio_token"


class StudioServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        service: JobManager,
        token: str,
        static_dir: Path,
    ) -> None:
        self.service = service
        self.token = token
        self.static_dir = static_dir
        super().__init__(address, StudioHandler)

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Stay quiet when a browser or test client drops the socket."""

        exc_type = sys.exc_info()[0]
        if exc_type is not None and issubclass(exc_type, ConnectionError):
            return
        super().handle_error(request, client_address)


class StudioHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"ytdl-studio/{__version__}"
    sys_version = ""

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return None

    @property
    def studio(self) -> StudioServer:
        return self.server  # type: ignore[return-value]

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        if not host:
            return True
        port = self.studio.server_port
        return host in {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parsed = urlparse(origin)
        except ValueError:
            return False
        port = self.studio.server_port
        return parsed.hostname in {"127.0.0.1", "localhost", "::1"} and parsed.port == port

    def _provided_token(self) -> str:
        header = self.headers.get("X-Ytdl-Token") or ""
        cookie_header = self.headers.get("Cookie") or ""
        cookie_token = ""
        if cookie_header:
            jar = SimpleCookie()
            try:
                jar.load(cookie_header)
                morsel = jar.get(_COOKIE_NAME)
                if morsel is not None:
                    cookie_token = morsel.value
            except Exception:
                cookie_token = ""
        return header or cookie_token

    def _authorized(self) -> bool:
        return hmac.compare_digest(self._provided_token(), self.studio.token)

    def _client_header_ok(self) -> bool:
        return (self.headers.get("X-Ytdl-Request") or "").lower() == "studio"

    def _send_common_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: https:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; font-src 'self'; media-src 'self'",
        )

    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self._send_common_headers()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def _send_error_json(
        self,
        message: str,
        *,
        status: int = 400,
        hint: str | None = None,
        kind: str = "error",
    ) -> None:
        self._send_json(
            {"ok": False, "error": message, "hint": hint, "kind": kind},
            status=status,
        )

    def _serve_static(self, filename: str, *, set_cookie: bool = False) -> None:
        path = self.studio.static_dir / filename
        if not path.is_file():
            self._send_error_json("Interface asset not found.", status=404)
            return
        body = path.read_bytes()
        content_type = _MIME.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0]
        self.send_response(200)
        self._send_common_headers()
        if set_cookie:
            self.send_header("Set-Cookie", self._cookie_header())
        self.send_header("Content-Type", content_type or "application/octet-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def _cookie_header(self) -> str:
        cookie = SimpleCookie()
        cookie[_COOKIE_NAME] = self.studio.token
        cookie[_COOKIE_NAME]["path"] = "/"
        cookie[_COOKIE_NAME]["httponly"] = True
        cookie[_COOKIE_NAME]["samesite"] = "Strict"
        return cookie[_COOKIE_NAME].OutputString()

    def _auth_page(self) -> None:
        body = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ytdl studio · locked</title>
<style>
:root{color-scheme:dark}*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0b0a09;
font:15px/1.6 "Segoe UI Variable","Segoe UI",system-ui,sans-serif;color:#f4f1ea;padding:24px}
.card{max-width:520px;padding:32px;border-radius:20px;background:#121110;
border:1px solid #272421;box-shadow:0 30px 90px -30px #000}
.mark{width:46px;height:46px;border-radius:13px;display:grid;place-items:center;
background:#ff5233;color:#1a0b06;font-size:22px;font-weight:700}
h1{font-size:24px;letter-spacing:-.03em;margin:20px 0 8px}
p{color:#948d83;margin:0 0 14px}
code{display:block;margin-top:18px;padding:12px 14px;border-radius:11px;background:#0f0e0d;
border:1px solid #272421;color:#ff8a70;font:13px/1.4 ui-monospace,Consolas,monospace}
small{color:#6b655d}
</style></head>
<body><div class="card">
<div class="mark">↓</div>
<h1>Studio is locked</h1>
<p>This local interface needs the one-time access link created by the launcher.</p>
<p>Run the studio again from your terminal, then use the URL it prints:</p>
<code>ytdl gui</code>
<small>Nothing is exposed to the network. The studio only listens on 127.0.0.1.</small>
</div></body></html>"""
        body_bytes = body.encode("utf-8")
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self._send_common_headers()
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'")
        self.send_header("Content-Length", str(len(body_bytes)))
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body_bytes)

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or "0")
        except ValueError as exc:
            raise InvalidOptionError("Invalid request length.") from exc
        if length <= 0:
            return {}
        if length > 2_000_000:
            raise InvalidOptionError("That request is too large.")
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InvalidOptionError("The request contained invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise InvalidOptionError("The request must be a JSON object.")
        return payload

    # ------------------------------------------------------------------
    # GET
    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        if not self._host_ok():
            self._send_error_json("Unexpected host.", status=403)
            return
        parsed = urlparse(self.path)
        path = parsed.path

        if path in {"/", "/index.html"}:
            query = parse_qs(parsed.query)
            query_token = (query.get("token") or [""])[0]
            cookie_ok = self._authorized()
            query_ok = bool(query_token) and hmac.compare_digest(query_token, self.studio.token)
            if not cookie_ok and not query_ok:
                self._auth_page()
                return
            self._serve_static("index.html", set_cookie=query_ok)
            return

        if path in {"/styles.css", "/app.js", "/favicon.svg"}:
            self._serve_static(path.lstrip("/"))
            return

        if path.startswith("/fonts/"):
            name = path[len("/fonts/"):]
            if "/" in name or "\\" in name or ".." in name or not name.endswith(".woff2"):
                self._send_error_json("Interface asset not found.", status=404)
                return
            self._serve_static(f"fonts/{name}")
            return

        if not self._authorized():
            self._send_error_json("Unauthorized.", status=401)
            return

        if path == "/api/bootstrap":
            self._bootstrap()
            return
        if path == "/api/jobs":
            self._send_json({"ok": True, **self.studio.service.snapshot()})
            return
        if path == "/api/events":
            self._events()
            return
        if path == "/api/doctor":
            try:
                report = self.studio.service.doctor()
                self._send_json({"ok": True, "report": report})
            except YtdlStudioError as exc:
                self._send_error_json(exc.message, hint=exc.hint)
            except Exception as exc:  # pragma: no cover - environmental
                self._send_error_json(f"Diagnostics failed: {exc}", status=500)
            return
        self._send_error_json("Not found.", status=404)

    def _bootstrap(self) -> None:
        try:
            try:
                config = load_config()
                config_error = None
            except ConfigError as exc:
                config = Config()
                config_error = exc.message

            defaults = defaults_from_config(config)
            config_path = find_config()
            payload = {
                "ok": True,
                "app": {
                    "name": __app_name__,
                    "version": __version__,
                    "platform": "Windows" if os.name == "nt" else platform.system(),
                },
                "config_path": str(config_path) if config_path else None,
                "config_error": config_error,
                "catalog": catalog_payload(),
                "defaults": defaults,
                "prefs": load_studio_prefs(),
                "templates": template_examples(),
            }
            self._send_json(payload)
        except Exception as exc:  # pragma: no cover - defensive boundary
            self._send_error_json(f"Could not load the studio settings: {exc}", status=500)

    def _events(self) -> None:
        self.send_response(200)
        self._send_common_headers()
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        channel = self.studio.service.bus.subscribe()
        try:
            self._chunk(b"retry: 2500\n\n")
            snapshot = {"type": "snapshot", **self.studio.service.snapshot()}
            self._event(snapshot)
            while True:
                try:
                    event = channel.get(timeout=12)
                except queue.Empty:
                    self._chunk(b": heartbeat\n\n")
                    continue
                self._event(event)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.studio.service.bus.unsubscribe(channel)
            try:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

    def _event(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._chunk(b"data: " + data + b"\n\n")

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
        self.wfile.flush()

    # ------------------------------------------------------------------
    # POST
    # ------------------------------------------------------------------
    def do_POST(self) -> None:  # noqa: N802
        if not self._host_ok() or not self._origin_ok():
            self._send_error_json("Unexpected request origin.", status=403)
            return
        if not self._authorized():
            self._send_error_json("Unauthorized.", status=401)
            return
        if not self._client_header_ok():
            self._send_error_json("Missing studio request header.", status=403)
            return

        parsed = urlparse(self.path)
        path = parsed.path
        try:
            payload = self._read_json()
            if path == "/api/inspect":
                self._send_json(
                    {"ok": True, **self.studio.service.inspect(payload)}
                )
            elif path == "/api/expand":
                self._send_json(
                    {"ok": True, **self.studio.service.expand(payload)}
                )
            elif path == "/api/aria2/install":
                result = self.studio.service.install_aria2c()
                self._send_json({"ok": True, **result})
            elif path == "/api/download":
                job = self.studio.service.start_job(payload)
                self._send_json({"ok": True, "job": job}, status=201)
            elif path == "/api/prefs":
                self._send_json({"ok": True, "prefs": save_studio_prefs(payload)})
            elif path == "/api/jobs/clear":
                snapshot = self.studio.service.clear_finished()
                self._send_json({"ok": True, **snapshot})
            elif path.startswith("/api/jobs/") and path.endswith("/cancel"):
                job_id = path.split("/")[3]
                job = self.studio.service.cancel_job(job_id)
                self._send_json({"ok": True, "job": job})
            elif path == "/api/open-path":
                target = str(payload.get("path") or "").strip()
                if not target:
                    raise InvalidOptionError("No path was provided.")
                open_path(target, select=bool(payload.get("select")))
                self._send_json({"ok": True})
            elif path == "/api/choose-folder":
                selected = choose_folder(
                    str(payload.get("initial") or ""),
                    title=str(payload.get("title") or "Choose a download folder"),
                )
                self._send_json({"ok": True, "path": selected})
            elif path == "/api/choose-file":
                selected = choose_file(
                    str(payload.get("initial") or ""),
                    title=str(payload.get("title") or "Choose a file"),
                )
                self._send_json({"ok": True, "path": selected})
            elif path == "/api/quit":
                self._send_json({"ok": True})
                threading.Timer(0.25, self.studio.shutdown).start()
            else:
                self._send_error_json("Not found.", status=404)
        except YtdlStudioError as exc:
            self._send_error_json(exc.message, hint=exc.hint)
        except Exception as exc:  # pragma: no cover - defensive boundary
            self._send_error_json(f"Unexpected error: {exc}", status=500)


def _static_dir() -> Path:
    return Path(__file__).resolve().parent / "web"


def run_gui(*, port: int = 0, open_browser: bool = True) -> None:
    """Start the local studio server and optionally open it in the browser."""

    console = Console()
    token = secrets.token_urlsafe(32)
    service = JobManager()
    try:
        server = StudioServer(("127.0.0.1", port), service, token, _static_dir())
    except OSError as exc:
        console.print(f"[red]Could not start the studio:[/] {exc}")
        raise SystemExit(1) from exc

    actual_port = server.server_port
    url = f"http://127.0.0.1:{actual_port}/?token={quote(token)}"
    title = Text()
    title.append("↓  ", style="bold #ff5233")
    title.append("ytdl studio", style="bold white")
    title.append(f"  {__version__}", style="dim")
    body = Text()
    body.append("The graphical studio is running locally.\n\n", style="white")
    body.append("Press ", style="dim")
    body.append("Ctrl+C", style="bold")
    body.append(" here to stop.", style="dim")
    console.print(
        Panel(
            body,
            title=title,
            title_align="left",
            border_style="#cc3f25",
            padding=(1, 3),
        )
    )
    console.print()
    console.print("[dim]Open:[/]")
    console.print(f"[bold #ff8a70]{url}[/]", soft_wrap=True)
    console.print()
    console.print("[dim]Local only · 127.0.0.1 · token protected[/]")

    if open_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        console.print("\n[dim]Studio stopped.[/]")
    finally:
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="ytdl-gui",
        description="Launch the warm-dark ytdl studio graphical interface.",
    )
    parser.add_argument("--port", type=int, default=0, help="Port to listen on (default: random free port).")
    parser.add_argument("--no-browser", action="store_true", help="Print the URL without opening a browser.")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    run_gui(port=args.port, open_browser=not args.no_browser)
