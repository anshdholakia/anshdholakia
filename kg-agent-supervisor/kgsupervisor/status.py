"""Thread-safe live status of a graph run, consumed by the web dashboard.

The supervisor updates this board as it works (a node goes
pending → running → done, or → recovering → running on a hang, or → failed).
The dashboard's HTTP server reads :meth:`StatusBoard.snapshot` and serves it as
JSON for the browser to render.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

PENDING = "pending"
RUNNING = "running"
RECOVERING = "recovering"
DONE = "done"
FAILED = "failed"


class StatusBoard:
    def __init__(self, graph):
        self._lock = threading.Lock()
        self.graph_title = graph.title
        levels = graph.levels()
        self._meta = [
            {
                "id": n.id,
                "prompt": n.prompt,
                "depends_on": list(n.depends_on),
                "level": levels[n.id],
            }
            for n in graph.nodes()
        ]
        self._edges = [{"source": s, "target": t} for s, t in graph.edges()]
        self._status: Dict[str, str] = {n.id: PENDING for n in graph.nodes()}
        self._detail: Dict[str, Optional[str]] = {}
        self._current: Optional[str] = None
        self._events: List[dict] = []
        self._started = time.time()

    # ------------------------------------------------------------- mutations
    def set(self, node_id: str, status: str, detail: Optional[str] = None) -> None:
        with self._lock:
            self._status[node_id] = status
            self._detail[node_id] = detail
            if status == RUNNING:
                self._current = node_id
            elif status in (DONE, FAILED) and self._current == node_id:
                self._current = None
            suffix = f" — {detail}" if detail else ""
            self._append_event(f"{node_id}: {status}{suffix}")

    def event(self, message: str) -> None:
        with self._lock:
            self._append_event(message)

    def _append_event(self, message: str) -> None:
        self._events.append({"ts": time.time(), "msg": message})
        # Keep the tail; the UI only shows recent activity.
        self._events = self._events[-100:]

    # --------------------------------------------------------------- reading
    def snapshot(self) -> dict:
        with self._lock:
            counts = {PENDING: 0, RUNNING: 0, RECOVERING: 0, DONE: 0, FAILED: 0}
            nodes = []
            for n in self._meta:
                st = self._status[n["id"]]
                counts[st] = counts.get(st, 0) + 1
                nodes.append({**n, "status": st, "detail": self._detail.get(n["id"])})
            return {
                "graph_title": self.graph_title,
                "nodes": nodes,
                "edges": self._edges,
                "current": self._current,
                "counts": counts,
                "total": len(self._meta),
                "elapsed": time.time() - self._started,
                "events": list(reversed(self._events)),  # newest first
            }
