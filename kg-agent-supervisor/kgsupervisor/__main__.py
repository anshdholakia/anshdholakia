"""Command-line entry point.

Usage::

    python -m kgsupervisor --config config.yaml
    python -m kgsupervisor --graph examples/sample_graph.json   # mock transport
    python -m kgsupervisor --reset --config config.yaml         # forget progress
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load_config
from .logging_setup import setup_logging
from .supervisor import Supervisor, TaskFailed


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="kgsupervisor",
        description="Walk a knowledge graph, prompt a Gemini Google Chat bot, "
        "and auto-recover when it hangs.",
    )
    parser.add_argument("--config", help="Path to YAML config file.")
    parser.add_argument("--graph", help="Override graph_file from config.")
    parser.add_argument(
        "--transport",
        choices=["mock", "webhook", "google_chat"],
        help="Override the chat transport.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Delete the saved progress file before running.",
    )
    parser.add_argument("--log-level", default="INFO")
    parser.add_argument("--log-file", default=None)
    args = parser.parse_args(argv)

    setup_logging(args.log_level, args.log_file)
    config = load_config(args.config)

    if args.graph:
        config.graph_file = args.graph
    if args.transport:
        config.chat.transport = args.transport

    if args.reset:
        state_path = Path(config.run.state_file)
        if state_path.exists():
            state_path.unlink()

    try:
        Supervisor(config).run()
    except TaskFailed as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. Progress is saved; re-run to resume.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
