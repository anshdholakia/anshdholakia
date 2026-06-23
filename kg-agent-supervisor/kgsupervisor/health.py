"""Detect when the agent has hung, died, or returned an unusable reply.

This is the heart of the "babysitting" behaviour. A reply is classified into
one of:

* ``OK``       – a real, usable answer.
* ``HUNG``     – no reply arrived within the timeout (the agent went silent).
* ``DEAD``     – the agent explicitly reported it crashed / disconnected /
                 needs a restart (matched against configurable phrases).
* ``EMPTY``    – the agent replied but the body was empty / whitespace.

The supervisor uses this verdict to decide whether to accept the answer or kick
off a recovery cycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional


class Verdict(str, Enum):
    OK = "ok"
    HUNG = "hung"
    DEAD = "dead"
    EMPTY = "empty"


# Phrases that indicate the Gemini agent fell over and needs a restart.
# Tune these in config; they are matched case-insensitively as substrings.
DEFAULT_FAILURE_PHRASES: List[str] = [
    "hung up",
    "i have hung up",
    "i've hung up",
    "disconnected",
    "connection lost",
    "connection was lost",
    "i need to restart",
    "please restart",
    "i have to restart",
    "i crashed",
    "an error occurred",
    "internal error",
    "something went wrong",
    "i am unable to continue",
    "i can no longer continue",
    "session expired",
    "context limit",
    "i lost the context",
    "i don't have any context",
    "rate limit",
    "503",
    "service unavailable",
]


@dataclass
class HealthResult:
    verdict: Verdict
    reason: str

    @property
    def healthy(self) -> bool:
        return self.verdict is Verdict.OK


class HealthChecker:
    def __init__(
        self,
        failure_phrases: Optional[List[str]] = None,
        min_response_chars: int = 1,
    ):
        phrases = failure_phrases if failure_phrases is not None else DEFAULT_FAILURE_PHRASES
        # Pre-compile a single alternation regex for speed.
        escaped = [re.escape(p.lower()) for p in phrases if p.strip()]
        self._pattern = re.compile("|".join(escaped)) if escaped else None
        self.min_response_chars = min_response_chars

    def evaluate(self, response: Optional[str]) -> HealthResult:
        """Classify a reply. ``None`` means nothing arrived in time (hung)."""
        if response is None:
            return HealthResult(Verdict.HUNG, "No reply received before timeout.")

        stripped = response.strip()
        if len(stripped) < self.min_response_chars:
            return HealthResult(Verdict.EMPTY, "Reply was empty or too short.")

        if self._pattern is not None:
            match = self._pattern.search(stripped.lower())
            if match:
                return HealthResult(
                    Verdict.DEAD,
                    f"Reply matched failure phrase: {match.group(0)!r}.",
                )

        return HealthResult(Verdict.OK, "Reply looks healthy.")
