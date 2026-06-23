"""Abstract chat transport.

A transport knows how to (a) post a message into the space the Gemini bot lives
in and (b) wait for the bot's reply. Recovery actions (restart) are expressed as
ordinary messages plus a readiness check, so the same interface covers webhooks
and the full REST API.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass
from typing import Optional


@dataclass
class Reply:
    """A single message read back from the space."""

    text: str
    sender: str          # display name or resource id of the author
    create_time: float   # epoch seconds
    raw: Optional[dict] = None


class ChatClient(abc.ABC):
    """Interface every transport must implement."""

    #: Whether this transport can read replies back. Webhooks cannot.
    can_read_replies: bool = True

    @abc.abstractmethod
    def post(self, text: str, thread_key: Optional[str] = None) -> None:
        """Send ``text`` into the space (optionally into a specific thread)."""

    @abc.abstractmethod
    def wait_for_reply(self, timeout: float, poll_interval: float) -> Optional[Reply]:
        """Block until the bot posts a new reply, or ``timeout`` seconds pass.

        Returns the :class:`Reply`, or ``None`` if nothing arrived in time
        (which the health checker treats as a hang).
        """

    def close(self) -> None:  # pragma: no cover - optional hook
        """Release any resources (HTTP sessions, etc.)."""
