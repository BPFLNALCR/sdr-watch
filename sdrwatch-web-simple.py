#!/usr/bin/env python3
"""
SDRwatch Web (controller-integrated) — Refactored to Flask templates

This version proxies start/stop/logs to the local sdrwatch-control.py HTTP API
and renders HTML from /templates/*.html to keep this file lean.

Run (example):
  # terminal 1 (controller)
  python sdrwatch-control.py serve --host 127.0.0.1 --port 8765 --token secret123

  # terminal 2 (web)
  SDRWATCH_CONTROL_URL=http://127.0.0.1:8765 \
  SDRWATCH_CONTROL_TOKEN=secret123 \
  python3 sdrwatch-web-simple.py --db sdrwatch.db --host 0.0.0.0 --port 8080

Auth notes:
- SDRWATCH_TOKEN       (optional) protects the web app's /api/* endpoints.
- SDRWATCH_CONTROL_TOKEN is used by the server to talk to the controller.
  If not set, we fall back to SDRWATCH_TOKEN as a convenience.
"""
from __future__ import annotations
import argparse, os, io, sqlite3, math, json
from typing import Any, Dict, List, Optional, Tuple
from flask import Flask, request, Response, render_template, render_template_string, jsonify, abort  # type: ignore
from urllib import request as urlreq, parse as urlparse, error as urlerr

# ================================
# Config
# ================================
CHART_HEIGHT_PX = 160
API_TOKEN = os.getenv("SDRWATCH_TOKEN", "")  # page auth (optional)
CONTROL_URL = os.getenv("SDRWATCH_CONTROL_URL", "http://127.0.0.1:8765")
CONTROL_TOKEN = os.getenv("SDRWATCH_CONTROL_TOKEN", "") or os.getenv("SDRWATCH_TOKEN", "")

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
# Graph helpers
# ================================

def _percentile(xs: List[float], p: float) -> Optional[float]:
    if not xs: return None
    xs = sorted(xs); k = (len(xs) - 1) * p; f = int(math.floor(k)); c = int(math.ceil(k))
    if f == c: return float(xs[f])
    return float(xs[f] + (xs[c] - xs[f]) * (k - f))

