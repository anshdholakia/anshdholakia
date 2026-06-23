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

from kgsupervisor.config import Config, _from_dict  # noqa: E402
from kgsupervisor.graph import KnowledgeGraph, Node  # noqa: E402
from kgsupervisor.health import HealthChecker, Verdict  # noqa: E402
from kgsupervisor.restart import perform_restart, wait_until_ready  # noqa: E402
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

        assert sup.state.answer_for("n1") is not None
        assert sup.state.answer_for("n2") is not None
        assert sup.state.answer_for("n3") is not None


def test_nested_config_from_dict():
    """Nested YAML sections must populate nested dataclasses (regression)."""
    cfg = _from_dict(
        Config,
        {
            "chat": {"transport": "google_chat", "google": {"bot_name": "Gemini Agent"}},
            "recovery": {
                "restart": {
                    "method": "both",
                    "shell_command": "echo restarting",
                    "readiness": {"command": "true", "poll_interval": 1},
                }
            },
        },
    )
    assert cfg.chat.transport == "google_chat"
    assert cfg.chat.google.bot_name == "Gemini Agent"
    assert cfg.recovery.restart.method == "both"
    assert cfg.recovery.restart.shell_command == "echo restarting"
    assert cfg.recovery.restart.readiness.command == "true"
    assert cfg.recovery.restart.readiness.poll_interval == 1


def test_shell_restart_and_readiness_run():
    """method=shell runs the command; readiness probe waits for exit 0."""
    calls = []

    class _Client:
        def post(self, text, thread_key=None):
            calls.append(text)

    cfg = Config()
    cfg.recovery.restart.method = "shell"
    cfg.recovery.restart.shell_command = "exit 0"
    cfg.recovery.restart.readiness.command = "true"
    cfg.recovery.restart.readiness.timeout_seconds = 5
    cfg.recovery.restart.readiness.poll_interval = 0.1

    perform_restart(cfg, _Client(), thread_key=None)
    assert calls == []  # shell method must NOT post to chat
    assert wait_until_ready(cfg) is True


def test_status_board_snapshot_tracks_state():
    from kgsupervisor.status import StatusBoard, RUNNING, DONE

    g = KnowledgeGraph(
        [Node("a", "do a"), Node("b", "do b", depends_on=["a"])],
        title="t",
    )
    board = StatusBoard(g)
    snap = board.snapshot()
    assert snap["total"] == 2
    assert snap["counts"]["pending"] == 2
    assert {e["source"] for e in snap["edges"]} == {"a"}
    # levels: a=0, b=1 (used for layout)
    levels = {n["id"]: n["level"] for n in snap["nodes"]}
    assert levels == {"a": 0, "b": 1}

    board.set("a", RUNNING)
    board.set("a", DONE)
    snap = board.snapshot()
    by_id = {n["id"]: n["status"] for n in snap["nodes"]}
    assert by_id["a"] == "done" and by_id["b"] == "pending"
    assert snap["counts"]["done"] == 1


def test_dashboard_serves_status_over_http():
    import json
    import urllib.request
    from kgsupervisor.dashboard import Dashboard
    from kgsupervisor.status import StatusBoard

    g = KnowledgeGraph([Node("only", "do it")], title="dash")
    board = StatusBoard(g)
    dash = Dashboard(board, host="127.0.0.1", port=0)  # port 0 = pick a free one
    dash.start()
    try:
        url = f"http://127.0.0.1:{dash.actual_port}/api/status"
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode())
        assert data["graph_title"] == "dash"
        assert data["nodes"][0]["id"] == "only"
        with urllib.request.urlopen(
            f"http://127.0.0.1:{dash.actual_port}/", timeout=5
        ) as resp:
            html = resp.read().decode()
        assert "<svg" in html and "knowledge graph" in html
    finally:
        dash.stop()


def test_graphstore_add_connect_save_and_reject_cycle():
    from kgsupervisor.editor import GraphStore, GraphError

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "g.json")
        store = GraphStore(path)
        store.add_node("a", "do a")
        store.add_node("b", "do b", depends_on=["a"])
        snap = store.snapshot()
        assert snap["editable"] is True
        assert snap["total"] == 2
        assert {(e["source"], e["target"]) for e in snap["edges"]} == {("a", "b")}

        # duplicate id rejected
        try:
            store.add_node("a", "dup")
        except GraphError:
            pass
        else:
            raise AssertionError("expected duplicate-id rejection")

        # cycle rejected (a depends on b, b already depends on a)
        try:
            store.update_node("a", depends_on=["b"])
        except GraphError:
            pass
        else:
            raise AssertionError("expected cycle rejection")

        # save + reload round-trips
        store.save()
        import json as _json

        reloaded = _json.loads(open(path).read())
        assert reloaded["nodes"][1]["depends_on"] == ["a"]

        # delete removes the node and any edges into it
        store.delete_node("a")
        snap = store.snapshot()
        assert snap["total"] == 1
        assert snap["nodes"][0]["depends_on"] == []


def test_resume_cache_invalidated_when_prompt_changes():
    """Editing a node's prompt must make it re-run, not show as cached-done."""
    import json

    graph_v1 = {"title": "g", "nodes": [{"id": "n1", "prompt": "original"}]}
    with tempfile.TemporaryDirectory() as d:
        graph_path = os.path.join(d, "g.json")
        state_path = os.path.join(d, "state.json")

        def make_cfg():
            cfg = Config()
            cfg.graph_file = graph_path
            cfg.chat.transport = "mock"
            cfg.chat.mock.fail_every = 0
            cfg.chat.mock.latency_seconds = 0.0
            cfg.run.inter_node_delay = 0.0
            cfg.run.response_timeout = 1.0
            cfg.run.poll_interval = 0.01
            cfg.run.state_file = state_path
            return cfg

        with open(graph_path, "w") as fh:
            json.dump(graph_v1, fh)
        Supervisor(make_cfg()).run()  # completes n1, writes state

        # Re-run unchanged: n1 should be skipped (cached).
        sup2 = Supervisor(make_cfg())
        assert sup2.state.is_done("n1", sup2._fingerprint(sup2.graph.get("n1")))

        # Now edit the prompt and confirm the cache no longer matches.
        graph_v2 = {"title": "g", "nodes": [{"id": "n1", "prompt": "EDITED"}]}
        with open(graph_path, "w") as fh:
            json.dump(graph_v2, fh)
        sup3 = Supervisor(make_cfg())
        assert not sup3.state.is_done("n1", sup3._fingerprint(sup3.graph.get("n1")))


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("\nAll tests passed.")
