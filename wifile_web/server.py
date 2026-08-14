"""HTTP backend for the WiFile web UI.

Serves the single-page app and a small JSON API, and runs the two engine
threads (sender and receiver) with a :class:`WebUI` adapter in front of the
shared :class:`WebState` store. Endpoints:

    GET  /            static SPA
    GET  /api/state   full snapshot as JSON
    GET  /api/events  Server-Sent Events stream of snapshots
    POST /api/start   start the server or client engine thread
    POST /api/upload  multipart upload of files to serve
    POST /api/answer  answer a pending prompt
    POST /api/settings  toggle discovery (broadcast/listen)
    POST /api/stop    stop an engine thread
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import time
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import wifile

from .adapter import WebUI
from .discovery import Discovery, ready_label
from .state import WebState

WEB_PORT = 8765
MAX_UPLOAD_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB per upload request
_UPLOAD_ROOT = os.path.join(tempfile.gettempdir(), "wifile_uploads")
_UPLOAD_TTL = 24 * 60 * 60  # seconds; stale upload dirs swept on startup

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".json": "application/json",
}


class _RequestError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _source_label(source: str) -> str:
    """Short label for discovery beacons."""
    if os.path.normcase(source).startswith(os.path.normcase(_UPLOAD_ROOT)):
        return "browser upload"
    return os.path.basename(source.rstrip("/\\")) or source


class _ServerUI(WebUI):
    """WebUI that also keeps the discovery beacon's source label fresh."""

    def __init__(
        self, state: WebState, mode: str, discovery: Discovery, from_upload: bool
    ) -> None:
        super().__init__(state, mode)
        self._discovery = discovery
        self._from_upload = from_upload

    def message(self, text: str) -> None:
        super().message(text)
        if self._from_upload:
            return
        label = ready_label(text)
        if label:
            self._discovery.update_source(label)


class EngineRunner:
    """Starts/stops the sender and receiver engine threads."""

    def __init__(self, state: WebState, discovery: Discovery) -> None:
        self._state = state
        self._discovery = discovery
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._upload_batches: set[str] = set()

    def start(self, mode: str, params: dict[str, Any]) -> None:
        with self._lock:
            existing = self._threads.get(mode)
            if existing is not None and existing.is_alive():
                raise RuntimeError(f"{mode} engine is already running")
            target = self._run_server if mode == "server" else self._run_client
            self._state.reset(mode)
            thread = threading.Thread(
                target=target,
                args=(mode, params),
                name=f"wifile-{mode}",
                daemon=True,
            )
            self._threads[mode] = thread
            thread.start()

    def register_upload(self, batch_dir: str) -> None:
        with self._lock:
            self._upload_batches.add(batch_dir)

    # -- engine entry points --------------------------------------------------

    def _run_server(self, mode: str, params: dict[str, Any]) -> None:
        source = str(params.get("source") or "")
        port = int(params["port"])
        try:
            if os.path.isfile(source):
                filepath, folder = source, None
            elif os.path.isdir(source):
                filepath, folder = None, source
            else:
                raise ValueError(f"source does not exist: {source!r}")
            self._state.set_running(mode, True)
            self._state.log(mode, f"Starting sender on port {port} for '{source}'")
            ui = _ServerUI(
                self._state,
                mode,
                self._discovery,
                from_upload=os.path.normcase(source).startswith(
                    os.path.normcase(_UPLOAD_ROOT)
                ),
            )
            self._discovery.configure_announce(port, _source_label(source))
            wifile.start_server(port, filepath=filepath, folder=folder, ui=ui)
        except (OSError, ValueError) as e:
            self._state.log(mode, f"Sender failed to start: {e}")
        finally:
            self._discovery.stop_announcing()
            self._state.set_running(mode, False)
            self._state.clear_progress(mode)
            self._cleanup_uploads()

    def _run_client(self, mode: str, params: dict[str, Any]) -> None:
        host = str(params["host"])
        port = int(params["port"])
        output_dir = str(params.get("output_dir") or ".")
        conflict = str(params.get("conflict") or "ask")
        try:
            os.makedirs(output_dir, exist_ok=True)
            self._state.set_running(mode, True)
            self._state.log(
                mode, f"Connecting to {host}:{port}, saving to '{output_dir}'"
            )
            wifile.start_client(
                host,
                port,
                output_dir,
                auto_overwrite=conflict == "overwrite",
                auto_rename=conflict == "rename",
                ui=WebUI(self._state, mode),
            )
        except OSError as e:
            self._state.log(mode, f"Receiver failed to start: {e}")
        finally:
            self._state.set_running(mode, False)
            self._state.clear_progress(mode)

    def _cleanup_uploads(self) -> None:
        """Remove upload batches once the sender engine has stopped."""
        with self._lock:
            batches = list(self._upload_batches)
            self._upload_batches.clear()
        for batch in batches:
            shutil.rmtree(batch, ignore_errors=True)


