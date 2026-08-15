"""Thread-safe store shared by the engine threads and the HTTP layer.

The store keeps one slot per mode ("server" for sending, "client" for
receiving). Each slot holds a bounded log, the current progress summary, an
optional pending prompt, and the running/stop flags. Every mutation bumps a
version counter and notifies waiters, which is what the SSE endpoint uses to
push changes to the browser.
"""

from __future__ import annotations

import queue
import threading
from collections import deque
from typing import Any

MAX_LOG_LINES = 200

MODES = ("server", "client")


class _PendingPrompt:
    __slots__ = ("id", "kind", "text", "options", "default", "reply")

    def __init__(
        self,
        prompt_id: str,
        kind: str,
        text: str,
        options: list[str],
        default: str,
        reply: queue.Queue[tuple[str, str | None]],
    ) -> None:
        self.id = prompt_id
        self.kind = kind  # "choose" or "ask_text"
        self.text = text
        self.options = options
        self.default = default
        self.reply = reply


class _Slot:
    def __init__(self, max_log_lines: int) -> None:
        self.running = False
        self.stop_requested = False
        self.log: deque[str] = deque(maxlen=max_log_lines)
        self.progress: dict[str, float] | None = None
        self.prompt: _PendingPrompt | None = None


class WebState:
    """All shared UI state, guarded by a single lock (contention is low)."""

    def __init__(self, max_log_lines: int = MAX_LOG_LINES) -> None:
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._slots: dict[str, _Slot] = {mode: _Slot(max_log_lines) for mode in MODES}
        self._version = 0
        self._prompt_counter = 0

    # -- mutation -------------------------------------------------------------

    def _bump(self) -> None:
        self._version += 1
        self._cond.notify_all()

    def log(self, mode: str, text: str) -> None:
        with self._lock:
            self._slots[mode].log.append(text)
            self._bump()

    def set_progress(
        self, mode: str, current: float, total: float, speed: float, eta: float
    ) -> None:
        with self._lock:
            percent = (current / total) * 100.0 if total > 0 else 100.0
            self._slots[mode].progress = {
                "current": current,
                "total": total,
                "percent": round(min(percent, 100.0), 2),
                "speed": speed,
                "eta": eta,
            }
            self._bump()

    def clear_progress(self, mode: str) -> None:
        with self._lock:
            slot = self._slots[mode]
            if slot.progress is not None:
                slot.progress = None
                self._bump()

    def create_prompt(
        self,
        mode: str,
        kind: str,
        text: str,
        options: Any,
        default: str,
        reply: queue.Queue[tuple[str, str | None]],
    ) -> str:
        """Register a pending prompt; returns its id for /api/answer."""
        with self._lock:
            self._prompt_counter += 1
            prompt_id = f"p{self._prompt_counter}"
            slot = self._slots[mode]
            if slot.stop_requested:
                # A stop arrived just before this prompt appeared; resolve it
                # immediately so the engine can observe the stop flag instead
                # of blocking on a question nobody will see.
                try:
                    reply.put_nowait(("eof", None))
                except queue.Full:
                    pass
                return prompt_id
            slot.prompt = _PendingPrompt(
                prompt_id, kind, text, list(options), default, reply
            )
            self._bump()
            return prompt_id

    def clear_prompt(self, mode: str, prompt_id: str) -> bool:
        with self._lock:
            slot = self._slots[mode]
            if slot.prompt is not None and slot.prompt.id == prompt_id:
                slot.prompt = None
                self._bump()
                return True
            return False

    def answer_prompt(
        self, mode: str, prompt_id: str, value: str = "", eof: bool = False
    ) -> bool:
        """Deliver an answer to a pending prompt; False if none matches."""
        with self._lock:
            slot = self._slots[mode]
            pending = slot.prompt
            if pending is None or pending.id != prompt_id:
                return False
            slot.prompt = None
            self._bump()
            reply = pending.reply
        try:
            reply.put_nowait(("eof", None) if eof else ("answer", value))
        except queue.Full:
            return False
        return True

    def set_running(self, mode: str, running: bool) -> None:
        with self._lock:
            slot = self._slots[mode]
            if slot.running != running:
                slot.running = running
                self._bump()

    def reset(self, mode: str) -> None:
        """Prepare a slot for a fresh engine start."""
        with self._lock:
            slot = self._slots[mode]
            slot.stop_requested = False
            slot.progress = None
            self._bump()

    def request_stop(self, mode: str) -> None:
        """Set the stop flag and unblock any pending prompt for the slot."""
        with self._lock:
            slot = self._slots[mode]
            slot.stop_requested = True
            pending = slot.prompt
            if pending is not None:
                slot.prompt = None
            self._bump()
            if pending is None:
                return
            reply = pending.reply
            if pending.kind == "ask_text":
                outcome: tuple[str, str | None] = ("eof", None)
            elif "e" in pending.options:
                outcome = ("answer", "e")
            elif "cancel" in pending.options:
                outcome = ("answer", "cancel")
            else:
                outcome = (
                    "answer",
                    pending.options[0] if pending.options else pending.default,
                )
        try:
            reply.put_nowait(outcome)
        except queue.Full:
            pass

    # -- queries ----------------------------------------------------------------

    def is_stop_requested(self, mode: str) -> bool:
        with self._lock:
            return self._slots[mode].stop_requested

    def is_running(self, mode: str) -> bool:
        with self._lock:
            return self._slots[mode].running

    def get_snapshot(self) -> dict[str, Any]:
        """JSON-ready picture of both slots plus the current version."""
        with self._lock:
            return {
                "version": self._version,
                "server": self._slot_snapshot("server"),
                "client": self._slot_snapshot("client"),
            }

    def _slot_snapshot(self, mode: str) -> dict[str, Any]:
        slot = self._slots[mode]
        prompt = None
        if slot.prompt is not None:
            pending = slot.prompt
            prompt = {
                "id": pending.id,
                "kind": pending.kind,
                "text": pending.text,
                "options": pending.options,
                "default": pending.default,
            }
        return {
            "running": slot.running,
            "stopRequested": slot.stop_requested,
            "progress": dict(slot.progress) if slot.progress else None,
            "prompt": prompt,
            "log": list(slot.log),
        }

    def wait_for_change(self, last_version: int, timeout: float) -> int:
        """Block until the version moves; returns the current version."""
        with self._cond:
            self._cond.wait_for(lambda: self._version != last_version, timeout=timeout)
            return self._version
