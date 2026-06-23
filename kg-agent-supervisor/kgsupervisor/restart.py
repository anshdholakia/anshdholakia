"""Restarting the agent and confirming it came back.

Two restart strategies (config ``recovery.restart.method``):

* ``chat``  – post a command like ``/restart`` into the space. Only works if the
  agent is still alive enough to read messages.
* ``shell`` – run a shell command, typically SSHing into the cloudtop that hosts
  the agent and running ``systemctl restart``. This is the reliable path when
  the agent has gone completely silent.
* ``both``  – run the shell restart first, then post the chat command.

After restarting, :func:`wait_until_ready` either polls a readiness command
(e.g. ``systemctl is-active``) until it reports healthy, or simply waits a grace
period when no probe is configured.
"""

from __future__ import annotations

import logging
import subprocess
import time

log = logging.getLogger("kgs")


class RestartError(RuntimeError):
    """Raised when a restart cannot be performed (e.g. misconfiguration)."""


def perform_restart(config, client, thread_key) -> None:
    r = config.recovery.restart
    method = (r.method or "chat").lower()

    if method not in ("chat", "shell", "both"):
        raise RestartError(
            f"recovery.restart.method must be chat|shell|both, got {method!r}."
        )

    if method in ("shell", "both"):
        if not r.shell_command:
            raise RestartError(
                "recovery.restart.method is %r but no shell_command is set. "
                "Set e.g. 'ssh cloudtop -- sudo systemctl restart my-agent.service'."
                % method
            )
        log.info("Restart: running shell command: %s", r.shell_command)
        code, out = _run_shell(r.shell_command, r.shell_timeout_seconds)
        if code != 0:
            # Don't abort the whole run; readiness probe / retries will catch it.
            log.warning("Restart shell command exited %s. Output:\n%s", code, out.strip())
        else:
            log.info("Restart: shell command completed.")

    if method in ("chat", "both"):
        log.info("Restart: posting chat command %r.", r.chat_command)
        try:
            client.post(r.chat_command, thread_key=thread_key)
        except Exception as exc:  # noqa: BLE001 - best effort; agent may be down
            log.warning("Restart chat command failed (agent may be down): %s", exc)


def wait_until_ready(config) -> bool:
    """Block until the agent looks healthy. Returns True if it does."""
    rec = config.recovery
    probe = rec.restart.readiness

    if probe.command:
        log.info(
            "Readiness: probing with %r (up to %.0fs).",
            probe.command,
            probe.timeout_seconds,
        )
        deadline = time.time() + probe.timeout_seconds
        while time.time() < deadline:
            code, _ = _run_shell(probe.command, timeout=probe.poll_interval + 30)
            if code == 0:
                log.info("Readiness: agent reports healthy.")
                return True
            time.sleep(min(probe.poll_interval, max(0.0, deadline - time.time())))
        log.warning(
            "Readiness: agent did not become healthy within %.0fs; continuing anyway.",
            probe.timeout_seconds,
        )
        return False

    grace = rec.restart_grace_seconds
    log.info("Readiness: no probe configured; waiting %.0fs for the agent to boot.", grace)
    time.sleep(grace)
    return True


def _run_shell(command: str, timeout: float):
    """Run ``command`` via the OS shell. Returns (exit_code, combined_output)."""
    try:
        proc = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, f"(timed out after {timeout:.0f}s)"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")
