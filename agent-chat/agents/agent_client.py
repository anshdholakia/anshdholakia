#!/usr/bin/env python3
"""
Reference agent connector
--------------------------
A minimal client that joins a chat room on the proxy, listens for every
message, and replies. Run one of these per agent (on your Cloudtop / WSL box).

    pip install websockets
    python agent_client.py --url ws://localhost:8080/ws --room general --name Gemini-Helper

How to wire it to YOUR agent:
    Replace `generate_reply()` with a call into your BOQ microservice
    (HTTP/Stubby/gRPC). Everything else — join, receive, loop-avoidance,
    reconnect — stays the same.

Loop avoidance:
    The proxy tags each message with role = "user" or "agent". By default this
    connector only replies to messages whose role is "user", so a roomful of
    agents won't talk to each other forever. Pass --reply-to-agents to change
    that, or use @mentions (see `should_reply`).
"""

import argparse
import asyncio
import json

import websockets  # pip install websockets


def generate_reply(text: str, name: str) -> str | None:
    """Produce this agent's response to an incoming message.

    >>> THIS IS THE ONE FUNCTION YOU REPLACE. <<<
    Swap the body for a call to your BOQ agent endpoint, e.g.:

        resp = requests.post(
            "http://localhost:<boq-port>/agent:respond",
            json={"prompt": text},
            timeout=30,
        )
        return resp.json()["reply"]

    Return None to stay silent on this message.
    """
    return f"[{name}] You said: {text!r} — (replace generate_reply() with your BOQ call)"


def should_reply(msg: dict, my_name: str, reply_to_agents: bool) -> bool:
    """Decide whether this agent should respond to a given message."""
    if msg.get("fromId") == should_reply.my_id:  # never reply to our own messages
        return False
    if msg.get("role") == "agent" and not reply_to_agents:
        # Allow explicit @mentions to break the agent-to-agent silence rule.
        return f"@{my_name.lower()}" in (msg.get("text") or "").lower()
    return True


should_reply.my_id = None  # filled in once we receive our 'joined' frame


async def run(url: str, room: str, name: str, reply_to_agents: bool) -> None:
    backoff = 1
    while True:
        try:
            async with websockets.connect(url, max_size=2**20) as ws:
                await ws.send(json.dumps({"type": "join", "room": room, "as": "agent", "name": name}))
                print(f"[{name}] joining #{room} at {url}")
                backoff = 1  # reset after a successful connect

                async for raw in ws:
                    evt = json.loads(raw)

                    if evt.get("type") == "joined":
                        should_reply.my_id = evt["you"]["id"]
                        members = ", ".join(m["name"] for m in evt.get("members", []))
                        print(f"[{name}] in #{evt['room']} with: {members or '(just me)'}")
                        continue

                    if evt.get("type") != "message":
                        continue

                    if not should_reply(evt, name, reply_to_agents):
                        continue

                    print(f"[{name}] <- {evt.get('from')}: {evt.get('text')}")
                    reply = generate_reply(evt.get("text", ""), name)
                    if reply:
                        await ws.send(json.dumps({"type": "message", "text": reply}))
                        print(f"[{name}] -> {reply}")

        except (OSError, websockets.WebSocketException) as e:
            print(f"[{name}] connection error: {e}; reconnecting in {backoff}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30)


def main() -> None:
    p = argparse.ArgumentParser(description="Connect an agent to the chat proxy.")
    p.add_argument("--url", default="ws://localhost:8080/ws")
    p.add_argument("--room", default="general")
    p.add_argument("--name", default="agent")
    p.add_argument("--reply-to-agents", action="store_true", help="Also respond to other agents' messages.")
    args = p.parse_args()
    try:
        asyncio.run(run(args.url, args.room, args.name, args.reply_to_agents))
    except KeyboardInterrupt:
        print(f"\n[{args.name}] bye")


if __name__ == "__main__":
    main()
