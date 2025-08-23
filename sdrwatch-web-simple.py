#!/usr/bin/env python3
"""
SDRwatch Web (control-enabled)
Run:
  python3 sdrwatch-web-simple.py --db sdrwatch.db --host 0.0.0.0 --port 8080

Adds a minimal process manager to launch/stop sdrwatch.py with JSON params,
Server-Sent Events (SSE) live logs, and a simple Control UI. Keeps all
previous dashboard/detections/baseline pages intact.

2025-08-23 update:
- Added "Detections by frequency (avg across scans)" graph under the existing
  latest-scan frequency graph.
- Added a left-side value bar (mini Y-axis with 0 / 50% / max ticks) to both
  frequency graphs to make bar magnitudes easier to read.
"""
from __future__ import annotations
import argparse, os, io, sqlite3, math, threading, queue, time, shlex, signal, subprocess, json, sys
from typing import Any, Dict, List, Optional, Tuple
from flask import Flask, request, Response, render_template_string, jsonify, abort  # type: ignore

# ================================
# Config
# ================================
CHART_HEIGHT_PX = 160  # Fixed bar height so bars render even if Tailwind is absent
API_TOKEN = os.getenv("SDRWATCH_TOKEN", "")  # set in systemd or shell
SDRWATCH_BIN = os.getenv("SDRWATCH_BIN", "sdrwatch.py")  # path to sdrwatch.py

