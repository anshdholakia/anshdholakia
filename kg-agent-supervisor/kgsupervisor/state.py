"""Durable run state so a crashed/closed supervisor can resume.

We persist which nodes are already done (their answer + a *fingerprint* of the
node) to a small JSON file after every node. On the next run a node is skipped
ONLY if its fingerprint still matches — so editing a node's prompt or
dependencies invalidates its cached result and it re-runs. Resume can be turned
off entirely (``run.resume: false`` / ``--no-resume``) or wiped (``--reset``).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional


@dataclass
class RunState:
    graph_title: str = ""
    # node_id -> {"answer": str, "fp": str|None}
    completed: Dict[str, dict] = field(default_factory=dict)
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
        completed = {}
        for node_id, value in (data.get("completed") or {}).items():
            if isinstance(value, str):
                # Legacy format (answer only, no fingerprint) → force re-run by
                # leaving fp None so it never matches a real fingerprint.
                completed[node_id] = {"answer": value, "fp": None}
            elif isinstance(value, dict):
                completed[node_id] = {
                    "answer": value.get("answer", ""),
                    "fp": value.get("fp"),
                }
        return cls(graph_title=graph_title, completed=completed, path=path)

    def is_done(self, node_id: str, fingerprint: str) -> bool:
        entry = self.completed.get(node_id)
        return entry is not None and entry.get("fp") == fingerprint

    def answer_for(self, node_id: str) -> Optional[str]:
        entry = self.completed.get(node_id)
        return entry["answer"] if entry else None

    def mark_done(self, node_id: str, answer: str, fingerprint: str) -> None:
        self.completed[node_id] = {"answer": answer, "fp": fingerprint}
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
