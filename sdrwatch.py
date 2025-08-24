#!/usr/bin/env python3
"""
SDRWatch — wideband scanner, baseline builder, and bandplan mapper

Goals
-----
- Sweep a frequency range with an SDR (SoapySDR backend by default; optional native RTL-SDR backend).
- Estimate noise floor robustly and detect signals via energy thresholding (CFAR‑like).
- Build a baseline (per‑bin occupancy over time) and flag "new" signals relative to that baseline.
- Map detections to a bandplan (FCC/CEPT/etc.) from a CSV file or built‑in minimal defaults.
- Log everything to SQLite and optionally emit desktop notifications or webhook JSON lines.

Hardware
--------
Any SoapySDR‑supported device (RTL‑SDR, HackRF, Airspy, SDRplay, LimeSDR, USRP...).
Alternatively, a native RTL-SDR path via pyrtlsdr (librtlsdr) is available with --driver rtlsdr_native.

Install (Debian)
----------------
# Core deps
sudo apt update && sudo apt install -y python3-numpy python3-scipy python3-soapysdr libsoapysdr0.8 libsoapysdr-dev
# Optional: native rtl-sdr path
sudo apt install -y librtlsdr0 librtlsdr-dev && pip3 install pyrtlsdr

DB schema (SQLite)
------------------
- scans(id INTEGER PK, t_start_utc TEXT, t_end_utc TEXT, f_start_hz INT, f_stop_hz INT, step_hz INT, samp_rate INT, fft INT, avg INT,
        device TEXT, driver TEXT)
- detections(scan_id INT, time_utc TEXT, f_center_hz INT, f_low_hz INT, f_high_hz INT, peak_db REAL, noise_db REAL, snr_db REAL,
             service TEXT, region TEXT, notes TEXT)
- baseline(bin_hz INT PK, ema_occ REAL, ema_power_db REAL, last_seen_utc TEXT, total_obs INT, hits INT)

License
-------
MIT (this file)
"""

import argparse
import csv
import math
import os
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, Iterable, List, Optional, Tuple

import numpy as np  # type: ignore

# SciPy is optional. If missing, we fall back to a simple periodogram average.
try:
    from scipy.signal import welch  # type: ignore

    HAVE_SCIPY = True
except Exception:
    HAVE_SCIPY = False

# SoapySDR is optional (used for multi-device support).
try:
    import SoapySDR  # type: ignore
    from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX  # type: ignore

    HAVE_SOAPY = True
except Exception:
    HAVE_SOAPY = False

# pyrtlsdr (native RTL-SDR path) is optional
try:
    from rtlsdr import RtlSdr  # type: ignore

    HAVE_RTLSDR = True
except Exception:
    HAVE_RTLSDR = False

# ------------------------------
# Utility
# ------------------------------

def utc_now_str() -> str:
    return datetime.now(timezone.utc).isoformat()


def db10(x: np.ndarray) -> np.ndarray:
    # avoid log(0)
    return 10.0 * np.log10(np.maximum(x, 1e-20))


def robust_noise_floor_db(psd_db: np.ndarray) -> float:
    """Robust noise floor estimate using median + 1.4826*MAD (approx std for Gaussian).
    """
    med = np.median(psd_db)
    mad = np.median(np.abs(psd_db - med))
    return float(med + 1.4826 * mad)


@dataclass
class Segment:
    f_low_hz: int
    f_high_hz: int
    f_center_hz: int
    peak_db: float
    noise_db: float
    snr_db: float


# ------------------------------
# Bandplan CSV lookup (minimal)
# ------------------------------

@dataclass
class Band:
    low_hz: int
    high_hz: int
    service: str
    region: str
    notes: str


