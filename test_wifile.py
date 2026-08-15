"""Tests for wifile's wire protocol and helpers.

Uses socket.socketpair() so no real network is involved. Covers the issues
raised in review: partial reads/writes, fragmented acknowledgements, hostile
(colon/newline) filenames, oversized headers, and receiver confirmation.
"""

import builtins
import io
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

import wifile


class FrameProtocolTest(unittest.TestCase):
    """Tests for the framed wire protocol primitives."""

    def test_frame_roundtrip_with_hostile_payload(self):
        """Colons, newlines and binary bytes must survive framing intact."""
        left, right = socket.socketpair()
        try:
            payload = b"my:file\nwith\tcolons\x00\x01\xff"
            wifile.send_frame(left, wifile.KIND_FILE, payload)
            kind, received = wifile.recv_frame(right)
            self.assertEqual(kind, wifile.KIND_FILE)
            self.assertEqual(received, payload)
        finally:
            left.close()
            right.close()

    def test_recv_exact_reassembles_fragments(self):
        """Fixed-size reads must reassemble data sent in tiny fragments."""
        left, right = socket.socketpair()
        try:
            frame = wifile.KIND_ACK + struct.pack(">I", 4) + b"\x00\x01\x02\x03"
            for i in range(0, len(frame), 2):
                left.sendall(frame[i : i + 2])
            kind, payload = wifile.recv_frame(right)
            self.assertEqual(kind, wifile.KIND_ACK)
            self.assertEqual(payload, b"\x00\x01\x02\x03")
        finally:
            left.close()
            right.close()

    def test_oversized_frame_is_rejected(self):
        """A peer claiming a huge payload must not make us buffer it."""
        left, right = socket.socketpair()
        try:
            left.sendall(wifile.KIND_FILE + struct.pack(">I", 10 * 1024 * 1024))
            with self.assertRaises(ValueError):
                wifile.recv_frame(right, max_payload=64 * 1024)
        finally:
            left.close()
            right.close()

    def test_eof_raises_connection_error(self):
        """A closed peer during a frame read raises ConnectionError."""
        left, right = socket.socketpair()
        left.close()
        with self.assertRaises(ConnectionError):
            wifile.recv_frame(right)
        right.close()


class CollectFilesTest(unittest.TestCase):
    """Tests for recursively collecting files from a folder."""

    def test_recursive_collection_preserves_relative_paths(self):
        """Nested files keep their relative paths when collected."""
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "sub", "deep"))
            for rel in ["a.txt", "sub/b.txt", "sub/deep/c.txt"]:
                with open(os.path.join(tmp, rel), "w", encoding="utf-8") as f:
                    f.write(rel)
            files = wifile.collect_files(folder=tmp)
            names = sorted(name for name, _ in files)
            self.assertEqual(names, ["a.txt", "sub/b.txt", "sub/deep/c.txt"])


