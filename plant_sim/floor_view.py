"""
Builds the animated "factory floor" HTML component: station boxes connected
by arrows, with small dots flowing between them to represent units of work
physically moving through the line, and live buffer/headcount/shift readouts
per station. This is playback of an already-computed SimulationResult.trace -
no simulation logic lives here, only visualisation.

Rendered via streamlit.components.v1.html(...) in app.py, since Streamlit's
native widgets can't do continuous animation - this generates one
self-contained HTML/CSS/JS document that plays independently inside its own
iframe.

Both product lines are drawn as their own row (Cut & Clash on top, Thermo
below - matching your factory-floor sketch), and each row ends at its own
copy of the Packing/Despatch box. Packing is genuinely a SINGLE shared
station in the simulation (both lines feed into it) - it's just drawn twice,
always showing identical numbers, so each line reads as a complete
left-to-right chain rather than needing awkward converging arrows.
"""

from __future__ import annotations

import json

from plant_sim import config as cfg

STATION_ICONS = {
    "optimising": "📐",
    "cnc_1536": "🪚",
    "eb_drilling": "🔩",
    "cnc_thermo": "🪚",
    "sanding": "🧹",
    "mb_sander": "🌀",
    "edging": "🧴",
    "press": "🗜️",
    "despatch": "📦",
}

