"""A tiny, dependency-free web dashboard for a live graph run.

Runs an HTTP server (stdlib only) in a background thread that serves:

* ``GET /``            – a self-contained HTML page (no CDN; works offline)
* ``GET /api/status``  – the :class:`~kgsupervisor.status.StatusBoard` snapshot

The page polls the status endpoint once a second and draws the knowledge graph
as nodes + edges, colouring each node by its live status.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("kgs")


class Dashboard:
    def __init__(self, board, host: str = "127.0.0.1", port: int = 8765):
        self.board = board
        self.host = host
        self.port = port
        self._httpd = None
        self._thread = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.actual_port}"

    @property
    def actual_port(self) -> int:
        if self._httpd is not None:
            return self._httpd.server_address[1]
        return self.port

    def start(self) -> None:
        board = self.board

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence per-request logging
                pass

            def _send(self, body: bytes, content_type: str):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path.startswith("/api/status"):
                    self._send(
                        json.dumps(board.snapshot()).encode("utf-8"),
                        "application/json",
                    )
                else:
                    self._send(INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")

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
    --pending:#3a3f4b; --running:#3b82f6; --recovering:#f59e0b; --done:#22c55e; --failed:#ef4444;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text);
         font:14px/1.45 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif; }
  header { padding:14px 18px; border-bottom:1px solid var(--line); display:flex;
           align-items:baseline; gap:16px; flex-wrap:wrap; }
  header h1 { font-size:16px; margin:0; font-weight:600; }
  header .sub { color:var(--muted); }
  .counts { margin-left:auto; display:flex; gap:14px; }
  .counts span { color:var(--muted); }
  .counts b { color:var(--text); }
  .wrap { display:flex; height:calc(100vh - 53px); }
  .graph { flex:1; overflow:auto; }
  .side { width:320px; border-left:1px solid var(--line); overflow:auto; padding:12px 14px; }
  .side h2 { font-size:12px; text-transform:uppercase; letter-spacing:.06em;
             color:var(--muted); margin:16px 0 8px; }
  .legend span { display:inline-flex; align-items:center; gap:6px; margin-right:12px; color:var(--muted); }
  .dot { width:10px; height:10px; border-radius:50%; display:inline-block; }
  .ev { padding:6px 8px; border:1px solid var(--line); border-radius:6px; margin-bottom:6px;
        background:var(--panel); font-size:12.5px; }
  .ev .t { color:var(--muted); font-variant-numeric:tabular-nums; margin-right:6px; }
  text { fill:var(--text); font:12px -apple-system,Segoe UI,Roboto,sans-serif; }
  .node rect { rx:8; ry:8; stroke:var(--line); stroke-width:1; }
  .running rect { animation:pulse 1.3s ease-in-out infinite; }
  @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.55} }
  .edge { stroke:var(--line); stroke-width:1.5; fill:none; }
  .nid { font-weight:600; }
  .nst { font-size:10.5px; fill:#0b0d12; text-transform:uppercase; letter-spacing:.04em; }
</style>
</head>
<body>
<header>
  <h1 id="title">knowledge graph</h1>
  <span class="sub" id="elapsed"></span>
  <div class="counts" id="counts"></div>
</header>
<div class="wrap">
  <div class="graph"><svg id="svg" width="100%" height="100%"></svg></div>
  <div class="side">
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
<script>
const SVGNS = "http://www.w3.org/2000/svg";
const COLW = 230, ROWH = 84, NW = 168, NH = 50, PADX = 40, PADY = 40;
const COLORS = {pending:"--pending",running:"--running",recovering:"--recovering",done:"--done",failed:"--failed"};
function cssv(v){ return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }

function layout(nodes){
  // group by level, stack vertically within each level
  const byLevel = {};
  nodes.forEach(n => (byLevel[n.level] = byLevel[n.level] || []).push(n));
  const pos = {};
  Object.keys(byLevel).forEach(lvl => {
    byLevel[lvl].forEach((n,i) => {
      pos[n.id] = { x: PADX + lvl*COLW, y: PADY + i*ROWH };
    });
  });
  return pos;
}

function render(data){
  document.getElementById("title").textContent = data.graph_title;
  document.getElementById("elapsed").textContent =
    `${Math.floor(data.elapsed)}s elapsed`;
  const c = data.counts;
  document.getElementById("counts").innerHTML =
    `<span>done <b>${c.done}/${data.total}</b></span>` +
    `<span>running <b>${c.running}</b></span>` +
    `<span>recovering <b>${c.recovering}</b></span>` +
    `<span>failed <b>${c.failed}</b></span>`;

  const svg = document.getElementById("svg");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  const pos = layout(data.nodes);

  // edges
  data.edges.forEach(e => {
    const a = pos[e.source], b = pos[e.target];
    if (!a || !b) return;
    const x1 = a.x + NW, y1 = a.y + NH/2, x2 = b.x, y2 = b.y + NH/2;
    const mx = (x1 + x2) / 2;
    const path = document.createElementNS(SVGNS, "path");
    path.setAttribute("class", "edge");
    path.setAttribute("d", `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`);
    svg.appendChild(path);
  });

  // nodes
  let maxX = 0, maxY = 0;
  data.nodes.forEach(n => {
    const p = pos[n.id];
    maxX = Math.max(maxX, p.x + NW + PADX);
    maxY = Math.max(maxY, p.y + NH + PADY);
    const g = document.createElementNS(SVGNS, "g");
    g.setAttribute("class", `node ${n.status}`);
    g.setAttribute("transform", `translate(${p.x},${p.y})`);

    const rect = document.createElementNS(SVGNS, "rect");
    rect.setAttribute("width", NW); rect.setAttribute("height", NH);
    rect.setAttribute("fill", cssv(COLORS[n.status] || "--pending"));
    const titleEl = document.createElementNS(SVGNS, "title");
    titleEl.textContent = (n.prompt || "") + (n.detail ? `\n\n[${n.status}] ${n.detail}` : "");
    rect.appendChild(titleEl);
    g.appendChild(rect);

    const id = document.createElementNS(SVGNS, "text");
    id.setAttribute("class", "nid"); id.setAttribute("x", 12); id.setAttribute("y", 22);
    id.textContent = n.id.length > 22 ? n.id.slice(0,21) + "…" : n.id;
    g.appendChild(id);

    const st = document.createElementNS(SVGNS, "text");
    st.setAttribute("class", "nst"); st.setAttribute("x", 12); st.setAttribute("y", 39);
    st.textContent = n.status + (n.status === "recovering" && n.detail ? " · " + n.detail.slice(0,18) : "");
    if (n.status === "pending" || n.status === "running")
      st.setAttribute("fill", "#fff");
    g.appendChild(st);

    svg.appendChild(g);
  });
  svg.setAttribute("width", Math.max(maxX, 600));
  svg.setAttribute("height", Math.max(maxY, 400));

  const ev = document.getElementById("events");
  ev.innerHTML = data.events.map(e => {
    const t = new Date(e.ts * 1000).toLocaleTimeString();
    return `<div class="ev"><span class="t">${t}</span>${escapeHtml(e.msg)}</div>`;
  }).join("");
}

function escapeHtml(s){ return s.replace(/[&<>]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }

async function tick(){
  try {
    const r = await fetch("/api/status");
    render(await r.json());
  } catch (e) { /* server gone; keep last view */ }
}
tick();
setInterval(tick, 1000);
</script>
</body>
</html>
"""
