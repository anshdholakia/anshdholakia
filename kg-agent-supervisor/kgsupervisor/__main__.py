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
    parser.add_argument(
        "--list-spaces",
        action="store_true",
        help="List the spaces/DMs your auth can see (to find a DM's API id), then exit.",
    )
    parser.add_argument(
        "--send",
        metavar="TEXT",
        help="Post one message to the configured space and print the reply, then "
        "exit. Quick connectivity test (needs only the chat.messages scope).",
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

    if args.list_spaces:
        return _list_spaces(config)

    if args.send is not None:
        return _send_test(config, args.send)

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


def _list_spaces(config) -> int:
    if config.chat.transport != "google_chat":
        print("--list-spaces needs chat.transport=google_chat.", file=sys.stderr)
        return 2
    from .chat.google_chat import list_spaces

    spaces = list_spaces(config)
    if not spaces:
        print("No spaces visible. Check your auth / that you're in the DM.")
        return 0
    print(f"{'API NAME':<28}  {'TYPE':<16}  DISPLAY NAME")
    for name, stype, display in spaces:
        label = display or ("(direct message)" if "DIRECT" in stype else "")
        print(f"{name:<28}  {stype:<16}  {label}")
    print("\nUse the API NAME (e.g. spaces/AAAA…) as KGS__CHAT__GOOGLE__SPACE.")
    return 0


def _send_test(config, text: str) -> int:
    from .chat import build_chat_client

    client = build_chat_client(config)
    target = getattr(config.chat.google, "space", None)
    print(f"Posting to {target} …")
    try:
        client.post(text)
    except Exception as exc:  # noqa: BLE001 - surface the API reason
        print(f"\nPOST failed: {exc}", file=sys.stderr)
        return 1
    print("Posted OK. Waiting up to 60s for a reply …")
    reply = client.wait_for_reply(timeout=60, poll_interval=3)
    if reply is None:
        print("No reply within 60s (agent may still be editing, or it hung).")
    else:
        print(f"\n--- reply from {reply.sender} ---\n{reply.text}\n")
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
