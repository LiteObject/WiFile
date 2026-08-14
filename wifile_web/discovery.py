"""UDP broadcast discovery so senders appear in every receiver's web UI.

While the sender engine is running, this module broadcasts a small JSON
beacon onto the local network every couple of seconds. Every web UI process
also listens on the same UDP port and feeds discovered peers into the shared
:class:`WebState`, so the Receive pane can list nearby senders with a
one-click Connect button.

    - Pure standard library, like the rest of WiFile.
    - Discovery is opt-in: senders broadcast and receivers listen only
      after the user flips the corresponding toggle in the web UI.
    - Only *senders* announce, and only while they are actually serving.
    - The receiver derives the sender's address from the packet source
      address, so the host is always an address a connection can reach.
    - Peers expire a few seconds after their last beacon; a "bye" packet
      removes them immediately.
"""

from __future__ import annotations

import json
import platform
import socket
import sys
import threading
import time
import uuid
from typing import Any

from .state import WebState

APP_ID = "wifile"
PROTOCOL_VERSION = 1
DISCOVERY_PORT = 54321  # UDP; beacons and listeners share this port
ANNOUNCE_INTERVAL = 2.0  # seconds between beacons while serving
PEER_TTL = 8.0  # seconds without a beacon before a peer is dropped

_BROADCAST = "255.255.255.255"
_MAX_DATAGRAM = 4096


def ready_label(text: str) -> str | None:
    """Turn an engine "Ready to send ..." message into a short label."""
    prefix = "Ready to send "
    if not text.startswith(prefix):
        return None
    rest = text[len(prefix) :].strip()
    if rest.startswith("'"):
        end = rest.find("'", 1)
        if end != -1:
            return rest[1:end]
    suffix = "(one by one)"
    if rest.endswith(suffix):
        rest = rest[: -len(suffix)].strip()
    return rest


def _hostname() -> str:
    try:
        return socket.gethostname() or "wifile"
    except OSError:
        return "wifile"