def parse_upload(body: bytes, content_type: str) -> tuple[str, int]:
    """Write multipart file parts under a fresh upload directory.

    Each file part must carry ``name=<relative path>``; paths are sanitized
    with :func:`wifile.sanitize_relative_path` so they stay inside the
    upload directory. Returns ``(batch_dir, file_count)``.
    """
    if not content_type.startswith("multipart/form-data"):
        raise ValueError("expected multipart/form-data")
    os.makedirs(_UPLOAD_ROOT, exist_ok=True)
    batch_dir = tempfile.mkdtemp(prefix="batch-", dir=_UPLOAD_ROOT)
    try:
        message = BytesParser(policy=policy.default).parsebytes(
            b"Content-Type: "
            + content_type.encode("utf-8", "replace")
            + b"\r\nMIME-Version: 1.0\r\n\r\n"
            + body
        )
        if not message.is_multipart():
            raise ValueError("multipart body has no parts")
        count = 0
        for part in message.iter_parts():
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            name = part.get_param("name", header="content-disposition")
            if not name:
                name = part.get_filename() or "upload"
            rel = wifile.sanitize_relative_path(name)
            dest = os.path.join(batch_dir, rel)
            parent = os.path.dirname(dest)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(dest, "wb") as f:
                f.write(payload)
            count += 1
        if count == 0:
            raise ValueError("no file parts in upload")
        return batch_dir, count
    except Exception:
        shutil.rmtree(batch_dir, ignore_errors=True)
        raise


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        state: WebState,
        runner: EngineRunner,
        discovery: Discovery,
    ) -> None:
        self.web_state = state
        self.runner = runner
        self.discovery = discovery
        super().__init__(address, _Handler)
        # Advertise the web UI port only when it is reachable from the LAN.
        host = self.server_address[0]
        if host in ("127.0.0.1", "localhost", "::1"):
            self.discovery.set_web_port(None)
        else:
            self.discovery.set_web_port(self.server_address[1])

    def server_close(self) -> None:
        try:
            self.discovery.stop()
        finally:
            super().server_close()


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "WiFileWeb/0.1"
    close_connection: bool

    @property
    def state(self) -> WebState:
        return self.server.web_state  # type: ignore[attr-defined]

    @property
    def runner(self) -> EngineRunner:
        return self.server.runner  # type: ignore[attr-defined]

    @property
    def discovery(self) -> Discovery:
        return self.server.discovery  # type: ignore[attr-defined]

    def log_message(self, *args: Any) -> None:
        pass  # keep the console quiet; state changes go to the UI instead

    # -- helpers ---------------------------------------------------------------

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        chunks: list[bytes] = []
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def _read_json(self) -> dict[str, Any]:
        raw = self._read_body()
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise _RequestError(400, f"invalid JSON: {e}") from e
        if not isinstance(data, dict):
            raise _RequestError(400, "JSON body must be an object")
        return data

    # -- routing ------------------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (http.server API)
        try:
            path = self.path.split("?", 1)[0]
            if path == "/api/state":
                self._send_json(200, self.state.get_snapshot())
            elif path == "/api/events":
                self._handle_events()
            else:
                self._serve_static(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except _RequestError as e:
            self._send_json(e.status, {"error": e.message})
        except (OSError, ValueError) as e:
            self._send_json(500, {"error": str(e)})

    def do_POST(self) -> None:  # noqa: N802
        try:
            path = self.path.split("?", 1)[0]
            if path == "/api/start":
                self._handle_start()
            elif path == "/api/stop":
                self._handle_stop()
            elif path == "/api/answer":
                self._handle_answer()
            elif path == "/api/settings":
                self._handle_settings()
            elif path == "/api/upload":
                self._handle_upload()
            else:
                self._send_json(404, {"error": "not found"})
        except _RequestError as e:
            self._send_json(e.status, {"error": e.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (OSError, ValueError) as e:
            self._send_json(500, {"error": str(e)})

    # -- endpoints ------------------------------------------------------------------

    def _handle_start(self) -> None:
        data = self._read_json()
        mode = data.get("mode")
        if mode not in ("server", "client"):
            raise _RequestError(400, "mode must be 'server' or 'client'")

        port = wifile.DEFAULT_PORT
        if data.get("port") is not None:
            try:
                port = int(data["port"])
            except (TypeError, ValueError):
                raise _RequestError(400, "'port' must be an integer") from None
            if not 1 <= port <= 65535:
                raise _RequestError(400, "'port' must be between 1 and 65535")

        params: dict[str, Any] = {"port": port}
        if mode == "server":
            source = str(data.get("source") or "")
            if not source:
                raise _RequestError(400, "'source' (file or folder path) is required")
            if not (os.path.isfile(source) or os.path.isdir(source)):
                raise _RequestError(400, f"source does not exist: {source}")
            params["source"] = source
        else:
            host = data.get("host")
            if not host:
                raise _RequestError(400, "'host' is required")
            conflict = str(data.get("conflict") or "ask")
            if conflict not in ("ask", "overwrite", "rename"):
                raise _RequestError(
                    400, "conflict must be 'ask', 'overwrite', or 'rename'"
                )
            params["host"] = str(host)
            params["conflict"] = conflict
            params["output_dir"] = str(data.get("output_dir") or ".")

        try:
            self.runner.start(mode, params)
        except RuntimeError as e:
            raise _RequestError(409, str(e)) from e
        self._send_json(200, {"ok": True, "mode": mode})

    def _handle_stop(self) -> None:
        data = self._read_json()
        mode = data.get("mode")
        if mode not in ("server", "client"):
            raise _RequestError(400, "mode must be 'server' or 'client'")
        self.state.request_stop(mode)
        self._send_json(200, {"ok": True})

    def _handle_answer(self) -> None:
        data = self._read_json()
        mode = data.get("mode")
        prompt_id = data.get("prompt_id")
        if mode not in ("server", "client") or not prompt_id:
            raise _RequestError(400, "'mode' and 'prompt_id' are required")
        if not self.state.answer_prompt(
            mode, str(prompt_id), str(data.get("choice", ""))
        ):
            raise _RequestError(409, "no pending prompt with that id")
        self._send_json(200, {"ok": True})

    def _handle_settings(self) -> None:
        data = self._read_json()
        updates: dict[str, bool] = {}
        for key in ("broadcast", "listen"):
            if key in data:
                if not isinstance(data[key], bool):
                    raise _RequestError(400, f"'{key}' must be a boolean")
                updates[key] = data[key]
        if not updates:
            raise _RequestError(400, "no settings provided")
        self.state.set_settings(updates)
        if "listen" in updates:
            if updates["listen"]:
                self.discovery.start_listener()
            else:
                self.discovery.stop_listener()
        if "broadcast" in updates:
            self.discovery.set_broadcast_enabled(updates["broadcast"])
        self._send_json(
            200, {"ok": True, "settings": self.state.get_snapshot()["settings"]}
        )

    def _handle_upload(self) -> None:
        raw_length = self.headers.get("Content-Length")
        try:
            length = int(raw_length) if raw_length is not None else 0
        except ValueError:
            raise _RequestError(400, "bad Content-Length") from None
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            raise _RequestError(413, "upload too large")
        try:
            batch_dir, count = parse_upload(
                self._read_body(), self.headers.get("Content-Type", "")
            )
        except ValueError as e:
            raise _RequestError(400, str(e)) from e
        self.runner.register_upload(batch_dir)
        self._send_json(200, {"ok": True, "source": batch_dir, "count": count})

    def _handle_events(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        last = -1
        try:
            while True:
                # Re-sends the latest snapshot even when unchanged, which
                # doubles as the SSE heartbeat every 15 seconds.
                last = self.state.wait_for_change(last, 15.0)
                payload = json.dumps(self.state.get_snapshot())
                self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def _serve_static(self, path: str) -> None:
        rel = path.lstrip("/") or "index.html"
        base = _STATIC_DIR.resolve()
        target = (base / rel).resolve()
        if target != base and base not in target.parents:
            self._send_json(403, {"error": "forbidden"})
            return
        if not target.is_file():
            self._send_json(404, {"error": "not found"})
            return
        body = target.read_bytes()
        ctype = _CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _sweep_uploads() -> None:
    """Delete upload batches older than _UPLOAD_TTL (crashed runs, etc.)."""
    if not os.path.isdir(_UPLOAD_ROOT):
        return
    now = time.time()
    for name in os.listdir(_UPLOAD_ROOT):
        path = os.path.join(_UPLOAD_ROOT, name)
        try:
            if os.path.isdir(path) and now - os.path.getmtime(path) > _UPLOAD_TTL:
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


def create_server(host: str = "127.0.0.1", port: int = WEB_PORT) -> _Server:
    """Create (and bind) the web server; call ``serve_forever()`` on it."""
    _sweep_uploads()
    state = WebState()
    discovery = Discovery(state)
    runner = EngineRunner(state, discovery)
    return _Server((host, port), state, runner, discovery)
