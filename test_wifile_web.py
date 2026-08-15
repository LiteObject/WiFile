"""Headless tests for the WiFile web backend (Phase 2).

Covers the state store, the WebUI adapter, multipart upload parsing, and the
JSON/SSE API over a real loopback HTTP connection (http.client + raw socket).
No browser or GUI is involved.
"""

import http.client
import json
import os
import queue
import shutil
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

import wifile
from webui import bind_server
from wifile_web.adapter import WebUI
from wifile_web.server import (
    _looks_virtual,
    create_server,
    get_net_addresses,
    parse_upload,
)
from wifile_web.state import WebState


def free_port() -> int:
    """Return a free TCP port by binding to port 0 on the loopback interface."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class RecordingUI:
    """Minimal UI implementation used by tests to drive wifile directly."""

    def __init__(self) -> None:
        """Initialize an empty message recorder."""
        self.messages: list[str] = []

    def message(self, text: str) -> None:
        """Record a UI message."""
        self.messages.append(text)

    def progress(self, current, total, start_time=None) -> None:
        """No-op progress callback; tests don't need progress output."""

    def choose(self, prompt, options, default, invalid) -> str:
        """Auto-pick an option: exit, cancel, or the first available."""
        del prompt, invalid
        if "e" in options:
            return "e"
        if "cancel" in options:
            return "cancel"
        return options[0] if options else default

    def ask_text(self, prompt: str) -> str:
        """Simulate a client that never provides free-form text."""
        raise EOFError

    def should_stop(self) -> bool:
        """Never request a stop in tests."""
        return False


class StateStoreTest(unittest.TestCase):
    """Tests for the WebState snapshot, log, prompt, and version handling."""

    def test_log_is_bounded(self):
        """The log keeps only the most recent max_log_lines entries."""
        state = WebState(max_log_lines=3)
        for i in range(5):
            state.log("server", f"line {i}")
        snap = state.get_snapshot()
        self.assertEqual(snap["server"]["log"], ["line 2", "line 3", "line 4"])

    def test_prompt_create_answer_and_clear(self):
        """Creating, answering, and clearing a prompt round-trips correctly."""
        state = WebState()
        reply: queue.Queue = queue.Queue(maxsize=1)
        prompt_id = state.create_prompt(
            "client", "choose", "Pick one", ["a", "b"], "a", reply
        )
        snap = state.get_snapshot()
        self.assertEqual(snap["client"]["prompt"]["options"], ["a", "b"])
        self.assertTrue(state.answer_prompt("client", prompt_id, "b"))
        self.assertEqual(reply.get(timeout=1), ("answer", "b"))
        self.assertIsNone(state.get_snapshot()["client"]["prompt"])

    def test_answer_with_wrong_id_is_rejected(self):
        """Answers with an unknown prompt id or mode are rejected."""
        state = WebState()
        reply: queue.Queue = queue.Queue(maxsize=1)
        state.create_prompt("client", "choose", "Pick", ["a"], "a", reply)
        self.assertFalse(state.answer_prompt("client", "nope", "a"))
        self.assertFalse(state.answer_prompt("server", "nope", "a"))

    def test_request_stop_unblocks_pending_prompt(self):
        """A stop request resolves a pending prompt with the exit choice."""
        state = WebState()
        reply: queue.Queue = queue.Queue(maxsize=1)
        state.create_prompt("server", "choose", "Next?", ["s", "n", "e"], "s", reply)
        state.request_stop("server")
        self.assertEqual(reply.get(timeout=1), ("answer", "e"))
        self.assertTrue(state.is_stop_requested("server"))
        self.assertIsNone(state.get_snapshot()["server"]["prompt"])

    def test_version_bumps_on_mutation(self):
        """Any mutation bumps the snapshot version."""
        state = WebState()
        before = state.get_snapshot()["version"]
        state.log("client", "hello")
        self.assertGreater(state.get_snapshot()["version"], before)


