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
from wifile_web.adapter import WebUI
from wifile_web.server import create_server, parse_upload
from wifile_web.state import WebState


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class RecordingUI:
    """Minimal UI implementation used by tests to drive wifile directly."""

    def __init__(self) -> None:
        self.messages: list[str] = []

    def message(self, text: str) -> None:
        self.messages.append(text)

    def progress(self, current, total, start_time=None) -> None:
        pass

    def choose(self, prompt, options, default, invalid) -> str:
        del prompt, invalid
        if "e" in options:
            return "e"
        if "cancel" in options:
            return "cancel"
        return options[0] if options else default

    def ask_text(self, prompt: str) -> str:
        raise EOFError

    def should_stop(self) -> bool:
        return False


class StateStoreTest(unittest.TestCase):
    def test_log_is_bounded(self):
        state = WebState(max_log_lines=3)
        for i in range(5):
            state.log("server", f"line {i}")
        snap = state.get_snapshot()
        self.assertEqual(snap["server"]["log"], ["line 2", "line 3", "line 4"])

    def test_prompt_create_answer_and_clear(self):
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
        state = WebState()
        reply: queue.Queue = queue.Queue(maxsize=1)
        state.create_prompt("client", "choose", "Pick", ["a"], "a", reply)
        self.assertFalse(state.answer_prompt("client", "nope", "a"))
        self.assertFalse(state.answer_prompt("server", "nope", "a"))

    def test_request_stop_unblocks_pending_prompt(self):
        state = WebState()
        reply: queue.Queue = queue.Queue(maxsize=1)
        state.create_prompt("server", "choose", "Next?", ["s", "n", "e"], "s", reply)
        state.request_stop("server")
        self.assertEqual(reply.get(timeout=1), ("answer", "e"))
        self.assertTrue(state.is_stop_requested("server"))
        self.assertIsNone(state.get_snapshot()["server"]["prompt"])

    def test_version_bumps_on_mutation(self):
        state = WebState()
        before = state.get_snapshot()["version"]
        state.log("client", "hello")
        self.assertGreater(state.get_snapshot()["version"], before)


class AdapterTest(unittest.TestCase):
    def setUp(self):
        self.state = WebState()

    def test_message_appends_log(self):
        WebUI(self.state, "server").message("hello")
        self.assertEqual(self.state.get_snapshot()["server"]["log"], ["hello"])

    def test_choose_blocks_until_answered(self):
        ui = WebUI(self.state, "server")
        result: dict[str, str] = {}

        def ask():
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
        ui = WebUI(self.state, "server")
        with mock.patch("wifile_web.adapter.PROMPT_TIMEOUT", 0.05):
            choice = ui.choose("Action?", ["o", "r", "c", "cancel"], "o", "nope")
        self.assertEqual(choice, "cancel")
        self.assertIsNone(self.state.get_snapshot()["server"]["prompt"])

    def test_ask_text_timeout_raises_eof(self):
        ui = WebUI(self.state, "server")
        with mock.patch("wifile_web.adapter.PROMPT_TIMEOUT", 0.05):
            with self.assertRaises(EOFError):
                ui.ask_text("Where?")
        self.assertIsNone(self.state.get_snapshot()["server"]["prompt"])

    def test_progress_final_update_persists(self):
        ui = WebUI(self.state, "server")
        ui.progress(2048, 2048, start_time=time.time())
        progress = self.state.get_snapshot()["server"]["progress"]
        self.assertIsNotNone(progress)
        self.assertEqual(progress["percent"], 100.0)

    def test_progress_is_throttled_between_updates(self):
        ui = WebUI(self.state, "server")
        ui.progress(1024, 4096, start_time=time.time())
        ui.progress(2048, 4096, start_time=time.time())  # too soon: dropped
        progress = self.state.get_snapshot()["server"]["progress"]
        self.assertEqual(progress["current"], 1024)


class UploadTest(unittest.TestCase):
    def _multipart(self, boundary: str, name: str, content: bytes) -> bytes:
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
        with self.assertRaises(ValueError):
            parse_upload(b"raw", "text/plain")


class ServerApiTest(unittest.TestCase):
    def setUp(self):
        self.httpd = create_server("127.0.0.1", 0)
        self.port = self.httpd.server_address[1]
        self.state = self.httpd.web_state
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(5)

    # -- helpers -------------------------------------------------------------

    def _request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def _post_json(self, path, payload):
        return self._request(
            "POST", path, json.dumps(payload), {"Content-Type": "application/json"}
        )

    def _wait_for(self, predicate, timeout=5.0):
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
        status, _, raw = self._request("GET", "/api/state")
        self.assertEqual(status, 200)
        data = json.loads(raw)
        self.assertIn("version", data)
        for key in ("server", "client"):
            slot = data[key]
            for field in ("running", "progress", "prompt", "log"):
                self.assertIn(field, slot)

    def test_index_is_served(self):
        status, headers, raw = self._request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers.get("Content-Type", ""))
        self.assertIn(b"WiFile", raw)

    def test_start_requires_valid_mode(self):
        status, _, _ = self._post_json("/api/start", {"mode": "bogus"})
        self.assertEqual(status, 400)

    def test_start_server_requires_source(self):
        status, _, _ = self._post_json(
            "/api/start", {"mode": "server", "port": free_port()}
        )
        self.assertEqual(status, 400)

    def test_start_server_rejects_nonexistent_source(self):
        status, _, raw = self._post_json(
            "/api/start",
            {"mode": "server", "port": free_port(), "source": "/nonexistent/xyz"},
        )
        self.assertEqual(status, 400)
        self.assertIn(b"does not exist", raw)

    def test_start_client_requires_host(self):
        status, _, _ = self._post_json("/api/start", {"mode": "client"})
        self.assertEqual(status, 400)

    def test_client_prompt_answer_flow(self):
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


if __name__ == "__main__":
    unittest.main()