class Bandplan:
    def __init__(self, csv_path: Optional[str] = None):
        self.bands: List[Band] = []
        if csv_path and os.path.exists(csv_path):
            self._load_csv(csv_path)
        else:
            # Minimal defaults. Extend with official CSVs.
            self.bands = [
                Band(433_050_000, 434_790_000, "ISM/SRD", "ITU-R1 (EU)", "Short-range devices"),
                Band(902_000_000, 928_000_000, "ISM", "US (FCC)", "902-928 MHz ISM"),
                Band(2_400_000_000, 2_483_500_000, "ISM", "Global", "2.4 GHz ISM"),
                Band(1_420_000_000, 1_427_000_000, "Radio Astronomy", "Global", "Hydrogen line"),
                Band(88_000_000, 108_000_000, "FM Broadcast", "Global", "88-108 MHz Radio"),
            ]

    def _load_csv(self, path: str):
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    # Prefer old headers, but accept fallback names if present
                    low = row.get("low_hz") or row.get("f_low_hz")
                    high = row.get("high_hz") or row.get("f_high_hz")
                    if low is None or high is None:
                        continue  # skip rows with missing frequency bounds
                    self.bands.append(
                        Band(
                            int(float(low)),
                            int(float(high)),
                            row.get("service", "").strip(),
                            row.get("region", "").strip(),
                            row.get("notes", "").strip(),
                        )
                    )
                except Exception:
                    continue

    def lookup(self, f_hz: int) -> Tuple[str, str, str]:
        for b in self.bands:
            if b.low_hz <= f_hz <= b.high_hz:
                return b.service, b.region, b.notes
        return "", "", ""

# ------------------------------
# SQLite store
# ------------------------------

