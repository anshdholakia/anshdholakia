'use strict';

/**
 * Agent Chat Proxy
 * ----------------
 * A small hub that sits between a chat UI and any number of agents.
 *
 *   [Web UI] <--WS--> [ THIS PROXY ] <--WS--> [Agent A]
 *                                     <--WS--> [Agent B]
 *                                     <--HTTP webhook--> [Agent C (BOQ microservice)]
 *
 * Responsibilities:
 *   - Host named chat "rooms" (analogous to a Google Chat Space).
 *   - Track who is in each room (human users + agents).
 *   - Fan every message out to every member of the room.
 *   - Optionally deliver messages to agents that expose an HTTP webhook
 *     (the natural shape for a BOQ microservice) instead of holding a socket open.
 *
 * It is intentionally dependency-light and stateless-on-disk so it is easy to
 * port onto Google-native infra later (see README "Mapping onto Google Workspace").
 */

const http = require('http');
const path = require('path');
const crypto = require('crypto');
const express = require('express');
const { WebSocketServer } = require('ws');

const PORT = process.env.PORT || 8080;

// ---------------------------------------------------------------------------
// In-memory room state. Swap this for Pub/Sub + a real store in production.
// ---------------------------------------------------------------------------

/** @type {Map<string, Room>} */
const rooms = new Map();

/**
 * @typedef {Object} Member
 * @property {string} id        Stable connection id.
 * @property {'user'|'agent'} role
 * @property {string} name      Display name.
 * @property {import('ws').WebSocket} [socket]   Present for socket-connected members.
 * @property {string} [webhook] Present for webhook-delivered agents (HTTP POST target).
 */

/**
 * @typedef {Object} Room
 * @property {string} name
 * @property {Map<string, Member>} members
 * @property {Array<object>} history   Recent messages (capped).
 */

function getRoom(name) {
  let room = rooms.get(name);
  if (!room) {
    room = { name, members: new Map(), history: [] };
    rooms.set(name, room);
  }
  return room;
}

function roster(room) {
  return [...room.members.values()].map((m) => ({ id: m.id, role: m.role, name: m.name }));
}

const newId = () => crypto.randomBytes(8).toString('hex');

// ---------------------------------------------------------------------------
// Message fan-out
// ---------------------------------------------------------------------------

/**
 * Deliver a message to every member of a room except the original sender.
 * Socket members get a WS frame; webhook agents get an HTTP POST.
 */
function broadcast(room, message, exceptId) {
  const frame = JSON.stringify(message);
  for (const member of room.members.values()) {
    if (member.id === exceptId) continue;

    if (member.socket && member.socket.readyState === member.socket.OPEN) {
      member.socket.send(frame);
    } else if (member.webhook) {
      deliverWebhook(member, room, message);
    }
  }
}

/**
 * POST a message to a webhook-style agent (e.g. a BOQ microservice endpoint).
 * The agent replies by calling POST /api/rooms/:room/messages (see HTTP API
 * below) — we deliberately do not block on the HTTP response so a slow agent
 * never stalls the room.
 */
function deliverWebhook(member, room, message) {
  // Only push genuine chat traffic to webhooks; skip presence noise.
  if (message.type !== 'message') return;

  const body = JSON.stringify({ room: room.name, message });
  const url = new URL(member.webhook);
  const lib = url.protocol === 'https:' ? require('https') : require('http');

  const req = lib.request(
    url,
    {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body),
        // In a Google deployment this is where LOAS / end-user creds would go.
        'X-Agent-Id': member.id,
      },
    },
    (res) => res.resume() // drain & ignore; the agent replies out-of-band
  );
  req.on('error', (err) => console.warn(`[webhook] ${member.name} failed:`, err.message));
  req.write(body);
  req.end();
}

/** Build a normalized message object and record it in room history. */
function makeMessage(room, { text, fromId, fromName, role }) {
  const msg = {
    type: 'message',
    msgId: newId(),
    room: room.name,
    text,
    fromId,
    from: fromName,
    role, // 'user' | 'agent' — agents use this to avoid replying to each other
    ts: Date.now(),
  };
  room.history.push(msg);
  if (room.history.length > 200) room.history.shift();
  return msg;
}

