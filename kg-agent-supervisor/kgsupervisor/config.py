"""Configuration loading.

Config is a YAML file (see ``config.example.yaml``). Any value may be
overridden by an environment variable using the ``KGS_`` prefix and ``__`` to
descend into nested keys, e.g. ``KGS_CHAT__GOOGLE__SPACE=spaces/AAAA``.

Secrets (webhook URL, credentials path) should come from the environment rather
than being committed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import List, Optional, get_type_hints


# --------------------------------------------------------------------- schema
@dataclass
class MockConfig:
    fail_every: int = 4          # pretend to hang on every Nth message (0 = never)
    latency_seconds: float = 0.3


@dataclass
class WebhookConfig:
    url: Optional[str] = None
    interactive: bool = True     # prompt operator to paste replies on console


@dataclass
class GoogleConfig:
    space: Optional[str] = None              # "spaces/AAAA…"
    credentials_file: Optional[str] = None   # service-account JSON key
    # Hybrid posting: if set, prompts are POSTed via this incoming-webhook URL
    # while replies are still READ via the API. Leave null to post via the API.
    webhook_url: Optional[str] = None
    # Only treat messages from this sender as the agent's reply. Match against
    # the sender display name (substring) or resource name. Strongly recommended
    # when posting via webhook so we never read our own prompts back.
    bot_name: Optional[str] = None
    # Your agent streams by EDITING one message until it's done. When true we
    # watch that message and only accept it once the edits settle.
    track_edits: bool = True
    # Quiet period (seconds) with no further edits that marks a reply "final".
    stability_seconds: float = 8.0
    # If your agent appends a sentinel to its finished message (recommended),
    # set it here and a reply is accepted the instant the marker appears,
    # regardless of further edits. e.g. "[[END]]" or "✅ done".
    final_marker: Optional[str] = None


@dataclass
class ChatConfig:
    transport: str = "mock"                  # mock | webhook | google_chat
    mock: MockConfig = field(default_factory=MockConfig)
    webhook: WebhookConfig = field(default_factory=WebhookConfig)
    google: GoogleConfig = field(default_factory=GoogleConfig)


@dataclass
class ReadinessConfig:
    # Shell command whose exit code 0 means "the agent is back up", e.g.
    #   "ssh cloudtop -- systemctl is-active --quiet my-agent.service"
    # If null, we just wait restart_grace_seconds after restarting.
    command: Optional[str] = None
    timeout_seconds: float = 120.0   # how long to keep probing before giving up
    poll_interval: float = 5.0


@dataclass
class RestartConfig:
    # chat  -> post chat_command into the space (only works if the agent is
    #          still alive enough to read it)
    # shell -> run shell_command (e.g. ssh cloudtop + systemctl restart) — the
    #          reliable path when the agent has gone silent
    # both  -> shell first, then chat
    method: str = "chat"
    chat_command: str = "/restart"
    # Full shell command run via the OS shell. Typically an SSH into your
    # cloudtop, e.g.
    #   "ssh cloudtop -- sudo systemctl restart my-agent.service"
    shell_command: Optional[str] = None
    shell_timeout_seconds: float = 60.0
    readiness: ReadinessConfig = field(default_factory=ReadinessConfig)


@dataclass
class RecoveryConfig:
    restart: RestartConfig = field(default_factory=RestartConfig)
    # Seconds to wait after restart (when there is no readiness probe) before
    # re-priming context, and how long to wait for the agent to ack the recap.
    restart_grace_seconds: float = 10.0
    # How many times to attempt recovery for a single node before giving up.
    max_attempts: int = 4
    # Exponential backoff base between recovery attempts.
    backoff_base_seconds: float = 5.0
    backoff_max_seconds: float = 120.0
    # Phrases that mean "the agent died / hung up". Empty list = use defaults.
    failure_phrases: List[str] = field(default_factory=list)


@dataclass
class RunConfig:
    response_timeout: float = 120.0   # how long to wait for a reply (hang cutoff)
    poll_interval: float = 3.0        # how often to poll for new replies
    inter_node_delay: float = 2.0     # politeness delay between tasks
    thread_key: Optional[str] = None  # keep the whole run in one Chat thread
    state_file: str = ".kgs_state.json"


@dataclass
class Config:
    graph_file: str = "examples/sample_graph.json"
    chat: ChatConfig = field(default_factory=ChatConfig)
    recovery: RecoveryConfig = field(default_factory=RecoveryConfig)
    run: RunConfig = field(default_factory=RunConfig)


# ---------------------------------------------------------------------- load
def load_config(path: Optional[str] = None) -> Config:
    data: dict = {}
    if path:
        data = _read_yaml(path)
    cfg = _from_dict(Config, data)
    _apply_env_overrides(cfg, prefix="KGS")
    return cfg


def _read_yaml(path: str) -> dict:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("Reading a config file needs: pip install pyyaml") from exc
    text = Path(path).read_text(encoding="utf-8")
    return yaml.safe_load(text) or {}


def _from_dict(cls, data: dict):
    """Recursively build a dataclass, ignoring unknown keys."""
    # ``from __future__ import annotations`` makes f.type a *string*, so resolve
    # the real types to detect nested dataclasses.
    hints = get_type_hints(cls)
    kwargs = {}
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        ftype = hints.get(f.name, f.type)
        if is_dataclass(ftype) and isinstance(value, dict):
            kwargs[f.name] = _from_dict(ftype, value)
        else:
            kwargs[f.name] = value
    return cls(**kwargs)


def _apply_env_overrides(obj, prefix: str) -> None:
    """Override scalar fields from env vars like PREFIX__SUB__KEY."""
    if not is_dataclass(obj):
        return
    for f in fields(obj):
        child = getattr(obj, f.name)
        env_key = f"{prefix}__{f.name}".upper()
        if is_dataclass(child):
            _apply_env_overrides(child, env_key)
        elif env_key in os.environ:
            setattr(obj, f.name, _coerce(os.environ[env_key], child))


def _coerce(raw: str, current):
    if isinstance(current, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int) and not isinstance(current, bool):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    return raw