# ================================
# HTML (now includes Control page)
# ================================
HTML = r"""
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>SDRwatch</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    .stat { border-radius: 1rem; padding: 1rem; background: #0f172a; color: #e2e8f0 }
    .card { border-radius: 1rem; padding: 1rem; background: rgba(255,255,255,.05); color: #e2e8f0; border: 1px solid rgba(255,255,255,.1) }
    .chip { display:inline-flex; align-items:center; padding:.125rem .5rem; border-radius:999px; font-size:.75rem; background: rgba(255,255,255,.1) }
    .btn { display:inline-flex; gap:.5rem; align-items:center; padding:.5rem .75rem; border-radius:.75rem; background:#0284c7; color:white }
    .btn.red { background:#ef4444 }
    .input { padding:.5rem .75rem; border-radius:.75rem; border:1px solid rgba(255,255,255,.1); background: rgba(255,255,255,.05); color:#e2e8f0 }
    .table { width:100%; font-size:.9rem }
    .th,.td { padding:.5rem .75rem; text-align:left; border-bottom:1px solid rgba(255,255,255,.1) }
    .bar { background:#0ea5e9; }
    .muted { color:#94a3b8 }
    .pill { padding:.125rem .5rem; border-radius:999px; font-size:.75rem }
    .pill.idle { background:#334155; color:#e2e8f0 }
    .pill.run { background:#16a34a; color:#052e16 }
    .yaxis { width: 40px; position: relative; }
    .yaxis .tick { position:absolute; left:0; right:0; font-size:10px; color:#94a3b8 }
    .ygrid { position:relative; }
    .ygrid::before, .ygrid::after { content:""; position:absolute; left:0; right:0; height:1px; background: rgba(255,255,255,.12); }
    .ygrid::before { top:0; }
    .ygrid::after { bottom:0; }
    .ygrid .mid { position:absolute; left:0; right:0; top:50%; height:1px; background: rgba(255,255,255,.12); }
  </style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen">
<header class="sticky top-0 z-10 backdrop-blur bg-slate-950/70 border-b border-white/10">
  <div class="max-w-7xl mx-auto px-4 py-3 flex items-center gap-6">
    <div class="text-xl font-semibold">📡 SDRwatch</div>
    <nav class="flex items-center gap-4 text-sm">
      <a href="/control" class="underline">Control</a>
      <a href="/" class="underline">Dashboard</a>
      <a href="/detections" class="underline">Detections</a>
      <a href="/scans" class="underline">Scans</a>
      <a href="/baseline" class="underline">Baseline</a>
    </nav>
  </div>
</header>

<main class="max-w-7xl mx-auto px-4 py-6">
{% if page == 'control' %}
  <section class="grid grid-cols-1 lg:grid-cols-3 gap-4">
    <div class="card lg:col-span-2">
      <div class="flex items-center justify-between mb-2">
        <h2 class="text-lg font-semibold">SDRwatch Control</h2>
        <div><span class="muted">State:</span> <span id="state-pill" class="pill idle">Idle</span></div>
      </div>
      <form id="ctl" class="grid grid-cols-2 md:grid-cols-4 gap-3">
        <input class="input" name="driver" value="rtlsdr" placeholder="driver" />
        <input class="input" name="gain" value="auto" placeholder="gain (dB|auto)" />
        <input class="input" name="start" value="88e6" placeholder="start Hz" />
        <input class="input" name="stop" value="108e6" placeholder="stop Hz" />
        <input class="input" name="step" value="2.4e6" placeholder="step Hz" />
        <input class="input" name="samp_rate" value="2.4e6" placeholder="samp rate Hz" />
        <input class="input" name="fft" value="4096" placeholder="fft" />
        <input class="input" name="avg" value="8" placeholder="avg" />
        <input class="input" name="threshold_db" value="8" placeholder="threshold dB" />
        <input class="input" name="guard_bins" value="1" placeholder="guard bins" />
        <input class="input" name="min_width_bins" value="2" placeholder="min width bins" />
        <select class="input" name="cfar">
          <option value="os" selected>CFAR: os</option>
          <option value="off">CFAR: off</option>
        </select>
        <input class="input" name="cfar_train" value="24" placeholder="cfar train" />
        <input class="input" name="cfar_guard" value="4" placeholder="cfar guard" />
        <input class="input" name="cfar_quantile" value="0.75" placeholder="cfar quantile" />
        <input class="input" name="cfar_alpha_db" value="" placeholder="cfar alpha dB (opt)" />
        <input class="input" name="bandplan" value="" placeholder="bandplan.csv (opt)" />
        <input class="input" name="db" value="{{ db_path }}" placeholder="db path" />
        <input class="input" name="jsonl" value="" placeholder="jsonl path (opt)" />
        <label class="flex items-center gap-2"><input type="checkbox" name="notify" /> <span class="text-sm">Desktop notify</span></label>
        <input class="input" name="new_ema_occ" value="0.02" placeholder="new ema occ" />
        <select class="input" name="mode">
          <option value="single">Single sweep</option>
          <option value="loop">Loop</option>
          <option value="repeat">Repeat N</option>
          <option value="duration">Duration</option>
        </select>
        <input class="input" name="repeat" value="" placeholder="N (repeat)" />
        <input class="input" name="duration" value="" placeholder="e.g. 10m" />
        <input class="input" name="sleep_between_sweeps" value="0" placeholder="sleep s" />
        <div class="col-span-2 flex gap-2 mt-1">
          <button class="btn" type="submit">▶ Start</button>
          <button class="btn red" type="button" id="stopBtn">■ Stop</button>
        </div>
      </form>
      <div class="mt-4">
        <div class="text-xs muted mb-1">Live logs</div>
        <pre id="log" style="height:260px;overflow:auto;background:#0b1220;color:#c7f9ff;padding:8px;border-radius:.75rem"></pre>
      </div>
    </div>
    <div class="card">
      <h3 class="text-lg font-semibold mb-2">Quick presets</h3>
      <div class="grid grid-cols-2 md:grid-cols-3 gap-2 text-sm">
        <button class="btn" onclick="preset('FULL')">Full sweep (24–1766 MHz)</button>
        <button class="btn" onclick="preset('FM')">FM 88–108 MHz</button>
        <button class="btn" onclick="preset('VHF_AIR')">VHF Air 118–137</button>
        <button class="btn" onclick="preset('UHF_MILAIR')">UHF MilAir 225–400</button>
        <button class="btn" onclick="preset('2m')">2 m 144–146</button>
        <button class="btn" onclick="preset('70cm')">70 cm 430–440</button>
        <button class="btn" onclick="preset('MARINE')">Marine VHF 156–162.6</button>
        <button class="btn" onclick="preset('NOAA')">NOAA WX 162.4–162.55</button>
        <button class="btn" onclick="preset('AIS')">AIS 161.975–162.025</button>
        <button class="btn" onclick="preset('ADSB')">ADS‑B 1090</button>
        <button class="btn" onclick="preset('PMR446')">PMR446 446.0–446.2</button>
        <button class="btn" onclick="preset('TETRA')">TETRA 390–430</button>
        <button class="btn" onclick="preset('LTE800')">LTE 800 DL 791–821</button>
      </div>
      <div class="mt-4 text-xs muted">Edit the fields after applying a preset if needed.</div>
      <div class="mt-4 text-xs"><span class="muted">Token:</span> stored in browser localStorage as <code>SDRWATCH_TOKEN</code>.</div>
    </div>
  </section>
  <script>
  const TOKEN = localStorage.getItem("SDRWATCH_TOKEN") || "";
  function authHeaders(){ return TOKEN ? {"Authorization":"Bearer "+TOKEN, "Content-Type":"application/json"} : {"Content-Type":"application/json"}; }
  const pill = document.getElementById('state-pill');
  function setState(s){ pill.textContent = s; pill.className = 'pill ' + (s==='running'?'run':'idle'); }

  function preset(k){
    const f = document.getElementById('ctl');
    // sensible global defaults
    f.samp_rate.value = '2.4e6';
    f.fft.value = '4096';
    f.avg.value = '8';
    f.gain.value = f.gain.value || 'auto';

    if(k==='FULL'){ f.start.value='24e6'; f.stop.value='1766e6'; f.step.value='2.4e6'; }
    if(k==='FM'){ f.start.value='88e6'; f.stop.value='108e6'; f.step.value='2.4e6'; }
    if(k==='VHF_AIR'){ f.start.value='118e6'; f.stop.value='137e6'; f.step.value='500e3'; }
    if(k==='UHF_MILAIR'){ f.start.value='225e6'; f.stop.value='400e6'; f.step.value='2.4e6'; }
    if(k==='2m'){ f.start.value='144e6'; f.stop.value='146e6'; f.step.value='1.2e6'; }
    if(k==='70cm'){ f.start.value='430e6'; f.stop.value='440e6'; f.step.value='2.4e6'; }
    if(k==='MARINE'){ f.start.value='156e6'; f.stop.value='162.6e6'; f.step.value='1.2e6'; }
    if(k==='NOAA'){ f.start.value='162.4e6'; f.stop.value='162.55e6'; f.step.value='100e3'; }
    if(k==='AIS'){ f.start.value='161.975e6'; f.stop.value='162.025e6'; f.step.value='200e3'; }
    if(k==='ADSB'){ f.start.value='1089e6'; f.stop.value='1091e6'; f.step.value='2.4e6'; }
    if(k==='PMR446'){ f.start.value='446.0e6'; f.stop.value='446.2e6'; f.step.value='200e3'; }
    if(k==='TETRA'){ f.start.value='390e6'; f.stop.value='430e6'; f.step.value='2.4e6'; }
    if(k==='LTE800'){ f.start.value='791e6'; f.stop.value='821e6'; f.step.value='2.4e6'; }
  }

  document.getElementById('ctl').addEventListener('submit', async (e)=>{
    e.preventDefault();
    const fd = new FormData(e.target);
    const body = Object.fromEntries(fd.entries());
    // numeric fields
    const maybeNum = (k)=>{ if(body[k]!==undefined && body[k]!=='' && !isNaN(Number(body[k]))) body[k] = Number(body[k]); };
    ['start','stop','step','samp_rate','fft','avg','threshold_db','guard_bins','min_width_bins','cfar_train','cfar_guard','cfar_quantile','cfar_alpha_db','new_ema_occ','sleep_between_sweeps','repeat'].forEach(maybeNum);
    // mode flags
    if(body.mode==='loop'){ body.loop = true; }
    if(body.mode==='repeat'){ body.repeat = body.repeat||1; }
    if(body.mode==='duration'){ /* keep duration string */ }
    delete body.mode;
    // booleans
    if(body.notify==='on'){ body.notify = true; }

    fetch('/api/scans', { method:'POST', headers: authHeaders(), body: JSON.stringify(body) })
      .then(r=>r.json()).then(j=>{ setState(j.status?.state||'running'); }).catch(console.error);
  });
  document.getElementById('stopBtn').onclick = ()=>{
    fetch('/api/scans/active', { method:'DELETE', headers: authHeaders() })
      .then(()=>setState('idle')).catch(console.error);
  };
  async function poll(){
    try{ const r = await fetch('/api/now', {headers: authHeaders()}); const j = await r.json(); setState(j.state||j.status?.state||'idle'); }catch(e){}
    setTimeout(poll, 1000);
  }
  poll();
  // SSE logs
  const log = document.getElementById('log');
  const es = new EventSource('/api/logs');
  es.onmessage = (e)=>{ log.textContent += e.data + "\n"; log.scrollTop = log.scrollHeight; };
  es.onerror = ()=>{ /* ignore */ };
  </script>

{% elif page == 'dashboard' %}
  <section class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
    <div class="stat"><div class="text-xs uppercase muted">Scans</div><div class="text-2xl font-semibold">{{ scans_total }}</div></div>
    <div class="stat"><div class="text-xs uppercase muted">Detections</div><div class="text-2xl font-semibold">{{ detections_total }}</div></div>
    <div class="stat"><div class="text-xs uppercase muted">Baseline bins</div><div class="text-2xl font-semibold">{{ baseline_total }}</div></div>
    <div class="stat">
      <div class="text-xs uppercase muted">Latest scan</div>
      {% if latest %}
      <div class="text-sm">ID {{ latest.id }}<br/>{{ latest.t_start_utc }} → {{ latest.t_end_utc or '…' }}<br/>{{ (latest.f_start_hz/1e6)|round(3) }}–{{ (latest.f_stop_hz/1e6)|round(3) }} MHz</div>
      {% else %}<div class="text-sm">No scans</div>{% endif %}
    </div>
  </section>

  <section class="grid grid-cols-1 lg:grid-cols-3 gap-4 mt-6">
    <!-- SNR histogram + stats -->
    <div class="card lg:col-span-2">
      <div class="flex items-center justify-between mb-2">
        <h2 class="text-lg font-semibold">SNR distribution</h2>
        <a href="/detections" class="underline">View detections »</a>
      </div>
      {% if snr_stats and snr_hist %}
      <div class="text-xs muted mb-2 flex flex-wrap gap-4">
        <div><span class="muted">Count:</span> {{ snr_stats.count }}</div>
        <div><span class="muted">Median:</span> {{ '%.1f' % snr_stats.p50 }} dB</div>
        <div><span class="muted">p90:</span> {{ '%.1f' % snr_stats.p90 }} dB</div>
        <div><span class="muted">Max:</span> {{ '%.1f' % snr_stats.p100 }} dB</div>
        <div><span class="muted">Bucket:</span> {{ snr_bucket_db }} dB</div>
      </div>
      <div class="flex gap-1 items-end" style="height: {{ chart_px }}px">
        {% for b in snr_hist %}
          <div class="flex flex-col items-center" title="{{ b.count }} detections">
            <div class="w-6 bar rounded-t" style="height: {{ b.height_px }}px; background: #0ea5e9;"></div>
            <div class="text-[10px] muted rotate-45 origin-top-left -mt-1">{{ b.label }}</div>
          </div>
        {% endfor %}
      </div>
      {% else %}
        <div class="text-sm muted">No SNR data.</div>
      {% endif %}
    </div>

    <!-- Top services -->
    <div class="card">
      <h2 class="text-lg font-semibold mb-2">Top services</h2>
      <ul class="space-y-1 text-sm">
      {% for s in top_services %}
        <li class="flex items-center justify-between"><span class="chip">{{ s.service }}</span><span class="text-slate-300">{{ s.count }}</span></li>
      {% else %}
        <li class="text-slate-400">No data.</li>
      {% endfor %}
      </ul>
    </div>
  </section>

  <section class="grid grid-cols-1 lg:grid-cols-3 gap-4 mt-4">
    <!-- Frequency distribution (latest scan) -->
    <div class="card lg:col-span-2">
      <div class="flex items-center justify-between mb-2">
        <h2 class="text-lg font-semibold">Detections by frequency (latest scan)</h2>
        {% if latest %}<div class="text-xs muted">{{ (latest.f_start_hz/1e6)|round(3) }}–{{ (latest.f_stop_hz/1e6)|round(3) }} MHz • bins={{ freq_bins|length }}{% endif %}</div>
      </div>
      {% if freq_bins and freq_bins | length > 0 %}
        <div class="flex items-end gap-3">
          <!-- left value bar -->
          <div class="yaxis" style="height: {{ chart_px }}px">
            <div class="tick" style="top:-6px">{{ freq_max }}</div>
            <div class="tick" style="top: calc(50% - 6px);">{{ '%d' % (freq_max/2) }}</div>
            <div class="tick" style="bottom:-6px">0</div>
          </div>
          <!-- chart with subtle grid lines -->
          <div class="ygrid" style="height: {{ chart_px }}px; width:100%">
            <div class="mid"></div>
            <div class="flex gap-[2px] items-end h-full">
              {% for b in freq_bins %}
                <div class="flex flex-col items-center" title="{{ '%.3f' % b.mhz_start }}–{{ '%.3f' % b.mhz_end }} MHz: {{ b.count }}">
                  <div class="w-3 bar rounded-t" style="height: {{ b.height_px }}px; background: #0ea5e9;"></div>
                </div>
              {% endfor %}
            </div>
          </div>
        </div>
        <div class="flex justify-between text-[10px] muted mt-1">
          <div>{{ '%.3f' % freq_bins[0].mhz_start }} MHz</div>
          <div>{{ '%.3f' % freq_bins[-1].mhz_end }} MHz</div>
        </div>
      {% else %}
        <div class="text-sm muted">No detections for the latest scan.</div>
      {% endif %}
    </div>

    <!-- Strongest signals -->
    <div class="card">
      <div class="flex items-center justify-between mb-2">
        <h2 class="text-lg font-semibold">Strongest signals</h2>
        <a class="underline text-xs" href="/detections?min_snr=10">filter ≥10 dB »</a>
      </div>
      <table class="table">
        <thead><tr class="text-slate-400"><th class="th">MHz</th><th class="th">SNR</th><th class="th">Service</th></tr></thead>
        <tbody>
        {% for r in strongest %}
          <tr class="hover:bg-white/5">
            <td class="td">{{ (r.f_center_hz/1e6)|round(6) }}</td>
            <td class="td">{{ '%.1f' % r.snr_db }}</td>
            <td class="td"><span class="chip">{{ r.service or 'Unknown' }}</span></td>
          </tr>
        {% else %}
          <tr><td class="td" colspan="3">No detections.</td></tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  </section>

  <!-- NEW: averaged-by-frequency across all scans -->
  <section class="grid grid-cols-1 lg:grid-cols-3 gap-4 mt-4">
    <div class="card lg:col-span-2">
      <div class="flex items-center justify-between mb-2">
        <h2 class="text-lg font-semibold">Detections by frequency (avg across scans)</h2>
        {% if avg_bins and avg_bins|length>0 %}
          <div class="text-xs muted">{{ '%.3f' % avg_start_mhz }}–{{ '%.3f' % avg_stop_mhz }} MHz • bins={{ avg_bins|length }} • avg per covered-scan</div>
        {% endif %}
      </div>
      {% if avg_bins and avg_bins | length > 0 %}
        <div class="flex items-end gap-3">
          <!-- left value bar -->
          <div class="yaxis" style="height: {{ chart_px }}px">
            <div class="tick" style="top:-6px">{{ '%.1f' % avg_max }}</div>
            <div class="tick" style="top: calc(50% - 6px);">{{ '%.1f' % (avg_max/2) }}</div>
            <div class="tick" style="bottom:-6px">0</div>
          </div>
          <div class="ygrid" style="height: {{ chart_px }}px; width:100%">
            <div class="mid"></div>
            <div class="flex gap-[2px] items-end h-full">
              {% for b in avg_bins %}
                <div class="flex flex-col items-center" title="{{ '%.3f' % b.mhz_start }}–{{ '%.3f' % b.mhz_end }} MHz: avg {{ '%.2f' % b.count }} ({{ b.coverage }} scans)">
                  <div class="w-3 bar rounded-t" style="height: {{ b.height_px }}px; background: #0ea5e9;"></div>
                </div>
              {% endfor %}
            </div>
          </div>
        </div>
        <div class="flex justify-between text-[10px] muted mt-1">
          <div>{{ '%.3f' % avg_bins[0].mhz_start }} MHz</div>
          <div>{{ '%.3f' % avg_bins[-1].mhz_end }} MHz</div>
        </div>
      {% else %}
        <div class="text-sm muted">Not enough data to compute average across scans.</div>
      {% endif %}
    </div>
  </section>

  <section class="grid grid-cols-1 lg:grid-cols-3 gap-4 mt-4">
    <!-- Detections per hour (last 24h) -->
    <div class="card lg:col-span-3">
      <div class="flex items-center justify-between mb-2">
        <h2 class="text-lg font-semibold">Detections over time (last 24h)</h2>
        <div class="text-xs muted">Hour buckets (UTC)</div>
      </div>
      {% if by_hour and by_hour | length > 0 %}
        <div class="flex gap-1 items-end" style="height: {{ chart_px }}px">
          {% for h in by_hour %}
            <div class="flex flex-col items-center" title="{{ h.hour }}: {{ h.count }}">
              <div class="w-4 bar rounded-t" style="height: {{ h.height_px }}px; background: #0ea5e9;"></div>
              <div class="text-[10px] muted rotate-45 origin-top-left -mt-1">{{ h.hour[-8:-3] }}</div>
            </div>
          {% endfor %}
        </div>
      {% else %}
        <div class="text-sm muted">No recent detections.</div>
      {% endif %}
    </div>
  </section>
{% elif page == 'detections' %}
  {{ detections_html|safe }}
{% elif page == 'scans' %}
  {{ scans_html|safe }}
{% elif page == 'baseline' %}
  {{ baseline_html|safe }}
{% endif %}
</main>
</body>
</html>
"""