// ---------------------------------------------------------------------------
// HTTP layer: serve the UI + a REST surface for webhook agents
// ---------------------------------------------------------------------------

const app = express();
app.use(express.json());
app.use('/', express.static(path.join(__dirname, '..', 'ui')));

// Register a webhook-style agent (no persistent socket needed).
app.post('/api/rooms/:room/agents', (req, res) => {
  const { name, webhook } = req.body || {};
  if (!name || !webhook) return res.status(400).json({ error: 'name and webhook required' });

  const room = getRoom(req.params.room);
  const id = newId();
  room.members.set(id, { id, role: 'agent', name, webhook });
  broadcast(room, { type: 'presence', room: room.name, members: roster(room) });
  console.log(`[join] webhook agent "${name}" -> #${room.name}`);
  res.json({ agentId: id, room: room.name });
});

// Post a message into a room over HTTP (used by webhook agents to reply,
// and handy for curl-testing).
app.post('/api/rooms/:room/messages', (req, res) => {
  const { text, fromId, from, role } = req.body || {};
  if (!text) return res.status(400).json({ error: 'text required' });

  const room = getRoom(req.params.room);
  const msg = makeMessage(room, {
    text,
    fromId: fromId || 'http',
    fromName: from || 'http-client',
    role: role || 'agent',
  });
  broadcast(room, msg, fromId);
  res.json({ ok: true, msgId: msg.msgId });
});

app.get('/api/rooms/:room/history', (req, res) => {
  res.json(getRoom(req.params.room).history);
});

const server = http.createServer(app);

// ---------------------------------------------------------------------------
// WebSocket layer: humans and long-lived agents
// ---------------------------------------------------------------------------

const wss = new WebSocketServer({ server, path: '/ws' });

wss.on('connection', (socket) => {
  const id = newId();
  let joined = null; // { room, member }

  socket.on('message', (raw) => {
    let evt;
    try {
      evt = JSON.parse(raw.toString());
    } catch {
      return socket.send(JSON.stringify({ type: 'error', error: 'invalid JSON' }));
    }

    switch (evt.type) {
      case 'join': {
        const room = getRoom(evt.room || 'general');
        const member = {
          id,
          role: evt.as === 'agent' ? 'agent' : 'user',
          name: evt.name || (evt.as === 'agent' ? 'agent' : 'guest'),
          socket,
        };
        room.members.set(id, member);
        joined = { room, member };

        socket.send(
          JSON.stringify({
            type: 'joined',
            room: room.name,
            you: { id, name: member.name, role: member.role },
            members: roster(room),
            history: room.history,
          })
        );
        broadcast(room, { type: 'presence', room: room.name, members: roster(room) }, id);
        console.log(`[join] ${member.role} "${member.name}" -> #${room.name}`);
        break;
      }

      case 'message': {
        if (!joined) return;
        const msg = makeMessage(joined.room, {
          text: evt.text,
          fromId: id,
          fromName: joined.member.name,
          role: joined.member.role,
        });
        // Echo to sender too so the UI renders optimistically-consistent state,
        // then fan out to everyone else.
        socket.send(JSON.stringify(msg));
        broadcast(joined.room, msg, id);
        break;
      }

      case 'leave':
        cleanup();
        break;

      default:
        socket.send(JSON.stringify({ type: 'error', error: `unknown type: ${evt.type}` }));
    }
  });

  socket.on('close', cleanup);

  function cleanup() {
    if (!joined) return;
    const { room, member } = joined;
    room.members.delete(id);
    broadcast(room, { type: 'presence', room: room.name, members: roster(room) });
    console.log(`[leave] ${member.role} "${member.name}" <- #${room.name}`);
    joined = null;
  }
});

server.listen(PORT, () => {
  console.log(`Agent Chat Proxy listening on http://localhost:${PORT}`);
  console.log(`  UI:        http://localhost:${PORT}/`);
  console.log(`  WebSocket: ws://localhost:${PORT}/ws`);
});
