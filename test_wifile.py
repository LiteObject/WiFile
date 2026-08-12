"""Tests for wifile's wire protocol and helpers.

Uses socket.socketpair() so no real network is involved. Covers the issues
raised in review: partial reads/writes, fragmented acknowledgements, hostile
(colon/newline) filenames, oversized headers, and receiver confirmation.
"""

import builtins
import os
import socket
import struct
import tempfile
import threading
import unittest
from unittest import mock

import wifile


class FrameProtocolTest(unittest.TestCase):
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
        left, right = socket.socketpair()
        left.close()
        with self.assertRaises(ConnectionError):
            wifile.recv_frame(right)
        right.close()


class CollectFilesTest(unittest.TestCase):
    def test_recursive_collection_preserves_relative_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "sub", "deep"))
            for rel in ["a.txt", "sub/b.txt", "sub/deep/c.txt"]:
                with open(os.path.join(tmp, rel), "w", encoding="utf-8") as f:
                    f.write(rel)
            files = wifile.collect_files(folder=tmp)
            names = sorted(name for name, _ in files)
            self.assertEqual(names, ["a.txt", "sub/b.txt", "sub/deep/c.txt"])


class FileTransferTest(unittest.TestCase):
    def setUp(self):
        self.send_result: bool | None = None
        self.send_error: Exception | None = None
        self.server_result: tuple[bytes, bytes] | None = None

    def _run_send(self, sock, name, path):
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
                def __init__(self, f):
                    self._f = f

                def write(self, chunk):
                    return self._f.write(chunk)

                def close(self):
                    self._f.close()
                    raise OSError("simulated close failure")

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    self.close()
                    return False

            def fake_open(path, mode, *args, **kwargs):
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


class SanitizePathTest(unittest.TestCase):
    def test_removes_parent_traversal(self):
        self.assertEqual(
            wifile.sanitize_relative_path("../../etc/passwd"),
            os.path.join("etc", "passwd"),
        )

    def test_normalizes_separators(self):
        self.assertEqual(
            wifile.sanitize_relative_path("a\\b\\c.txt"),
            os.path.join("a", "b", "c.txt"),
        )

    def test_absolute_path_is_relativized(self):
        self.assertEqual(
            wifile.sanitize_relative_path("/etc/passwd"),
            os.path.join("etc", "passwd"),
        )

    def test_empty_becomes_unnamed(self):
        self.assertEqual(wifile.sanitize_relative_path(""), "unnamed")
        self.assertEqual(wifile.sanitize_relative_path("/"), "unnamed")

    @unittest.skipUnless(os.name == "nt", "Windows drive handling")
    def test_drive_qualified_component_is_stripped(self):
        self.assertEqual(wifile.sanitize_relative_path("C:/outside.txt"), "outside.txt")
        self.assertEqual(
            wifile.sanitize_relative_path("C:\\outside.txt"), "outside.txt"
        )
        self.assertEqual(wifile.sanitize_relative_path("C:"), "unnamed")


if __name__ == "__main__":
    unittest.main()