# ================================
# DB helpers
# ================================

def open_db_ro(path: str) -> sqlite3.Connection:
    abspath = os.path.abspath(path)
    con = sqlite3.connect(f"file:{abspath}?mode=ro", uri=True, check_same_thread=False)
    con.execute("PRAGMA busy_timeout=2000;")
    con.row_factory = lambda cur, row: {d[0]: row[i] for i, d in enumerate(cur.description)}
    return con

def q1(con: sqlite3.Connection, sql: str, params: Tuple[Any, ...] = ()):  # one row
    cur = con.execute(sql, params)
    return cur.fetchone()

def qa(con: sqlite3.Connection, sql: str, params: Tuple[Any, ...] = ()):  # all rows
    cur = con.execute(sql, params)
    return cur.fetchall()

# ================================
# Stats/graphs helpers
# ================================

def _percentile(xs: List[float], p: float) -> Optional[float]:
    if not xs: return None
    xs = sorted(xs); k = (len(xs) - 1) * p; f = int(math.floor(k)); c = int(math.ceil(k))
    if f == c: return float(xs[f])
    return float(xs[f] + (xs[c] - xs[f]) * (k - f))

def _scale_counts_to_px(series: List[Dict[str, Any]], count_key: str = "count") -> float:
    """Attach height_px to each item based on its count; return max count used for scaling.
    Works with ints or floats. Ensures a minimum visible bar height of 2px for nonzero values."""
    values: List[float] = []
    for x in series:
        try:
            v = float(x.get(count_key, 0) or 0)
        except Exception:
            v = 0.0
        values.append(v)
    maxc = max(values) if values else 0.0
    for i, x in enumerate(series):
        c = values[i]
        if maxc <= 0 or c <= 0:
            x["height_px"] = 0
        else:
            h = int(round((c / maxc) * CHART_HEIGHT_PX))
            x["height_px"] = max(2, h)
    return maxc

