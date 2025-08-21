#!/usr/bin/env python3
"""
SDRwatch Web (simple, no Jinja macros/blocks)
Schema expected:
  scans(id, t_start_utc, t_end_utc, f_start_hz, f_stop_hz, step_hz, samp_rate, fft, avg, device, driver)
  detections(scan_id, time_utc, f_center_hz, f_low_hz, f_high_hz, peak_db, noise_db, snr_db, service, region, notes)
  baseline(bin_hz, ema_occ, ema_power_db, last_seen_utc, total_obs, hits)
Run:
  python3 sdrwatch-web-simple.py --db sdrwatch.db --host 0.0.0.0 --port 8080
"""
from __future__ import annotations
import argparse, os, io, sqlite3, math
from typing import Any, Dict, List, Optional, Tuple
from flask import Flask, request, Response, render_template_string  # type: ignore

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
    .input { padding:.5rem .75rem; border-radius:.75rem; border:1px solid rgba(255,255,255,.1); background: rgba(255,255,255,.05); color:#e2e8f0 }
    .table { width:100%; font-size:.9rem }
    .th,.td { padding:.5rem .75rem; text-align:left; border-bottom:1px solid rgba(255,255,255,.1) }
    .bar { background:#0ea5e9; } /* sky-500 */
    .muted { color:#94a3b8 } /* slate-400 */
  </style>
</head>
<body class="bg-slate-950 text-slate-100 min-h-screen">
<header class="sticky top-0 z-10 backdrop-blur bg-slate-950/70 border-b border-white/10">
  <div class="max-w-7xl mx-auto px-4 py-3 flex items-center gap-6">
    <div class="text-xl font-semibold">📡 SDRwatch</div>
    <nav class="flex items-center gap-4 text-sm">
      <a href="/" class="underline">Dashboard</a>
      <a href="/detections" class="underline">Detections</a>
      <a href="/scans" class="underline">Scans</a>
      <a href="/baseline" class="underline">Baseline</a>
    </nav>
  </div>
</header>
<main class="max-w-7xl mx-auto px-4 py-6">
{% if page == 'dashboard' %}
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

      {% set maxc = snr_hist | map(attribute='count') | max %}
      <div class="flex gap-1 items-end h-40">
        {% for b in snr_hist %}
          {% set h = (b.count / maxc * 100) if maxc > 0 else 0 %}
          <div class="flex flex-col items-center" title="{{ b.count }} detections">
            <div class="w-6 bar rounded-t" style="height: {{ '%.1f' % h }}%"></div>
            <div class="text-[10px] muted rotate-45 origin-top-left -mt-1">{{ b.label }}</div>
          </div>
        {% endfor %}
      </div>
      {% else %}
        <div class="text-sm muted">No SNR data.</div>
      {% endif %}
    </div>

    <!-- Top services (unchanged) -->
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
        {% set maxf = freq_bins | map(attribute='count') | max %}
        <div class="flex gap-[2px] items-end h-40">
          {% for b in freq_bins %}
            {% set h = (b.count / maxf * 100) if maxf > 0 else 0 %}
            <div class="flex flex-col items-center" title="{{ '%.3f' % (b.mhz_start) }}–{{ '%.3f' % (b.mhz_end) }} MHz: {{ b.count }}">
              <div class="w-3 bar rounded-t" style="height: {{ '%.1f' % h }}%"></div>
            </div>
          {% endfor %}
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

  <section class="grid grid-cols-1 lg:grid-cols-3 gap-4 mt-4">
    <!-- Detections per hour (last 24h) -->
    <div class="card lg:col-span-3">
      <div class="flex items-center justify-between mb-2">
        <h2 class="text-lg font-semibold">Detections over time (last 24h)</h2>
        <div class="text-xs muted">Hour buckets (UTC)</div>
      </div>
      {% if by_hour and by_hour | length > 0 %}
        {% set maxh = by_hour | map(attribute='count') | max %}
        <div class="flex gap-1 items-end h-40">
          {% for h in by_hour %}
            {% set ht = (h.count / maxh * 100) if maxh > 0 else 0 %}
            <div class="flex flex-col items-center" title="{{ h.hour }}: {{ h.count }}">
              <div class="w-4 bar rounded-t" style="height: {{ '%.1f' % ht }}%"></div>
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
  <section class="card">
    <div class="flex items-end gap-3 flex-wrap"><div class="grow"><h2 class="text-lg font-semibold">Detections</h2><div class="text-xs muted">{{ total }} total</div></div><a class="btn" href="/export/detections.csv?{{ qs }}">Export CSV</a></div>
    <form class="mt-4 grid grid-cols-2 md:grid-cols-6 gap-3" method="get" action="/detections">
      <select name="service" class="input">
        <option value="">Service: any</option>
        {% for s in services %}
          <option value="{{ s }}" {% if req_args.get('service')==s %}selected{% endif %}>{{ s }}</option>
        {% endfor %}
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
          <tr class="hover:bg-white/5"><td class="td">{{ r.time_utc }}</td><td class="td">{{ r.scan_id }}</td><td class="td">{{ (r.f_center_hz/1e6)|round(6) }}</td><td class="td">{{ (r.f_low_hz/1e6)|round(6) }}–{{ (r.f_high_hz/1e6)|round(6) }}</td><td class="td">{{ '%.1f' % r.peak_db if r.peak_db is not none else '' }}</td><td class="td">{{ '%.1f' % r.noise_db if r.noise_db is not none else '' }}</td><td class="td">{{ '%.1f' % r.snr_db if r.snr_db is not none else '' }}</td><td class="td"><span class="chip">{{ r.service or 'Unknown' }}</span></td><td class="td">{{ r.region or '' }}</td><td class="td truncate max-w-[24ch]" title="{{ r.notes or '' }}">{{ r.notes or '' }}</td></tr>
        {% else %}
          <tr><td class="td" colspan="10">No detections found.</td></tr>
        {% endfor %}
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
{% elif page == 'scans' %}
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
{% elif page == 'baseline' %}
  <section class="card">
    <div class="flex items-end gap-3 flex-wrap"><div class="grow"><h2 class="text-lg font-semibold">Baseline (EMA around frequency)</h2><div class="text-xs muted">Peek at ema_occ & ema_power_db near a center frequency.</div></div></div>
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
{% endif %}
</main>
</body>
</html>
"""

# --- DB helpers ---

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

# --- helpers for stats/graphs (no external libs) ---

def _percentile(xs: List[float], p: float) -> Optional[float]:
    if not xs:
        return None
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return float(xs[int(k)])
    return float(xs[f] + (xs[c] - xs[f]) * (k - f))

def snr_histogram(con: sqlite3.Connection, bucket_db: int = 3):
    rows = qa(con, "SELECT snr_db FROM detections WHERE snr_db IS NOT NULL")
    vals: List[float] = []
    for r in rows:
        try:
            vals.append(float(r['snr_db']))
        except Exception:
            pass
    # histogram
    buckets: Dict[int, int] = {}
    for s in vals:
        b = int(math.floor(s / bucket_db)) * bucket_db
        buckets[b] = buckets.get(b, 0) + 1
    # build for template
    labels_sorted = sorted(buckets.keys())
    hist = [{"label": f"{b}–{b+bucket_db}", "count": buckets[b]} for b in labels_sorted]
    stats = None
    if vals:
        stats = {
            "count": len(vals),
            "p50": _percentile(vals, 0.50) or 0.0,
            "p90": _percentile(vals, 0.90) or 0.0,
            "p100": max(vals),
        }
    return hist, stats

def detections_by_hour(con: sqlite3.Connection, hours: int = 24):
    # last N hours including current hour
    rows = qa(con, """
        SELECT strftime('%Y-%m-%d %H:00:00', time_utc) AS hour, COUNT(*) AS c
        FROM detections
        WHERE time_utc >= datetime('now', ?)
        GROUP BY hour
        ORDER BY hour
    """, (f"-{hours-1} hours",))
    # Normalize to include empty hours
    # Get list of hours from now-(hours-1) .. now
    from datetime import datetime, timedelta, timezone
    # use UTC because DB is UTC
    now = datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    timeline = [(now - timedelta(hours=i)).strftime("%Y-%m-%d %H:00:00") for i in reversed(range(hours))]
    lookup = {r['hour']: r['c'] for r in rows if r['hour'] is not None}
    out = [{"hour": h, "count": int(lookup.get(h, 0))} for h in timeline]
    return out

def frequency_bins_latest_scan(con: sqlite3.Connection, num_bins: int = 40):
    latest = q1(con, "SELECT id, t_start_utc, t_end_utc, f_start_hz, f_stop_hz FROM scans ORDER BY COALESCE(t_end_utc,t_start_utc) DESC LIMIT 1")
    if not latest:
        return [], None
    f0 = float(latest['f_start_hz']); f1 = float(latest['f_stop_hz'])
    if f1 <= f0:
        return [], latest
    # Pull detections for that scan only (tightest/most relevant snapshot)
    dets = qa(con, "SELECT f_center_hz FROM detections WHERE scan_id = ?", (latest['id'],))
    if not dets:
        return [], latest
    width = (f1 - f0) / num_bins
    bins = [{"count":0, "mhz_start": (f0 + i*width)/1e6, "mhz_end": (f0 + (i+1)*width)/1e6} for i in range(num_bins)]
    for r in dets:
        try:
            fc = float(r['f_center_hz'])
        except Exception:
            continue
        if fc < f0 or fc >= f1:
            continue
        idx = int((fc - f0) // width)
        idx = max(0, min(num_bins-1, idx))
        bins[idx]["count"] += 1
    return bins, latest

def strongest_signals(con: sqlite3.Connection, limit: int = 10):
    return qa(con, """
        SELECT f_center_hz, snr_db, service
        FROM detections
        WHERE snr_db IS NOT NULL
        ORDER BY snr_db DESC
        LIMIT ?
    """, (limit,))

# --- Flask ---

def create_app(db_path: str) -> Flask:
    app = Flask(__name__)
    app._con = open_db_ro(db_path)

    def con():
        return app._con

    @app.route('/')
    def dashboard():
        scans_total = q1(con(), "SELECT COUNT(*) AS c FROM scans")['c'] if q1(con(), "SELECT COUNT(*) AS c FROM scans") else 0
        detections_total = q1(con(), "SELECT COUNT(*) AS c FROM detections")['c'] if q1(con(), "SELECT COUNT(*) AS c FROM detections") else 0
        baseline_total = q1(con(), "SELECT COUNT(*) AS c FROM baseline")['c'] if q1(con(), "SELECT COUNT(*) AS c FROM baseline") else 0

        # SNR hist + stats (bucket size 3 dB like before; tweakable)
        snr_bucket_db = 3
        snr_hist, snr_stats = snr_histogram(con(), bucket_db=snr_bucket_db)

        # Frequency distribution from latest scan
        freq_bins, latest = frequency_bins_latest_scan(con(), num_bins=40)

        # Detections per hour (last 24h)
        by_hour = detections_by_hour(con(), hours=24)

        # Top services (unchanged)
        top_services = qa(con(), "SELECT COALESCE(service,'Unknown') AS service, COUNT(*) AS count FROM detections GROUP BY COALESCE(service,'Unknown') ORDER BY count DESC LIMIT 10")

        # Strongest signals
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
            by_hour=by_hour,
            top_services=top_services,
            strongest=strongest,
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
        # service list
        sv = [r['service'] for r in qa(con(), "SELECT DISTINCT COALESCE(service,'Unknown') AS service FROM detections ORDER BY service")]
        qs = args.to_dict(flat=True)
        qs = "&".join([f"{k}={v}" for k,v in qs.items()])
        return render_template_string(HTML, page='detections', rows=rows, page_num=page, page_size=page_size, total=total, services=sv, req_args=args, qs=qs)

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
        return render_template_string(HTML, page='scans', rows=rows, page_num=page, page_size=page_size, total=total, req_args=args)

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
        return render_template_string(HTML, page='baseline', rows=rows, req_args=args)

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

    return app

def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument('--db', required=True)
    ap.add_argument('--host', default='0.0.0.0')
    ap.add_argument('--port', type=int, default=8080)
    return ap.parse_args()

if __name__ == '__main__':
    args = parse_args()
    app = create_app(args.db)
    app.run(host=args.host, port=args.port)
