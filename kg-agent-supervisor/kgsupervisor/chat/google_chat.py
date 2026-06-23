"""Google Chat REST API transport — edit-aware reading + hybrid posting.

Your Gemini agent streams its answer by *editing the same message* over and over
until it's finished. A plain "read the latest message" approach would grab a
half-written answer. So this transport watches the agent's message and only
accepts it once:

* it contains ``final_marker`` (if you configured one — the robust option), or
* its edits have settled for ``stability_seconds`` (no further edits), or
* edits never settle before the timeout → reported as a hang (``None``).

Auth has three modes (`chat.google`):

* ``user_auth: true`` — act as **you** via your own ADC
  (``gcloud auth application-default login``). This is the only way to reach a
  1:1 **DM** with another Chat app (you can't add a 3rd app to a DM).
* ``credentials_file`` — a service-account JSON key (Chat app identity).
* ``impersonate_service_account`` — key-free: impersonate a service account
  using your ADC. Both SA modes require the app to be a member of the space, so
  they work for spaces, not DMs with other apps.

Posting can go through the API (default) or, if ``chat.google.webhook_url`` is
set, through an incoming webhook while replies are still read via the API. Set
``chat.google.bot_name`` to only treat the agent's messages as replies.

Run ``python3 -m kgsupervisor --config <cfg> --list-spaces`` to print the API
ids of the spaces/DMs you can see, so you can find the DM with your agent.
"""

from __future__ import annotations

import time
from typing import List, Optional, Set, Tuple

from .base import ChatClient, Reply

API_ROOT = "https://chat.googleapis.com/v1"
# Service-account (Chat app) scope.
SCOPES = ["https://www.googleapis.com/auth/chat.bot"]
# User-auth scope: post + read messages on behalf of the signed-in user.
USER_SCOPES = ["https://www.googleapis.com/auth/chat.messages"]


class GoogleChatClient(ChatClient):
    can_read_replies = True

    def __init__(self, config):
        try:
            import requests
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
                f"Got {self.space!r}. Tip: run with --list-spaces to find it."
            )

        self._session, self._identity_email = _make_session(g)

        self._requests = requests
        self._webhook_url = g.webhook_url            # hybrid posting
        self._bot_name = g.bot_name                  # sender filter
        self._track_edits = g.track_edits
        self._stability = float(g.stability_seconds)
        self._final_marker = g.final_marker

        # Names of messages WE created via the API, so we never read them back.
        self._own_messages: Set[str] = set()
        # Only consider messages created after this moment.
        self._last_seen = time.time()


    # ------------------------------------------------------------------ post
    def post(self, text: str, thread_key: Optional[str] = None) -> None:
        if self._webhook_url:
            self._post_via_webhook(text, thread_key)
        else:
            self._post_via_api(text, thread_key)
        # Anything the agent says from here on is a reply to what we just sent.
        self._last_seen = time.time()

    def _post_via_api(self, text: str, thread_key: Optional[str]) -> None:
        url = f"{API_ROOT}/{self.space}/messages"
        params, body = {}, {"text": text}
        if thread_key:
            body["thread"] = {"threadKey": thread_key}
            params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
        resp = self._session.post(url, params=params, json=body, timeout=30)
        resp.raise_for_status()
        name = resp.json().get("name")
        if name:
            self._own_messages.add(name)

    def _post_via_webhook(self, text: str, thread_key: Optional[str]) -> None:
        params, body = {}, {"text": text}
        if thread_key:
            body["thread"] = {"threadKey": thread_key}
            params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
        resp = self._requests.post(
            self._webhook_url, params=params, json=body, timeout=30
        )
        resp.raise_for_status()

    # ------------------------------------------------------------------ read
    def wait_for_reply(self, timeout: float, poll_interval: float) -> Optional[Reply]:
        deadline = time.time() + timeout
        target: Optional[Tuple[str, str]] = None  # (message_name, sender_label)
        last_text = ""
        last_change = time.time()

        while time.time() < deadline:
            if target is None:
                found = self._find_new_reply_message()
                if found is None:
                    self._nap(poll_interval, deadline)
                    continue
                name, text, sender = found
                target = (name, sender)
                last_text, last_change = text, time.time()
            else:
                name, sender = target
                text = self._get_message_text(name)
                if text != last_text:
                    last_text, last_change = text, time.time()

            # Accept conditions -------------------------------------------------
            if self._final_marker and self._final_marker in last_text:
                return self._finalize(last_text, target[1])
            if not self._track_edits and last_text.strip():
                return self._finalize(last_text, target[1])
            settled = (time.time() - last_change) >= self._stability
            if self._track_edits and last_text.strip() and settled:
                return self._finalize(last_text, target[1])

            self._nap(poll_interval, deadline)

        # Timed out. If a final_marker was required we never got a complete
        # answer, and a message that never settles means the agent stalled
        # mid-stream — both are hangs, so report None and let recovery run.
        return None

    def _finalize(self, text: str, sender: str) -> Reply:
        self._last_seen = time.time()
        return Reply(text=text, sender=sender, create_time=time.time())

    # -------------------------------------------------------------- internals
    def _find_new_reply_message(self) -> Optional[Tuple[str, str, str]]:
        """Return (name, text, sender_label) of the newest agent message, if any."""
        url = f"{API_ROOT}/{self.space}/messages"
        params = {
            "filter": f'createTime > "{_rfc3339(self._last_seen)}"',
            "orderBy": "createTime ASC",
            "pageSize": 50,
        }
        resp = self._session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        candidates: List[Tuple[float, str, str, str]] = []
        for msg in resp.json().get("messages", []):
            name = msg.get("name", "")
            if name in self._own_messages:
                continue
            sender = msg.get("sender", {})
            label = sender.get("displayName") or sender.get("name", "unknown")
            if not self._is_agent(sender, label):
                continue
            candidates.append(
                (
                    _parse_rfc3339(msg.get("createTime")),
                    name,
                    msg.get("text") or msg.get("argumentText") or "",
                    label,
                )
            )
        if not candidates:
            return None
        candidates.sort(key=lambda c: c[0])
        _, name, text, label = candidates[-1]
        return name, text, label

    def _get_message_text(self, name: str) -> str:
        resp = self._session.get(f"{API_ROOT}/{name}", timeout=30)
        resp.raise_for_status()
        msg = resp.json()
        return msg.get("text") or msg.get("argumentText") or ""

    def _is_agent(self, sender: dict, label: str) -> bool:
        # Never treat our own service-account posts as replies.
        if self._identity_email and sender.get("name", "").endswith(self._identity_email):
            return False
        # If a bot_name filter is configured, the sender must match it.
        if self._bot_name:
            return self._bot_name in label or self._bot_name in sender.get("name", "")
        return True

    @staticmethod
    def _nap(poll_interval: float, deadline: float) -> None:
        time.sleep(min(poll_interval, max(0.0, deadline - time.time())))

    def close(self) -> None:  # pragma: no cover
        self._session.close()


