# kg-agent-supervisor

A small Python supervisor that walks a **knowledge graph** of tasks, prompts your
**Gemini-backed Google Chat bot** for each task, watches the replies, and
**automatically recovers** when the agent hangs up or dies — by sending a
restart, replaying the conversation context, and resuming exactly where it left
off.

Think of it as a babysitter for a flaky chat agent: it keeps the work moving,
node by node, without you having to manually restart the bot and re-explain
everything each time it falls over.

```
knowledge graph ──► build prompt ──► post to Google Chat ──► read bot reply
       ▲                                                          │
       │                                                  health check
       │                                                          │
   next task ◄── record answer ◄─── OK ◄────────────────┬─────────┘
                                                         │ hung / dead / empty
                                              restart → replay context → retry
```

---

## How it works

1. **Graph traversal** — your tasks live in a JSON file (`examples/sample_graph.json`).
   Each *node* is a task with a `prompt` and optional `depends_on` edges. The
   supervisor runs them in dependency order and feeds each task the outputs of
   the tasks it depends on.
2. **Prompting** — it posts the prompt into the Google Chat space your Gemini
   bot lives in and waits for the reply.
3. **Health check** (`kgsupervisor/health.py`) — the reply is classified as
   `OK`, `HUNG` (no reply before the timeout), `DEAD` (the bot said something
   like *"I hung up, please restart"* / *"503 service unavailable"*), or `EMPTY`.
4. **Recovery** (`kgsupervisor/supervisor.py`) — on anything but `OK` it backs
   off (exponential), sends your configured `restart_command`, waits for the bot
   to come back, **replays a compact recap** of the conversation so the
   freshly-restarted bot has its context again, then re-issues the task. Up to
   `max_attempts` times.
5. **Resumable** — completed tasks are saved to `.kgs_state.json`, so if you
   close your laptop or the process dies, re-running picks up where it stopped.

---

## Download it onto your corp MacBook

You can't install Claude Code, but this is just a plain Python package — clone
it (or download the ZIP) from your private repo:

```bash
# Option A: clone (if git is available)
git clone https://github.com/anshdholakia/anshdholakia.git
cd anshdholakia/kg-agent-supervisor

# Option B: download a ZIP from the GitHub UI
#   Code ▸ Download ZIP, unzip, then:
cd <unzipped-folder>/kg-agent-supervisor
```

> The code lives in the `kg-agent-supervisor/` subfolder of the repo.

### Install (Python 3.9+)

Use a virtualenv so you don't touch the corp-managed system Python:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

If `pip install` is blocked on your corp network, you can still run the **mock**
transport with the standard library alone (PyYAML is only needed if you use a
YAML config file — you can skip it and pass flags instead).

---

## Try it immediately (no credentials)

The `mock` transport simulates a Gemini bot that hangs every few messages, so
you can watch the whole restart/replay/resume loop work before wiring up real
Google Chat:

```bash
python3 -m kgsupervisor --graph examples/sample_graph.json --transport mock
```

You'll see it complete some tasks, hit a simulated hang, recover, and finish.

Run the tests too:

```bash
python3 tests/test_core.py        # or:  python3 -m pytest
```

---

## Point it at your real Google Chat bot

Copy the config and edit it:

```bash
cp config.example.yaml config.yaml
```

There are two transports for real use:

### Option 1 — `webhook` (fastest to set up, send-only)

1. In your Google Chat space: **Apps & integrations ▸ Webhooks ▸ Add webhook**,
   copy the URL.