def snr_histogram(con: sqlite3.Connection, bucket_db: int = 3):
    rows = qa(con, "SELECT snr_db FROM detections WHERE snr_db IS NOT NULL")
    vals: List[float] = []
    for r in rows:
        try: vals.append(float(r['snr_db']))
        except Exception: pass
    buckets: Dict[int, int] = {}
    for s in vals:
        b = int(math.floor(s / bucket_db)) * bucket_db
        buckets[b] = buckets.get(b, 0) + 1
    labels_sorted = sorted(buckets.keys())
    hist = [{"label": f"{b}–{b+bucket_db}", "count": buckets[b]} for b in labels_sorted]
    _scale_counts_to_px(hist, "count")
    stats = None
    if vals:
        stats = {"count": len(vals), "p50": _percentile(vals, 0.50) or 0.0, "p90": _percentile(vals, 0.90) or 0.0, "p100": max(vals)}
    return hist, stats

def detections_by_hour(con: sqlite3.Connection, hours: int = 24):
    rows = qa(con, """
        SELECT strftime('%Y-%m-%d %H:00:00', time_utc) AS hour, COUNT(*) AS c
        FROM detections
        WHERE time_utc >= datetime('now', ?)
        GROUP BY hour
        ORDER BY hour
    """, (f"-{hours-1} hours",))
    # Normalize to include empty hours (UTC)
    from datetime import datetime, timedelta
    now = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    timeline = [(now - timedelta(hours=i)).strftime("%Y-%m-%d %H:00:00") for i in reversed(range(hours))]
    lookup = {r['hour']: int(r['c']) for r in rows if r['hour'] is not None}
    out = [{"hour": h, "count": lookup.get(h, 0)} for h in timeline]
    _scale_counts_to_px(out, "count")
    return out

