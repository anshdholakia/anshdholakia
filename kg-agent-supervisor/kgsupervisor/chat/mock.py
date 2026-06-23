"""A fake Gemini bot so you can run the whole supervisor with zero credentials.

It echoes a plausible answer for each prompt and, every ``fail_every`` messages,
pretends to hang up — so you can watch the restart/replay/resume machinery work
end to end before wiring up real Google Chat. Configure under ``chat.mock``.
"""

from __future__ import annotations

import random
import time
from typing import Optional

from .base import ChatClient, Reply


class MockChatClient(ChatClient):
    can_read_replies = True

    def __init__(self, config):
        mock_cfg = getattr(config.chat, "mock", None)
        self.fail_every = getattr(mock_cfg, "fail_every", 4) if mock_cfg else 4
        self.latency = getattr(mock_cfg, "latency_seconds", 0.3) if mock_cfg else 0.3
        self._count = 0
        self._pending: Optional[str] = None
        self._rng = random.Random(7)

    def post(self, text: str, thread_key: Optional[str] = None) -> None:
        self._count += 1
        # Restart command resets the failure counter (bot "comes back").
        if "restart" in text.lower():
            self._pending = "Restarted and ready. I have re-read the context you sent."
            return
        if self.fail_every and self._count % self.fail_every == 0:
            self._pending = self._rng.choice(
                [
                    "Sorry, I hung up and lost my context. Please restart me.",
                    "An internal error occurred (503 service unavailable).",
                ]
            )
            return
        # Otherwise produce a believable answer that references the prompt.
        snippet = text.strip().splitlines()[-1][:120]
        self._pending = f"[mock-gemini] Done. Re: {snippet!r} — here is a synthesized result."

    def wait_for_reply(self, timeout: float, poll_interval: float) -> Optional[Reply]:
        time.sleep(min(self.latency, timeout))
        if self._pending is None:
            return None
        text, self._pending = self._pending, None
        return Reply(text=text, sender="mock-gemini", create_time=time.time())
