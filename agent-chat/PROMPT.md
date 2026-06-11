# Prompt for internal agents — build a localhost multi-agent chat hub

Copy everything below the line into the internal agent/chatbot.

---

I want you to build a **multi-agent chat system** that runs entirely on my
machine (Cloudtop, with agents running under WSL via BOQ microservices). No
Google Chat Spaces, no Workspace APIs — just a localhost website + a small
server. I will open the website in my browser, my agents will join the same
chat room programmatically, and **every agent in the room must receive every
message and be able to respond.**

## Architecture (build exactly this)

```
 Browser UI (localhost:8080) ◄──WebSocket──►  ┌─────────────────┐
                                              │  Chat hub server │ ◄──WebSocket──► long-lived agent process
                                              │  (rooms, fan-out)│ ──HTTP POST───► BOQ microservice endpoint
                                              └─────────────────┘ ◄──HTTP POST─── (agent replies back over HTTP)
```

Three components:

1. **Chat hub server** — one process listening on `localhost:8080`. Hosts
   named *rooms*, tracks members (humans + agents), and fans every message out
   to every member of the room. It also serves the web UI as static files.
   Language: your choice (Node, Go, or Python — whatever you can build and run
   on this Cloudtop most easily). Keep dependencies minimal.

2. **Web UI** — a single page served at `http://localhost:8080/`. Sidebar:
   my display name, room name field, join/leave button, and a live member list
   showing who is in the room (with a visual distinction between humans and
   agents). Main pane: scrolling message log (sender name + role + text,
   agents visually distinct from humans) and a message composer. Connects to
   the hub over WebSocket. No build step — one HTML file with inline JS/CSS is
   fine.

3. **Agent connector** — a small client library/script my agents use to join a
   room. Two transports, both required:
   - **WebSocket mode** for agents that run as long-lived processes: connect,
     send a `join` frame, then receive/send `message` frames.
   - **HTTP/webhook mode** for BOQ microservices that prefer request/response:
     the agent registers a webhook URL with the hub; the hub POSTs each new
     room message to that URL; the agent replies by POSTing back to the hub's
     message endpoint. Delivery must be fire-and-forget (a slow agent must
     never block the room); the hub should not wait on the webhook response.

## Wire protocol (use this exact JSON shape)

WebSocket — client/agent → hub:

```jsonc
{ "type": "join",    "room": "general", "as": "user" | "agent", "name": "MyAgent" }
{ "type": "message", "text": "hello room" }   // sender + room inferred from connection
{ "type": "leave" }
```

WebSocket — hub → client/agent:

```jsonc
{ "type": "joined",   "room": "general", "you": { "id", "name", "role" },
  "members": [{ "id", "name", "role" }], "history": [ ...recent messages ] }
{ "type": "message",  "msgId": "..", "room": "..", "text": "..",
  "from": "displayName", "fromId": "..", "role": "user" | "agent", "ts": 1700000000000 }
{ "type": "presence", "room": "..", "members": [ ... ] }   // sent on any join/leave
{ "type": "error",    "error": ".." }
```

HTTP endpoints on the hub:

```
POST /api/rooms/:room/agents    body { "name": "...", "webhook": "http://localhost:PORT/path" }
     → registers a webhook agent in the room; returns { "agentId": "..." }.
       From then on the hub POSTs { "room": "...", "message": {<message object>} }
       to that webhook for every chat message in the room.

POST /api/rooms/:room/messages  body { "text": "...", "from": "...", "fromId": "...", "role": "agent" }
     → posts a message into the room (how webhook agents reply; also useful for curl tests).

GET  /api/rooms/:room/history   → JSON array of recent messages (cap ~200, in-memory is fine).
```

## Behavior requirements

- **Fan-out:** every `message` goes to every room member except the sender
  (echo it back to the sender too so their UI is consistent).
- **Role tagging:** every message carries `role: "user"` or `"agent"`, set by
  the hub from the sender's join info — never trusted from the message body on
  the WebSocket path.
- **Loop avoidance (critical):** in the agent connector, the default policy is
  *reply only to `role: "user"` messages*. Exception: if a message contains
  `@<agentname>` (case-insensitive), the named agent may reply even if the
  sender was another agent. Make the policy a flag so I can enable
  agent-to-agent chatter deliberately. Never reply to your own messages
  (compare `fromId`).
- **Reconnect:** the WebSocket agent connector must auto-reconnect with
  exponential backoff (1s → 30s cap) and rejoin its room.
- **Presence:** member list updates pushed to all members on every join/leave.
- **History:** new joiners receive the recent message history in the `joined`
  frame.
- **State:** in-memory only is acceptable; this is a localhost tool.
- **Bind to localhost only.** No auth needed for v1, but structure the hub so
  an auth check can be added at the join/register points later.

## BOQ integration (the part that matters most to me)

My agents are BOQ microservices on this machine. For each agent, generate the
integration glue:

- A handler I can mount in the BOQ service that accepts the hub's webhook POST
  (`{ "room", "message" }`), applies the loop-avoidance policy above, calls my
  agent's existing inference entry point with `message.text`, and POSTs the
  reply text to `POST /api/rooms/:room/messages` with `role: "agent"` and the
  agent's registered `fromId`.
- A one-shot registration step (startup hook or small script) that calls
  `POST /api/rooms/:room/agents` with the agent's name and its webhook URL,
  and stores the returned `agentId` for use as `fromId`.
- Leave the actual inference call as a clearly-marked single function
  (`generate_reply(text) -> string | None`, where `None` = stay silent) so I
  can wire in each agent's real entry point myself.

If WSL networking makes `localhost` ambiguous between the WSL guest and the
Windows host, detect/document the right host address for the webhook URL
(e.g. the WSL-to-host gateway IP) instead of assuming `localhost` works in
both directions.

## Deliverables

1. Hub server (single process, serves UI + WebSocket + HTTP API on :8080).
2. The web UI page.
3. Agent connector for WebSocket mode (CLI: `--url --room --name --reply-to-agents`).
4. BOQ webhook glue as described above.
5. A README with: how to start the hub, how to join from the browser, how to
   attach an agent in each mode, and a curl-based smoke test.

## Acceptance test (run this before telling me you're done)

1. Start the hub; open `http://localhost:8080`, join room `general` as `ansh`.
2. Attach two agents to `general` (one WebSocket, one webhook mode).
3. Send "hello" from the browser → **both** agents receive it and both replies
   appear in the browser, labeled as agents; member list shows 1 human + 2 agents.
4. Confirm the two agents do NOT respond to each other's replies (no loop).
5. Send "@<agent2name> what do you think?" as agent1 (or via curl with
   `role: "agent"`) → only agent2 replies.
6. Kill and restart the WebSocket agent → it rejoins automatically and the
   member list updates both times.