class FileTransferTest(unittest.TestCase):
    """Tests for single-file send/receive over a socketpair."""

    def setUp(self):
        """Reset result holders before each test."""
        self.send_result: bool | None = None
        self.send_error: Exception | None = None
        self.server_result: tuple[bytes, bytes] | None = None

    def _run_send(self, sock, name, path):
        """Call send_one_file in a thread, capturing success or error."""
        try:
            self.send_result = wifile.send_one_file(sock, name, path)
        except (OSError, ValueError) as e:  # failure path
            self.send_error = e

    def _run_simple_server(
        self, sock, name, content, declared_size=None, close_after_send=False
    ):
        """Minimal server: header -> ACK -> content -> read result."""
        try:
            size = len(content) if declared_size is None else declared_size
            payload = name.encode("utf-8") + b"\x00" + struct.pack(">Q", size)
            wifile.send_frame(sock, wifile.KIND_FILE, payload)
            wifile.recv_frame(sock)  # ACK
            sock.sendall(content)
            if close_after_send:
                sock.close()
                return
            self.server_result = wifile.recv_frame(sock)
        except (OSError, ValueError):
            pass

    def _client_receive(self, sock, expected_size):
        """Play the client side: frame -> ACK -> read content -> RESULT ok."""
        kind, payload = wifile.recv_frame(sock)
        self.assertEqual(kind, wifile.KIND_FILE)
        _, size = payload.split(b"\x00", 1)
        self.assertEqual(struct.unpack(">Q", size)[0], expected_size)

        wifile.send_frame(sock, wifile.KIND_ACK)

        received = bytearray()
        remaining = expected_size
        while remaining > 0:
            chunk = sock.recv(min(1024, remaining))
            self.assertTrue(chunk, "server closed before sending all content")
            received.extend(chunk)
            remaining -= len(chunk)

        wifile.send_frame(sock, wifile.KIND_RESULT, b"\x00")
        return bytes(received)

    def test_single_file_transfer_integrity(self):
        """200KB of random data must arrive byte-for-byte intact."""
        left, right = socket.socketpair()
        data = os.urandom(200_000)
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "payload.bin")
            with open(src, "wb") as f:
                f.write(data)
            t = threading.Thread(target=self._run_send, args=(left, "payload.bin", src))
            t.start()
            received = self._client_receive(right, len(data))
            t.join()

            self.assertEqual(received, data)
            self.assertIsNone(getattr(self, "send_error", None))
            self.assertTrue(self.send_result)
        left.close()
        right.close()

    def test_declined_transfer_stops_sender(self):
        """Client cancels before ACK; the server must not report success."""
        left, right = socket.socketpair()
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "x.txt")
            with open(src, "w", encoding="utf-8") as f:
                f.write("hello")

            t = threading.Thread(target=self._run_send, args=(left, "x.txt", src))
            t.start()

            kind, _ = wifile.recv_frame(right)
            self.assertEqual(kind, wifile.KIND_FILE)
            wifile.send_frame(right, wifile.KIND_RESULT, b"\x01")

            t.join()
            self.assertIsNone(getattr(self, "send_error", None))
            self.assertFalse(self.send_result)
        left.close()
        right.close()

    def test_receiver_failure_is_reported_to_sender(self):
        """Sender must learn about a receiver-side write failure."""
        left, right = socket.socketpair()
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "x.txt")
            with open(src, "w", encoding="utf-8") as f:
                f.write("hello")

            t = threading.Thread(target=self._run_send, args=(left, "x.txt", src))
            t.start()

            kind, payload = wifile.recv_frame(right)
            self.assertEqual(kind, wifile.KIND_FILE)
            _, size_bytes = payload.split(b"\x00", 1)
            size = struct.unpack(">Q", size_bytes)[0]

            wifile.send_frame(right, wifile.KIND_ACK)
            # Drain exactly the file content, then report failure.
            remaining = size
            while remaining > 0:
                chunk = right.recv(min(1024, remaining))
                self.assertTrue(chunk, "server closed before sending all content")
                remaining -= len(chunk)
            wifile.send_frame(right, wifile.KIND_RESULT, b"\x01")

            t.join()
            self.assertIsNone(getattr(self, "send_error", None))
            self.assertFalse(self.send_result)
        left.close()
        right.close()

    def test_receive_one_file_success(self):
        """receive_one_file writes the final file and reports success."""
        left, right = socket.socketpair()
        data = os.urandom(5000)
        with tempfile.TemporaryDirectory() as tmp:
            t = threading.Thread(
                target=self._run_simple_server, args=(left, "ok.bin", data)
            )
            t.start()
            kind, header_payload = wifile.recv_frame(right)
            self.assertEqual(kind, wifile.KIND_FILE)
            result = wifile.receive_one_file(right, tmp, True, False, header_payload)
            t.join()

            self.assertEqual(result, "file")
            with open(os.path.join(tmp, "ok.bin"), "rb") as f:
                self.assertEqual(f.read(), data)
            self.assertEqual(self.server_result, (wifile.KIND_RESULT, b"\x00"))
            leftovers = [n for n in os.listdir(tmp) if ".wifile-part" in n]
            self.assertEqual(leftovers, [])
        left.close()
        right.close()

    def test_failed_transfer_does_not_clobber_existing_file(self):
        """A failed transfer must leave an existing destination intact."""
        left, right = socket.socketpair()
        with tempfile.TemporaryDirectory() as tmp:
            dest = os.path.join(tmp, "keep.txt")
            with open(dest, "w", encoding="utf-8") as f:
                f.write("ORIGINAL")

            t = threading.Thread(
                target=self._run_simple_server,
                args=(left, "keep.txt", b"he"),
                kwargs={"declared_size": 5, "close_after_send": True},
            )
            t.start()
            kind, header_payload = wifile.recv_frame(right)
            self.assertEqual(kind, wifile.KIND_FILE)
            result = wifile.receive_one_file(right, tmp, True, False, header_payload)
            t.join()

            self.assertEqual(result, "file")
            with open(dest, "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "ORIGINAL")
            leftovers = [n for n in os.listdir(tmp) if ".wifile-part" in n]
            self.assertEqual(leftovers, [])
        left.close()
        right.close()

    def test_close_failure_is_reported_as_error(self):
        """A flush/close failure after all bytes must not report success."""
        left, right = socket.socketpair()
        content = b"hello"
        with tempfile.TemporaryDirectory() as tmp:
            real_open = builtins.open

            class FailingFile:
                """A file wrapper whose close always raises OSError."""

                def __init__(self, f):
                    """Wrap the real file object."""
                    self._f = f

                def write(self, chunk):
                    """Write through to the wrapped file."""
                    return self._f.write(chunk)

                def close(self):
                    """Close the wrapped file, then raise a simulated failure."""
                    self._f.close()
                    raise OSError("simulated close failure")

                def __enter__(self):
                    """Return self as the context manager."""
                    return self

                def __exit__(self, *exc):
                    """Close on exit and propagate the failure."""
                    self.close()
                    return False

            def fake_open(path, mode, *args, **kwargs):
                """Wrap every open call with FailingFile."""
                return FailingFile(real_open(path, mode, *args, **kwargs))

            t = threading.Thread(
                target=self._run_simple_server, args=(left, "x.txt", content)
            )
            t.start()
            kind, header_payload = wifile.recv_frame(right)
            self.assertEqual(kind, wifile.KIND_FILE)
            with mock.patch("builtins.open", new=fake_open):
                result = wifile.receive_one_file(
                    right, tmp, True, False, header_payload
                )
            t.join()

            self.assertEqual(result, "file")
            self.assertEqual(self.server_result, (wifile.KIND_RESULT, b"\x01"))
            leftovers = [n for n in os.listdir(tmp) if ".wifile-part" in n]
            self.assertEqual(leftovers, [])
        left.close()
        right.close()

    def test_silent_client_is_bounded_by_decision_timeout(self):
        """A client that never answers must not hold the sender forever."""
        left, right = socket.socketpair()
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "x.txt")
            with open(src, "w", encoding="utf-8") as f:
                f.write("hello")

            with mock.patch.object(wifile, "DECISION_TIMEOUT", 0.2):
                t = threading.Thread(target=self._run_send, args=(left, "x.txt", src))
                t.start()
                # Read the header, then never respond.
                kind, _ = wifile.recv_frame(right)
                self.assertEqual(kind, wifile.KIND_FILE)
                t.join(timeout=5)

            self.assertFalse(t.is_alive())
            self.assertIsNone(getattr(self, "send_error", None))
            self.assertFalse(self.send_result)
        left.close()
        right.close()