2. Set it (don't commit it):
   ```bash
   export KGS__CHAT__WEBHOOK__URL="https://chat.googleapis.com/v1/spaces/.../messages?key=...&token=..."
   ```
3. Run:
   ```bash
   python3 -m kgsupervisor --config config.yaml --transport webhook
   ```

⚠️ Webhooks can **post but not read**. The supervisor will prompt you on the
console to paste the bot's reply so it can still health-check it. For fully
hands-off hang-detection, use Option 2.

### Option 2 — `google_chat` (full REST API: post **and** read replies)

This lets the supervisor read the bot's replies itself and auto-detect hangs.
It is also **edit-aware**: if your agent streams its answer by editing one
message until it's done, the supervisor watches that message and only accepts it
once the edits settle (or a final marker appears) — see below.

1. In a Google Cloud project, **enable the Google Chat API**.
2. Create a **service account** + JSON key.
3. Configure your Chat app (App configuration) to use that service account.
4. Add the app to your space; copy the space id (`spaces/AAAA…`).
5. Wire it up via env vars:
   ```bash
   export KGS__CHAT__GOOGLE__SPACE="spaces/AAAAxxxxxxx"
   export KGS__CHAT__GOOGLE__CREDENTIALS_FILE="/path/to/service-account.json"
   export KGS__CHAT__GOOGLE__BOT_NAME="Gemini Agent"   # so we read only its replies
   ```
6. Run:
   ```bash
   python3 -m kgsupervisor --config config.yaml --transport google_chat
   ```

> The client filters out its own posts and (with `bot_name` set) only treats
> your agent's messages as replies.

### Talking to an agent in a 1:1 DM (act as you)

If you message your agent in a **direct message** (URL like
`…#chat/dm/…`) rather than a named space, you **cannot** add the orchestrator as
a service-account Chat app — a DM is private between you and that one app. So the
orchestrator authenticates **as you** and drives your existing DM. Set
`chat.google.user_auth: true` (already set in `config.hulk.yaml`), then:

```bash
# one-time login WITH the chat scopes (messages = post/read; spaces.readonly =
# needed by --list-spaces). Also set a quota project so the API has somewhere to
# bill to:
gcloud auth application-default login \
  --scopes=https://www.googleapis.com/auth/chat.messages,https://www.googleapis.com/auth/chat.spaces.readonly,https://www.googleapis.com/auth/cloud-platform
gcloud auth application-default set-quota-project gemclaw-anshdholakia-6vl6j5

# find the DM's API id (prints spaces/… for every space + DM you can see):
python3 -m kgsupervisor --config config.hulk.yaml --list-spaces

# then run, pointing at that DM:
export KGS__CHAT__GOOGLE__SPACE="spaces/AAAA..."
python3 -m kgsupervisor --config config.hulk.yaml --graph examples/sample_graph.json
```

In this mode the supervisor posts as you (exactly like typing in the DM) and
reads the agent's replies. No service account, no key, no adding apps to the DM.

> If your org blocks adding the `chat.messages` scope to ADC, the fallback is to
> create a Chat **space**, add both your agent and a service-account orchestrator
> app to it, and use the `credentials_file`/`impersonate_service_account` modes
> below instead.

**Key-free auth (org blocks SA keys).** Many orgs enforce a policy that blocks
downloading service-account JSON keys
(`iam.disableServiceAccountKeyCreation`). In that case, don't create a key —
**impersonate** the service account using your own identity:

```bash
gcloud auth application-default login        # once per machine
# you need the "Service Account Token Creator" role on the orchestrator SA
export KGS__CHAT__GOOGLE__IMPERSONATE_SERVICE_ACCOUNT="kgs-orchestrator@<project>.iam.gserviceaccount.com"
export KGS__CHAT__GOOGLE__SPACE="spaces/AAAA..."
python3 -m kgsupervisor --config config.hulk.yaml
```

The supervisor mints short-lived tokens for the SA on the fly — no key file ever
exists. (Set `credentials_file` instead only if your org *does* allow keys.)

**Keep using your webhook to post (hybrid).** If you'd rather post through the
incoming webhook you already created but still read replies via the API, set
`chat.google.webhook_url` (or `KGS__CHAT__GOOGLE__WEBHOOK_URL`). Set `bot_name`
too so the supervisor never reads its own prompts back.

### Your agent streams by editing one message

Because your agent edits a single Chat message repeatedly until the final
answer, "read the latest message" would grab a half-written reply. The
`google_chat` transport handles this (`chat.google`):

- `track_edits: true` — watch the agent's message and accept it only once its
  edits have been quiet for `stability_seconds` (default 8s).
- `final_marker` — **the robust option.** If you have your agent append a
  sentinel to its finished message (e.g. `[[END]]` or `✅ done`), set
  `final_marker: "[[END]]"` and the reply is accepted the instant that marker
  appears, regardless of further edits.
- If edits never settle / the marker never appears before `run.response_timeout`,
  that's treated as a **hang** and recovery kicks in.

### Restarting a dead agent (SSH + systemctl)

When your agent dies it goes silent, so posting `/restart` in chat won't reach
it — you normally SSH into the cloudtop and `systemctl restart`. Configure that
under `recovery.restart`:

```yaml
recovery:
  restart:
    method: shell        # chat | shell | both  (shell is reliable when it's dead)
    shell_command: "ssh cloudtop -- sudo systemctl restart my-agent.service"
    readiness:
      command: "ssh cloudtop -- systemctl is-active --quiet my-agent.service"
      timeout_seconds: 120
      poll_interval: 5
```

On recovery the supervisor runs `shell_command`, then polls the `readiness`
command until it exits 0 (agent healthy), then replays the conversation recap
and resumes the task. Use `method: both` to run the systemctl restart *and* send
the chat `/restart`. Secrets/commands can also come from env vars, e.g.
`KGS__RECOVERY__RESTART__SHELL_COMMAND`.

> The shell command runs through your local shell, so your normal SSH config /
> keys / `gcert` session to the cloudtop must already work from the terminal.

### Running both agents (hulk + fan5)

Two ready-to-edit configs are included — `config.hulk.yaml` and
`config.fan5.yaml` — one per service on `anshdholakia.c.googlers.com`. Each one
restarts **only its own** service, so recovering one agent never disturbs the
other:

```bash
# terminal 1 — hulk's space (same service-account key for both agents):
export KGS__CHAT__GOOGLE__SPACE="spaces/AAAA..." \
       KGS__CHAT__GOOGLE__CREDENTIALS_FILE="$HOME/sa.json"
python3 -m kgsupervisor --config config.hulk.yaml --graph hulk_tasks.json

# terminal 2 — fan5's space:
export KGS__CHAT__GOOGLE__SPACE="spaces/BBBB..." \
       KGS__CHAT__GOOGLE__CREDENTIALS_FILE="$HOME/sa.json"
python3 -m kgsupervisor --config config.fan5.yaml --graph fan5_tasks.json
```

Notes for your setup:

- **Separate spaces (your case): just set each `SPACE`.** hulk and fan5 live in
  different Chat spaces, so each config points at its own `SPACE` and `BOT_NAME`
  is optional — the supervisor only reads messages newer than the prompt it just
  sent and filters out its own posts. (You'd only need `BOT_NAME` if two agents
  shared one space.)
- **Auth = one shared Chat app for the orchestrator.** Create a single service
  account in your existing Cloud project, configure it as a Chat app, and add
  that app to *both* spaces. Point `CREDENTIALS_FILE` at its JSON key. You do not
  need a separate project or key per agent.
- **No final marker (Gemini can't add one).** Detection therefore relies on the
  message edits going quiet for `stability_seconds` (default 12s in these
  configs). If you sometimes accept a half-finished answer, raise
  `stability_seconds`; if it feels sluggish, lower it. Full silence past
  `run.response_timeout` (300s) is always treated as a hang regardless.
- To restart *both* services together instead, set `method: both`-style or just
  change `shell_command` to restart both units, e.g.
  `... systemctl restart gemclaw-hulk.service gemclaw-fan5.service`.

---

## Live graph dashboard

Watch the graph execute in your browser — each node lights up as the agent works
through it (pending → running → done, or amber while recovering from a hang, red
on failure). It's a self-contained page (no internet/CDN needed) served by the
supervisor itself.

```bash
python3 -m kgsupervisor --config config.hulk.yaml --graph my_tasks.json --dashboard
# then open http://127.0.0.1:8765
```

Or enable it in config:

```yaml
dashboard:
  enabled: true
  port: 8765
```

The page polls a JSON status endpoint (`GET /api/status`) once a second, draws
the graph as nodes + dependency edges laid out by depth, and shows a live
activity log. After the run finishes it keeps serving the final state until you
press Ctrl-C, so you can review what happened. Status is also a clean data
surface if you want to build your own UI: hit `/api/status` for the full
node/edge/status snapshot.

## Build the graph in the browser (editor mode)

Instead of hand-writing JSON, you can create and connect nodes visually
(Constella-style) and save back to the graph file:

```bash
python3 -m kgsupervisor --edit --graph my_tasks.json
# open http://127.0.0.1:8765
```

In the editor you can:

- **+ New node** — give it an id and a prompt (the task for the agent).
- **Depends on** — tick other nodes to draw dependency edges; their outputs are
  fed in as context, and order is derived from the edges.
- Click any node to **edit** or **delete** it; the title is editable too.
- **💾 Save to file** — writes valid graph JSON to `--graph` (every edit is
  validated for unique ids, known dependencies, and no cycles before it's
  accepted, so the saved graph is always runnable).

It's the same page as the live dashboard, just in editable mode — so your
internal/google3 tooling can drive the same `/api/node/*` + `/api/save`
endpoints if you want to build on it. When you're done editing, run the graph
normally.

## Resuming & the progress cache

The supervisor saves completed nodes to `run.state_file` and **skips them on the
next run** so a long run can resume after a crash. A node is only skipped if its
**fingerprint** (prompt + dependencies) is unchanged — so editing a node's
prompt makes it re-run rather than showing stale "done". To control this:

- `--reset` — wipe saved progress and run everything fresh.
- `--no-resume` (or `run.resume: false`) — ignore the cache and re-run every
  node, without deleting the file.

Skipped nodes are logged explicitly (`skipping (cached from a previous run…)`),
so a "done" you didn't expect is always traceable to the cache.

## Define your own knowledge graph

Edit a JSON file like this:

```json
{
  "title": "My project",
  "nodes": [
    { "id": "research", "prompt": "Research X and summarize.", "depends_on": [] },
    { "id": "draft",    "prompt": "Draft a doc from the research.", "depends_on": ["research"] },
    { "id": "review",   "prompt": "Review the draft for gaps.", "depends_on": ["draft"],
      "done_when": "all gaps are listed" }
  ]
}
```

- `id` — unique task name.
- `prompt` — what to ask the agent.
- `depends_on` — tasks whose answers should be fed in as context first.
- `done_when` *(optional)* — an exit-condition hint passed to the agent.

Cycles and unknown dependencies are rejected on load.

---

## Configuration reference

All settings live in `config.example.yaml` with comments. Key knobs:

| Setting | What it does |
|---|---|
| `recovery.restart.method` | `chat`, `shell`, or `both`. `shell` = run `shell_command` (ssh + systemctl). |
| `recovery.restart.shell_command` | Command to restart the agent, e.g. `ssh cloudtop -- sudo systemctl restart my-agent.service`. |
| `recovery.restart.readiness.command` | Shell command that exits 0 when the agent is back; polled after restart. |
| `recovery.max_attempts` | How many times to try recovering one task before giving up. |
| `recovery.failure_phrases` | Extra phrases that mean "the bot died". Empty = built-in defaults (`hung up`, `disconnected`, `503`, …). |
| `chat.google.track_edits` / `stability_seconds` | Wait for a streamed (edited) message to settle before accepting it. |
| `chat.google.final_marker` | Accept a reply the instant it contains this sentinel. |
| `chat.google.webhook_url` | Post via your incoming webhook while reading replies via the API. |
| `chat.google.bot_name` | Only treat this sender's messages as the agent's replies. |
| `run.response_timeout` | Seconds to wait for a reply before declaring a hang. |
| `run.thread_key` | Set a string to keep the whole run in one Chat thread. |
| `run.state_file` | Progress file used to resume after a restart. |

Any value can be overridden by an env var: `KGS__SECTION__KEY` (e.g.
`KGS__RUN__RESPONSE_TIMEOUT=300`).

Useful flags:

```bash
python3 -m kgsupervisor --config config.yaml --reset      # forget saved progress
python3 -m kgsupervisor --graph other.json --transport mock --log-level DEBUG
```

---

## Layout

```
kg-agent-supervisor/
├── README.md
├── requirements.txt
├── config.example.yaml
├── examples/sample_graph.json
├── tests/test_core.py
└── kgsupervisor/
    ├── __main__.py        # CLI
    ├── config.py          # YAML + env config
    ├── graph.py           # knowledge-graph model + topo traversal
    ├── agent.py           # conversation history + context replay
    ├── health.py          # hang / dead / empty detection
    ├── supervisor.py      # the main loop + recovery cycle
    ├── state.py           # resumable progress (fingerprinted)
    ├── status.py          # live run state for the dashboard
    ├── editor.py          # editable graph store (browser editor backend)
    ├── dashboard.py       # self-contained web dashboard + editor (stdlib HTTP)
    └── chat/              # transports: mock, webhook, google_chat
```

## Notes & limitations

- The `webhook` transport cannot read replies (and therefore can't see your
  agent's message edits). To read a streamed/edited reply you must use
  `google_chat` — optionally with `webhook_url` set if you still want to *post*
  via your webhook.
- The `shell`/`both` restart runs commands through your local shell, so your SSH
  access to the cloudtop must already work from the terminal (keys, `gcert`,
  etc.). The supervisor doesn't manage credentials.
- Don't commit `config.yaml`, service-account keys, or webhook URLs — the
  `.gitignore` already excludes them.
- `final_marker` is the most reliable way to know a streamed answer is complete.
  If you can make your agent end its final message with a sentinel, do it.