def _make_session(g):
    """Build an authorized HTTP session. Returns (session, identity_email|None).

    Picks the auth mode from config: user_auth (act as you), credentials_file
    (SA key), or impersonate_service_account (key-free SA).
    """
    from google.auth.transport.requests import AuthorizedSession

    if getattr(g, "user_auth", False):
        # Act as the signed-in user. Reaches DMs you're a participant in.
        # First run: gcloud auth application-default login \
        #   --scopes=https://www.googleapis.com/auth/chat.messages,\
        #            https://www.googleapis.com/auth/cloud-platform
        from google.auth import default as adc_default

        creds, _ = adc_default(scopes=USER_SCOPES)
        return AuthorizedSession(creds), None

    if g.credentials_file:
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_file(
            g.credentials_file, scopes=SCOPES
        )
        return AuthorizedSession(creds), creds.service_account_email

    if g.impersonate_service_account:
        from google.auth import default as adc_default
        from google.auth import impersonated_credentials

        source, _ = adc_default()
        creds = impersonated_credentials.Credentials(
            source_credentials=source,
            target_principal=g.impersonate_service_account,
            target_scopes=SCOPES,
        )
        return AuthorizedSession(creds), g.impersonate_service_account

    raise ValueError(
        "Configure auth under chat.google: set user_auth: true (act as you — "
        "required for a DM with another app), OR credentials_file (SA key), OR "
        "impersonate_service_account (key-free SA). For user_auth/impersonation, "
        "first run: gcloud auth application-default login"
    )


def list_spaces(config) -> List[Tuple[str, str, str]]:
    """Return (name, spaceType, displayName) for every space/DM you can see."""
    session, _ = _make_session(config.chat.google)
    out: List[Tuple[str, str, str]] = []
    page = None
    while True:
        params = {"pageSize": 100}
        if page:
            params["pageToken"] = page
        resp = session.get(f"{API_ROOT}/spaces", params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        for s in data.get("spaces", []):
            out.append(
                (
                    s.get("name", ""),
                    s.get("spaceType") or s.get("type") or "",
                    s.get("displayName") or "",
                )
            )
        page = data.get("nextPageToken")
        if not page:
            break
    return out


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