class Store:
    def __init__(self, path: str):
        self.con = sqlite3.connect(path)
        self._init()

    def _init(self):
        cur = self.con.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS scans (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              t_start_utc TEXT,
              t_end_utc   TEXT,
              f_start_hz  INTEGER,
              f_stop_hz   INTEGER,
              step_hz     INTEGER,
              samp_rate   INTEGER,
              fft         INTEGER,
              avg         INTEGER,
              device      TEXT,
              driver      TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS detections (
              scan_id     INTEGER,
              time_utc    TEXT,
              f_center_hz INTEGER,
              f_low_hz    INTEGER,
              f_high_hz   INTEGER,
              peak_db     REAL,
              noise_db    REAL,
              snr_db      REAL,
              service     TEXT,
              region      TEXT,
              notes       TEXT
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS baseline (
              bin_hz      INTEGER PRIMARY KEY,
              ema_occ     REAL,
              ema_power_db REAL,
              last_seen_utc TEXT,
              total_obs   INTEGER,
              hits        INTEGER
            )
            """
        )
        self.con.commit()

    # Transaction helpers
    def begin(self):
        self.con.execute("BEGIN")

    def commit(self):
        self.con.commit()

    def start_scan(self, meta: dict) -> int:
        cur = self.con.cursor()
        cur.execute(
            """
            INSERT INTO scans(t_start_utc, t_end_utc, f_start_hz, f_stop_hz, step_hz, samp_rate, fft, avg, device, driver)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                meta.get("t_start_utc"),
                meta.get("t_end_utc"),
                meta.get("f_start_hz"),
                meta.get("f_stop_hz"),
                meta.get("step_hz"),
                meta.get("samp_rate"),
                meta.get("fft"),
                meta.get("avg"),
                meta.get("device"),
                meta.get("driver"),
            ),
        )
        self.con.commit()
        if cur.lastrowid is None:
            raise RuntimeError("Failed to retrieve lastrowid from scan insert")
        return int(cur.lastrowid)

    def end_scan(self, scan_id: int, t_end_utc: str):
        self.con.execute("UPDATE scans SET t_end_utc = ? WHERE id = ?", (t_end_utc, scan_id))
        self.con.commit()

    def add_detection(self, scan_id: int, seg: Segment, service: str, region: str, notes: str):
        self.con.execute(
            """
            INSERT INTO detections(scan_id, time_utc, f_center_hz, f_low_hz, f_high_hz, peak_db, noise_db, snr_db, service, region, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_id,
                utc_now_str(),
                seg.f_center_hz,
                seg.f_low_hz,
                seg.f_high_hz,
                seg.peak_db,
                seg.noise_db,
                seg.snr_db,
                service,
                region,
                notes,
            ),
        )

    def update_baseline(self, freqs_hz: np.ndarray, psd_db: np.ndarray, occupied_mask: np.ndarray, ema_alpha: float = 0.05):
        cur = self.con.cursor()
        tnow = utc_now_str()
        for f, p, occ in zip(freqs_hz.astype(int), psd_db.astype(float), occupied_mask.astype(int)):
            cur.execute("SELECT ema_occ, ema_power_db, last_seen_utc, total_obs, hits FROM baseline WHERE bin_hz = ?", (int(f),))
            row = cur.fetchone()
            if row is None:
                ema_occ = occ
                ema_pow = p
                tot = 1
                hits = occ
            else:
                ema_occ_prev, ema_pow_prev, _, total_obs, hits_prev = row
                ema_occ = (1 - ema_alpha) * (ema_occ_prev if ema_occ_prev is not None else 0.0) + ema_alpha * occ
                ema_pow = (1 - ema_alpha) * (ema_pow_prev if ema_pow_prev is not None else p) + ema_alpha * p
                tot = (total_obs or 0) + 1
                hits = (hits_prev or 0) + occ
            cur.execute(
                """
                INSERT INTO baseline(bin_hz, ema_occ, ema_power_db, last_seen_utc, total_obs, hits)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(bin_hz) DO UPDATE SET
                    ema_occ=excluded.ema_occ,
                    ema_power_db=excluded.ema_power_db,
                    last_seen_utc=excluded.last_seen_utc,
                    total_obs=excluded.total_obs,
                    hits=excluded.hits
                """,
                (int(f), float(ema_occ), float(ema_pow), tnow, int(tot), int(hits)),
            )

    def baseline_occ(self, f_center_hz: int) -> Optional[float]:
        cur = self.con.cursor()
        cur.execute("SELECT ema_occ FROM baseline WHERE bin_hz = ?", (int(f_center_hz),))
        row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None


# ------------------------------
# SDR sources
# ------------------------------

class SDRSource:
    """SoapySDR source (generic)."""

    def __init__(self, driver: str, samp_rate: float, gain: str | float, soapy_args: Optional[Dict[str, str]] = None):
        if not HAVE_SOAPY:
            raise RuntimeError("SoapySDR not available")
        dev_args: Dict[str, str] = {"driver": driver}
        if soapy_args:
            # Merge caller-provided selection hints (e.g., serial=..., index=...)
            dev_args.update({str(k): str(v) for k, v in soapy_args.items()})
        self.dev = SoapySDR.Device(dev_args)
        self.dev.setSampleRate(SOAPY_SDR_RX, 0, samp_rate)
        if isinstance(gain, str) and gain == "auto":
            try:
                self.dev.setGainMode(SOAPY_SDR_RX, 0, True)
            except Exception:
                pass
        else:
            self.dev.setGain(SOAPY_SDR_RX, 0, float(gain))
        self.stream = self.dev.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
        self.dev.activateStream(self.stream)

    def tune(self, center_hz: float):
        self.dev.setFrequency(SOAPY_SDR_RX, 0, center_hz)

    def read(self, count: int) -> np.ndarray:
        buffs = []
        got = 0
        while got < count:
            sr = int(min(8192, count - got))
            buff = np.empty(sr, dtype=np.complex64)
            st = self.dev.readStream(self.stream, [buff], sr)
            n = st.ret if hasattr(st, "ret") else st
            if n > 0:
                buffs.append(buff[:n])
                got += n
            else:
                time.sleep(0.001)
        if not buffs:
            return np.zeros(count, dtype=np.complex64)
        return np.concatenate(buffs)

    def close(self):
        try:
            self.dev.deactivateStream(self.stream)
            self.dev.closeStream(self.stream)
        except Exception:
            pass


class RTLSDRSource:
    """Native librtlsdr via pyrtlsdr."""

    def __init__(self, samp_rate: float, gain: str | float):
        if not HAVE_RTLSDR:
            raise RuntimeError("pyrtlsdr not available")
        self.dev = RtlSdr()
        self.dev.sample_rate = samp_rate
        if isinstance(gain, str) and gain == "auto":
            self.dev.gain = "auto"
        else:
            self.dev.gain = float(gain)

    def tune(self, center_hz: float):
        self.dev.center_freq = center_hz

    def read(self, count: int) -> np.ndarray:
        # pyrtlsdr returns np.complex64
        return self.dev.read_samples(count)

    def close(self):
        try:
            self.dev.close()
        except Exception:
            pass


# ------------------------------
# CFAR helpers
# ------------------------------

def _sliding_window_view(x: np.ndarray, window: int) -> np.ndarray:
    """Return a sliding window view over the last axis. Fallback if not available."""
    try:
        return np.lib.stride_tricks.sliding_window_view(x, window)
    except Exception:
        # Minimal fallback for 1D arrays
        x = np.asarray(x)
        if x.ndim != 1:
            raise ValueError("sliding window fallback only supports 1D arrays")
        shape = (x.size - window + 1, window)
        strides = (x.strides[0], x.strides[0])
        return np.lib.stride_tricks.as_strided(x, shape=shape, strides=strides)


def cfar_os_mask(psd_db: np.ndarray, train: int, guard: int, quantile: float, alpha_db: float) -> Tuple[np.ndarray, np.ndarray]:
    """Order-Statistic CFAR (OS-CFAR) on a 1D PSD in dB.
    Returns (mask_above, noise_est_db_per_bin). The threshold is noise_est + alpha_db, applied in **linear** power domain.
    """
    psd_db = np.asarray(psd_db).astype(np.float64)
    N = psd_db.size
    if N == 0:
        return np.zeros(0, dtype=bool), np.zeros(0, dtype=np.float64)
    # Convert to linear power
    psd_lin = np.power(10.0, psd_db / 10.0)
    # Build sliding windows with padding so we have a window for each bin
    win = 2 * train + 2 * guard + 1
    if win <= 1:
        # Degenerate: no training cells; fall back to global median as noise
        noise_db = np.full(N, float(np.median(psd_db)))
        above = psd_db > (noise_db + alpha_db)
        return above, noise_db
    pad = train + guard
    padded = np.pad(psd_lin, (pad, pad), mode="edge")
    windows = _sliding_window_view(padded, win)  # shape (N, win)
    # Exclude guard + CUT region by masking them out
    mask = np.ones(win, dtype=bool)
    mask[train : train + 2 * guard + 1] = False  # False over guard + CUT
    train_windows = windows[:, mask]  # shape (N, 2*train)
    # Order statistic via quantile over training cells
    q = float(np.clip(quantile, 1e-6, 1.0 - 1e-6))
    noise_lin = np.quantile(train_windows, q, axis=1)
    alpha = np.power(10.0, alpha_db / 10.0)
    threshold_lin = noise_lin * alpha
    above = psd_lin > threshold_lin
    noise_db = 10.0 * np.log10(np.maximum(noise_lin, 1e-20))
    return above, noise_db


# ------------------------------
# Detection & PSD
# ------------------------------

def detect_segments(
    freqs_hz: np.ndarray,
    psd_db: np.ndarray,
    thresh_db: float,
    guard_bins: int = 1,
    min_width_bins: int = 2,
    cfar_mode: str = "off",
    cfar_train: int = 24,
    cfar_guard: int = 4,
    cfar_quantile: float = 0.75,
    cfar_alpha_db: Optional[float] = None,
) -> Tuple[List[Segment], np.ndarray, np.ndarray]:
    """Detect contiguous energy segments.
    If cfar_mode != 'off', use OS-CFAR to produce the detection mask. Otherwise use a global robust noise floor.
    Returns (segments, above_mask, noise_est_db_per_bin).
    """
    psd_db = np.asarray(psd_db).astype(np.float64)
    freqs_hz = np.asarray(freqs_hz).astype(np.float64)
    N = psd_db.size
    if N == 0:
        return [], np.zeros(0, dtype=bool), np.zeros(0, dtype=np.float64)

    if cfar_mode and cfar_mode.lower() != "off":
        alpha_db = float(cfar_alpha_db if cfar_alpha_db is not None else thresh_db)
        above, noise_local_db = cfar_os_mask(psd_db, cfar_train, cfar_guard, cfar_quantile, alpha_db)
        noise_for_snr_db = noise_local_db
    else:
        # Global robust threshold
        nf = robust_noise_floor_db(psd_db)
        dynamic = nf + float(thresh_db)
        above = psd_db > dynamic
        noise_for_snr_db = np.full(N, nf, dtype=np.float64)

    # Merge small gaps (guard_bins) and form contiguous segments
    segs: List[Segment] = []
    i = 0
    while i < N:
        if above[i]:
            start_i = i
            j = i + 1
            gap = 0
            while j < N and (above[j] or gap < guard_bins):
                if above[j]:
                    gap = 0
                else:
                    gap += 1
                j += 1
            end_i = j  # exclusive
            # Ensure minimum width
            if (end_i - start_i) >= min_width_bins:
                sl = slice(start_i, end_i)
                peak_idx_local = int(np.argmax(psd_db[sl]))
                peak_idx = start_i + peak_idx_local
                peak_db = float(psd_db[peak_idx])
                # Representative noise for SNR = local noise at the peak bin
                noise_db = float(noise_for_snr_db[peak_idx])
                snr_db = float(peak_db - noise_db)
                # freq bounds (use bin edges assuming uniform spacing)
                f_low = float(freqs_hz[start_i])
                f_high = float(freqs_hz[end_i - 1])
                f_center = float(freqs_hz[(start_i + end_i) // 2])
                segs.append(
                    Segment(
                        f_low_hz=int(round(f_low)),
                        f_high_hz=int(round(f_high)),
                        f_center_hz=int(round(f_center)),
                        peak_db=peak_db,
                        noise_db=noise_db,
                        snr_db=snr_db,
                    )
                )
            i = j
        else:
            i += 1

    return segs, above, noise_for_snr_db


def compute_psd_db(samples: np.ndarray, samp_rate: float, fft_size: int, avg: int) -> Tuple[np.ndarray, np.ndarray]:
    if HAVE_SCIPY:
        # Welch PSD over 'avg' segments
        nperseg = fft_size
        noverlap = 0
        freqs, psd = welch(samples, fs=samp_rate, nperseg=nperseg, noverlap=noverlap, return_onesided=False, scaling="density")
        # Welch returns frequencies in ascending order; we want baseband centered
        # Convert to centered, consistent with fftshift convention
        order = np.argsort(freqs)
        freqs = freqs[order]
        psd = psd[order]
        # shift to baseband (-Fs/2..+Fs/2)
        mid = len(freqs) // 2
        freqs = np.concatenate((freqs[mid:], freqs[:mid]))
        psd = np.concatenate((psd[mid:], psd[:mid]))
    else:
        # Simple averaged periodogram
        seg = fft_size
        windows = []
        for i in range(avg):
            start = i * seg
            x = samples[start : start + seg]
            if len(x) < seg:
                break
            X = np.fft.fftshift(np.fft.fft(x * np.hanning(seg), n=seg))
            Pxx = (np.abs(X) ** 2) / (seg * samp_rate)
            windows.append(Pxx)
        if not windows:
            X = np.fft.fftshift(np.fft.fft(samples[:fft_size] * np.hanning(fft_size), n=fft_size))
            Pxx = (np.abs(X) ** 2) / (fft_size * samp_rate)
            windows = [Pxx]
        psd = np.mean(np.vstack(windows), axis=0)
        freqs = np.linspace(-samp_rate / 2, samp_rate / 2, len(psd), endpoint=False)

    psd_db = db10(psd)
    return freqs, psd_db


# ------------------------------
# Output helpers
# ------------------------------

def maybe_notify(title: str, body: str, enabled: bool):
    if not enabled:
        return
    try:
        import subprocess

        subprocess.Popen(["notify-send", title, body])
    except Exception:
        pass


def maybe_emit_jsonl(path: Optional[str], record: dict):
    if not path:
        return
    try:
        import json
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass


# ------------------------------
# Main sweep logic
# ------------------------------

def _parse_duration_to_seconds(text: Optional[str]) -> Optional[float]:
    """Parse a human-friendly duration string to seconds.
    Supports integers/floats (seconds) or suffixes: s, m, h, d.
    Returns None if text is falsy.
    """
    if not text:
        return None
    try:
        t = str(text).strip().lower()
        mult = 1.0
        if t.endswith("s"):
            t = t[:-1]
        elif t.endswith("m"):
            mult = 60.0
            t = t[:-1]
        elif t.endswith("h"):
            mult = 3600.0
            t = t[:-1]
        elif t.endswith("d"):
            mult = 86400.0
            t = t[:-1]
        return float(t) * mult
    except Exception:
        raise ValueError(f"Invalid duration string: {text}")


def _do_one_sweep(args, store: Store, bandplan: Bandplan, src) -> int:
    """Perform a single full sweep across [start, stop] inclusive, returning the scan_id."""
    meta = dict(
        t_start_utc=utc_now_str(),
        f_start_hz=int(args.start),
        f_stop_hz=int(args.stop),
        step_hz=int(args.step),
        samp_rate=int(args.samp_rate),
        fft=int(args.fft),
        avg=int(args.avg),
        device=str(getattr(src, 'dev', getattr(src, 'device', '')) if HAVE_SOAPY else getattr(src, 'device', '')),
        driver=args.driver,
    )
    scan_id = store.start_scan(meta)

    try:
        center = args.start
        while center <= args.stop:
            src.tune(center)
            nsamps = int(args.fft * args.avg)
            # Discard a small warmup buffer to allow tuner/AGC to settle
            _ = src.read(int(args.fft))
            samples = src.read(nsamps)
            baseband_f, psd_db = compute_psd_db(samples, args.samp_rate, args.fft, args.avg)
            # Translate baseband freqs to RF
            rf_freqs = baseband_f + center

            # Detect segments
            segs, occ_mask_cfar, _noise_local_db = detect_segments(
                rf_freqs,
                psd_db,
                thresh_db=args.threshold_db,
                guard_bins=args.guard_bins,
                min_width_bins=args.min_width_bins,
                cfar_mode=args.cfar,
                cfar_train=args.cfar_train,
                cfar_guard=args.cfar_guard,
                cfar_quantile=args.cfar_quantile,
                cfar_alpha_db=args.cfar_alpha_db,
            )

            # Occupancy mask per bin for baseline update
            noise_db = robust_noise_floor_db(psd_db)
            dynamic = noise_db + args.threshold_db
            occupied_mask = occ_mask_cfar if (args.cfar and args.cfar != 'off') else (psd_db > dynamic)

            # --- begin per-window batched DB writes ---
            store.begin()

            store.update_baseline(rf_freqs, psd_db, occupied_mask)

            # Persist detections and possibly alert on "new" bins
            for seg in segs:
                svc, reg, note = bandplan.lookup(seg.f_center_hz)
                store.add_detection(scan_id, seg, svc, reg, note)

                # Decide "new to baseline": occupancy EMA below threshold
                occ = store.baseline_occ(seg.f_center_hz)
                is_new = (occ is not None and occ < args.new_ema_occ)

                record = {
                    "time_utc": utc_now_str(),
                    "f_center_hz": seg.f_center_hz,
                    "f_low_hz": seg.f_low_hz,
                    "f_high_hz": seg.f_high_hz,
                    "peak_db": seg.peak_db,
                    "noise_db": seg.noise_db,
                    "snr_db": seg.snr_db,
                    "service": svc,
                    "region": reg,
                    "notes": note,
                    "is_new": bool(is_new),
                }
                maybe_emit_jsonl(args.jsonl, record)
                if is_new:
                    body = f"{seg.f_center_hz/1e6:.6f} MHz; SNR {seg.snr_db:.1f} dB; {svc or 'Unknown'} {reg or ''}"
                    maybe_notify("SDRWatch: New signal", body, args.notify)

            # commit batched writes for this window
            store.commit()

            # Advance center frequency
            center += args.step

    finally:
        # End scan (always set end time)
        store.end_scan(scan_id, utc_now_str())

    return scan_id


def run(args):
    bandplan = Bandplan(args.bandplan)
    store = Store(args.db)

    # Parse --soapy-args into a dict if present
    soapy_args_dict: Optional[Dict[str, str]] = None
    if getattr(args, "soapy_args", None):
        soapy_args_dict = {}
        for kv in str(args.soapy_args).split(","):
            if "=" in kv:
                k, v = kv.split("=", 1)
                soapy_args_dict[k.strip()] = v.strip()

    # Select source backend
    if args.driver == "rtlsdr_native":
        src = RTLSDRSource(samp_rate=args.samp_rate, gain=args.gain)
        hwkey = "RTL-SDR (native)"
        setattr(src, 'device', hwkey)
    else:
        src = SDRSource(driver=args.driver, samp_rate=args.samp_rate, gain=args.gain, soapy_args=soapy_args_dict)

    # Determine termination policy
    duration_s = _parse_duration_to_seconds(args.duration)
    start_time = time.time()

    # Compute how many sweeps to run: None for infinite
    # Rules:
    # - If --loop, infinite.
    # - If --repeat N, exactly N sweeps.
    # - If --duration is provided WITHOUT --loop/--repeat, run until time expires (infinite sweeps governed by time).
    # - Otherwise (no flags), run exactly one sweep.
    if args.loop:
        sweeps_remaining: Optional[int] = None
    elif args.repeat is not None:
        sweeps_remaining = int(args.repeat)
    elif duration_s is not None:
        sweeps_remaining = None  # duration governs
    else:
        sweeps_remaining = 1  # default single sweep

    try:
        while True:
            # Duration check (before starting next sweep)
            if duration_s is not None and (time.time() - start_time) >= duration_s:
                break

            _do_one_sweep(args, store, bandplan, src)

            # After each sweep, respect duration again
            if duration_s is not None and (time.time() - start_time) >= duration_s:
                break

            if sweeps_remaining is not None:
                sweeps_remaining -= 1
                if sweeps_remaining <= 0:
                    break

            # Sleep between sweeps if requested
            if args.sleep_between_sweeps > 0:
                time.sleep(args.sleep_between_sweeps)

    except KeyboardInterrupt:
        # Graceful exit on Ctrl-C
        pass
    finally:
        try:
            src.close()
        except Exception:
            pass


# ------------------------------
# CLI
# ------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Wideband scanner & baseline builder using SoapySDR or native RTL-SDR")
    p.add_argument("--start", type=float, required=True, help="Start frequency in Hz (e.g., 88e6)")
    p.add_argument("--stop", type=float, required=True, help="Stop frequency in Hz (e.g., 108e6)")
    p.add_argument("--step", type=float, default=2.4e6, help="Center frequency step per window [Hz]")

    p.add_argument("--samp-rate", type=float, default=2.4e6, help="Sample rate [Hz]")
    p.add_argument("--fft", type=int, default=4096, help="FFT size (per Welch segment)")
    p.add_argument("--avg", type=int, default=8, help="Averaging factor (segments per PSD)")

    p.add_argument("--driver", type=str, default="rtlsdr", help="Soapy driver key (e.g., rtlsdr, hackrf, airspy, etc.) or 'rtlsdr_native' for direct librtlsdr")
    p.add_argument("--soapy-args", type=str, default=None, help="Comma-separated Soapy device args (e.g., 'serial=00000001,index=0')")
    p.add_argument("--gain", type=str, default="auto", help='Gain in dB or "auto"')

    p.add_argument("--threshold-db", type=float, default=8.0, help="Detection threshold above noise floor [dB]")
    p.add_argument("--guard-bins", type=int, default=1, help="Allow this many below-threshold bins inside a detection")
    p.add_argument("--min-width-bins", type=int, default=2, help="Minimum contiguous bins for a detection")
    # CFAR options
    p.add_argument("--cfar", choices=["off", "os", "ca"], default="os", help="CFAR mode (default: os)")
    p.add_argument("--cfar-train", type=int, default=24, help="Training cells per side for CFAR")
    p.add_argument("--cfar-guard", type=int, default=4, help="Guard cells per side (excluded around CUT) for CFAR")
    p.add_argument("--cfar-quantile", type=float, default=0.75, help="Quantile (0..1) for OS-CFAR order statistic")
    p.add_argument("--cfar-alpha-db", type=float, default=None, help="Override threshold scaling for CFAR in dB; defaults to --threshold-db")

    p.add_argument("--bandplan", type=str, default=None, help="Optional bandplan CSV to map detections")
    p.add_argument("--db", type=str, default="sdrwatch.db", help="SQLite DB path")
    p.add_argument("--jsonl", type=str, default=None, help="Emit detections as line-delimited JSON to this path")
    p.add_argument("--notify", action="store_true", help="Desktop notifications for new signals")
    p.add_argument("--new-ema-occ", type=float, default=0.02, help="EMA occupancy threshold to flag a bin as NEW")

    # Sweep control modes (mutually exclusive)
    group = p.add_mutually_exclusive_group()
    group.add_argument("--loop", action="store_true", help="Run continuous sweep cycles until cancelled")
    group.add_argument("--repeat", type=int, help="Run exactly N full sweep cycles, then exit")
    group.add_argument("--duration", type=str, help="Run sweeps for a duration (e.g., '300', '10m', '2h'). Overrides --repeat count while time remains")

    p.add_argument("--sleep-between-sweeps", type=float, default=0.0, help="Seconds to sleep between sweep cycles")

    args = p.parse_args()
    # Backend availability check: only require Soapy if not using rtlsdr_native
    if args.driver != "rtlsdr_native" and not HAVE_SOAPY:
        p.error("python3-soapysdr not installed. Install it (or use --driver rtlsdr_native).")
    if args.driver == "rtlsdr_native" and not HAVE_RTLSDR:
        p.error("pyrtlsdr not installed. Install with: pip3 install pyrtlsdr")
    if args.stop < args.start:
        p.error("--stop must be >= --start")

    # Validate duration string early
    if args.duration:
        _ = _parse_duration_to_seconds(args.duration)

    return args


if __name__ == "__main__":
    run(parse_args())
