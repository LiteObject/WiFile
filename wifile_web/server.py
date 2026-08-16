"""HTTP backend for the WiFile web UI.

Serves the single-page app and a small JSON API, and runs the two engine
threads (sender and receiver) with a :class:`WebUI` adapter in front of the
shared :class:`WebState` store. Endpoints:

    GET  /            static SPA
    GET  /api/state   full snapshot as JSON
    GET  /api/events  Server-Sent Events stream of snapshots
    GET  /api/netinfo machine LAN IP addresses clients can connect to
    POST /api/start   start the server or client engine thread
    POST /api/upload  multipart upload of files to serve
    POST /api/answer  answer a pending prompt
    POST /api/stop    stop an engine thread
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import socket
import sys
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


def get_net_addresses() -> list[str]:
    """Return the machine's LAN IPv4 addresses, primary outbound first.

    The primary address (the interface used to reach the internet) is always
    listed first. On Windows, the remaining addresses come from
    GetAdaptersAddresses so virtual adapters (WSL, Hyper-V, Docker,
    Bluetooth) are excluded by adapter name; elsewhere the host name is used
    and well-known virtual ranges are filtered. Best effort: falls back to
    ``127.0.0.1`` when nothing can be determined.
    """
    addresses: list[str] = []
    primary = _primary_outbound_ip()
    if primary:
        addresses.append(primary)
    if sys.platform == "win32":
        try:
            real = _windows_adapter_addresses()
        except (OSError, AttributeError, ValueError):
            real = None
        if real is not None:
            for ip in real:
                if ip not in addresses:
                    addresses.append(ip)
            return addresses or ["127.0.0.1"]
    for ip in _hostname_addresses():
        if ip not in addresses:
            addresses.append(ip)
    return addresses or ["127.0.0.1"]


def _primary_outbound_ip() -> str | None:
    """Return the IP used to reach the internet, or None if unknown."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
    except OSError:
        return None


def _looks_virtual(ip: str) -> bool:
    """Return True for addresses typically found on virtual adapters.

    Matches link-local (169.254/16) and the 172.16/12 block used by many
    Docker, WSL, and Hyper-V virtual switches. The primary outbound address
    is exempted by the caller, so a real LAN on 172.x still shows up.
    """
    if ip.startswith("169.254."):
        return True
    parts = ip.split(".")
    return (
        len(parts) == 4
        and parts[0] == "172"
        and parts[1].isdigit()
        and 16 <= int(parts[1]) <= 31
    )


def _hostname_addresses() -> list[str]:
    """Return non-loopback IPv4 addresses the host name resolves to."""
    addresses: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if (
                not ip.startswith("127.")
                and not _looks_virtual(ip)
                and ip not in addresses
            ):
                addresses.append(ip)
    except OSError:
        pass
    return addresses


