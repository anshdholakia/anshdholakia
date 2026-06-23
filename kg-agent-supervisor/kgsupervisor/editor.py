"""Editable, persisted knowledge graph backing the dashboard's editor mode.

Where :class:`~kgsupervisor.status.StatusBoard` is a read-only view of a *run*,
``GraphStore`` is a mutable view of the graph *file*: add / edit / delete nodes
and dependency edges from the UI, then save back to JSON. Every mutation is
validated (unique ids, known dependencies, no cycles) before it's accepted, so
the graph on disk is always runnable.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import List, Optional

from .graph import KnowledgeGraph


class GraphError(ValueError):
    """A rejected edit (duplicate id, unknown dependency, cycle, ...)."""


class GraphStore:
    editable = True

    def __init__(self, path: str):
        self.path = str(path)
        self._lock = threading.RLock()
        self.title = "New graph"
        self._nodes: List[dict] = []  # {id, prompt, depends_on, done_when}
        self._load()

    # ------------------------------------------------------------------ load
    def _load(self) -> None:
        p = Path(self.path)
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            self.title = data.get("title", "New graph")
            for raw in data.get("nodes", []):
                self._nodes.append(
                    {
                        "id": str(raw["id"]),
                        "prompt": str(raw.get("prompt", "")),
                        "depends_on": [str(d) for d in raw.get("depends_on", [])],
                        "done_when": raw.get("done_when"),
                    }
                )
        self._validate(self._nodes)

    # ------------------------------------------------------------- mutations
    def add_node(
        self,
        node_id: str,
        prompt: str = "",
        depends_on: Optional[List[str]] = None,
        done_when: Optional[str] = None,
    ) -> None:
        with self._lock:
            node_id = (node_id or "").strip()
            if not node_id:
                raise GraphError("Node id is required.")
            if self._find(node_id):
                raise GraphError(f"A node named {node_id!r} already exists.")
            candidate = self._nodes + [
                {
                    "id": node_id,
                    "prompt": prompt or "",
                    "depends_on": _clean(depends_on),
                    "done_when": done_when or None,
                }
            ]
            self._validate(candidate)
            self._nodes = candidate

    def update_node(
        self,
        node_id: str,
        prompt: Optional[str] = None,
        depends_on: Optional[List[str]] = None,
        done_when: Optional[str] = None,
    ) -> None:
        with self._lock:
            existing = self._find(node_id)
            if not existing:
                raise GraphError(f"No node named {node_id!r}.")
            updated = dict(existing)
            if prompt is not None:
                updated["prompt"] = prompt
            if depends_on is not None:
                updated["depends_on"] = _clean(depends_on)
            if done_when is not None:
                updated["done_when"] = done_when or None
            candidate = [updated if n["id"] == node_id else n for n in self._nodes]
            self._validate(candidate)
            self._nodes = candidate

    def delete_node(self, node_id: str) -> None:
        with self._lock:
            if not self._find(node_id):
                raise GraphError(f"No node named {node_id!r}.")
            candidate = []
            for n in self._nodes:
                if n["id"] == node_id:
                    continue
                copy = dict(n)
                copy["depends_on"] = [d for d in n["depends_on"] if d != node_id]
                candidate.append(copy)
            self._validate(candidate)
            self._nodes = candidate

    def set_title(self, title: str) -> None:
        with self._lock:
            self.title = (title or "").strip() or "Untitled graph"

    def save(self) -> None:
        with self._lock:
            payload = {
                "title": self.title,
                "nodes": [self._serialize(n) for n in self._nodes],
            }
            directory = os.path.dirname(os.path.abspath(self.path)) or "."
            fd, tmp = tempfile.mkstemp(dir=directory, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2)
                    fh.write("\n")
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

    # --------------------------------------------------------------- reading
    def snapshot(self) -> dict:
        with self._lock:
            graph = self._as_graph(self._nodes)
            levels = graph.levels()
            nodes = [
                {
                    "id": n["id"],
                    "prompt": n["prompt"],
                    "depends_on": list(n["depends_on"]),
                    "done_when": n.get("done_when"),
                    "level": levels[n["id"]],
                    "status": "pending",
                    "detail": None,
                }
                for n in self._nodes
            ]
            edges = [{"source": s, "target": t} for s, t in graph.edges()]
            return {
                "graph_title": self.title,
                "nodes": nodes,
                "edges": edges,
                "current": None,
                "counts": {
                    "pending": len(nodes),
                    "running": 0,
                    "recovering": 0,
                    "done": 0,
                    "failed": 0,
                },
                "total": len(nodes),
                "elapsed": 0,
                "events": [],
                "editable": True,
                "path": self.path,
            }

    # -------------------------------------------------------------- internals
    def _find(self, node_id: str) -> Optional[dict]:
        return next((n for n in self._nodes if n["id"] == node_id), None)

    def _as_graph(self, nodes: List[dict]) -> KnowledgeGraph:
        return KnowledgeGraph.from_dict({"title": self.title, "nodes": nodes})

    def _validate(self, nodes: List[dict]) -> None:
        try:
            self._as_graph(nodes)
        except ValueError as exc:
            raise GraphError(str(exc)) from exc

    @staticmethod
    def _serialize(n: dict) -> dict:
        out = {"id": n["id"], "prompt": n["prompt"], "depends_on": n["depends_on"]}
        if n.get("done_when"):
            out["done_when"] = n["done_when"]
        return out


def _clean(deps: Optional[List[str]]) -> List[str]:
    seen, out = set(), []
    for d in deps or []:
        d = str(d).strip()
        if d and d not in seen:
            seen.add(d)
            out.append(d)
    return out
