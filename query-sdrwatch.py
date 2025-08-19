#!/usr/bin/env python3
"""
query_sdrwatch.py — quick CLI to inspect an SDRWatch SQLite database

Updated to match the upgraded sdrwatch schema:
- scans table now uses columns: fft, avg (not fft_size)
- fields kept: t_start_utc, t_end_utc, f_start_hz, f_stop_hz, step_hz, samp_rate, driver, device
- detections: f_low_hz, f_center_hz, f_high_hz, peak_db, noise_db, snr_db, service, region, notes
- baseline: bin_hz, ema_occ, ema_power_db, last_seen_utc, total_obs, hits

No external dependencies. Prints tidy tables to stdout and supports CSV export.

Usage examples:
  # List recent scans
  python3 query_sdrwatch.py scans --limit 10

  # Show detections from latest scan
  python3 query_sdrwatch.py detections --limit 20

  # Show detections with filters
  python3 query_sdrwatch.py detections --min-snr 8 --service ISM --since "2025-08-17T00:00:00Z"

  # Baseline around 95 MHz ±500 kHz
  python3 query_sdrwatch.py baseline --center 95 --span-khz 500

  # Top detections by SNR
  python3 query_sdrwatch.py top --limit 15

  # Summary roll‑up
  python3 query_sdrwatch.py summary

  # Export detections to CSV
  python3 query_sdrwatch.py export --outfile detections.csv --min-snr 6
"""

from __future__ import annotations
import argparse
import csv
import os
import sqlite3
import sys
from typing import Any, Optional, Sequence, Tuple, List

# ----------------------------
# Helpers
# ----------------------------

def err(msg: str) -> None:
    print(f"[error] {msg}", file=sys.stderr)


def open_db(path: str) -> sqlite3.Connection:
    if not os.path.exists(path):
        err(f"Database not found: {path}")
        sys.exit(2)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


def fmt_table(rows, headers=None, max_width=28):
    if not rows:
        return "(no rows)"
    if headers is None:
        headers = list(rows[0].keys())
    cols = list(headers)

    def get_val(r, k):
        try:
            return r[k]
        except Exception:
            return ""

    data = [[str(get_val(r, c)) for c in cols] for r in rows]

    def clip(s: str) -> str:
        s = s if s is not None else ""
        if len(s) > max_width:
            return s[: max_width - 1] + "…"
        return s

    data = [[clip(x) for x in row] for row in data]
    widths = [max(len(str(h)), max(len(str(row[i])) for row in data)) for i, h in enumerate(cols)]
    sep = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    out = [sep]
    out.append("| " + " | ".join(str(h).ljust(widths[i]) for i, h in enumerate(cols)) + " |")
    out.append(sep)
    for row in data:
        out.append("| " + " | ".join(str(row[i]).ljust(widths[i]) for i in range(len(cols))) + " |")
    out.append(sep)
    return "\n".join(out)


def to_hz(mhz: Optional[float]) -> Optional[int]:
    return int(mhz * 1e6) if mhz is not None else None


def between_clause(col: str, lo: Optional[int], hi: Optional[int]) -> Tuple[str, list]:
    if lo is not None and hi is not None:
        return f"{col} BETWEEN ? AND ?", [lo, hi]
    elif lo is not None:
        return f"{col} >= ?", [lo]
    elif hi is not None:
        return f"{col} <= ?", [hi]
    else:
        return "", []

# ----------------------------
# Commands
# ----------------------------

def cmd_scans(con: sqlite3.Connection, args: argparse.Namespace) -> None:
    q = (
        "SELECT id, t_start_utc, t_end_utc, "
        "ROUND(f_start_hz/1e6, 6) AS f_start_MHz, "
        "ROUND(f_stop_hz/1e6, 6) AS f_stop_MHz, "
        "ROUND(step_hz/1e6, 6) AS step_MHz, "
        "samp_rate AS samp_rate_Hz, fft AS fft, avg AS avg, driver, device "
        "FROM scans ORDER BY id DESC LIMIT ?"
    )
    rows = con.execute(q, (args.limit,)).fetchall()
    print(fmt_table(rows))