class AdapterTest(unittest.TestCase):
    """Tests for the WebUI adapter driving WebState."""

    def setUp(self):
        """Create a fresh WebState before each test."""
        self.state = WebState()

    def test_message_appends_log(self):
        """A UI message is appended to the mode's log."""
        WebUI(self.state, "server").message("hello")
        self.assertEqual(self.state.get_snapshot()["server"]["log"], ["hello"])

    def test_choose_blocks_until_answered(self):
        """choose blocks until another thread answers the prompt."""
        ui = WebUI(self.state, "server")
        result: dict[str, str] = {}

        def ask():
            """Run choose in a worker thread and store its answer."""
            result["value"] = ui.choose("Pick:", ["a", "b"], "a", "nope")

        thread = threading.Thread(target=ask)
        thread.start()
        answered = False
        deadline = time.time() + 2
        while time.time() < deadline:
            pending = self.state.get_snapshot()["server"]["prompt"]
            if pending is not None:
                self.state.answer_prompt("server", pending["id"], "b")
                answered = True
                break
            time.sleep(0.01)
        self.assertTrue(answered, "prompt never appeared")
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result.get("value"), "b")

    def test_choose_timeout_returns_cancel(self):
        """An unanswered choose prompt times out and returns cancel."""
        ui = WebUI(self.state, "server")
        with mock.patch("wifile_web.adapter.PROMPT_TIMEOUT", 0.05):
            choice = ui.choose("Action?", ["o", "r", "c", "cancel"], "o", "nope")
        self.assertEqual(choice, "cancel")
        self.assertIsNone(self.state.get_snapshot()["server"]["prompt"])

    def test_ask_text_timeout_raises_eof(self):
        """An unanswered ask_text prompt times out and raises EOFError."""
        ui = WebUI(self.state, "server")
        with mock.patch("wifile_web.adapter.PROMPT_TIMEOUT", 0.05):
            with self.assertRaises(EOFError):
                ui.ask_text("Where?")
        self.assertIsNone(self.state.get_snapshot()["server"]["prompt"])

    def test_progress_final_update_persists(self):
        """A final progress update is persisted with 100 percent."""
        ui = WebUI(self.state, "server")
        ui.progress(2048, 2048, start_time=time.time())
        progress = self.state.get_snapshot()["server"]["progress"]
        self.assertIsNotNone(progress)
        self.assertEqual(progress["percent"], 100.0)

    def test_progress_is_throttled_between_updates(self):
        """Rapid progress updates are throttled to the first value."""
        ui = WebUI(self.state, "server")
        ui.progress(1024, 4096, start_time=time.time())
        ui.progress(2048, 4096, start_time=time.time())  # too soon: dropped
        progress = self.state.get_snapshot()["server"]["progress"]
        self.assertEqual(progress["current"], 1024)


class UploadTest(unittest.TestCase):
    """Tests for multipart upload parsing."""

    def _multipart(self, boundary: str, name: str, content: bytes) -> bytes:
        """Build a single-file multipart body for testing."""
        return (
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"; filename="f.bin"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n"
            ).encode("utf-8")
            + content
            + f"\r\n--{boundary}--\r\n".encode("utf-8")
        )

    def test_multipart_roundtrip_with_subfolder(self):
        """Uploaded files land in subfolders relative to the batch dir."""
        boundary = "wifileboundary"
        body = self._multipart(boundary, "sub/note.txt", b"uploaded content")
        batch_dir, count = parse_upload(
            body, f"multipart/form-data; boundary={boundary}"
        )
        try:
            self.assertEqual(count, 1)
            path = os.path.join(batch_dir, "sub", "note.txt")
            self.assertTrue(os.path.isfile(path))
            with open(path, "rb") as f:
                self.assertEqual(f.read(), b"uploaded content")
        finally:
            shutil.rmtree(batch_dir, ignore_errors=True)

    def test_path_traversal_is_sanitized(self):
        """Traversal names in uploads are stripped to a safe filename."""
        boundary = "wifileboundary"
        body = self._multipart(boundary, "../../evil.txt", b"x")
        batch_dir, count = parse_upload(
            body, f"multipart/form-data; boundary={boundary}"
        )
        base = os.path.dirname(batch_dir)
        try:
            self.assertEqual(count, 1)
            self.assertTrue(os.path.isfile(os.path.join(batch_dir, "evil.txt")))
            self.assertEqual(
                [n for n in os.listdir(base) if n == "evil.txt"],
                [],
                "a traversal name escaped the upload directory",
            )
        finally:
            shutil.rmtree(batch_dir, ignore_errors=True)

    def test_non_multipart_is_rejected(self):
        """Non-multipart content is rejected with a ValueError."""
        with self.assertRaises(ValueError):
            parse_upload(b"raw", "text/plain")