class SanitizePathTest(unittest.TestCase):
    """Tests for sanitize_relative_path keeping names inside output_dir."""

    def test_removes_parent_traversal(self):
        """Parent traversal components are stripped."""
        self.assertEqual(
            wifile.sanitize_relative_path("../../etc/passwd"),
            os.path.join("etc", "passwd"),
        )

    def test_normalizes_separators(self):
        """Backslash separators are normalized to native ones."""
        self.assertEqual(
            wifile.sanitize_relative_path("a\\b\\c.txt"),
            os.path.join("a", "b", "c.txt"),
        )

    def test_absolute_path_is_relativized(self):
        """Absolute paths are converted to relative names."""
        self.assertEqual(
            wifile.sanitize_relative_path("/etc/passwd"),
            os.path.join("etc", "passwd"),
        )

    def test_empty_becomes_unnamed(self):
        """Empty or root-only paths fall back to 'unnamed'."""
        self.assertEqual(wifile.sanitize_relative_path(""), "unnamed")
        self.assertEqual(wifile.sanitize_relative_path("/"), "unnamed")

    @unittest.skipUnless(os.name == "nt", "Windows drive handling")
    def test_drive_qualified_component_is_stripped(self):
        """A leading drive-qualified component is stripped."""
        self.assertEqual(wifile.sanitize_relative_path("C:/outside.txt"), "outside.txt")
        self.assertEqual(
            wifile.sanitize_relative_path("C:\\outside.txt"), "outside.txt"
        )
        self.assertEqual(wifile.sanitize_relative_path("C:"), "unnamed")

    @unittest.skipUnless(os.name == "nt", "Windows UNC handling")
    def test_unc_prefix_is_confined(self):
        """A UNC path must be relativized so it cannot escape output_dir."""
        result = wifile.sanitize_relative_path("\\\\server\\share\\evil.txt")
        self.assertEqual(result, os.path.join("server", "share", "evil.txt"))

    @unittest.skipUnless(os.name == "nt", "Windows drive handling")
    def test_drive_relative_name_is_confined(self):
        """A drive-relative name like C:foo must not escape output_dir."""
        self.assertEqual(wifile.sanitize_relative_path("C:foo.txt"), "foo.txt")

    @unittest.skipUnless(os.name == "nt", "Windows drive handling")
    def test_nested_drive_component_is_stripped(self):
        """A drive-qualified component anywhere must not survive."""
        self.assertEqual(
            wifile.sanitize_relative_path("foo/C:/sub/evil.txt"),
            os.path.join("foo", "sub", "evil.txt"),
        )
        self.assertEqual(
            wifile.sanitize_relative_path("foo/C:sub/evil.txt"),
            os.path.join("foo", "sub", "evil.txt"),
        )
        self.assertEqual(
            wifile.sanitize_relative_path("foo/C:/evil.txt"),
            os.path.join("foo", "evil.txt"),
        )


