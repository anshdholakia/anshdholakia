"""Google Chat REST API transport (post messages AND read the bot's replies).

This uses a Google Cloud **service account** authenticated as a Chat app. The
service account posts into ``chat.space`` and polls ``spaces.messages.list`` for
new messages authored by anyone *other than* the service account itself — i.e.
the Gemini bot's replies.

Setup (see README for the click-by-click version):

1. In a Google Cloud project, enable the **Google Chat API**.
2. Create a service account and a JSON key; point ``GOOGLE_APPLICATION_CREDENTIALS``
   (or ``chat.google.credentials_file``) at the key.
3. Configure the Chat app (App configuration page) using that service account.
4. Add the Chat app to the space, and note the space id ``spaces/AAAA…``.

Required scope: ``https://www.googleapis.com/auth/chat.bot``.
"""

from __future__ import annotations

import time
from typing import List, Optional

from .base import ChatClient, Reply

API_ROOT = "https://chat.googleapis.com/v1"
SCOPES = ["https://www.googleapis.com/auth/chat.bot"]


class GoogleChatClient(ChatClient):
    can_read_replies = True

    def __init__(self, config):
        try:
            import requests  # noqa: F401
            from google.oauth2 import service_account
            from google.auth.transport.requests import AuthorizedSession
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "google_chat transport needs extra packages. Install with:\n"
                "    pip install requests google-auth"
            ) from exc

        g = config.chat.google
        self.space = g.space
        if not self.space or not self.space.startswith("spaces/"):
            raise ValueError(
                "chat.google.space must look like 'spaces/AAAAxxxx'. "
                f"Got {self.space!r}."
            )

        creds = service_account.Credentials.from_service_account_file(
            g.credentials_file, scopes=SCOPES
        )
        self._service_account_email = creds.service_account_email
        self._session = AuthorizedSession(creds)
        # Only consider messages created after the client starts, so we never
        # mistake old history for a fresh reply.
        self._last_seen = time.time()

    # ------------------------------------------------------------------ post
    def post(self, text: str, thread_key: Optional[str] = None) -> None:
        url = f"{API_ROOT}/{self.space}/messages"
        params = {}
        body: dict = {"text": text}
        if thread_key:
            body["thread"] = {"threadKey": thread_key}
            params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
        resp = self._session.post(url, params=params, json=body, timeout=30)
        resp.raise_for_status()

    # ------------------------------------------------------------------ read
    def wait_for_reply(self, timeout: float, poll_interval: float) -> Optional[Reply]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            replies = self._fetch_new_bot_messages()
            if replies:
                # Advance the watermark past everything we just consumed.
                self._last_seen = max(r.create_time for r in replies)
                return replies[-1]  # most recent reply
            time.sleep(min(poll_interval, max(0.0, deadline - time.time())))
        return None

    # -------------------------------------------------------------- internals
    def _fetch_new_bot_messages(self) -> List[Reply]:
        url = f"{API_ROOT}/{self.space}/messages"
        # RFC3339 timestamp filter keeps the payload small.
        after = _rfc3339(self._last_seen)
        params = {
            "filter": f'createTime > "{after}"',
            "orderBy": "createTime ASC",
            "pageSize": 50,
        }
        resp = self._session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        out: List[Reply] = []
        for msg in resp.json().get("messages", []):
            sender = msg.get("sender", {})
            # Skip our own posts; we want the Gemini bot's replies.
            if sender.get("name", "").endswith(self._service_account_email):
                continue
            if sender.get("type") == "HUMAN" and not _treat_humans_as_agent():
                # By default we listen only to the bot, but a human can step in.
                pass
            text = msg.get("text") or msg.get("argumentText") or ""
            out.append(
                Reply(
                    text=text,
                    sender=sender.get("displayName") or sender.get("name", "unknown"),
                    create_time=_parse_rfc3339(msg.get("createTime")),
                    raw=msg,
                )
            )
        return out

    def close(self) -> None:  # pragma: no cover
        self._session.close()


def _treat_humans_as_agent() -> bool:
    # Hook for future config; for now humans in the space are also surfaced.
    return True


def _rfc3339(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


def _parse_rfc3339(value: Optional[str]) -> float:
    if not value:
        return time.time()
    # Google returns e.g. "2026-06-23T17:00:00.123456Z"; trim fractional secs.
    cleaned = value.split(".")[0].rstrip("Z")
    try:
        return time.mktime(time.strptime(cleaned, "%Y-%m-%dT%H:%M:%S")) - time.timezone
    except ValueError:
        return time.time()