def _scale_counts_to_px(series: List[Dict[str, Any]], count_key: str = "count") -> float:
    values: List[float] = []
    for x in series:
        try: v = float(x.get(count_key, 0) or 0)
        except Exception: v = 0.0
        values.append(v)
    maxc = max(values) if values else 0.0
    for i, x in enumerate(series):
        c = values[i]
        if maxc <= 0 or c <= 0: x["height_px"] = 0
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
    bounds = q1(con, "SELECT MIN(f_center_hz) AS fmin, MAX(f_center_hz) AS fmax FROM detections")
    if not bounds or bounds['fmin'] is None or bounds['fmax'] is None:
        return [], 0.0, 0.0, 0.0
    f0 = float(bounds['fmin']); f1 = float(bounds['fmax'])
    if not (f1 > f0):
        return [], 0.0, 0.0, 0.0
    dets = qa(con, "SELECT f_center_hz FROM detections WHERE f_center_hz BETWEEN ? AND ?", (int(f0), int(f1)))
    scans = qa(con, "SELECT f_start_hz, f_stop_hz FROM scans WHERE f_stop_hz > f_start_hz")
    width = (f1 - f0) / max(1, num_bins)
    bins: List[Dict[str, Any]] = [{"count":0.0, "coverage":0, "mhz_start": (f0 + i*width)/1e6, "mhz_end": (f0 + (i+1)*width)/1e6} for i in range(num_bins)]
    for r in dets:
        try: fc = float(r['f_center_hz'])
        except Exception: continue
        if fc < f0 or fc >= f1: continue
        idx = int((fc - f0) // width); idx = max(0, min(num_bins-1, idx)); bins[idx]["count"] += 1.0
    for i in range(num_bins):
        b_start = f0 + i*width; b_end   = f0 + (i+1)*width; cov = 0
        for s in scans:
            try: s0 = float(s['f_start_hz']); s1 = float(s['f_stop_hz'])
            except Exception: continue
            if (s0 < b_end) and (s1 > b_start): cov += 1
        bins[i]["coverage"] = cov
        bins[i]["count"] = bins[i]["count"] / float(cov) if cov>0 else 0.0
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
# Controller HTTP client
# ================================
class ControllerClient:
    def __init__(self, base_url: str, token: str = ""):
        self.base = base_url.rstrip('/')
        self.token = token

    def _req(self, method: str, path: str, params: Dict[str, Any] | None = None, body: Dict[str, Any] | None = None, want_text: bool = False):
        url = self.base + path
        if params:
            q = urlparse.urlencode(params)
            url += ("?" + q)
        data = None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if body is not None:
            data = json.dumps(body).encode('utf-8')
        req = urlreq.Request(url, data=data, headers=headers, method=method.upper())
        try:
            with urlreq.urlopen(req, timeout=10) as resp:
                ct = resp.headers.get('Content-Type','')
                raw = resp.read()
                if want_text or not ct.startswith('application/json'):
                    return raw.decode('utf-8', errors='replace')
                return json.loads(raw.decode('utf-8'))
        except urlerr.HTTPError as e:
            raise RuntimeError(f"controller HTTP {e.code}: {e.read().decode('utf-8', errors='replace')}")
        except Exception as e:
            raise RuntimeError(str(e))

    # API wrappers
    def devices(self):
        return self._req('GET', '/devices')

    def list_jobs(self):
        return self._req('GET', '/jobs')

    def start_job(self, device_key: str, label: str, params: Dict[str, Any]):
        return self._req('POST', '/jobs', body={"device_key": device_key, "label": label, "params": params})

    def job_detail(self, job_id: str):
        return self._req('GET', f'/jobs/{job_id}')

    def stop_job(self, job_id: str):
        return self._req('DELETE', f'/jobs/{job_id}')

    def job_logs(self, job_id: str, tail: Optional[int] = None) -> str:
        params = {"tail": int(tail)} if tail else None
        return self._req('GET', f'/jobs/{job_id}/logs', params=params, want_text=True)

# ================================
# Flask app
# ================================

def create_app(db_path: str) -> Flask:
    app = Flask(__name__, template_folder='templates', static_folder='static')
    app._con = open_db_ro(db_path)
    app._db_path = db_path
    app._ctl = ControllerClient(CONTROL_URL, CONTROL_TOKEN)
    app._current_job = None

    def con(): return app._con

    def require_auth():
        if not API_TOKEN: return
        hdr = request.headers.get("Authorization", "")
        if hdr != f"Bearer {API_TOKEN}":
            abort(401)

    # ---------- Pages ----------
    @app.get('/control')
    def control():
        return render_template("control.html", db_path=app._db_path)

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
        return render_template(
            "dashboard.html",
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
        return render_template(
            "detections.html",
            rows=rows, page_num=page, page_size=page_size, total=total,
            services=sv, req_args=args, qs=qs
        )

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
        return render_template(
            "scans.html",
            rows=rows, page_num=page, page_size=page_size, total=total, req_args=args
        )

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
        return render_template("baseline.html", rows=rows, req_args=args)

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

    # ---------- Controller proxy API ----------
    @app.get('/ctl/devices')
    def ctl_devices():
        try:
            devs = app._ctl.devices()
            return jsonify(devs)
        except Exception as e:
            # Surface the reason to the frontend as a JSON object (not an empty list)
            msg = str(e)
            hint = "unauthorized" if "401" in msg or "unauthorized" in msg.lower() else "unreachable"
            return jsonify({"error": f"controller_{hint}", "detail": msg})

    @app.post('/api/scans')
    def api_start_scan():
        require_auth()
        payload = request.get_json(force=True, silent=False) or {}
        device_key = payload.get('device_key')
        label = payload.get('label') or 'web'
        params = payload.get('params') or {}
        if not device_key:
            abort(400, description='device_key is required')
        # Guard: running?
        if app._current_job:
            try:
                st = app._ctl.job_detail(app._current_job)
                if st and st.get('status') == 'running':
                    abort(409, description='Scan already running')
            except Exception:
                app._current_job = None
        try:
            job = app._ctl.start_job(device_key, label, params)
            app._current_job = job.get('id')
            return jsonify({"ok": True, "status": {"state":"running", "job_id": app._current_job}})
        except Exception as e:
            abort(400, description=str(e))

    @app.delete('/api/scans/active')
    def api_stop_active():
        require_auth()
        if not app._current_job:
            return jsonify({"ok": True})
        try:
            app._ctl.stop_job(app._current_job)
        except Exception:
            pass
        app._current_job = None
        return jsonify({"ok": True})

    @app.get('/api/now')
    def api_now():
        require_auth()
        if not app._current_job:
            return jsonify({"state":"idle"})
        try:
            st = app._ctl.job_detail(app._current_job)
            state = st.get('status', 'finished')
            if state != 'running':
                app._current_job = None
            return jsonify({"state": state, "job_id": st.get('id')})
        except Exception:
            app._current_job = None
            return jsonify({"state":"idle"})

    @app.get('/api/logs')
    def api_logs():
        job_id = app._current_job
        if not job_id:
            return Response("", mimetype='text/plain')
        try:
            txt = app._ctl.job_logs(job_id, tail=500)
            return Response(txt, mimetype='text/plain')
        except Exception:
            return Response("", mimetype='text/plain')

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

