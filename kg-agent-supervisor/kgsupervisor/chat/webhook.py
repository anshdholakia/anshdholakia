"""Google Chat *incoming webhook* transport (send-only).

This is the quickest way to start posting into a space: in the space, open
"Apps & integrations" → "Webhooks", create one, and paste the URL into
``chat.webhook.url``. No service account required.

LIMITATION: webhooks can only *post*. They cannot read the bot's replies, so the
supervisor cannot auto-detect hangs from message content — it can only detect a
hang by timeout, and you must confirm recovery yourself. For full
hang-detection + auto-restart, use the ``google_chat`` transport instead.

When ``can_read_replies`` is False, the supervisor runs in *fire-and-watch*
mode: it posts prompts and waits ``response_timeout`` seconds, optionally asking
you (on the console) to paste the bot's reply so it can still classify health.
"""

from __future__ import annotations

import time
from typing import Optional

from .base import ChatClient, Reply


class WebhookChatClient(ChatClient):
    can_read_replies = False

    def __init__(self, config):
        try:
            import requests  # noqa: F401
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("webhook transport needs: pip install requests") from exc
        import requests

        self._requests = requests
        w = config.chat.webhook
        self.url = w.url
        if not self.url:
            raise ValueError("chat.webhook.url is required for the webhook transport.")
        # If true, prompt the operator to paste the bot's reply on the console so
        # health-checking still works without read access.
        self.interactive = getattr(w, "interactive", True)

    def post(self, text: str, thread_key: Optional[str] = None) -> None:
        payload = {"text": text}
        params = {}
        if thread_key:
            payload["thread"] = {"threadKey": thread_key}
            params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
        resp = self._requests.post(self.url, params=params, json=payload, timeout=30)
        resp.raise_for_status()

    def wait_for_reply(self, timeout: float, poll_interval: float) -> Optional[Reply]:
        if not self.interactive:
            # No way to read; just wait out the window and report a hang so the
            # caller can decide what to do.
            time.sleep(timeout)
            return None
        print(
            f"\n[webhook] Waiting up to {int(timeout)}s for the bot. "
            "Paste its reply then press Enter (blank = it hung):"
        )
        # Best-effort console capture; in non-tty environments this returns "".
        try:
            line = input().strip()
        except EOFError:
            line = ""
        if not line:
            return None
        return Reply(text=line, sender="operator-pasted", create_time=time.time())
