"""Tests for wifile's wire protocol and helpers.

Uses socket.socketpair() so no real network is involved. Covers the issues
raised in review: partial reads/writes, fragmented acknowledgements, hostile
(colon/newline) filenames, oversized headers, and receiver confirmation.
"""

import os
import socket
import struct
import tempfile
import threading
import unittest

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

    def _run_send(self, sock, name, path):
        try:
            self.send_result = wifile.send_one_file(sock, name, path)
        except (OSError, ValueError) as e:  # failure path
            self.send_error = e

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


if __name__ == "__main__":
    unittest.main()
