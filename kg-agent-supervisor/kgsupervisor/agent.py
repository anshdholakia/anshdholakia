"""Conversation context tracking for the agent.

The :class:`AgentSession` remembers everything that has been said so far so we
can *replay* it after a restart. When the Gemini bot hangs and we have to
restart it, the bot loses its memory of the conversation; this class lets us
rebuild a compact recap to hand back to it before resuming the interrupted task.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class Turn:
    """One exchange in the conversation."""

    node_id: Optional[str]
    prompt: str
    response: Optional[str] = None
    ts: float = field(default_factory=time.time)


class AgentSession:
    """Tracks conversation history and per-node results for one graph run."""

    def __init__(self, graph_title: str):
        self.graph_title = graph_title
        self.turns: List[Turn] = []
        self.results: Dict[str, str] = {}  # node_id -> final response text

    # ------------------------------------------------------------- recording
    def start_turn(self, node_id: Optional[str], prompt: str) -> Turn:
        turn = Turn(node_id=node_id, prompt=prompt)
        self.turns.append(turn)
        return turn

    def complete_turn(self, turn: Turn, response: str) -> None:
        turn.response = response
        if turn.node_id is not None:
            self.results[turn.node_id] = response

    def result_for(self, node_id: str) -> Optional[str]:
        return self.results.get(node_id)

    # -------------------------------------------------------- context replay
    def dependency_context(self, dep_ids: List[str]) -> str:
        """Render the outputs of the given dependency nodes as context."""
        if not dep_ids:
            return ""
        chunks = []
        for dep in dep_ids:
            answer = self.results.get(dep)
            if answer:
                chunks.append(f"### Result of earlier step '{dep}':\n{answer}")
        return "\n\n".join(chunks)

    def recap(self, max_turns: int = 12, max_chars_per_turn: int = 1200) -> str:
        """Build a compact transcript to re-prime the agent after a restart.

        We keep the most recent ``max_turns`` exchanges and truncate long
        bodies so the recap stays well under prompt limits.
        """
        recent = [t for t in self.turns if t.response][-max_turns:]
        lines = [
            f"You are resuming work on the task: {self.graph_title!r}.",
            "The connection dropped and you lost your memory. Here is a recap of "
            "what was already done so you can continue exactly where we left off. "
            "Do NOT redo completed steps.",
            "",
        ]
        for t in recent:
            label = f"[{t.node_id}] " if t.node_id else ""
            lines.append(f"{label}You were asked: {_truncate(t.prompt, max_chars_per_turn)}")
            lines.append(f"You answered: {_truncate(t.response or '', max_chars_per_turn)}")
            lines.append("")
        return "\n".join(lines).strip()


def _truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