def _windows_adapter_addresses() -> list[str] | None:
    """Return IPv4 addresses of physical adapters via GetAdaptersAddresses.

    Returns None if the Win32 API is unavailable or fails, so callers can
    fall back to host-name resolution. Virtual adapters (WSL, Hyper-V,
    Docker, Bluetooth, loopback) are skipped by their friendly name.
    """
    from ctypes import wintypes

    class _Sockaddr(ctypes.Structure):
        """Minimal sockaddr: address family followed by address bytes."""

        _fields_ = [
            ("sa_family", ctypes.c_ushort),
            ("sa_data", ctypes.c_ubyte * 14),
        ]

    class _SocketAddress(ctypes.Structure):
        """SOCKET_ADDRESS: a sockaddr pointer plus its length."""

        _fields_ = [
            ("lpSockaddr", ctypes.POINTER(_Sockaddr)),
            ("iSockaddrLength", ctypes.c_int),
        ]

    class _Unicast(ctypes.Structure):
        """IP_ADAPTER_UNICAST_ADDRESS node (head fields only)."""

        _fields_ = [
            ("u", ctypes.c_ulonglong),  # covers the Length/Flags union
            ("Next", ctypes.c_void_p),
            ("Address", _SocketAddress),
        ]

    class _Adapters(ctypes.Structure):
        """IP_ADAPTER_ADDRESSES node up to the friendly name field."""

        _fields_ = [
            ("u", ctypes.c_ulonglong),  # covers the Length/IfIndex union
            ("Next", ctypes.c_void_p),
            ("AdapterName", ctypes.c_char_p),
            ("FirstUnicastAddress", ctypes.c_void_p),
            ("FirstAnycastAddress", ctypes.c_void_p),
            ("FirstMulticastAddress", ctypes.c_void_p),
            ("FirstDnsServerAddress", ctypes.c_void_p),
            ("DnsSuffix", ctypes.c_wchar_p),
            ("Description", ctypes.c_wchar_p),
            ("FriendlyName", ctypes.c_wchar_p),
        ]

    get_adapters = ctypes.windll.iphlpapi.GetAdaptersAddresses
    get_adapters.restype = wintypes.DWORD
    get_adapters.argtypes = [
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.POINTER(_Adapters),
        ctypes.POINTER(wintypes.DWORD),
    ]

    size = wintypes.DWORD(0)
    get_adapters(socket.AF_INET, 0, None, None, ctypes.byref(size))
    if size.value == 0:
        return []
    buf = ctypes.create_string_buffer(size.value)
    result = get_adapters(
        socket.AF_INET,
        0,
        None,
        ctypes.cast(buf, ctypes.POINTER(_Adapters)),
        ctypes.byref(size),
    )
    if result != 0:  # ERROR_BUFFER_OVERFLOW: retry once with the new size
        buf = ctypes.create_string_buffer(size.value or 15000)
        result = get_adapters(
            socket.AF_INET,
            0,
            None,
            ctypes.cast(buf, ctypes.POINTER(_Adapters)),
            ctypes.byref(size),
        )
        if result != 0:
            return None

    virtual = ("vEthernet", "WSL", "Hyper-V", "Docker", "Loopback", "Bluetooth")
    addresses: list[str] = []
    node = ctypes.cast(buf, ctypes.POINTER(_Adapters))
    while node:
        adapter = node.contents
        name = adapter.FriendlyName or ""
        if not any(token.lower() in name.lower() for token in virtual):
            unicast = ctypes.cast(adapter.FirstUnicastAddress, ctypes.POINTER(_Unicast))
            while unicast:
                entry = unicast.contents
                sockaddr = entry.Address.lpSockaddr
                if sockaddr and sockaddr.contents.sa_family == socket.AF_INET:
                    # sockaddr_in: sa_data[0:2] port, sa_data[2:6] address.
                    ip = socket.inet_ntoa(bytes(sockaddr.contents.sa_data[2:6]))
                    if (
                        not ip.startswith("127.")
                        and not _looks_virtual(ip)
                        and ip not in addresses
                    ):
                        addresses.append(ip)
                unicast = ctypes.cast(entry.Next, ctypes.POINTER(_Unicast))
        node = ctypes.cast(adapter.Next, ctypes.POINTER(_Adapters))
    return addresses


class _RequestError(Exception):
    """An HTTP error carrying a status code to send back to the client."""

    def __init__(self, status: int, message: str) -> None:
        """Store the status code and message for the error response."""
        super().__init__(message)
        self.status = status
        self.message = message