def _latest_scan_id(con: sqlite3.Connection) -> Optional[int]:
    row = con.execute("SELECT id FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    return int(row[0]) if row else None


def cmd_detections(con: sqlite3.Connection, args: argparse.Namespace) -> None:
    params: List[Any] = []
    where: List[str] = []

    if args.scan_id is None and not args.all_scans:
        sid = _latest_scan_id(con)
        if sid is None:
            print("(no scans)")
            return
        where.append("scan_id = ?")
        params.append(sid)
    elif args.scan_id is not None:
        where.append("scan_id = ?")
        params.append(args.scan_id)

    if args.min_snr is not None:
        where.append("snr_db >= ?")
        params.append(args.min_snr)

    if args.service:
        where.append("service LIKE ?")
        params.append(f"%{args.service}%")

    if args.region:
        where.append("region LIKE ?")
        params.append(f"%{args.region}%")

    if args.since:
        where.append("time_utc >= ?")
        params.append(args.since)

    if args.mhz_min is not None or args.mhz_max is not None:
        clause, binds = between_clause("f_center_hz", to_hz(args.mhz_min), to_hz(args.mhz_max))
        if clause:
            where.append(clause)
            params.extend(binds)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    q = (
        "SELECT time_utc, scan_id, "
        "ROUND(f_low_hz/1e6,6) AS f_low_MHz, "
        "ROUND(f_center_hz/1e6,6) AS f_center_MHz, "
        "ROUND(f_high_hz/1e6,6) AS f_high_MHz, "
        "ROUND(peak_db,1) AS peak_dB, ROUND(noise_db,1) AS noise_dB, ROUND(snr_db,1) AS SNR_dB, "
        "COALESCE(NULLIF(service,''),'—') AS service, COALESCE(NULLIF(region,''),'') AS region, "
        "COALESCE(NULLIF(notes,''),'') AS notes "
        f"FROM detections{where_sql} "
        "ORDER BY time_utc DESC LIMIT ?"
    )
    params2 = params + [args.limit]
    rows = con.execute(q, params2).fetchall()

    if args.csv:
        writer = csv.writer(sys.stdout)
        writer.writerow(rows[0].keys() if rows else [])
        for r in rows:
            writer.writerow([r[k] for k in r.keys()])
    else:
        print(fmt_table(rows))


def cmd_baseline(con: sqlite3.Connection, args: argparse.Namespace) -> None:
    params: List[Any] = []
    where: List[str] = []

    lo_hz: Optional[int] = None
    hi_hz: Optional[int] = None

    if args.center is not None:
        span_hz = int((args.span_khz or 100) * 1e3)
        c_hz = to_hz(args.center)
        if c_hz is not None:
            lo_hz = c_hz - span_hz
            hi_hz = c_hz + span_hz
        else:
            lo_hz = None
            hi_hz = None
    else:
        lo_hz = to_hz(args.mhz_min) if args.mhz_min is not None else None
        hi_hz = to_hz(args.mhz_max) if args.mhz_max is not None else None

    clause, binds = between_clause("bin_hz", lo_hz, hi_hz)
    if clause:
        where.append(clause)
        params.extend(binds)

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    q = (
        "SELECT ROUND(bin_hz/1e6,6) AS MHz, "
        "ROUND(ema_occ,3) AS occ, ROUND(ema_power_db,1) AS power_dB, last_seen_utc, total_obs, hits "
        f"FROM baseline{where_sql} ORDER BY bin_hz LIMIT ?"
    )
    params.append(args.limit)
    rows = con.execute(q, params).fetchall()
    print(fmt_table(rows))


def cmd_top(con: sqlite3.Connection, args: argparse.Namespace) -> None:
    q = (
        "SELECT ROUND(f_center_hz/1e6,6) AS MHz, ROUND(snr_db,1) AS SNR_dB, time_utc, "
        "COALESCE(NULLIF(service,''),'—') AS service, COALESCE(NULLIF(region,''),'') AS region "
        "FROM detections ORDER BY snr_db DESC LIMIT ?"
    )
    rows = con.execute(q, (args.limit,)).fetchall()
    print(fmt_table(rows))


def cmd_summary(con: sqlite3.Connection, args: argparse.Namespace) -> None:
    total_scans = con.execute("SELECT COUNT(*) FROM scans").fetchone()[0]
    total_det = con.execute("SELECT COUNT(*) FROM detections").fetchone()[0]
    total_bins = con.execute("SELECT COUNT(*) FROM baseline").fetchone()[0]

    latest = con.execute(
        "SELECT id, t_start_utc, t_end_utc, ROUND(f_start_hz/1e6,3), ROUND(f_stop_hz/1e6,3), fft, avg, samp_rate "
        "FROM scans ORDER BY id DESC LIMIT 1"
    ).fetchone()

    by_service = con.execute(
        "SELECT COALESCE(NULLIF(service,''),'(unknown)') AS service, COUNT(*) AS count "
        "FROM detections GROUP BY COALESCE(NULLIF(service,''),'(unknown)') "
        "ORDER BY count DESC LIMIT 10"
    ).fetchall()

    print("== Overview ==")
    print(f"scans: {total_scans}  detections: {total_det}  baseline bins: {total_bins}")
    if latest:
        print(
            f"latest scan id={latest[0]}  {latest[1]} → {latest[2]}  range={latest[3]}–{latest[4]} MHz  "
            f"fft={latest[5]} avg={latest[6]} samp_rate={latest[7]} Hz"
        )
    print()
    print("== Top services ==")
    print(fmt_table(by_service, headers=["service", "count"]))

    snr_hist = con.execute(
        "SELECT CAST((snr_db/3) AS INT)*3 AS snr_dB_bucket, COUNT(*) AS count FROM detections GROUP BY snr_dB_bucket ORDER BY snr_dB_bucket"
    ).fetchall()
    if snr_hist:
        print()
        print("== SNR histogram (3 dB buckets) ==")
        print(fmt_table(snr_hist, headers=["snr_dB_bucket", "count"]))


def cmd_export(con: sqlite3.Connection, args: argparse.Namespace) -> None:
    # Reuse detection filters and dump to CSV file
    ns = argparse.Namespace(
        scan_id=args.scan_id,
        all_scans=args.all_scans,
        min_snr=args.min_snr,
        service=args.service,
        region=args.region,
        since=args.since,
        mhz_min=args.mhz_min,
        mhz_max=args.mhz_max,
        limit=args.limit,
        csv=True,
    )
    with open(args.outfile, "w", newline="", encoding="utf-8") as f:
        old = sys.stdout
        sys.stdout = f
        try:
            cmd_detections(con, ns)
        finally:
            sys.stdout = old
    print(f"wrote {args.outfile}")

# ----------------------------
# Main
# ----------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Inspect an SDRWatch SQLite database")
    p.add_argument("--db", default="sdrwatch.db", help="Path to sdrwatch.db")

    sub = p.add_subparsers(dest="cmd", required=True)

    p_scans = sub.add_parser("scans", help="List scans")
    p_scans.add_argument("--limit", type=int, default=10)
    p_scans.set_defaults(func=cmd_scans)

    p_det = sub.add_parser("detections", help="List detections (defaults to latest scan)")
    p_det.add_argument("--scan-id", type=int)
    p_det.add_argument("--all-scans", action="store_true", help="Do not restrict to latest scan")
    p_det.add_argument("--min-snr", type=float)
    p_det.add_argument("--service", type=str)
    p_det.add_argument("--region", type=str)
    p_det.add_argument("--since", type=str, help="ISO-8601 lower bound, e.g., 2025-08-17T00:00:00Z")
    p_det.add_argument("--mhz-min", type=float)
    p_det.add_argument("--mhz-max", type=float)
    p_det.add_argument("--limit", type=int, default=20)
    p_det.add_argument("--csv", action="store_true", help="Output CSV to stdout")
    p_det.set_defaults(func=cmd_detections)

    p_base = sub.add_parser("baseline", help="Show baseline bins in a frequency window")
    g = p_base.add_mutually_exclusive_group(required=False)
    g.add_argument("--center", type=float, help="Center frequency in MHz")
    p_base.add_argument("--span-khz", type=float, default=500.0, help="Half-span around center in kHz")
    g.add_argument("--mhz-min", type=float, help="Lower bound in MHz")
    p_base.add_argument("--mhz-max", type=float, help="Upper bound in MHz")
    p_base.add_argument("--limit", type=int, default=200)
    p_base.set_defaults(func=cmd_baseline)

    p_top = sub.add_parser("top", help="Top detections by SNR")
    p_top.add_argument("--limit", type=int, default=10)
    p_top.set_defaults(func=cmd_top)

    p_sum = sub.add_parser("summary", help="Database summary")
    p_sum.set_defaults(func=cmd_summary)

    p_exp = sub.add_parser("export", help="Export detections to CSV (respects detection filters)")
    p_exp.add_argument("--outfile", required=True)
    p_exp.add_argument("--scan-id", type=int)
    p_exp.add_argument("--all-scans", action="store_true")
    p_exp.add_argument("--min-snr", type=float)
    p_exp.add_argument("--service", type=str)
    p_exp.add_argument("--region", type=str)
    p_exp.add_argument("--since", type=str)
    p_exp.add_argument("--mhz-min", type=float)
    p_exp.add_argument("--mhz-max", type=float)
    p_exp.add_argument("--limit", type=int, default=100000)
    p_exp.set_defaults(func=cmd_export)

    return p


def main(argv: Optional[Sequence[str]] = None) -> None:
    p = build_parser()
    args = p.parse_args(argv)
    con = open_db(args.db)
    try:
        args.func(con, args)
    finally:
        con.close()


if __name__ == "__main__":
    main()
