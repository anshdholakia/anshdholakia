"""Knowledge-graph model and traversal.

The graph is a plain JSON document. Each node is a unit of work ("task") that
gets turned into a prompt for the agent. Edges express dependencies: a node is
only prompted once all of the nodes it ``depends_on`` are done, and the outputs
of those dependencies are fed in as context.

Example (see ``examples/sample_graph.json``)::

    {
      "title": "Quarterly competitor brief",
      "nodes": [
        {
          "id": "gather",
          "prompt": "List our top 3 competitors and their latest releases.",
          "depends_on": []
        },
        {
          "id": "analyze",
          "prompt": "Compare those releases to our roadmap and flag gaps.",
          "depends_on": ["gather"]
        }
      ]
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional


@dataclass
class Node:
    """A single task in the knowledge graph."""

    id: str
    prompt: str
    depends_on: List[str] = field(default_factory=list)
    # Optional human-readable hint describing when this node is considered done.
    # Surfaced to the agent so it knows the exit condition.
    done_when: Optional[str] = None
    # Free-form metadata the caller may attach (priority, tags, etc.).
    meta: Dict[str, object] = field(default_factory=dict)


class KnowledgeGraph:
    """An in-memory DAG of :class:`Node` objects with topological traversal."""

    def __init__(self, nodes: Iterable[Node], title: str = "Untitled graph"):
        self.title = title
        self._nodes: Dict[str, Node] = {}
        for node in nodes:
            if node.id in self._nodes:
                raise ValueError(f"Duplicate node id: {node.id!r}")
            self._nodes[node.id] = node
        self._validate()

    # ------------------------------------------------------------------ load
    @classmethod
    def from_file(cls, path: str | Path) -> "KnowledgeGraph":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> "KnowledgeGraph":
        if "nodes" not in data or not isinstance(data["nodes"], list):
            raise ValueError("Graph JSON must contain a 'nodes' array.")
        nodes = []
        for raw in data["nodes"]:
            if "id" not in raw or "prompt" not in raw:
                raise ValueError("Every node needs an 'id' and a 'prompt'.")
            nodes.append(
                Node(
                    id=str(raw["id"]),
                    prompt=str(raw["prompt"]),
                    depends_on=[str(d) for d in raw.get("depends_on", [])],
                    done_when=raw.get("done_when"),
                    meta=raw.get("meta", {}) or {},
                )
            )
        return cls(nodes, title=str(data.get("title", "Untitled graph")))

    # -------------------------------------------------------------- accessors
    def __len__(self) -> int:
        return len(self._nodes)

    def __contains__(self, node_id: str) -> bool:
        return node_id in self._nodes

    def get(self, node_id: str) -> Node:
        return self._nodes[node_id]

    def nodes(self) -> List[Node]:
        return list(self._nodes.values())

    def dependencies(self, node_id: str) -> List[Node]:
        return [self._nodes[d] for d in self._nodes[node_id].depends_on]

    # ------------------------------------------------------------- traversal
    def topological_order(self) -> List[Node]:
        """Return nodes in dependency order (Kahn's algorithm).

        Raises ``ValueError`` if the graph contains a cycle.
        """
        indegree = {nid: 0 for nid in self._nodes}
        for node in self._nodes.values():
            for dep in node.depends_on:
                indegree[node.id] += 1  # node waits on each dependency

        # Stable ordering: process ready nodes in insertion order.
        ready = [nid for nid in self._nodes if indegree[nid] == 0]
        order: List[Node] = []
        while ready:
            nid = ready.pop(0)
            order.append(self._nodes[nid])
            for other in self._nodes.values():
                if nid in other.depends_on:
                    indegree[other.id] -= 1
                    if indegree[other.id] == 0:
                        ready.append(other.id)

        if len(order) != len(self._nodes):
            unresolved = sorted(set(self._nodes) - {n.id for n in order})
            raise ValueError(f"Knowledge graph has a cycle involving: {unresolved}")
        return order

    # -------------------------------------------------------------- internals
    def _validate(self) -> None:
        for node in self._nodes.values():
            for dep in node.depends_on:
                if dep not in self._nodes:
                    raise ValueError(
                        f"Node {node.id!r} depends on unknown node {dep!r}."
                    )
        # Surface cycles early.
        self.topological_order()
