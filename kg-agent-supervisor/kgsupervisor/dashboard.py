"""A tiny, dependency-free web dashboard for the knowledge graph.

Two modes, same page, driven by a *provider*:

* **run mode**   – provider is a read-only :class:`~kgsupervisor.status.StatusBoard`;
  the page shows live node statuses while the supervisor runs.
* **editor mode** – provider is an editable :class:`~kgsupervisor.editor.GraphStore`;
  the page lets you create / edit / connect / delete nodes (Constella-style) and
  save back to the graph JSON.

The server is stdlib-only (``http.server``) and the page embeds everything (no
CDN), so it runs offline / inside locked-down build environments.

Endpoints:

* ``GET  /``            – the HTML page
* ``GET  /api/status``  – provider snapshot (nodes, edges, statuses, ``editable``)
* ``POST /api/node/add``    – {id, prompt, depends_on, done_when}   (editor only)
* ``POST /api/node/update`` – {id, prompt, depends_on, done_when}   (editor only)
* ``POST /api/node/delete`` – {id}                                  (editor only)
* ``POST /api/title``       – {title}                               (editor only)
* ``POST /api/save``        – {}  write the graph JSON to disk       (editor only)
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("kgs")


class Dashboard:
    def __init__(self, provider, host: str = "127.0.0.1", port: int = 8765):
        self.provider = provider
        self.host = host
        self.port = port
        self._httpd = None
        self._thread = None

    @property
    def actual_port(self) -> int:
        if self._httpd is not None:
            return self._httpd.server_address[1]
        return self.port

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.actual_port}"

    def start(self) -> None:
        provider = self.provider

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence per-request logging
                pass

            # ------------------------------------------------------ helpers
            def _send(self, body: bytes, content_type: str, code: int = 200):
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, code: int = 200):
                self._send(json.dumps(obj).encode("utf-8"), "application/json", code)

            def _read_json(self) -> dict:
                length = int(self.headers.get("Content-Length", 0) or 0)
                raw = self.rfile.read(length) if length else b"{}"
                return json.loads(raw.decode("utf-8") or "{}")

            # ---------------------------------------------------------- GET
            def do_GET(self):
                if self.path.startswith("/api/status"):
                    self._json(provider.snapshot())
                else:
                    self._send(
                        INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8"
                    )

            # --------------------------------------------------------- POST
            def do_POST(self):
                if not getattr(provider, "editable", False):
                    self._json({"error": "This view is read-only."}, 405)
                    return
                try:
                    payload = self._read_json()
                except Exception:
                    self._json({"error": "Invalid JSON body."}, 400)
                    return
                path = self.path.split("?")[0]
                try:
                    if path == "/api/node/add":
                        provider.add_node(
                            payload.get("id"),
                            payload.get("prompt", ""),
                            payload.get("depends_on", []),
                            payload.get("done_when"),
                        )
                    elif path == "/api/node/update":
                        provider.update_node(
                            payload.get("id"),
                            prompt=payload.get("prompt"),
                            depends_on=payload.get("depends_on"),
                            done_when=payload.get("done_when"),
                        )
                    elif path == "/api/node/delete":
                        provider.delete_node(payload.get("id"))
                    elif path == "/api/title":
                        provider.set_title(payload.get("title"))
                    elif path == "/api/save":
                        provider.save()
                    else:
                        self._json({"error": "Unknown endpoint."}, 404)
                        return
                except ValueError as exc:  # GraphError (invalid edit) etc.
                    self._json({"error": str(exc)}, 400)
                    return
                except Exception as exc:  # noqa: BLE001
                    self._json({"error": str(exc)}, 500)
                    return
                self._json(provider.snapshot())

        self._httpd = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        log.info("Dashboard live at %s", self.url)

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>kg-agent-supervisor</title>
<style>
  :root {
    --bg:#0f1117; --panel:#171a23; --line:#2a2f3a; --text:#e6e8ee; --muted:#8a90a2;
    --accent:#6366f1;
    --pending:#3a3f4b; --running:#3b82f6; --recovering:#f59e0b; --done:#22c55e; --failed:#ef4444;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }
  header { padding:12px 18px; border-bottom:1px solid var(--line); display:flex;
           align-items:center; gap:14px; flex-wrap:wrap; }
  header h1 { font-size:16px; margin:0; font-weight:600; }
  header input.title { background:var(--panel); border:1px solid var(--line); color:var(--text);
           font-size:15px; font-weight:600; padding:6px 10px; border-radius:6px; min-width:240px; }
  .counts { margin-left:auto; display:flex; gap:14px; align-items:center; }
  .counts span { color:var(--muted); } .counts b { color:var(--text); }
  button { background:var(--panel); color:var(--text); border:1px solid var(--line);
           padding:7px 12px; border-radius:7px; cursor:pointer; font-size:13px; }
  button:hover { border-color:var(--accent); }
  button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
  button.danger:hover { border-color:var(--failed); color:var(--failed); }
  .wrap { display:flex; height:calc(100vh - 53px); }
  .graph { flex:1; overflow:auto; }
  .side { width:340px; border-left:1px solid var(--line); overflow:auto; padding:14px; }
  .side h2 { font-size:12px; text-transform:uppercase; letter-spacing:.06em;
             color:var(--muted); margin:4px 0 10px; }
  label { display:block; font-size:12px; color:var(--muted); margin:10px 0 4px; }
  input[type=text], textarea { width:100%; background:var(--bg); border:1px solid var(--line);
           color:var(--text); border-radius:6px; padding:8px; font:inherit; }
  textarea { min-height:84px; resize:vertical; }
  .deps { max-height:150px; overflow:auto; border:1px solid var(--line); border-radius:6px;
          padding:6px 8px; }
  .deps label { display:flex; align-items:center; gap:8px; color:var(--text); margin:3px 0; }
  .row { display:flex; gap:8px; margin-top:12px; }
  .msg { margin-top:10px; font-size:12.5px; min-height:18px; color:var(--muted); }
  .msg.err { color:var(--failed); }
  .legend span { display:inline-flex; align-items:center; gap:6px; margin-right:12px; color:var(--muted); }
  .dot { width:10px; height:10px; border-radius:50%; display:inline-block; }
  .ev { padding:6px 8px; border:1px solid var(--line); border-radius:6px; margin-bottom:6px;
        background:var(--panel); font-size:12.5px; }
  .ev .t { color:var(--muted); font-variant-numeric:tabular-nums; margin-right:6px; }
  text { fill:var(--text); font:12px -apple-system,Segoe UI,Roboto,sans-serif; }
  .node { cursor:pointer; }
  .node rect { rx:8; ry:8; stroke:var(--line); stroke-width:1; }
  .node.sel rect { stroke:var(--accent); stroke-width:2.5; }
  .running rect { animation:pulse 1.3s ease-in-out infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.55} }
  .edge { stroke:var(--line); stroke-width:1.5; fill:none; }
  .nid { font-weight:600; } .hide { display:none; }
  .nst { font-size:10.5px; fill:#0b0d12; text-transform:uppercase; letter-spacing:.04em; }
</style>
</head>
<body>
<header>
  <h1 id="title">knowledge graph</h1>
  <input class="title hide" id="titleEdit" />
  <button id="btnNew" class="primary hide">+ New node</button>
  <button id="btnSave" class="hide">💾 Save to file</button>
  <div class="counts" id="counts"></div>
</header>
<div class="wrap">
  <div class="graph"><svg id="svg" width="100%" height="100%"></svg></div>
  <div class="side">
    <!-- editor (editable mode) -->
    <div id="editor" class="hide">
      <h2 id="editorHead">New node</h2>
      <label>Node id</label>
      <input type="text" id="f-id" placeholder="e.g. analyze_logs" />
      <label>Prompt (what to ask the agent)</label>
      <textarea id="f-prompt"></textarea>
      <label>Done when (optional)</label>
      <input type="text" id="f-done" placeholder="exit condition hint" />
      <label>Depends on (runs after these; their output is fed in)</label>
      <div class="deps" id="f-deps"></div>
      <div class="row">
        <button class="primary" id="btnSaveNode">Save node</button>
        <button class="danger hide" id="btnDelete">Delete</button>
      </div>
      <div class="msg" id="msg"></div>
    </div>
    <!-- activity (run mode) -->
    <div id="activity" class="hide">
      <div class="legend">
        <span><i class="dot" style="background:var(--pending)"></i>pending</span>
        <span><i class="dot" style="background:var(--running)"></i>running</span>
        <span><i class="dot" style="background:var(--recovering)"></i>recovering</span>
        <span><i class="dot" style="background:var(--done)"></i>done</span>
        <span><i class="dot" style="background:var(--failed)"></i>failed</span>
      </div>
      <h2>Activity</h2>
      <div id="events"></div>
    </div>
  </div>
</div>
<script>
const SVGNS = "http://www.w3.org/2000/svg";
const COLW = 230, ROWH = 84, NW = 168, NH = 50, PADX = 40, PADY = 40;
const COLORS = {pending:"--pending",running:"--running",recovering:"--recovering",done:"--done",failed:"--failed"};
function cssv(v){ return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }
function $(id){ return document.getElementById(id); }

let last = null;          // last snapshot
let selectedId = null;    // node being edited (null = new node)
let pollTimer = null;

function layout(nodes){
  const byLevel = {};
  nodes.forEach(n => (byLevel[n.level] = byLevel[n.level] || []).push(n));
  const pos = {};
  Object.keys(byLevel).forEach(lvl => byLevel[lvl].forEach((n,i) =>
    pos[n.id] = { x: PADX + lvl*COLW, y: PADY + i*ROWH }));
  return pos;
}

function render(data){
  last = data;
  const editable = !!data.editable;
  $("title").classList.toggle("hide", editable);
  $("titleEdit").classList.toggle("hide", !editable);
  $("btnNew").classList.toggle("hide", !editable);
  $("btnSave").classList.toggle("hide", !editable);
  $("editor").classList.toggle("hide", !editable);
  $("activity").classList.toggle("hide", editable);

  $("title").textContent = data.graph_title;
  if (editable && document.activeElement !== $("titleEdit")) $("titleEdit").value = data.graph_title;

  const c = data.counts;
  $("counts").innerHTML = editable
    ? `<span>nodes <b>${data.total}</b></span>`
    : `<span>done <b>${c.done}/${data.total}</b></span>` +
      `<span>running <b>${c.running}</b></span>` +
      `<span>recovering <b>${c.recovering}</b></span>` +
      `<span>failed <b>${c.failed}</b></span>`;

  const svg = $("svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  const pos = layout(data.nodes);

  data.edges.forEach(e => {
    const a = pos[e.source], b = pos[e.target]; if (!a || !b) return;
    const x1=a.x+NW, y1=a.y+NH/2, x2=b.x, y2=b.y+NH/2, mx=(x1+x2)/2;
    const path = document.createElementNS(SVGNS,"path");
    path.setAttribute("class","edge");
    path.setAttribute("d", `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`);
    svg.appendChild(path);
  });

  let maxX=0, maxY=0;
  data.nodes.forEach(n => {
    const p = pos[n.id];
    maxX=Math.max(maxX,p.x+NW+PADX); maxY=Math.max(maxY,p.y+NH+PADY);
    const g = document.createElementNS(SVGNS,"g");
    g.setAttribute("class", `node ${n.status}${n.id===selectedId?" sel":""}`);
    g.setAttribute("transform", `translate(${p.x},${p.y})`);
    if (editable) g.addEventListener("click", () => selectNode(n.id));
    const rect = document.createElementNS(SVGNS,"rect");
    rect.setAttribute("width",NW); rect.setAttribute("height",NH);
    rect.setAttribute("fill", cssv(COLORS[n.status]||"--pending"));
    const tt = document.createElementNS(SVGNS,"title");
    tt.textContent = (n.prompt||"") + (n.detail?`\n\n[${n.status}] ${n.detail}`:"");
    rect.appendChild(tt); g.appendChild(rect);
    const id = document.createElementNS(SVGNS,"text");
    id.setAttribute("class","nid"); id.setAttribute("x",12); id.setAttribute("y",22);
    id.textContent = n.id.length>22 ? n.id.slice(0,21)+"…" : n.id; g.appendChild(id);
    const st = document.createElementNS(SVGNS,"text");
    st.setAttribute("class","nst"); st.setAttribute("x",12); st.setAttribute("y",39);
    st.textContent = editable ? `${n.depends_on.length} dep(s)` : n.status;
    if (editable || n.status==="pending" || n.status==="running") st.setAttribute("fill","#fff");
    g.appendChild(st); svg.appendChild(g);
  });
  svg.setAttribute("width", Math.max(maxX,600));
  svg.setAttribute("height", Math.max(maxY,400));

  if (!editable){
    $("events").innerHTML = data.events.map(e => {
      const t = new Date(e.ts*1000).toLocaleTimeString();
      return `<div class="ev"><span class="t">${t}</span>${esc(e.msg)}</div>`;
    }).join("");
  }
}

function esc(s){ return String(s).replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
function setMsg(t, err){ const m=$("msg"); m.textContent=t||""; m.classList.toggle("err",!!err); }

function buildDeps(currentId, selectedDeps){
  const box = $("f-deps"); box.innerHTML = "";
  const others = (last?last.nodes:[]).filter(n => n.id !== currentId);
  if (!others.length){ box.innerHTML = '<span style="color:var(--muted)">no other nodes yet</span>'; return; }
  others.forEach(n => {
    const id = "dep_"+n.id;
    const wrap = document.createElement("label");
    const cb = document.createElement("input");
    cb.type="checkbox"; cb.className="depcb"; cb.value=n.id; cb.id=id;
    cb.checked = (selectedDeps||[]).includes(n.id);
    const span = document.createElement("span"); span.textContent = n.id;
    wrap.appendChild(cb); wrap.appendChild(span); box.appendChild(wrap);
  });
}

function selectNode(id){
  const n = last.nodes.find(x => x.id===id); if (!n) return;
  selectedId = id;
  $("editorHead").textContent = "Edit node";
  $("f-id").value = n.id; $("f-id").disabled = true;
  $("f-prompt").value = n.prompt || "";
  $("f-done").value = n.done_when || "";
  buildDeps(id, n.depends_on);
  $("btnDelete").classList.remove("hide");
  setMsg("");
  render(last); // refresh selection highlight
}

function newNode(){
  selectedId = null;
  $("editorHead").textContent = "New node";
  $("f-id").value = ""; $("f-id").disabled = false;
  $("f-prompt").value = ""; $("f-done").value = "";
  buildDeps(null, []);
  $("btnDelete").classList.add("hide");
  setMsg(""); render(last);
}

async function api(path, body){
  const r = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"},
                              body:JSON.stringify(body||{})});
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || ("HTTP "+r.status));
  return data;
}

async function saveNode(){
  const id = $("f-id").value.trim();
  const body = {
    id,
    prompt: $("f-prompt").value,
    done_when: $("f-done").value,
    depends_on: [...document.querySelectorAll(".depcb:checked")].map(c=>c.value),
  };
  try {
    const data = await api(selectedId ? "/api/node/update" : "/api/node/add", body);
    render(data); selectNode(id);
    setMsg("Saved node ‘"+id+"’. Click ‘Save to file’ to persist.");
  } catch(e){ setMsg(e.message, true); }
}

async function deleteNode(){
  if (!selectedId) return;
  try { const data = await api("/api/node/delete", {id:selectedId});
        render(data); newNode(); setMsg("Deleted. Click ‘Save to file’ to persist."); }
  catch(e){ setMsg(e.message, true); }
}

async function saveFile(){
  try { await api("/api/save", {}); setMsg("Saved to "+(last.path||"file")+" ✓"); }
  catch(e){ setMsg(e.message, true); }
}

async function saveTitle(){
  try { const data = await api("/api/title", {title:$("titleEdit").value}); render(data); }
  catch(e){ setMsg(e.message, true); }
}

$("btnNew").addEventListener("click", newNode);
$("btnSave").addEventListener("click", saveFile);
$("btnSaveNode").addEventListener("click", saveNode);
$("btnDelete").addEventListener("click", deleteNode);
$("titleEdit").addEventListener("change", saveTitle);

async function tick(){
  try {
    const r = await fetch("/api/status");
    const data = await r.json();
    const wasEditable = last && last.editable;
    render(data);
    if (data.editable){
      // editor mode: stop polling so we don't clobber the form you're typing in
      if (pollTimer){ clearInterval(pollTimer); pollTimer = null; }
      if (!wasEditable) newNode();
    }
  } catch(e){ /* server gone; keep last view */ }
}
tick();
pollTimer = setInterval(tick, 1000);
</script>
</body>
</html>
"""
