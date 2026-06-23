"""The supervisor loop.

For each node in dependency order it:

1. Builds a prompt (the node's task + the outputs of its dependencies).
2. Posts it to the Google Chat space and waits for the bot's reply.
3. Health-checks the reply.
4. If healthy → record the answer and move on.
   If hung/dead/empty → run a *recovery cycle*: send a restart, wait for the
   bot to come back, replay a recap of the conversation, and re-issue the
   prompt — with exponential backoff, up to ``recovery.max_attempts`` times.

Completed nodes are persisted, so killing and re-running the process resumes
from where it left off.
"""

from __future__ import annotations

import logging
import time

from .agent import AgentSession
from .chat import ChatClient, build_chat_client
from .config import Config
from .graph import KnowledgeGraph, Node
from .health import HealthChecker, Verdict
from .restart import perform_restart, wait_until_ready
from .state import RunState

log = logging.getLogger("kgs")


class TaskFailed(RuntimeError):
    """Raised when a node cannot be completed even after all recovery attempts."""


class Supervisor:
    def __init__(self, config: Config):
        self.config = config
        self.graph = KnowledgeGraph.from_file(config.graph_file)
        self.session = AgentSession(self.graph.title)
        self.client: ChatClient = build_chat_client(config)
        self.health = HealthChecker(
            failure_phrases=config.recovery.failure_phrases or None
        )
        self.state = RunState.load(config.run.state_file, self.graph.title)
        # Restore prior results into the session so dependency context survives
        # a process restart.
        for node_id, answer in self.state.completed.items():
            if node_id in self.graph:
                turn = self.session.start_turn(node_id, self.graph.get(node_id).prompt)
                self.session.complete_turn(turn, answer)

    # ------------------------------------------------------------------- run
    def run(self) -> None:
        order = self.graph.topological_order()
        total = len(order)
        log.info("Knowledge graph %r: %d task(s).", self.graph.title, total)
        if not self.client.can_read_replies:
            log.warning(
                "Transport %r cannot read replies; hang-detection relies on "
                "timeouts/operator input only.",
                self.config.chat.transport,
            )

        for idx, node in enumerate(order, start=1):
            if self.state.is_done(node.id):
                log.info("[%d/%d] %s — already done, skipping.", idx, total, node.id)
                continue
            log.info("[%d/%d] %s — starting.", idx, total, node.id)
            answer = self._process_node(node)
            self.state.mark_done(node.id, answer)
            log.info("[%d/%d] %s — done.", idx, total, node.id)
            if idx < total:
                time.sleep(self.config.run.inter_node_delay)

        log.info("All tasks complete. ✅")
        self.client.close()

    # -------------------------------------------------------------- per node
    def _process_node(self, node: Node) -> str:
        prompt = self._build_prompt(node)
        turn = self.session.start_turn(node.id, prompt)

        attempt = 0
        while True:
            response = self._ask(prompt)
            verdict = self.health.evaluate(response.text if response else None)

            if verdict.healthy:
                assert response is not None
                self.session.complete_turn(turn, response.text)
                return response.text

            attempt += 1
            log.warning(
                "%s — unhealthy reply (%s): %s [recovery attempt %d/%d]",
                node.id,
                verdict.verdict.value,
                verdict.reason,
                attempt,
                self.config.recovery.max_attempts,
            )
            if attempt > self.config.recovery.max_attempts:
                raise TaskFailed(
                    f"Node {node.id!r} failed after "
                    f"{self.config.recovery.max_attempts} recovery attempts: "
                    f"{verdict.reason}"
                )

            self._recover(verdict.verdict, attempt)
            # After recovery we re-issue the *same* prompt; context was replayed
            # inside _recover().

    # ----------------------------------------------------------- conversation
    def _ask(self, prompt: str):
        self.client.post(prompt, thread_key=self.config.run.thread_key)
        return self.client.wait_for_reply(
            timeout=self.config.run.response_timeout,
            poll_interval=self.config.run.poll_interval,
        )

    def _build_prompt(self, node: Node) -> str:
        parts = []
        ctx = self.session.dependency_context(node.depends_on)
        if ctx:
            parts.append(
                "Here are the results of the prior steps you'll build on:\n" + ctx
            )
        parts.append(f"## Task '{node.id}'\n{node.prompt}")
        if node.done_when:
            parts.append(f"(This task is complete when: {node.done_when})")
        return "\n\n".join(parts)

    # -------------------------------------------------------------- recovery
    def _recover(self, verdict: Verdict, attempt: int) -> None:
        rec = self.config.recovery

        # 1) Back off (exponential, capped) before touching the agent again.
        delay = min(
            rec.backoff_base_seconds * (2 ** (attempt - 1)),
            rec.backoff_max_seconds,
        )
        log.info("Recovery: backing off %.0fs before restarting the agent.", delay)
        time.sleep(delay)

        # 2) Restart the agent (chat command and/or systemctl over SSH) and wait
        #    until it reports healthy before re-priming it.
        perform_restart(self.config, self.client, self.config.run.thread_key)
        wait_until_ready(self.config)

        # 3) Replay a compact recap so the freshly-restarted agent has context.
        recap = self.session.recap()
        if recap:
            log.info("Recovery: replaying conversation recap to restore context.")
            self.client.post(recap, thread_key=self.config.run.thread_key)
            # Let it acknowledge; ignore the content.
            self.client.wait_for_reply(
                timeout=rec.restart_grace_seconds,
                poll_interval=self.config.run.poll_interval,
            )

        log.info("Recovery: context restored; resuming the task.")