class NetInfoTest(unittest.TestCase):
    """Tests for LAN address detection."""

    def test_get_net_addresses_returns_at_least_one_ipv4(self):
        """get_net_addresses always returns a usable IPv4 address."""
        addresses = get_net_addresses()
        self.assertIsInstance(addresses, list)
        self.assertTrue(addresses)
        for ip in addresses:
            self.assertRegex(ip, r"^\d+\.\d+\.\d+\.\d+$")

    def test_looks_virtual_filters_known_ranges(self):
        """The heuristic drops link-local and 172.16/12 virtual ranges."""
        self.assertTrue(_looks_virtual("169.254.1.1"))
        self.assertTrue(_looks_virtual("172.17.0.1"))
        self.assertTrue(_looks_virtual("172.30.16.1"))
        self.assertFalse(_looks_virtual("192.168.7.123"))
        self.assertFalse(_looks_virtual("10.0.0.5"))
        self.assertFalse(_looks_virtual("127.0.0.1"))


class ServerApiTest(unittest.TestCase):
    """End-to-end tests over a real loopback HTTP server."""

    def setUp(self):
        """Start a loopback HTTP server and its serving thread."""
        self.httpd = create_server("127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.state = self.httpd.web_state
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        """Shut down the HTTP server and join the serving thread."""
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)

    # -- helpers -------------------------------------------------------------

    def _request(self, method, path, body=None, headers=None):
        """Send an HTTP request and return (status, headers, body)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def _post_json(self, path, payload):
        """POST a JSON payload and return (status, headers, body)."""
        return self._request(
            "POST", path, json.dumps(payload), {"Content-Type": "application/json"}
        )

    def _wait_for(self, predicate, timeout=5.0):
        """Poll the state snapshot until predicate matches or timeout."""
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            last = self.state.get_snapshot()
            if predicate(last):
                return last
            time.sleep(0.05)
        self.fail(f"condition not met within {timeout}s; snapshot: {last}")

    # -- tests -----------------------------------------------------------------

    def test_state_endpoint_shape(self):
        """GET /api/state returns the full snapshot shape."""
        status, _, raw = self._request("GET", "/api/state")
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertIn("version", data)
        for key in ("server", "client"):
            slot = data[key]
            for field in ("running", "progress", "prompt", "log"):
                self.assertIn(field, slot)

    def test_netinfo_endpoint(self):
        """GET /api/netinfo returns the machine's LAN addresses."""
        status, _, raw = self._request("GET", "/api/netinfo")
        self.assertEqual(status, 200)
        data = json.loads(raw)
        addresses = data.get("addresses")
        self.assertIsInstance(addresses, list)
        self.assertTrue(addresses)

    def test_index_is_served(self):
        """GET / serves the HTML index page."""
        status, headers, raw = self._request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn(b"WiFile", raw)

    def test_start_requires_valid_mode(self):
        """Starting with an unknown mode returns 400."""
        status, _, _ = self._post_json("/api/start", {"mode": "bogus"})
        self.assertEqual(status, 400)

    def test_start_server_requires_source(self):
        """Starting a server without a source returns 400."""
        status, _, _ = self._post_json(
            "/api/start", {"mode": "server", "port": free_port()}
        )
        self.assertEqual(status, 400)

    def test_start_server_rejects_nonexistent_source(self):
        """Starting a server with a missing source returns 400."""
        status, _, raw = self._post_json(
            "/api/start",
            {"mode": "server", "port": free_port(), "source": "/nonexistent/xyz"},
        )
        self.assertEqual(status, 400)
        self.assertIn(b"does not exist", raw)

    def test_start_client_requires_host(self):
        """Starting a client without a host returns 400."""
        status, _, _ = self._post_json("/api/start", {"mode": "client"})
        self.assertEqual(status, 400)

    def test_client_prompt_answer_flow(self):
        """A client run surfaces a prompt that can be answered via the API."""
        with tempfile.TemporaryDirectory() as tmp:
            status, _, _ = self._post_json(
                "/api/start",
                {
                    "mode": "client",
                    "host": "127.0.0.1",
                    "port": free_port(),
                    "output_dir": tmp,
                    "conflict": "ask",
                },
            )
            self.assertEqual(status, 200)
            snap = self._wait_for(lambda s: s["client"]["prompt"] is not None)
            prompt = snap["client"]["prompt"]
            self.assertEqual(prompt["kind"], "choose")
            self.assertIn("e", prompt["options"])

            status, _, _ = self._post_json(
                "/api/answer",
                {"mode": "client", "prompt_id": prompt["id"], "choice": "e"},
            )
            self.assertEqual(status, 200)
            snap = self._wait_for(
                lambda s: not s["client"]["running"] and s["client"]["prompt"] is None
            )
            self.assertTrue(
                any("Client error" in line for line in snap["client"]["log"])
            )

    def test_double_start_is_rejected(self):
        """Starting twice while already running returns 409."""
        with tempfile.TemporaryDirectory() as tmp:
            payload = {
                "mode": "client",
                "host": "127.0.0.1",
                "port": free_port(),
                "output_dir": tmp,
            }
            status, _, _ = self._post_json("/api/start", payload)
            self.assertEqual(status, 200)
            status, _, _ = self._post_json("/api/start", payload)
            self.assertEqual(status, 409)
            self._post_json("/api/stop", {"mode": "client"})
            self._wait_for(lambda s: not s["client"]["running"])

    def test_upload_endpoint_roundtrip(self):
        """POST /api/upload stores files and returns their source dir."""
        boundary = "wifileboundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="sub/note.txt"; filename="note.txt"\r\n'
            "Content-Type: text/plain\r\n\r\n"
            "uploaded content"
            f"\r\n--{boundary}--\r\n"
        ).encode("utf-8")
        status, _, raw = self._request(
            "POST",
            "/api/upload",
            body,
            {"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )
        self.assertEqual(status, 200)
        data = json.loads(raw)
        path = os.path.join(data["source"], "sub", "note.txt")
        self.assertTrue(os.path.isfile(path))
        with open(path, "r", encoding="utf-8") as f:
            self.assertEqual(f.read(), "uploaded content")

    def test_loopback_transfer_end_to_end(self):
        """A full server/client transfer works over the loopback interface."""
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "hello.txt")
            with open(src, "w", encoding="utf-8") as f:
                f.write("hello from the web backend")

            transfer_port = free_port()
            status, _, _ = self._post_json(
                "/api/start", {"mode": "server", "port": transfer_port, "source": src}
            )
            self.assertEqual(status, 200)
            self._wait_for(
                lambda s: any("Server listening" in line for line in s["server"]["log"])
            )

            out = os.path.join(tmp, "out")
            recv_ui = RecordingUI()
            errors: list[BaseException] = []

            def receive():
                try:
                    wifile.start_client(
                        "127.0.0.1", transfer_port, out, True, False, ui=recv_ui
                    )
                except (OSError, ValueError) as e:  # pragma: no cover
                    errors.append(e)

            thread = threading.Thread(target=receive)
            thread.start()
            deadline = time.time() + 10
            while time.time() < deadline:
                if os.path.isfile(os.path.join(out, "hello.txt")):
                    break
                time.sleep(0.05)
            thread.join(5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])
            with open(os.path.join(out, "hello.txt"), "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "hello from the web backend")

            status, _, _ = self._post_json("/api/stop", {"mode": "server"})
            self.assertEqual(status, 200)
            self._wait_for(lambda s: not s["server"]["running"])

    def test_sse_streams_snapshot(self):
        """GET /api/events streams snapshot JSON over SSE."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            sock.sendall(
                b"GET /api/events HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                b"Accept: text/event-stream\r\n\r\n"
            )
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            self.assertIn(b"200", buf.split(b"\r\n", 1)[0])

            deadline = time.time() + 5
            while b"data: " not in buf and time.time() < deadline:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
            line = next(
                (l for l in buf.split(b"\r\n") if l.startswith(b"data: ")), None
            )
            self.assertIsNotNone(line, "no SSE event received")
            snapshot = json.loads(line[len(b"data: ") :])
            self.assertIn("version", snapshot)
        finally:
            sock.close()


class WebUiEntryTest(unittest.TestCase):
    """webui.py port handling: falling back when a port is taken."""

    def test_bind_server_uses_ephemeral_port(self):
        """bind_server with port 0 picks an ephemeral port without error."""
        server, error = bind_server("127.0.0.1", 0)
        try:
            self.assertIsNone(error)
            self.assertGreater(server.server_address[1], 0)
        finally:
            server.server_close()

    def test_bind_server_falls_back_when_port_taken(self):
        """bind_server falls back when the requested port is already taken."""
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        taken_port = blocker.getsockname()[1]
        try:
            server, error = bind_server("127.0.0.1", taken_port)
            try:
                self.assertIsNotNone(error)
                self.assertNotEqual(server.server_address[1], taken_port)
            finally:
                server.server_close()
        finally:
            blocker.close()


if __name__ == "__main__":
    unittest.main()
