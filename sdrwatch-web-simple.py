#!/usr/bin/env python3
"""
SDRwatch Web (simple, no Jinja macros/blocks)
Schema expected:
  scans(id, t_start_utc, t_end_utc, f_start_hz, f_stop_hz, step_hz, samp_rate, fft, avg, device, driver)
  detections(scan_id, time_utc, f_center_hz, f_low_hz, f_high_hz, peak_db, noise_db, snr_db, service, region, notes)
  baseline(bin_hz, ema_occ, ema_power_db, last_seen_utc, total_obs, hits)
Run:
  python3 sdrwatch_web_simple.py --db sdrwatch_test.db --host 0.0.0.0 --port 8080
"""
from __future__ import annotations
import argparse, os, io, sqlite3
from typing import Any, Dict, List, Optional, Tuple
from flask import Flask, request, Response, render_template_string # type: ignore

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
    <div class="stat"><div class="text-xs uppercase text-slate-400">Scans</div><div class="text-2xl font-semibold">{{ scans_total }}</div></div>
    <div class="stat"><div class="text-xs uppercase text-slate-400">Detections</div><div class="text-2xl font-semibold">{{ detections_total }}</div></div>
    <div class="stat"><div class="text-xs uppercase text-slate-400">Baseline bins</div><div class="text-2xl font-semibold">{{ baseline_total }}</div></div>
    <div class="stat">
      <div class="text-xs uppercase text-slate-400">Latest scan</div>
      {% if latest %}
      <div class="text-sm">ID {{ latest.id }}<br/>{{ latest.t_start_utc }} → {{ latest.t_end_utc or '…' }}<br/>{{ (latest.f_start_hz/1e6)|round(3) }}–{{ (latest.f_stop_hz/1e6)|round(3) }} MHz</div>
      {% else %}<div class="text-sm">No scans</div>{% endif %}
    </div>
  </section>
  <section class="grid grid-cols-1 lg:grid-cols-3 gap-4 mt-6">
    <div class="card lg:col-span-2">
      <div class="flex items-center justify-between mb-2"><h2 class="text-lg font-semibold">SNR histogram (3 dB)</h2><a href="/detections" class="underline">View detections »</a></div>
      {% if hist %}
      {% set maxc = hist | map(attribute='count') | max %}
      <div class="flex gap-1 items-end h-40">
        {% for b in hist %}
          {% set h = (b.count / maxc * 100) if maxc > 0 else 0 %}
          <div class="flex flex-col items-center" title="{{ b.count }}">
            <div class="w-6 bg-sky-500 rounded-t" style="height: {{ '%.1f' % h }}%"></div>
            <div class="text-[10px] text-slate-400 rotate-45 origin-top-left -mt-1">{{ b.bucket }}</div>
          </div>
        {% endfor %}
      </div>
      {% else %}<div class="text-sm text-slate-400">No SNR data.</div>{% endif %}
    </div>
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
{% elif page == 'detections' %}
  <section class="card">
    <div class="flex items-end gap-3 flex-wrap"><div class="grow"><h2 class="text-lg font-semibold">Detections</h2><div class="text-xs text-slate-400">{{ total }} total</div></div><a class="btn" href="/export/detections.csv?{{ qs }}">Export CSV</a></div>
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
    <div class="flex items-end gap-3 flex-wrap"><div class="grow"><h2 class="text-lg font-semibold">Scans</h2><div class="text-xs text-slate-400">{{ total }} total</div></div></div>
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
    <div class="flex items-end gap-3 flex-wrap"><div class="grow"><h2 class="text-lg font-semibold">Baseline (EMA around frequency)</h2><div class="text-xs text-slate-400">Peek at ema_occ & ema_power_db near a center frequency.</div></div></div>
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
        latest = q1(con(), "SELECT id, t_start_utc, t_end_utc, f_start_hz, f_stop_hz FROM scans ORDER BY COALESCE(t_end_utc,t_start_utc) DESC LIMIT 1")
        # histogram
        rows = qa(con(), "SELECT snr_db FROM detections WHERE snr_db IS NOT NULL")
        buckets: Dict[str,int] = {}
        for r in rows:
            try:
                s = float(r['snr_db'])
            except Exception:
                continue
            floor = int(s // 3) * 3
            key = f"{floor}–{floor+3}"
            buckets[key] = buckets.get(key,0)+1
        hist = [{"bucket":k, "count":buckets[k]} for k in sorted(buckets, key=lambda x:int(x.split('–')[0]))]
        top_services = qa(con(), "SELECT COALESCE(service,'Unknown') AS service, COUNT(*) AS count FROM detections GROUP BY COALESCE(service,'Unknown') ORDER BY count DESC LIMIT 10")
        return render_template_string(HTML, page='dashboard', scans_total=scans_total, detections_total=detections_total, baseline_total=baseline_total, latest=latest, hist=hist, top_services=top_services)

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
        # simple export: no filters beyond existing qs to keep simple; reuse same logic as detections
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