_TEMPLATE = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<style>
  :root { --bg:#0f1620; --panel:#16202c; --border:#253242; --text:#e6edf3; --muted:#8b98a5;
          --day:#4bc9c9; --aft:#e0a83e; --accent:#4472C4; }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--text); font-family:Arial,Helvetica,sans-serif; }
  .controls { display:flex; align-items:center; gap:10px; flex-wrap:wrap; background:var(--panel);
              border:1px solid var(--border); border-radius:8px; padding:8px 12px; margin-bottom:10px; }
  .controls button { background:var(--border); color:var(--text); border:none; border-radius:5px;
                      padding:6px 10px; font-size:12px; cursor:pointer; }
  .controls button.primary { background:var(--accent); font-weight:600; }
  .controls input[type=range] { flex:1; min-width:150px; accent-color:var(--accent); }
  .controls select { background:#101823; color:var(--text); border:1px solid var(--border); border-radius:5px; }
  .time-label { font-variant-numeric:tabular-nums; font-size:12px; min-width:140px; }
  .row-label { font-size:11px; color:var(--muted); text-transform:uppercase; letter-spacing:.05em;
               margin:10px 0 4px; }
  .floor-row { display:flex; align-items:stretch; gap:4px; overflow-x:auto; padding:4px 2px 10px; }
  .station-box { flex:0 0 auto; width:132px; background:#101823; border:1px solid var(--border);
                 border-radius:9px; padding:9px; position:relative; transition:background-color .3s; }
  .station-box.off { opacity:.42; }
  .station-icon { font-size:17px; line-height:1; }
  .station-box h4 { margin:3px 0 2px; font-size:11.5px; }
  .on-dot { position:absolute; top:8px; right:8px; width:7px; height:7px; border-radius:50%; background:var(--border); }
  .on-dot.day { background:var(--day); box-shadow:0 0 5px var(--day); }
  .on-dot.aft { background:var(--aft); box-shadow:0 0 5px var(--aft); }
  .ops-label { font-size:9.5px; color:var(--muted); margin-top:2px; }
  .buf-label { font-size:9px; color:var(--muted); margin-top:5px; }
  .buf-value { font-size:16px; font-weight:800; }
  .buf-bar-bg { background:#253242; border-radius:4px; height:7px; overflow:hidden; margin-top:4px; }
  .buf-bar-fill { height:100%; width:0%; background:var(--accent); transition:width .1s linear; }
  .arrow-track { flex:0 0 20px; position:relative; }
  .arrow-line { position:absolute; top:50%; left:0; right:0; height:2px; background:var(--border); transform:translateY(-50%); }
  .arrow-line:after { content:''; position:absolute; right:-1px; top:50%; transform:translate(0,-50%);
                       border-left:5px solid var(--border); border-top:3px solid transparent; border-bottom:3px solid transparent; }
  .particle { position:absolute; top:50%; width:6px; height:6px; border-radius:50%; transform:translate(-50%,-50%); pointer-events:none; }
  .footnote { font-size:10px; color:var(--muted); margin-top:6px; }
</style>
</head>
<body>
  <div class="controls">
    <button id="playBtn" class="primary">▶ Play</button>
    <select id="speedSelect"><option value="0.5">0.5×</option><option value="1" selected>1×</option>
      <option value="2">2×</option><option value="4">4×</option><option value="8">8×</option></select>
    <button id="resetBtn">⟲ Reset</button>
    <input type="range" id="scrub" min="0" max="1000" value="0">
    <div class="time-label" id="scrubLabel">0 / 0 hrs</div>
  </div>
  <div id="rows"></div>
  <div class="footnote">Packing / Despatch is one shared station fed by both lines - shown at the end
    of each row (always in sync) rather than drawn with converging arrows. ● teal = day shift running,
    ● amber = afternoon shift running, dim = off shift.</div>

<script>
const ROWS = __ROWS_JSON__;
const TRACE = __TRACE_JSON__;
const MAXBUF = __MAXBUF_JSON__;

const rowsEl = document.getElementById('rows');
ROWS.forEach(row => {
  const label = document.createElement('div');
  label.className = 'row-label';
  label.textContent = row.label;
  rowsEl.appendChild(label);
  const floorRow = document.createElement('div');
  floorRow.className = 'floor-row';
  row.stations.forEach((st, i) => {
    const box = document.createElement('div');
    box.className = 'station-box';
    box.id = 'box-' + row.key + '-' + st.id;
    box.innerHTML = `
      <div class="on-dot" id="dot-${row.key}-${st.id}"></div>
      <div class="station-icon">${st.icon}</div>
      <h4>${st.label}</h4>
      <div class="ops-label" id="ops-${row.key}-${st.id}">0 staff</div>
      <div class="buf-label">Buffer (m²)</div>
      <div class="buf-value" id="buf-${row.key}-${st.id}">0</div>
      <div class="buf-bar-bg"><div class="buf-bar-fill" id="bar-${row.key}-${st.id}"></div></div>`;
    floorRow.appendChild(box);
    if (i < row.stations.length - 1) {
      const track = document.createElement('div');
      track.className = 'arrow-track';
      track.id = 'arrow-' + row.key + '-' + st.id;
      track.innerHTML = '<div class="arrow-line"></div>';
      floorRow.appendChild(track);
    }
  });
  rowsEl.appendChild(floorRow);
});

let currentHour = 0, playing = false, speed = 1, lastTs = null;
const HOURS_PER_REAL_SEC = 4;
const spawnAccum = {};

function traceAt(hourIdx) {
  const idx = Math.min(TRACE.length - 1, Math.max(0, Math.round(hourIdx)));
  return TRACE[idx];
}

function render(hourIdx) {
  const t = traceAt(hourIdx);
  ROWS.forEach(row => {
    row.stations.forEach(st => {
      const boxEl = document.getElementById('box-' + row.key + '-' + st.id);
      const dotEl = document.getElementById('dot-' + row.key + '-' + st.id);
      const bufEl = document.getElementById('buf-' + row.key + '-' + st.id);
      const barEl = document.getElementById('bar-' + row.key + '-' + st.id);
      const opsEl = document.getElementById('ops-' + row.key + '-' + st.id);
      const isActive = t.active.includes(st.id);
      boxEl.classList.toggle('off', !isActive);
      const ops = (t.ops && t.ops[st.id]) || 0;
      opsEl.textContent = ops + (ops === 1 ? ' staff' : ' staff');
      // day/aft shading: we don't carry the exact label back from Python,
      // so use "active" as day (teal) - afternoon-only distinction isn't
      // needed for this view since ops count already reflects who's on.
      dotEl.classList.toggle('day', isActive);
      dotEl.classList.toggle('aft', false);
      const bufVal = (t.buf && t.buf[st.id]) || 0;
      bufEl.textContent = Math.round(bufVal).toLocaleString();
      const maxBuf = MAXBUF[st.id] || 1;
      barEl.style.width = Math.min(100, (bufVal / maxBuf) * 100) + '%';
      barEl.style.background = bufVal <= 0.01 ? '#c0392b' : 'var(--accent)';
    });
  });
  const scrub = document.getElementById('scrub');
  scrub.value = Math.round((hourIdx / TRACE.length) * 1000);
  document.getElementById('scrubLabel').textContent =
    (hourIdx / 24).toFixed(1) + ' / ' + (TRACE.length / 24).toFixed(1) + ' days';
}

function spawnParticle(trackId, color) {
  const track = document.getElementById(trackId);
  if (!track) return;
  if (track.querySelectorAll('.particle').length > 8) return;
  const p = document.createElement('div');
  p.className = 'particle'; p.style.background = color; p.style.left = '0%';
  track.appendChild(p);
  requestAnimationFrame(() => { p.style.transition = 'left 900ms linear, opacity 200ms'; p.style.left = '100%'; });
  setTimeout(() => p.remove(), 950);
}

function maybeSpawnForRow(row, hoursElapsed) {
  const t = traceAt(currentHour);
  row.stations.forEach((st, i) => {
    if (i >= row.stations.length - 1) return;
    const key = row.key + '-' + st.id;
    const outVal = (t.out && t.out[st.id]) || 0;
    if (!(key in spawnAccum)) spawnAccum[key] = 0;
    spawnAccum[key] += (outVal / 0.4) * hoursElapsed / 20;
    while (spawnAccum[key] >= 1) { spawnParticle('arrow-' + key, '#4472C4'); spawnAccum[key] -= 1; }
  });
}

function animate(ts) {
  if (lastTs === null) lastTs = ts;
  const dtSec = Math.min(0.25, (ts - lastTs) / 1000);
  lastTs = ts;
  if (playing) {
    const hoursElapsed = dtSec * HOURS_PER_REAL_SEC * speed;
    currentHour = Math.min(TRACE.length - 1, currentHour + hoursElapsed);
    render(currentHour);
    ROWS.forEach(row => maybeSpawnForRow(row, hoursElapsed));
    if (currentHour >= TRACE.length - 1) { playing = false; updatePlayBtn(); }
  }
  requestAnimationFrame(animate);
}

function updatePlayBtn() { document.getElementById('playBtn').textContent = playing ? '⏸ Pause' : '▶ Play'; }

document.getElementById('playBtn').addEventListener('click', () => { playing = !playing; updatePlayBtn(); });
document.getElementById('resetBtn').addEventListener('click', () => { currentHour = 0; playing = false; updatePlayBtn(); render(0); });
document.getElementById('speedSelect').addEventListener('change', e => { speed = parseFloat(e.target.value); });
document.getElementById('scrub').addEventListener('input', e => {
  playing = false; updatePlayBtn();
  currentHour = (parseFloat(e.target.value) / 1000) * TRACE.length;
  render(currentHour);
});

render(0);
requestAnimationFrame(animate);
</script>
</body>
</html>
"""


def build_floor_html(trace: list[dict]) -> str:
    """trace is SimulationResult.trace (a list of per-hour dicts)."""
    row_defs = [
        {"key": "cutclash", "label": "Cut & Clash (1536)",
         "stations": cfg.ROUTE_SEQUENCE[cfg.Route.CUT_AND_CLASH] + [cfg.SHARED_TERMINAL_STATION]},
        {"key": "thermo", "label": "Thermo (Series 1/2/3)",
         "stations": cfg.ROUTE_SEQUENCE[cfg.Route.THERMO] + [cfg.SHARED_TERMINAL_STATION]},
    ]
    rows_json = [
        {
            "key": row["key"], "label": row["label"],
            "stations": [
                {"id": sid, "label": cfg.STATIONS[sid].label, "icon": STATION_ICONS.get(sid, "")}
                for sid in row["stations"]
            ],
        }
        for row in row_defs
    ]

    # Slim the trace down to just what the floor view needs (buf/out/ops/active),
    # rounded to keep the embedded JSON small even for a month-long (~730 hour) run.
    slim_trace = [
        {
            "buf": {k: round(v, 1) for k, v in t["buf"].items()},
            "out": {k: round(v, 2) for k, v in t["out"].items()},
            "ops": t.get("ops", {}),
            "active": t.get("active", []),
        }
        for t in trace
    ]

    all_station_ids = {sid for row in row_defs for sid in row["stations"]}
    max_buf = {
        sid: max((t["buf"].get(sid, 0.0) for t in slim_trace), default=1.0) or 1.0
        for sid in all_station_ids
    }

    html = _TEMPLATE
    html = html.replace("__ROWS_JSON__", json.dumps(rows_json))
    html = html.replace("__TRACE_JSON__", json.dumps(slim_trace))
    html = html.replace("__MAXBUF_JSON__", json.dumps(max_buf))
    return html