def frequency_bins_latest_scan(con: sqlite3.Connection, num_bins: int = 40):
    latest = q1(con, "SELECT id, t_start_utc, t_end_utc, f_start_hz, f_stop_hz FROM scans ORDER BY COALESCE(t_end_utc,t_start_utc) DESC LIMIT 1")
    if not latest:
        return [], None, 0
    f0 = float(latest['f_start_hz']); f1 = float(latest['f_stop_hz'])
    if not (f1 > f0):
        return [], latest, 0
    dets = qa(con, "SELECT f_center_hz FROM detections WHERE scan_id = ?", (latest['id'],))
    if not dets:
        return [], latest, 0
    width = (f1 - f0) / max(1, num_bins)
    bins = [{"count":0, "mhz_start": (f0 + i*width)/1e6, "mhz_end": (f0 + (i+1)*width)/1e6} for i in range(num_bins)]
    for r in dets:
        try: fc = float(r['f_center_hz'])
        except Exception: continue
        if fc < f0 or fc >= f1: continue
        idx = int((fc - f0) // width); idx = max(0, min(num_bins-1, idx)); bins[idx]["count"] += 1
    maxc = _scale_counts_to_px(bins, "count")
    return bins, latest, int(maxc)

def frequency_bins_all_scans_avg(con: sqlite3.Connection, num_bins: int = 40):
    # Establish a global frequency span from detections (so we only plot where data exists)
    bounds = q1(con, "SELECT MIN(f_center_hz) AS fmin, MAX(f_center_hz) AS fmax FROM detections")
    if not bounds or bounds['fmin'] is None or bounds['fmax'] is None:
        return [], 0.0, 0.0, 0.0
    f0 = float(bounds['fmin']); f1 = float(bounds['fmax'])
    if not (f1 > f0):
        return [], 0.0, 0.0, 0.0
    # Pre-fetch data
    dets = qa(con, "SELECT f_center_hz FROM detections WHERE f_center_hz BETWEEN ? AND ?", (int(f0), int(f1)))
    scans = qa(con, "SELECT f_start_hz, f_stop_hz FROM scans WHERE f_stop_hz > f_start_hz")
    width = (f1 - f0) / max(1, num_bins)
    bins: List[Dict[str, Any]] = [{"count":0.0, "coverage":0, "mhz_start": (f0 + i*width)/1e6, "mhz_end": (f0 + (i+1)*width)/1e6} for i in range(num_bins)]
    # Count detections per bin (absolute)
    for r in dets:
        try: fc = float(r['f_center_hz'])
        except Exception: continue
        if fc < f0 or fc >= f1: continue
        idx = int((fc - f0) // width); idx = max(0, min(num_bins-1, idx)); bins[idx]["count"] += 1.0
    # Compute coverage (how many scans actually covered each bin) and convert count -> per-covered-scan average
    for i in range(num_bins):
        b_start = f0 + i*width
        b_end   = f0 + (i+1)*width
        cov = 0
        for s in scans:
            try:
                s0 = float(s['f_start_hz']); s1 = float(s['f_stop_hz'])
            except Exception:
                continue
            if (s0 < b_end) and (s1 > b_start):  # overlap
                cov += 1
        bins[i]["coverage"] = cov
        if cov > 0:
            bins[i]["count"] = bins[i]["count"] / float(cov)
        else:
            bins[i]["count"] = 0.0
    maxc = _scale_counts_to_px(bins, "count")
    return bins, f0/1e6, f1/1e6, maxc

def strongest_signals(con: sqlite3.Connection, limit: int = 10):
    return qa(con, """
        SELECT f_center_hz, snr_db, service
        FROM detections
        WHERE snr_db IS NOT NULL
        ORDER BY snr_db DESC
        LIMIT ?
    """, (limit,))

# ================================
# Process Manager
# ================================
class ScanManager:
    def __init__(self, db_path: str):
        self.proc: Optional[subprocess.Popen] = None
        self.logs_q: "queue.Queue[str]" = queue.Queue(maxsize=2000)
        self.status: Dict[str, Any] = {"state": "idle", "pid": None, "started_at": None, "params": None}
        self.db_path = db_path
        self._pump_thread: Optional[threading.Thread] = None

    def _to_args(self, p: Dict[str, Any]) -> List[str]:
        args: List[str] = []
        def add(flag: str, val: Any=None):
            if val is None or val=="": return
            args.append(flag); args.append(str(val))
        # Required
        add("--start", p.get("start"))
        add("--stop", p.get("stop"))
        # Optional core
        add("--step", p.get("step"))
        add("--samp-rate", p.get("samp_rate"))
        add("--fft", p.get("fft"))
        add("--avg", p.get("avg"))
        # Backend
        if p.get("driver"): add("--driver", p.get("driver"))
        if p.get("gain"): add("--gain", p.get("gain"))
        # Detect
        add("--threshold-db", p.get("threshold_db"))
        add("--guard-bins", p.get("guard_bins"))
        add("--min-width-bins", p.get("min_width_bins"))
        if p.get("cfar"):
            args += ["--cfar", str(p.get("cfar"))]
        add("--cfar-train", p.get("cfar_train"))
        add("--cfar-guard", p.get("cfar_guard"))
        add("--cfar-quantile", p.get("cfar_quantile"))
        if p.get("cfar_alpha_db") not in (None,""):
            add("--cfar-alpha-db", p.get("cfar_alpha_db"))
        # Misc
        if p.get("bandplan"): add("--bandplan", p.get("bandplan"))
        add("--db", p.get("db", self.db_path))
        if p.get("jsonl"): add("--jsonl", p.get("jsonl"))
        if p.get("notify") is True: args.append("--notify")
        add("--new-ema-occ", p.get("new_ema_occ"))
        # Modes (mutually exclusive in CLI)
        if p.get("loop"): args.append("--loop")
        if p.get("repeat") not in (None,""):
            add("--repeat", p.get("repeat"))
        if p.get("duration") not in (None,""):
            add("--duration", p.get("duration"))
        add("--sleep-between-sweeps", p.get("sleep_between_sweeps"))
        return args

    def start(self, params: Dict[str, Any]):
        if self.proc and self.proc.poll() is None:
            raise RuntimeError("Scan already running")
        # Ensure DB path is set
        if not params.get("db"): params["db"] = self.db_path
        cmd = [os.fspath(sys.executable), SDRWATCH_BIN] + self._to_args(params)
        self._emit(f"Launching: {' '.join(shlex.quote(c) for c in cmd)}")
        self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
        self.status.update({"state":"running","pid":self.proc.pid,"started_at":time.time(),"params":params})
        self._pump_thread = threading.Thread(target=self._pump, daemon=True)
        self._pump_thread.start()

    def stop(self):
        if not self.proc or self.proc.poll() is not None:
            return
        try:
            self._emit("Stopping scan (SIGTERM)…")
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._emit("Force killing scan (SIGKILL)…")
                self.proc.kill()
        finally:
            self.proc = None
            self.status.update({"state":"idle","pid":None})

    def _pump(self):
        assert self.proc is not None
        for line in self.proc.stdout or []:
            self._emit(line.rstrip("\n"))
        rc = self.proc.wait()
        self._emit(f"Process exited with code {rc}")
        self.status.update({"state":"idle","pid":None})
        self.proc = None

    def _emit(self, line: str):
        try:
            self.logs_q.put_nowait(line)
        except queue.Full:
            try: self.logs_q.get_nowait()
            except Exception: pass
            try: self.logs_q.put_nowait(line)
            except Exception: pass

    def stream_logs(self):
        # Generator for SSE
        yield "retry: 1000\n\n"
        while True:
            try:
                line = self.logs_q.get(timeout=1.0)
                yield f"data: {line}\n\n"
            except queue.Empty:
                # heartbeat to keep connection alive
                yield ":\n\n"

# ================================
# Flask app
# ================================

def create_app(db_path: str) -> Flask:
    app = Flask(__name__)
    app._con = open_db_ro(db_path)
    app._mgr = ScanManager(db_path)
    app._db_path = db_path

    def con(): return app._con

    def require_auth():
        if not API_TOKEN:
            return  # auth disabled
        hdr = request.headers.get("Authorization", "")
        if hdr != f"Bearer {API_TOKEN}":
            abort(401)

    # ---------- Pages ----------
    @app.get('/control')
    def control():
        return render_template_string(HTML, page='control', db_path=app._db_path)

    # Dashboard/Datasets (reuse old markup as partials)
    @app.route('/')
    def dashboard():
        scans_total = q1(con(), "SELECT COUNT(*) AS c FROM scans")['c'] or 0
        detections_total = q1(con(), "SELECT COUNT(*) AS c FROM detections")['c'] or 0
        baseline_total = q1(con(), "SELECT COUNT(*) AS c FROM baseline")['c'] or 0

        snr_bucket_db = 3
        snr_hist, snr_stats = snr_histogram(con(), bucket_db=snr_bucket_db)
        freq_bins, latest, freq_max = frequency_bins_latest_scan(con(), num_bins=40)
        avg_bins, avg_start_mhz, avg_stop_mhz, avg_max = frequency_bins_all_scans_avg(con(), num_bins=40)
        by_hour = detections_by_hour(con(), hours=24)
        top_services = qa(con(), "SELECT COALESCE(service,'Unknown') AS service, COUNT(*) AS count FROM detections GROUP BY COALESCE(service,'Unknown') ORDER BY count DESC LIMIT 10")
        strongest = strongest_signals(con(), limit=10)

        return render_template_string(
            HTML,
            page='dashboard',
            scans_total=scans_total,
            detections_total=detections_total,
            baseline_total=baseline_total,
            latest=latest,
            snr_hist=snr_hist,
            snr_stats=snr_stats,
            snr_bucket_db=snr_bucket_db,
            freq_bins=freq_bins,
            freq_max=freq_max,
            avg_bins=avg_bins,
            avg_start_mhz=avg_start_mhz,
            avg_stop_mhz=avg_stop_mhz,
            avg_max=avg_max,
            by_hour=by_hour,
            top_services=top_services,
            strongest=strongest,
            chart_px=CHART_HEIGHT_PX,
        )

    @app.route('/detections')
    def detections():
        args = request.args
        service = args.get('service') or None
        min_snr = args.get('min_snr')
        fmin = args.get('f_min_mhz')
        fmax = args.get('f_max_mhz')
        hours = args.get('since_hours')
        where = []
        params: List[Any] = []
        if service:
            where.append("COALESCE(service,'Unknown') = ?"); params.append(service)
        if min_snr not in (None, ''):
            where.append("snr_db >= ?"); params.append(float(min_snr))
        if fmin not in (None, ''):
            where.append("f_center_hz >= ?"); params.append(int(float(fmin)*1e6))
        if fmax not in (None, ''):
            where.append("f_center_hz <= ?"); params.append(int(float(fmax)*1e6))
        if hours not in (None, '') and int(float(hours))>0:
            where.append("time_utc >= datetime('now', ?)"); params.append(f"-{int(float(hours))} hours")
        where_sql = (" WHERE "+" AND ".join(where)) if where else ""
        page = max(1, int(float(args.get('page',1))))
        page_size = min(200, max(10, int(float(args.get('page_size',50)))))
        total = q1(con(), f"SELECT COUNT(*) AS c FROM detections{where_sql}", tuple(params))['c']
        offset = (page-1)*page_size
        rows = qa(con(), f"""
            SELECT time_utc, scan_id, f_center_hz, f_low_hz, f_high_hz,
                   peak_db, noise_db, snr_db, service, region, notes
            FROM detections {where_sql}
            ORDER BY time_utc DESC
            LIMIT ? OFFSET ?
        """, tuple(params)+(page_size, offset))
        sv = [r['service'] for r in qa(con(), "SELECT DISTINCT COALESCE(service,'Unknown') AS service FROM detections ORDER BY service")]
        qs = args.to_dict(flat=True)
        qs = "&".join([f"{k}={v}" for k,v in qs.items()])
        # build partial
        detections_html = render_template_string(r"""
  <section class="card">
    <div class="flex items-end gap-3 flex-wrap">
      <div class="grow"><h2 class="text-lg font-semibold">Detections</h2><div class="text-xs muted">{{ total }} total</div></div>
      <a class="btn" href="/export/detections.csv?{{ qs }}">Export CSV</a>
    </div>
    <form class="mt-4 grid grid-cols-2 md:grid-cols-6 gap-3" method="get" action="/detections">
      <select name="service" class="input">
        <option value="">Service: any</option>
        {% for s in services %}<option value="{{ s }}" {% if req_args.get('service')==s %}selected{% endif %}>{{ s }}</option>{% endfor %}
      </select>
      <input class="input" type="number" step="0.1" name="min_snr" value="{{ req_args.get('min_snr','') }}" placeholder="Min SNR dB" />
      <input class="input" type="number" step="0.1" name="f_min_mhz" value="{{ req_args.get('f_min_mhz','') }}" placeholder="Min MHz" />
      <input class="input" type="number" step="0.1" name="f_max_mhz" value="{{ req_args.get('f_max_mhz','') }}" placeholder="Max MHz" />
      <input class="input" type="number" step="1" name="since_hours" value="{{ req_args.get('since_hours','') }}" placeholder="Last N hours" />
      <button class="btn" type="submit">Apply</button>
    </form>
    <div id="detections-table" class="mt-4">
      <table class="table">
        <thead><tr class="text-slate-400"><th class="th">Time (UTC)</th><th class="th">Scan</th><th class="th">Center (MHz)</th><th class="th">Low–High (MHz)</th><th class="th">Peak (dB)</th><th class="th">Noise (dB)</th><th class="th">SNR (dB)</th><th class="th">Service</th><th class="th">Region</th><th class="th">Notes</th></tr></thead>
        <tbody>
          {% for r in rows %}
            <tr class="hover:bg-white/5">
              <td class="td">{{ r.time_utc }}</td><td class="td">{{ r.scan_id }}</td>
              <td class="td">{{ (r.f_center_hz/1e6)|round(6) }}</td>
              <td class="td">{{ (r.f_low_hz/1e6)|round(6) }}–{{ (r.f_high_hz/1e6)|round(6) }}</td>
              <td class="td">{{ '%.1f' % r.peak_db if r.peak_db is not none else '' }}</td>
              <td class="td">{{ '%.1f' % r.noise_db if r.noise_db is not none else '' }}</td>
              <td class="td">{{ '%.1f' % r.snr_db if r.snr_db is not none else '' }}</td>
              <td class="td"><span class="chip">{{ r.service or 'Unknown' }}</span></td>
              <td class="td">{{ r.region or '' }}</td>
              <td class="td truncate max-w-[24ch]" title="{{ r.notes or '' }}">{{ r.notes or '' }}</td>
            </tr>
          {% else %}<tr><td class="td" colspan="10">No detections found.</td></tr>{% endfor %}
        </tbody>
      </table>
      <div class="mt-3 flex items-center gap-2">
        {% set pages = (total // page_size) + (1 if (total % page_size) else 0) %}
        <div>Page {{ page_num }} / {{ pages if pages else 1 }}</div>
        <form method="get" action="/detections" class="flex items-center gap-2">
          <input type="hidden" name="page_size" value="{{ page_size }}" />
          <input type="hidden" name="service" value="{{ req_args.get('service','') }}" />
          <input type="hidden" name="min_snr" value="{{ req_args.get('min_snr','') }}" />
          <input type="hidden" name="f_min_mhz" value="{{ req_args.get('f_min_mhz','') }}" />
          <input type="hidden" name="f_max_mhz" value="{{ req_args.get('f_max_mhz','') }}" />
          <input type="hidden" name="since_hours" value="{{ req_args.get('since_hours','') }}" />
          <button class="btn" name="page" value="{{ 1 if page_num<=1 else page_num-1 }}">Prev</button>
          <button class="btn" name="page" value="{{ pages if page_num>=pages else page_num+1 }}">Next</button>
        </form>
        <form method="get" action="/detections" class="ml-auto">
          <input type="hidden" name="service" value="{{ req_args.get('service','') }}" />
          <input type="hidden" name="min_snr" value="{{ req_args.get('min_snr','') }}" />
          <input type="hidden" name="f_min_mhz" value="{{ req_args.get('f_min_mhz','') }}" />
          <input type="hidden" name="f_max_mhz" value="{{ req_args.get('f_max_mhz','') }}" />
          <input type="hidden" name="since_hours" value="{{ req_args.get('since_hours','') }}" />
          <select name="page_size" class="input" onchange="this.form.submit()">
            {% for sz in [25,50,100,200] %}
              <option value="{{ sz }}" {% if page_size==sz %}selected{% endif %}>{{ sz }} / page</option>
            {% endfor %}
          </select>
        </form>
      </div>
    </div>
  </section>
        """, rows=rows, page_num=page, page_size=page_size, total=total, services=sv, req_args=args, qs=qs)
        return render_template_string(HTML, page='detections', detections_html=detections_html)

    @app.route('/scans')
    def scans():
        args = request.args
        page = max(1, int(float(args.get('page',1))))
        page_size = min(200, max(10, int(float(args.get('page_size',25)))))
        total = q1(con(), "SELECT COUNT(*) AS c FROM scans")['c']
        offset = (page-1)*page_size
        rows = qa(con(), """
            SELECT id, t_start_utc, t_end_utc, f_start_hz, f_stop_hz, step_hz, samp_rate, fft, avg, device, driver
            FROM scans
            ORDER BY COALESCE(t_end_utc,t_start_utc) DESC
            LIMIT ? OFFSET ?
        """, (page_size, offset))
        scans_html = render_template_string(r"""
  <section class="card">
    <div class="flex items-end gap-3 flex-wrap"><div class="grow"><h2 class="text-lg font-semibold">Scans</h2><div class="text-xs muted">{{ total }} total</div></div></div>
    <div class="mt-4">
      <table class="table">
        <thead><tr class="text-slate-400"><th class="th">ID</th><th class="th">Start (UTC)</th><th class="th">End (UTC)</th><th class="th">Range (MHz)</th><th class="th">Step (Hz)</th><th class="th">Rate (Hz)</th><th class="th">FFT</th><th class="th">Avg</th><th class="th">Device</th><th class="th">Driver</th></tr></thead>
        <tbody>
        {% for r in rows %}
          <tr class="hover:bg-white/5"><td class="td">{{ r.id }}</td><td class="td">{{ r.t_start_utc }}</td><td class="td">{{ r.t_end_utc }}</td><td class="td">{{ (r.f_start_hz/1e6)|round(3) }}–{{ (r.f_stop_hz/1e6)|round(3) }}</td><td class="td">{{ r.step_hz }}</td><td class="td">{{ r.samp_rate }}</td><td class="td">{{ r.fft }}</td><td class="td">{{ r.avg }}</td><td class="td">{{ r.device }}</td><td class="td">{{ r.driver }}</td></tr>
        {% else %}
          <tr><td class="td" colspan="10">No scans found.</td></tr>
        {% endfor %}
        </tbody>
      </table>
      <div class="mt-3 flex items-center gap-2">
        {% set pages = (total // page_size) + (1 if (total % page_size) else 0) %}
        <div>Page {{ page_num }} / {{ pages if pages else 1 }}</div>
        <form method="get" action="/scans" class="flex items-center gap-2">
          <input type="hidden" name="page_size" value="{{ page_size }}" />
          <button class="btn" name="page" value="{{ 1 if page_num<=1 else page_num-1 }}">Prev</button>
          <button class="btn" name="page" value="{{ pages if page_num>=pages else page_num+1 }}">Next</button>
        </form>
        <form method="get" action="/scans" class="ml-auto">
          <select name="page_size" class="input" onchange="this.form.submit()">
            {% for sz in [25,50,100,200] %}
              <option value="{{ sz }}" {% if page_size==sz %}selected{% endif %}>{{ sz }} / page</option>
            {% endfor %}
          </select>
        </form>
      </div>
    </div>
  </section>
        """, rows=rows, page_num=page, page_size=page_size, total=total, req_args=args)
        return render_template_string(HTML, page='scans', scans_html=scans_html)

    @app.route('/baseline')
    def baseline():
        args = request.args
        rows: List[Dict[str,Any]] = []
        if args.get('f_mhz') not in (None,''):
            fmhz = float(args.get('f_mhz'))
            window_khz = int(float(args.get('window_khz', 50)))
            center = int(fmhz*1e6)
            half = int(window_khz*1e3)
            rows = qa(con(), """
                SELECT bin_hz, ema_occ, ema_power_db, last_seen_utc, total_obs, hits
                FROM baseline
                WHERE bin_hz BETWEEN ? AND ?
                ORDER BY bin_hz
            """, (center-half, center+half))
        baseline_html = render_template_string(r"""
  <section class="card">
    <div class="flex items-end gap-3 flex-wrap"><div class="grow"><h2 class="text-lg font-semibold">Baseline (EMA around frequency)</h2><div class="text-xs muted">Peek at ema_occ &amp; ema_power_db near a center frequency.</div></div></div>
    <form class="mt-4 grid grid-cols-2 md:grid-cols-6 gap-3" method="get" action="/baseline">
      <input class="input" type="number" step="0.000001" name="f_mhz" value="{{ req_args.get('f_mhz','') }}" placeholder="Center MHz" />
      <input class="input" type="number" step="1" name="window_khz" value="{{ req_args.get('window_khz','50') }}" placeholder="±kHz window" />
      <button class="btn" type="submit">Show</button>
    </form>
    <div class="mt-4">
      <table class="table">
        <thead><tr class="text-slate-400"><th class="th">Bin (MHz)</th><th class="th">EMA occ</th><th class="th">EMA power (dB)</th><th class="th">Last seen (UTC)</th><th class="th">Obs</th><th class="th">Hits</th></tr></thead>
        <tbody>
        {% for r in rows %}
          <tr class="hover:bg-white/5"><td class="td">{{ (r.bin_hz/1e6)|round(6) }}</td><td class="td">{{ '%.3f' % r.ema_occ if r.ema_occ is not none else '' }}</td><td class="td">{{ '%.1f' % r.ema_power_db if r.ema_power_db is not none else '' }}</td><td class="td">{{ r.last_seen_utc }}</td><td class="td">{{ r.total_obs }}</td><td class="td">{{ r.hits }}</td></tr>
        {% else %}
          <tr><td class="td" colspan="6">Enter a frequency to view baseline bins.</td></tr>
        {% endfor %}
        </tbody>
      </table>
    </div>
  </section>
        """, rows=rows, req_args=args)
        return render_template_string(HTML, page='baseline', baseline_html=baseline_html)

    @app.route('/export/detections.csv')
    def export_csv():
        args = request.args
        params: List[Any] = []
        where = []
        if args.get('service'): where.append("COALESCE(service,'Unknown') = ?"); params.append(args.get('service'))
        if args.get('min_snr'): where.append("snr_db >= ?"); params.append(float(args.get('min_snr')))
        if args.get('f_min_mhz'): where.append("f_center_hz >= ?"); params.append(int(float(args.get('f_min_mhz'))*1e6))
        if args.get('f_max_mhz'): where.append("f_center_hz <= ?"); params.append(int(float(args.get('f_max_mhz'))*1e6))
        if args.get('since_hours'): where.append("time_utc >= datetime('now', ?)"); params.append(f"-{int(float(args.get('since_hours')))} hours")
        where_sql = (" WHERE "+" AND ".join(where)) if where else ""
        rows = qa(con(), f"""
            SELECT time_utc, scan_id, f_center_hz, f_low_hz, f_high_hz,
                   peak_db, noise_db, snr_db, service, region, notes
            FROM detections {where_sql}
            ORDER BY time_utc DESC
            LIMIT 100000
        """, tuple(params))
        import csv
        buf = io.StringIO()
        fieldnames = ["time_utc","scan_id","f_center_hz","f_low_hz","f_high_hz","peak_db","noise_db","snr_db","service","region","notes"]
        w = csv.DictWriter(buf, fieldnames=fieldnames); w.writeheader()
        for r in rows: w.writerow({k:r.get(k,'') for k in fieldnames})
        buf.seek(0)
        return Response(buf.read(), mimetype='text/csv', headers={'Content-Disposition':'attachment; filename=detections.csv'})

    # ---------- API ----------
    @app.post('/api/scans')
    def api_start_scan():
        require_auth()
        params = request.get_json(force=True, silent=False) or {}
        # Quick param validation
        def need(k):
            if k not in params or params[k] in (None,""):
                abort(400, description=f"Missing required field: {k}")
        for k in ("start","stop"):
            need(k)
        # Guard: running?
        if app._mgr.proc and app._mgr.proc.poll() is None:
            abort(409, description="Scan already running")
        try:
            app._mgr.start(params)
        except Exception as e:
            abort(400, description=str(e))
        return jsonify({"ok": True, "status": app._mgr.status})

    @app.delete('/api/scans/<scan_id>')  # scan_id ignored for now; single active scan
    def api_stop_scan(scan_id: str):
        require_auth()
        app._mgr.stop()
        return jsonify({"ok": True})

    @app.get('/api/now')
    def api_now():
        require_auth()
        return jsonify(app._mgr.status)

    @app.get('/api/logs')
    def api_logs():
        # Logs are viewable without auth to simplify local debugging; add require_auth() if desired
        return Response(app._mgr.stream_logs(), mimetype='text/event-stream')

    return app

# ================================
# CLI
# ================================

def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=8080)
    return ap.parse_args()

if __name__ == '__main__':
    args = parse_args()
    app = create_app(args.db)
    app.run(host=args.host, port=args.port, threaded=True)
