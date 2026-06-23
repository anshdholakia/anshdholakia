"""Durable run state so a crashed/closed supervisor can resume.

We persist which nodes are already done (and their answers) to a small JSON
file after every node. On restart, completed nodes are skipped and their outputs
are restored into the :class:`~kgsupervisor.agent.AgentSession` as context.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict


@dataclass
class RunState:
    graph_title: str = ""
    completed: Dict[str, str] = field(default_factory=dict)  # node_id -> answer
    path: str = ".kgs_state.json"

    @classmethod
    def load(cls, path: str, graph_title: str) -> "RunState":
        p = Path(path)
        if not p.exists():
            return cls(graph_title=graph_title, path=path)
        data = json.loads(p.read_text(encoding="utf-8"))
        # If the graph changed, start fresh rather than mixing runs.
        if data.get("graph_title") != graph_title:
            return cls(graph_title=graph_title, path=path)
        return cls(
            graph_title=graph_title,
            completed=data.get("completed", {}),
            path=path,
        )

    def is_done(self, node_id: str) -> bool:
        return node_id in self.completed

    def mark_done(self, node_id: str, answer: str) -> None:
        self.completed[node_id] = answer
        self.save()

    def save(self) -> None:
        payload = {"graph_title": self.graph_title, "completed": self.completed}
        # Atomic write so a crash mid-save can't corrupt the file.
        directory = os.path.dirname(os.path.abspath(self.path)) or "."
        fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