class Discovery:
    """Announce our sender and listen for other WiFile senders.

    The listener runs for the whole process life and publishes peers into the
    shared WebState so the browser sees them over SSE. The announcer runs
    only while the sender engine is active.
    """

    def __init__(
        self,
        state: WebState,
        port: int = DISCOVERY_PORT,
        announce_interval: float = ANNOUNCE_INTERVAL,
        peer_ttl: float = PEER_TTL,
    ) -> None:
        self._state = state
        self._port = port
        self._announce_interval = announce_interval
        self._peer_ttl = peer_ttl
        self._instance_id = uuid.uuid4().hex
        self._hostname = _hostname()
        self._platform = platform.system().lower() or sys.platform
        self._web_port: int | None = None
        self._broadcast_enabled = False
        self._announce_info: dict[str, Any] | None = None
        self._listen_enabled = False
        self._lock = threading.Lock()
        self._peers: dict[str, dict[str, Any]] = {}  # id -> {"peer": ..., "seen": ...}
        self._announce: dict[str, Any] | None = None
        self._stop_announce = threading.Event()
        self._announce_thread: threading.Thread | None = None
        self._listener_thread: threading.Thread | None = None
        self._listener_sock: socket.socket | None = None
        self._closed = False

    # -- announcing (sender side) ---------------------------------------------

    def set_web_port(self, port: int | None) -> None:
        """Advertise the web UI port, or None if the UI is loopback-only."""
        self._web_port = port

    def configure_announce(
        self, port: int, source: str, target: str = _BROADCAST
    ) -> None:
        """Record what a running sender should announce.

        Beacons start only if broadcasting is enabled (the UI toggle); if
        broadcasting is off, the info is kept so the toggle can start it
        later.
        """
        with self._lock:
            self._announce_info = {
                "port": int(port),
                "source": source,
                "target": target,
            }
            enabled = self._broadcast_enabled
            active = self._announce is not None
            info = dict(self._announce_info)
        if enabled and not active:
            self.start_announcing(info["port"], info["source"], info["target"])

    def set_broadcast_enabled(self, enabled: bool) -> None:
        """Turn broadcasting on/off; applies to a running sender immediately."""
        with self._lock:
            self._broadcast_enabled = bool(enabled)
            info = dict(self._announce_info) if self._announce_info else None
            active = self._announce is not None
        if enabled and info is not None and not active:
            self.start_announcing(info["port"], info["source"], info["target"])
        elif not enabled and active:
            self._halt_announcer()

    def start_announcing(
        self, port: int, source: str, target: str = _BROADCAST
    ) -> None:
        """Start beacons immediately (the low-level primitive)."""
        with self._lock:
            self._announce_info = {
                "port": int(port),
                "source": source,
                "target": target,
            }
            if self._announce is not None and self._announce_thread is not None:
                self._announce["port"] = int(port)
                self._announce["source"] = source
                self._announce["target"] = target
                return
            self._stop_announce.clear()
            self._announce = {
                "port": int(port),
                "source": source,
                "target": target,
            }
            thread = threading.Thread(
                target=self._announce_loop,
                name="wifile-discovery-announce",
                daemon=True,
            )
            self._announce_thread = thread
        thread.start()

    def update_source(self, source: str) -> None:
        with self._lock:
            if self._announce_info is not None:
                self._announce_info["source"] = source
            if self._announce is not None:
                self._announce["source"] = source

    def stop_announcing(self) -> None:
        """Stop beacons and forget what was being announced."""
        with self._lock:
            self._announce_info = None
        self._halt_announcer()

    def _halt_announcer(self) -> None:
        with self._lock:
            info = self._announce
            self._announce = None
        self._stop_announce.set()
        if info is not None:
            self._send(info, bye=True)
        thread = self._announce_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _announce_loop(self) -> None:
        while not self._stop_announce.wait(self._announce_interval):
            with self._lock:
                info = dict(self._announce) if self._announce else None
            if info is not None:
                self._send(info)

    def _packet(self, info: dict[str, Any], bye: bool = False) -> bytes:
        payload = {
            "app": APP_ID,
            "v": PROTOCOL_VERSION,
            "id": self._instance_id,
            "name": self._hostname,
            "platform": self._platform,
            "port": info["port"],
            "source": info.get("source") or "",
            "web_port": self._web_port,
            "bye": bye,
        }
        return json.dumps(payload).encode("utf-8")

    def _send(self, info: dict[str, Any], bye: bool = False) -> None:
        data = self._packet(info, bye=bye)
        target = info.get("target") or _BROADCAST
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.settimeout(0.5)
                sock.sendto(data, (target, self._port))
        except OSError:
            pass  # discovery is best-effort; never break the engine

    # -- listening (receiver side) --------------------------------------------

    def start_listener(self) -> bool:
        """Bind the discovery port and start receiving; False if unavailable."""
        if self._listener_sock is not None:
            return True
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if not sys.platform.startswith("win"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except (OSError, AttributeError):
                    pass
            sock.bind(("", self._port))
            sock.settimeout(1.0)
        except OSError:
            return False
        self._listen_enabled = True
        self._listener_sock = sock
        self._listener_thread = threading.Thread(
            target=self._listen_loop,
            args=(sock,),
            name="wifile-discovery-listener",
            daemon=True,
        )
        self._listener_thread.start()
        return True

    def stop_listener(self) -> None:
        """Stop receiving and clear any discovered peers."""
        self._listen_enabled = False
        sock = self._listener_sock
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        thread = self._listener_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._listener_sock = None
        self._listener_thread = None
        with self._lock:
            self._peers.clear()
        self._state.set_peers([])

    def is_listening(self) -> bool:
        return self._listen_enabled and self._listener_sock is not None

    def _listen_loop(self, sock: socket.socket) -> None:
        while not self._closed and self._listen_enabled:
            try:
                data, addr = sock.recvfrom(_MAX_DATAGRAM)
            except socket.timeout:
                self._expire_peers()
                continue
            except OSError:
                break
            self._handle_packet(data, addr)

    def _handle_packet(self, data: bytes, addr: tuple[str, int]) -> None:
        try:
            packet = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            return
        if not isinstance(packet, dict) or packet.get("app") != APP_ID:
            return
        peer_id = packet.get("id")
        if not isinstance(peer_id, str) or peer_id == self._instance_id:
            return
        with self._lock:
            if packet.get("bye"):
                changed = self._peers.pop(peer_id, None) is not None
            else:
                changed = self._upsert_peer(peer_id, packet, addr[0])
        if changed:
            self._publish()

    def _upsert_peer(self, peer_id: str, packet: dict[str, Any], host: str) -> bool:
        try:
            port = int(packet.get("port"))
        except (TypeError, ValueError):
            return False
        if not 1 <= port <= 65535:
            return False
        web_port = packet.get("web_port")
        try:
            web_port = int(web_port) if web_port is not None else None
        except (TypeError, ValueError):
            web_port = None
        peer = {
            "id": peer_id,
            "name": str(packet.get("name") or "wifile")[:64],
            "host": host,
            "port": port,
            "source": str(packet.get("source") or "")[:200],
            "platform": str(packet.get("platform") or "")[:32],
            "web_port": web_port,
        }
        entry = self._peers.get(peer_id)
        if entry is None or entry["peer"] != peer:
            self._peers[peer_id] = {"peer": peer, "seen": time.monotonic()}
            return True
        entry["seen"] = time.monotonic()
        return False

    def _expire_peers(self) -> None:
        deadline = time.monotonic() - self._peer_ttl
        with self._lock:
            stale = [
                peer_id
                for peer_id, entry in self._peers.items()
                if entry["seen"] < deadline
            ]
            for peer_id in stale:
                del self._peers[peer_id]
        if stale:
            self._publish()

    def _publish(self) -> None:
        with self._lock:
            peers = sorted(
                (entry["peer"] for entry in self._peers.values()),
                key=lambda p: (p["name"].lower(), p["host"], p["port"]),
            )
        self._state.set_peers(peers)

    # -- lifecycle -------------------------------------------------------------

    def stop(self) -> None:
        """Stop the listener and announcer; clears peers from the store."""
        self._closed = True
        self.stop_announcing()
        self.stop_listener()