class PromptNextTargetTest(unittest.TestCase):
    """Persistent server: choosing the same target, switching, or quitting."""

    def test_same_target_returns_current(self):
        """Choosing 's' keeps serving the current target."""
        files = [("a.txt", "/abs/a.txt")]
        with mock.patch("builtins.input", return_value="s"):
            result = wifile.prompt_next_target(files, False, "/abs/a.txt")
        self.assertEqual(result, (files, False, "/abs/a.txt"))

    def test_exit_returns_none(self):
        """Choosing 'e' stops the persistent server."""
        with mock.patch("builtins.input", return_value="e"):
            result = wifile.prompt_next_target([("a.txt", "/a")], False, "/a")
        self.assertIsNone(result)

    def test_switch_to_new_file(self):
        """Choosing 'n' and a file path serves that file next."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "new.txt")
            with open(path, "w", encoding="utf-8") as f:
                f.write("x")
            with mock.patch("builtins.input", side_effect=["n", path]):
                files, batch_mode, source = wifile.prompt_next_target(
                    [("a.txt", "/a")], False, "/a"
                )
            self.assertFalse(batch_mode)
            self.assertEqual(source, path)
            self.assertEqual(files, [(os.path.basename(path), path)])

    def test_switch_to_new_folder(self):
        """Choosing 'n' and a folder serves that folder as a batch."""
        with tempfile.TemporaryDirectory() as tmp:
            sub = os.path.join(tmp, "sub")
            os.makedirs(sub)
            fpath = os.path.join(sub, "f.txt")
            with open(fpath, "w", encoding="utf-8") as f:
                f.write("x")
            with mock.patch("builtins.input", side_effect=["n", sub]):
                files, batch_mode, source = wifile.prompt_next_target(
                    [("a.txt", "/a")], False, "/a"
                )
            self.assertTrue(batch_mode)
            self.assertEqual(source, sub)
            self.assertEqual(files, [("f.txt", fpath)])

    def test_invalid_path_repeats_prompt(self):
        """A missing path must not crash; the prompt loops until a valid input."""
        with mock.patch("builtins.input", side_effect=["n", "/nonexistent/xyz", "e"]):
            result = wifile.prompt_next_target([("a.txt", "/a")], False, "/a")
        self.assertIsNone(result)

    def test_invalid_choice_repeats_prompt(self):
        """An invalid choice re-prompts until a valid one is given."""
        with mock.patch("builtins.input", side_effect=["x", "e"]):
            result = wifile.prompt_next_target([("a.txt", "/a")], False, "/a")
        self.assertIsNone(result)

    def test_eof_returns_none(self):
        """EOF (Ctrl+D) must end the persistent server cleanly."""
        with mock.patch("builtins.input", side_effect=EOFError):
            result = wifile.prompt_next_target([("a.txt", "/a")], False, "/a")
        self.assertIsNone(result)


class PromptNextOutputTest(unittest.TestCase):
    """Persistent client: keeping, switching, or quitting the output dir."""

    def test_continue_returns_same_dir(self):
        """Choosing 'c' keeps the current output directory."""
        with mock.patch("builtins.input", return_value="c"):
            result = wifile.prompt_next_output("/tmp/out")
        self.assertEqual(result, "/tmp/out")

    def test_exit_returns_none(self):
        """Choosing 'e' stops the persistent client."""
        with mock.patch("builtins.input", return_value="e"):
            result = wifile.prompt_next_output("/tmp/out")
        self.assertIsNone(result)

    def test_new_dir_is_created_and_returned(self):
        """Choosing 'n' and a path creates and returns that directory."""
        with tempfile.TemporaryDirectory() as tmp:
            new_dir = os.path.join(tmp, "newout")
            with mock.patch("builtins.input", side_effect=["n", new_dir]):
                result = wifile.prompt_next_output(tmp)
            self.assertEqual(result, new_dir)
            self.assertTrue(os.path.isdir(new_dir))

    def test_invalid_dir_path_does_not_crash(self):
        """A path that names an existing file must not raise FileExistsError."""
        with tempfile.TemporaryDirectory() as tmp:
            file_path = os.path.join(tmp, "afile")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("x")
            with mock.patch("builtins.input", side_effect=["n", file_path, "e"]):
                result = wifile.prompt_next_output(tmp)
            self.assertIsNone(result)

    def test_eof_returns_none(self):
        """EOF (Ctrl+D) must end the persistent client cleanly."""
        with mock.patch("builtins.input", side_effect=EOFError):
            result = wifile.prompt_next_output("/tmp/out")
        self.assertIsNone(result)


class BatchProtocolTest(unittest.TestCase):
    """The framed batch flow: KIND_BATCH -> per-file handshake -> KIND_DONE."""

    def test_batch_transfers_multiple_files_with_subfolders(self):
        """A full batch with nested files arrives intact."""
        left, right = socket.socketpair()
        with tempfile.TemporaryDirectory() as tmp:
            sources: dict[str, str] = {}
            data_by_name: dict[str, bytes] = {}
            for name in ("a.txt", "sub/b.bin"):
                abs_path = os.path.join(tmp, name)
                os.makedirs(os.path.dirname(abs_path), exist_ok=True)
                data = os.urandom(100)
                with open(abs_path, "wb") as f:
                    f.write(data)
                sources[name] = abs_path
                data_by_name[name] = data

            errors: list[str] = []

            def server():
                """Play the server side: batch frame, files, then done."""
                try:
                    wifile.send_frame(
                        left, wifile.KIND_BATCH, struct.pack(">I", len(sources))
                    )
                    for name, abs_path in sources.items():
                        if not wifile.send_one_file(left, name, abs_path):
                            errors.append(f"send_one_file failed for {name}")
                            return
                    wifile.send_frame(left, wifile.KIND_DONE)
                except (OSError, ValueError) as e:  # pragma: no cover
                    errors.append(repr(e))

            t = threading.Thread(target=server)
            t.start()

            out = os.path.join(tmp, "out")
            kind, payload = wifile.recv_frame(right)
            self.assertEqual(kind, wifile.KIND_BATCH)
            self.assertEqual(struct.unpack(">I", payload)[0], 2)
            for name in ("a.txt", "sub/b.bin"):
                kind, header = wifile.recv_frame(right)
                self.assertEqual(kind, wifile.KIND_FILE)
                result = wifile.receive_one_file(right, out, True, False, header)
                self.assertEqual(result, "file")
            kind, _ = wifile.recv_frame(right)
            self.assertEqual(kind, wifile.KIND_DONE)
            t.join()

            self.assertEqual(errors, [])
            for name, data in data_by_name.items():
                with open(os.path.join(out, name), "rb") as f:
                    self.assertEqual(f.read(), data)
        left.close()
        right.close()

    def test_cancel_mid_batch_stops_sender(self):
        """A decline during the second file must stop the whole batch."""
        left, right = socket.socketpair()
        with tempfile.TemporaryDirectory() as tmp:
            for name in ("one.txt", "two.txt"):
                with open(os.path.join(tmp, name), "w", encoding="utf-8") as f:
                    f.write("data")
            out = os.path.join(tmp, "out")
            os.makedirs(out)
            with open(os.path.join(out, "two.txt"), "w", encoding="utf-8") as f:
                f.write("existing")

            errors: list[str] = []

            def server():
                """Play the server side and close on cancellation."""
                try:
                    wifile.send_frame(left, wifile.KIND_BATCH, struct.pack(">I", 2))
                    wifile.send_one_file(left, "one.txt", os.path.join(tmp, "one.txt"))
                    wifile.send_one_file(left, "two.txt", os.path.join(tmp, "two.txt"))
                except (OSError, ValueError) as e:  # pragma: no cover
                    errors.append(repr(e))
                finally:
                    left.close()

            t = threading.Thread(target=server)
            t.start()

            kind, payload = wifile.recv_frame(right)
            self.assertEqual(kind, wifile.KIND_BATCH)
            self.assertEqual(struct.unpack(">I", payload)[0], 2)

            # First file transfers normally.
            kind, header = wifile.recv_frame(right)
            self.assertEqual(kind, wifile.KIND_FILE)
            result = wifile.receive_one_file(right, out, True, False, header)
            self.assertEqual(result, "file")

            # Second file already exists and the user cancels at the prompt.
            kind, header = wifile.recv_frame(right)
            self.assertEqual(kind, wifile.KIND_FILE)
            with mock.patch("builtins.input", return_value="c"):
                result = wifile.receive_one_file(right, out, False, False, header)
            self.assertEqual(result, "cancelled")

            t.join()
            self.assertEqual(errors, [])
            # The server stopped: no KIND_DONE, and it closed the connection.
            self.assertEqual(right.recv(1), b"")
            with open(os.path.join(out, "one.txt"), "rb") as f:
                self.assertEqual(f.read(), b"data")
            with open(os.path.join(out, "two.txt"), "r", encoding="utf-8") as f:
                self.assertEqual(f.read(), "existing")
        left.close()
        right.close()


class ServerMessageTest(unittest.TestCase):
    """Tests for the informational messages the server prints."""

    def test_custom_port_included_in_client_command(self):
        """A server on a custom port must tell clients to pass --port."""
        with tempfile.TemporaryDirectory() as tmp:
            fpath = os.path.join(tmp, "f.txt")
            with open(fpath, "w", encoding="utf-8") as f:
                f.write("hello")

            probe = socket.socket()
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
            probe.close()

            def client():
                """Play one client round against the running server."""
                c = socket.socket()
                c.settimeout(5)
                c.connect(("127.0.0.1", port))
                kind, header = wifile.recv_frame(c)
                self.assertEqual(kind, wifile.KIND_FILE)
                wifile.send_frame(c, wifile.KIND_ACK)
                _, size_bytes = header.split(b"\x00", 1)
                size = struct.unpack(">Q", size_bytes)[0]
                remaining = size
                while remaining > 0:
                    chunk = c.recv(min(1024, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                wifile.send_frame(c, wifile.KIND_RESULT, b"\x00")
                c.close()

            buf = io.StringIO()
            with mock.patch(
                "wifile.get_local_ip", return_value="192.168.1.50"
            ), mock.patch("builtins.input", return_value="e"), redirect_stdout(buf):
                # Run the server in a thread so the client can connect after
                # the listener is up (avoids a bind/connect race).
                server_thread = threading.Thread(
                    target=lambda: wifile.start_server(port, filepath=fpath)
                )
                server_thread.start()
                time.sleep(0.2)
                client()
                server_thread.join()

            output = buf.getvalue()
            self.assertIn("--port", output)
            self.assertIn(str(port), output)


class BatchValidationTest(unittest.TestCase):
    """A batch is only complete on KIND_DONE with a matching file count."""

    def _serve_one_file(self, left, name, content):
        """Server side of a single-file handshake on the given socket."""
        payload = name.encode("utf-8") + b"\x00" + struct.pack(">Q", len(content))
        wifile.send_frame(left, wifile.KIND_FILE, payload)
        wifile.recv_frame(left)  # ACK
        left.sendall(content)
        wifile.recv_frame(left)  # RESULT

    def test_successful_batch_reports_complete(self):
        """A complete batch is reported as 'Batch complete'."""
        left, right = socket.socketpair()
        with tempfile.TemporaryDirectory() as tmp:

            def server():
                """Serve one file then send KIND_DONE."""
                try:
                    self._serve_one_file(left, "a.txt", b"hi")
                    wifile.send_frame(left, wifile.KIND_DONE)
                except (OSError, ValueError):  # pragma: no cover
                    pass
                finally:
                    left.close()

            t = threading.Thread(target=server)
            t.start()
            buf = io.StringIO()
            with redirect_stdout(buf):
                wifile.receive_batch(right, tmp, True, False, struct.pack(">I", 1))
            t.join(timeout=5)
            self.assertFalse(t.is_alive())

            self.assertIn("Batch complete: 1 of 1", buf.getvalue())
            with open(os.path.join(tmp, "a.txt"), "rb") as f:
                self.assertEqual(f.read(), b"hi")
        left.close()
        right.close()

    def test_early_done_is_rejected(self):
        """KIND_DONE before the announced count must not report completion."""
        left, right = socket.socketpair()
        with tempfile.TemporaryDirectory() as tmp:
            wifile.send_frame(left, wifile.KIND_DONE)
            buf = io.StringIO()
            with redirect_stdout(buf):
                wifile.receive_batch(right, tmp, True, False, struct.pack(">I", 2))
            output = buf.getvalue()
            self.assertNotIn("Batch complete", output)
            self.assertIn("ended the batch early", output)
            self.assertIn("0 of 2", output)
        left.close()
        right.close()

    def test_unexpected_frame_kind_is_rejected(self):
        """A non-KIND_FILE/non-KIND_DONE frame must not report completion."""
        left, right = socket.socketpair()
        with tempfile.TemporaryDirectory() as tmp:
            wifile.send_frame(left, wifile.KIND_ACK)  # unexpected kind
            buf = io.StringIO()
            with redirect_stdout(buf):
                wifile.receive_batch(right, tmp, True, False, struct.pack(">I", 2))
            output = buf.getvalue()
            self.assertNotIn("Batch complete", output)
            self.assertIn("ended unexpectedly", output)
        left.close()
        right.close()

    def test_extra_file_beyond_announced_count_is_rejected(self):
        """More files than announced must not be accepted silently."""
        left, right = socket.socketpair()
        with tempfile.TemporaryDirectory() as tmp:

            def server():
                """Serve one file plus an unannounced second file."""
                try:
                    self._serve_one_file(left, "a.txt", b"hi")
                    # Second, unannounced file (the batch announced only 1).
                    payload = b"b.txt\x00" + struct.pack(">Q", 2)
                    wifile.send_frame(left, wifile.KIND_FILE, payload)
                except (OSError, ValueError):  # pragma: no cover
                    pass
                finally:
                    left.close()

            t = threading.Thread(target=server)
            t.start()
            buf = io.StringIO()
            with redirect_stdout(buf):
                wifile.receive_batch(right, tmp, True, False, struct.pack(">I", 1))
            t.join(timeout=5)
            self.assertFalse(t.is_alive())

            output = buf.getvalue()
            self.assertNotIn("Batch complete", output)
            self.assertIn("ended unexpectedly", output)
        left.close()
        right.close()


if __name__ == "__main__":
    unittest.main()
