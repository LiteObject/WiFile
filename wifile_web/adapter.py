"""WebUI: adapts the ``wifile.UI`` protocol to the shared :class:`WebState`.

Messages go into the slot's log, progress into its progress summary
(throttled so the store isn't hammered at high transfer speeds), and prompts
block the engine thread until the HTTP layer delivers an answer via
``state.answer_prompt`` (or until they time out, in which case a safe
cancel/exit is answered automatically).
"""

from __future__ import annotations

import queue
import time
from collections.abc import Sequence

from .state import WebState

PROMPT_TIMEOUT = 300  # seconds; matches wifile.DECISION_TIMEOUT's generosity
PROGRESS_INTERVAL = 0.1  # seconds between progress store updates


class WebUI:
    """UI implementation that forwards engine events into a WebState slot."""

    def __init__(self, state: WebState, mode: str) -> None:
        self._state = state
        self._mode = mode
        self._last_progress_time = 0.0

    def message(self, text: str) -> None:
        self._state.log(self._mode, text)

    def progress(
        self, current: float, total: float, start_time: float | None = None
    ) -> None:
        if total <= 0:
            return
        now = time.monotonic()
        final = current >= total
        if not final and now - self._last_progress_time < PROGRESS_INTERVAL:
            return
        self._last_progress_time = now

        speed = 0.0
        eta = 0.0
        if start_time:
            elapsed = time.time() - start_time
            if elapsed > 0 and current > 0:
                speed = current / elapsed
                eta = (total - current) / speed if speed > 0 else 0.0
        self._state.set_progress(self._mode, current, total, speed, eta)

    def choose(
        self, prompt: str, options: Sequence[str], default: str, invalid: str
    ) -> str:
        del invalid  # buttons make invalid input impossible in the browser
        return self._await("choose", prompt, options, default)

    def ask_text(self, prompt: str) -> str:
        return self._await("ask_text", prompt, (), "")

    def should_stop(self) -> bool:
        return self._state.is_stop_requested(self._mode)

    def _await(self, kind: str, text: str, options: Sequence[str], default: str) -> str:
        reply: queue.Queue[tuple[str, str | None]] = queue.Queue(maxsize=1)
        prompt_id = self._state.create_prompt(
            self._mode, kind, text, options, default, reply
        )
        try:
            outcome, value = reply.get(timeout=PROMPT_TIMEOUT)
        except queue.Empty:
            # A stale tab never answered; resolve so the engine can't hang
            # forever. Cancel/exit are the safest automatic answers.
            self._state.clear_prompt(self._mode, prompt_id)
            if kind == "ask_text":
                raise EOFError(f"Prompt {prompt_id!r} timed out") from None
            if "cancel" in options:
                return "cancel"
            if "e" in options:
                return "e"
            return options[0] if options else default
        if outcome == "eof":
            raise EOFError(f"Prompt {prompt_id!r} was closed")
        return value if value is not None else default
