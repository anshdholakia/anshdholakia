"""Smoke tests for graph traversal, health checks, and the recovery loop.

Run with:  python -m pytest   (or)   python tests/test_core.py
These tests use only the standard library + the mock transport, so they run
without any credentials or network access.
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kgsupervisor.config import Config  # noqa: E402
from kgsupervisor.graph import KnowledgeGraph, Node  # noqa: E402
from kgsupervisor.health import HealthChecker, Verdict  # noqa: E402
from kgsupervisor.supervisor import Supervisor  # noqa: E402


def test_topological_order_respects_dependencies():
    g = KnowledgeGraph(
        [
            Node("c", "c", depends_on=["b"]),
            Node("a", "a"),
            Node("b", "b", depends_on=["a"]),
        ]
    )
    order = [n.id for n in g.topological_order()]
    assert order.index("a") < order.index("b") < order.index("c")


def test_cycle_is_rejected():
    try:
        KnowledgeGraph([Node("a", "a", depends_on=["b"]), Node("b", "b", depends_on=["a"])])
    except ValueError as e:
        assert "cycle" in str(e).lower()
    else:
        raise AssertionError("expected a cycle error")


def test_unknown_dependency_is_rejected():
    try:
        KnowledgeGraph([Node("a", "a", depends_on=["ghost"])])
    except ValueError as e:
        assert "unknown" in str(e).lower()
    else:
        raise AssertionError("expected an unknown-dependency error")


def test_health_classification():
    hc = HealthChecker()
    assert hc.evaluate("Here is a real answer.").verdict is Verdict.OK
    assert hc.evaluate(None).verdict is Verdict.HUNG
    assert hc.evaluate("   ").verdict is Verdict.EMPTY
    assert hc.evaluate("Sorry, I hung up and lost context.").verdict is Verdict.DEAD
    assert hc.evaluate("503 service unavailable").verdict is Verdict.DEAD


def test_supervisor_completes_with_recovering_mock():
    """The mock bot hangs periodically; the supervisor must still finish."""
    graph = {
        "title": "test graph",
        "nodes": [
            {"id": "n1", "prompt": "do 1"},
            {"id": "n2", "prompt": "do 2", "depends_on": ["n1"]},
            {"id": "n3", "prompt": "do 3", "depends_on": ["n2"]},
        ],
    }
    with tempfile.TemporaryDirectory() as d:
        graph_path = os.path.join(d, "g.json")
        with open(graph_path, "w") as fh:
            import json

            json.dump(graph, fh)

        cfg = Config()
        cfg.graph_file = graph_path
        cfg.chat.transport = "mock"
        cfg.chat.mock.fail_every = 2       # hang often to exercise recovery
        cfg.chat.mock.latency_seconds = 0.0
        cfg.recovery.backoff_base_seconds = 0.0
        cfg.recovery.restart_grace_seconds = 0.0
        cfg.run.inter_node_delay = 0.0
        cfg.run.response_timeout = 1.0
        cfg.run.poll_interval = 0.01
        cfg.run.state_file = os.path.join(d, "state.json")

        sup = Supervisor(cfg)
        sup.run()  # should not raise

        assert sup.state.is_done("n1")
        assert sup.state.is_done("n2")
        assert sup.state.is_done("n3")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("\nAll tests passed.")