class EngineRunner:
    """Starts/stops the sender and receiver engine threads."""

    def __init__(self, state: WebState) -> None:
        """Track engine threads and upload batches for the shared state."""
        self._state = state
        self._threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._upload_batches: set[str] = set()

    def start(self, mode: str, params: dict[str, Any]) -> None:
        """Start the server or client engine thread, rejecting double starts."""
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
        """Track an upload batch so it is cleaned up when the sender stops."""
        with self._lock:
            self._upload_batches.add(batch_dir)

    # -- engine entry points --------------------------------------------------

    def _run_server(self, mode: str, params: dict[str, Any]) -> None:
        """Run the sender engine, logging failures into the state store."""
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
            wifile.start_server(
                port, filepath=filepath, folder=folder, ui=WebUI(self._state, mode)
            )
        except (OSError, ValueError) as e:
            self._state.log(mode, f"Sender failed to start: {e}")
        finally:
            self._state.set_running(mode, False)
            self._state.clear_progress(mode)
            self._cleanup_uploads()

    def _run_client(self, mode: str, params: dict[str, Any]) -> None:
        """Run the receiver engine, logging failures into the state store."""
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
    """Threaded HTTP server that carries the shared state and engine runner."""

    daemon_threads = True

    def __init__(
        self, address: tuple[str, int], state: WebState, runner: EngineRunner
    ) -> None:
        """Attach the state and runner, then bind the server."""
        self.web_state = state
        self.runner = runner
        super().__init__(address, _Handler)


class _Handler(BaseHTTPRequestHandler):
    """Serves the SPA and the JSON/SSE API against the shared WebState."""

    protocol_version = "HTTP/1.1"
    server_version = "WiFileWeb/0.1"
    close_connection: bool

    @property
    def state(self) -> WebState:
        """The shared WebState attached to the server."""
        return self.server.web_state  # type: ignore[attr-defined]

    @property
    def runner(self) -> EngineRunner:
        """The EngineRunner attached to the server."""
        return self.server.runner  # type: ignore[attr-defined]

    def log_message(self, *args: Any) -> None:
        """Suppress default access logs; state changes go to the UI instead."""

    # -- helpers ---------------------------------------------------------------

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        """Send a JSON response with the given status code."""
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> bytes:
        """Read the request body up to the declared Content-Length."""
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
        """Read and parse the request body as a JSON object."""
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

    def do_GET(  # noqa: N802 (http.server API)  # pylint: disable=invalid-name
        self,
    ) -> None:
        """Route GET requests to the API, events stream, or static files."""
        try:
            path = self.path.split("?", 1)[0]
            if path == "/api/state":
                self._send_json(200, self.state.get_snapshot())
            elif path == "/api/events":
                self._handle_events()
            elif path == "/api/netinfo":
                self._handle_netinfo()
            else:
                self._serve_static(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except _RequestError as e:
            self._send_json(e.status, {"error": e.message})
        except (OSError, ValueError) as e:
            self._send_json(500, {"error": str(e)})

    def do_POST(self) -> None:  # noqa: N802  # pylint: disable=invalid-name
        """Route POST requests to the API endpoints."""
        try:
            path = self.path.split("?", 1)[0]
            if path == "/api/start":
                self._handle_start()
            elif path == "/api/stop":
                self._handle_stop()
            elif path == "/api/answer":
                self._handle_answer()
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
        """Validate and start a server or client engine."""
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
        """Request a stop for a server or client engine."""
        data = self._read_json()
        mode = data.get("mode")
        if mode not in ("server", "client"):
            raise _RequestError(400, "mode must be 'server' or 'client'")
        self.state.request_stop(mode)
        self._send_json(200, {"ok": True})

    def _handle_answer(self) -> None:
        """Deliver an answer to a pending prompt."""
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

    def _handle_upload(self) -> None:
        """Accept a multipart upload and register the resulting batch."""
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

    def _handle_netinfo(self) -> None:
        """Report the machine's LAN addresses so clients know what to enter."""
        self._send_json(200, {"addresses": get_net_addresses()})

    def _handle_events(self) -> None:
        """Stream snapshot JSON as Server-Sent Events with a heartbeat."""
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
        """Serve a static file, guarding against path traversal."""
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
    runner = EngineRunner(state)
    return _Server((host, port), state, runner)
