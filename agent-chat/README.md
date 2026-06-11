# Agent Chat — multi-agent chat hub (rough draft)

A reference design + working prototype for a chat interface where **multiple
agents join a room, see every message, and respond.** Built portable on
purpose so it can be handed to internal Google tooling and re-implemented on
Workspace-native infrastructure (Google Chat + Pub/Sub + BOQ).

```
                 ┌───────────────┐
   Web UI  ◄────►│               │◄────► Agent A   (persistent WebSocket, e.g. on Cloudtop/WSL)
                 │  Proxy hub    │◄────► Agent B   (persistent WebSocket)
   curl   ◄────► │  (rooms,      │
                 │   fan-out)    │──HTTP webhook──► Agent C  (BOQ microservice endpoint)
                 └───────────────┘
```

The proxy is the only stateful piece. The UI and each agent are thin clients
that speak the same small JSON protocol.

---

## What's in here

| Path | What it is |
|------|------------|
| `proxy/server.js` | The hub. Node + `ws` + `express`. Hosts rooms, tracks members, fans messages out over WebSocket **or** HTTP webhook. |
| `ui/index.html` | Single-page chat UI. No build step — the proxy serves it statically. |
| `agents/agent_client.py` | Reference agent connector. Joins a room, listens, replies. Replace one function to plug in your real agent. |

---

## Run the prototype locally

```bash
# 1. start the hub
cd proxy
npm install
npm start                      # -> http://localhost:8080

# 2. open the UI
#    http://localhost:8080  (set a name + room, click "Join room")

# 3. start one or more agents (new terminals)
cd ../agents
pip install websockets
python agent_client.py --name Gemini-Helper --room general
python agent_client.py --name Code-Reviewer --room general
```

Type in the UI; every connected agent receives the message and replies into the
room. Add more agents by launching more connectors.

---

## Message protocol (JSON)

Everything is a single JSON object per WebSocket frame.

**Client/agent → proxy**

```jsonc
{ "type": "join",    "room": "general", "as": "user"|"agent", "name": "Gemini-Helper" }
{ "type": "message", "text": "hello room" }     // sender/room inferred from the connection
{ "type": "leave" }
```

**Proxy → client/agent**

```jsonc
{ "type": "joined",   "room": "general", "you": {...}, "members": [...], "history": [...] }
{ "type": "message",  "msgId": "..", "room": "..", "text": "..",
  "from": "ansh", "fromId": "..", "role": "user"|"agent", "ts": 1700000000000 }
{ "type": "presence", "room": "general", "members": [...] }
{ "type": "error",    "error": "..." }
```

The `role` field (`user` vs `agent`) is what keeps agents from talking to each
other forever — see **Loop avoidance** below.

### HTTP surface (for BOQ / webhook agents)

A BOQ microservice usually prefers a request/response endpoint over a held-open
socket. The proxy supports that too:

```
POST /api/rooms/:room/agents      { "name": "...", "webhook": "http://host:port/path" }
        → registers an agent; the proxy POSTs each new message to that webhook.

POST /api/rooms/:room/messages    { "text": "...", "from": "...", "role": "agent" }
        → how a webhook agent posts its reply back into the room.

GET  /api/rooms/:room/history     → recent messages (debugging).
```

So a BOQ agent's loop is: **receive POST from proxy → run inference →
POST reply to `/api/rooms/:room/messages`.** No long-lived connection required.

---

## Loop avoidance (important for multi-agent rooms)

If every agent replies to every message, two agents will ping-pong forever. The
proxy tags each message with `role`, and `agent_client.py` only replies to
`role: "user"` messages by default. Override per agent with
`--reply-to-agents`, or gate on `@mentions` (the reference connector already
treats `@agentname` as a signal to reply even to another agent). For a
production hub, also consider: max-turns-per-thread, a cooldown per agent, and
a "round-robin / only one agent answers" mode.

---

## Mapping onto Google Workspace (hand this section to the internal chatbots)

This prototype is deliberately a 1:1 stand-in for Google-native pieces. To
implement the same thing on your Workspace, translate as follows:

| Prototype piece | Google-native equivalent |
|---|---|
| A **room** | A **Google Chat _Space_**. Create via the [Chat API](https://developers.google.com/workspace/chat) `spaces.create`, or just a manually-created Space. |
| The **Web UI** | Either (a) the **Google Chat client itself** (Spaces in Workspace), or (b) a custom UI calling the Chat API. Start with (a) — you get the UI for free. |
| Each **agent** | A **Google Chat app** (bot) backed by your BOQ microservice. Each agent = one Chat app added as a member of the Space. |
| The **proxy's webhook fan-out** | Chat delivers events to your app via **Pub/Sub** (recommended inside Google) or an **HTTPS endpoint**. Your BOQ service subscribes and receives a `MESSAGE` event for every message in the Space. |
| Posting a **reply** | Chat API `spaces.messages.create` (async reply) or the synchronous response to the event. |
| **Loop avoidance** | Chat events include the sender; check `message.sender.type`. Ignore `BOT` senders unless `@mentioned` (`ADDED_TO_SPACE` / annotations tell you about mentions). |
| **Auth between proxy and BOQ** | Inside Google, prefer LOAS/end-user creds over the `X-Agent-Id` header used here. |

### Recommended Workspace architecture

1. **One Google Chat Space** = the chat room. Humans and all agent bots are members.
2. **Each agent is a separate Chat app** registered in the Google Cloud / Workspace
   admin console, configured to receive events via **Pub/Sub**.
3. Each Chat app's Pub/Sub subscription runs on the agent's **BOQ microservice**
   (on Cloudtop/WSL): it pulls `MESSAGE` events, runs the agent, and calls
   `spaces.messages.create` to reply.
4. To message all agents at once, a human just posts in the Space — every agent
   bot that is a member receives the event. (To force a specific agent, `@mention` it.)

> Why this maps cleanly: the proxy in this repo *is* a hand-rolled Chat-API +
> Pub/Sub. When you move to Workspace, Google operates the hub for you; your
> BOQ services only need the "agent" half (`agent_client.py`'s receive → reply
> loop), pointed at Pub/Sub + the Chat API instead of this proxy's WebSocket.

### Concrete asks for the internal chatbots

- Stand up a **Google Chat Space** to act as the room.
- For each agent, **create a Chat app** wired to its BOQ endpoint, delivering
  events via **Pub/Sub**, with permission to post to the Space.
- Port `generate_reply()` from `agents/agent_client.py` into each BOQ service,
  replacing the WebSocket transport with: *Pub/Sub pull → `spaces.messages.create`*.
- Apply the loop-avoidance rule: ignore `sender.type == BOT` unless `@mentioned`.

---

## Notes / not-yet-done (it's a rough draft)

- In-memory room state only — restarting the proxy clears history. Swap for a
  store (or rely on Google Chat's own persistence) in production.
- No auth on the prototype proxy. Add SSO / LOAS before exposing it.
- No rate limiting or turn caps — add these before putting many agents in one room.
- Webhook delivery is fire-and-forget; add retries/backoff for real use.
