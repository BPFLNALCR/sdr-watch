#!/usr/bin/env python3
"""
SDRWatch — wideband scanner, baseline builder, and bandplan mapper

Goals
-----
- Sweep a frequency range with an SDR (SoapySDR backend by default).
- Estimate noise floor robustly and detect signals via energy thresholding (CFAR‑like).
- Build a baseline (per‑bin occupancy over time) and flag "new" signals relative to that baseline.
- Map detections to a bandplan (FCC/CEPT/etc.) from a CSV file or built‑in minimal defaults.
- Log everything to SQLite and optionally emit desktop notifications or webhook JSON lines.

Hardware
--------
Any SoapySDR‑supported device (RTL‑SDR, HackRF, Airspy, SDRplay, LimeSDR, USRP...).

Install (Debian)
----------------
# Core deps
sudo apt update && sudo apt install -y python3 python3-pip python3-numpy python3-scipy \
    python3-soapysdr soapysdr-module-rtlsdr rtl-sdr

# Optional: for notifications
sudo apt install -y libnotify-bin

Run
---
python3 sdrwatch.py --start 88e6 --stop 108e6 --step 1.8e6 --samp-rate 2.4e6 \
  --fft 4096 --avg 8 --driver rtlsdr --gain auto --notify --bandplan bandplan.csv

Notes
-----
- Start/stop inclusive bounds, stepped by "step" (center frequency increment per sweep window).
- For each window: grab N = fft*avg complex samples, compute Welch PSD, detect segments above a dynamic threshold.
- Baseline is updated per frequency bin with an exponential moving average of occupancy.

CSV Bandplan format (UTF-8)
---------------------------
low_hz,high_hz,service,region,notes
433050000,434790000,ISM,ITU-R1 (EU),Short-range devices (SRD)
902000000,928000000,ISM,US (FCC),902-928 MHz ISM band
2400000000,2483500000,ISM,Global,2.4 GHz ISM
... (extend as needed from official tables)

SQLite schema (auto-created)
----------------------------
- scans(id INTEGER PK, t_start_utc TEXT, t_end_utc TEXT, f_start_hz INTEGER, f_stop_hz INTEGER, step_hz INTEGER,
        samp_rate INTEGER, fft_size INTEGER, avg INT, device TEXT, driver TEXT)
- detections(id INTEGER PK, scan_id INT, time_utc TEXT, f_low_hz INT, f_high_hz INT, f_center_hz INT,
             peak_db REAL, noise_db REAL, snr_db REAL, service TEXT, region TEXT, notes TEXT)
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
from typing import Iterable, List, Optional, Tuple

import numpy as np

# SciPy is optional. If missing, we fall back to a simple periodogram average.
try:
    from scipy.signal import welch  # type: ignore

    HAVE_SCIPY = True
except Exception:
    HAVE_SCIPY = False

# SoapySDR is required for broad device support.
try:
    import SoapySDR  # type: ignore
    from SoapySDR import SOAPY_SDR_CF32, SOAPY_SDR_RX

    HAVE_SOAPY = True
except Exception as e:
    HAVE_SOAPY = False

# ------------------------------
# Utility & math helpers
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
# Bandplan mapper
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
            ]

    def _load_csv(self, path: str):
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    self.bands.append(
                        Band(
                            int(float(row["low_hz"])),
                            int(float(row["high_hz"])),
                            row.get("service", "").strip(),
                            row.get("region", "").strip(),
                            row.get("notes", "").strip(),
                        )
                    )
                except Exception:
                    continue

    def lookup(self, f_center_hz: int) -> Tuple[str, str, str]:
        for b in self.bands:
            if b.low_hz <= f_center_hz <= b.high_hz:
                return b.service, b.region, b.notes
        return "", "", ""


# ------------------------------
# SDR source abstraction (SoapySDR)
# ------------------------------

class SDRSource:
    def __init__(self, driver: str, samp_rate: float, gain: str, channel: int = 0):
        if not HAVE_SOAPY:
            raise RuntimeError("SoapySDR Python module not available. Install python3-soapysdr.")
        # Build args dict e.g., {"driver":"rtlsdr"}
        args = {"driver": driver} if driver else {}
        self.dev = SoapySDR.Device(args)
        self.chan = channel
        self.dev.setSampleRate(SOAPY_SDR_RX, self.chan, samp_rate)
        if gain.lower() == "auto":
            try:
                self.dev.setGainMode(SOAPY_SDR_RX, self.chan, True)
            except Exception:
                pass
        else:
            try:
                self.dev.setGain(SOAPY_SDR_RX, self.chan, float(gain))
            except Exception:
                pass
        # Use complex float32 samples
        self.stream = self.dev.setupStream(SOAPY_SDR_RX, SOAPY_SDR_CF32)
        self.dev.activateStream(self.stream)

    def tune(self, freq_hz: float):
        self.dev.setFrequency(SOAPY_SDR_RX, self.chan, float(freq_hz))
        # brief settle
        time.sleep(0.03)

    def read(self, nsamps: int) -> np.ndarray:
        buff = np.empty(nsamps, dtype=np.complex64)
        total = 0
        while total < nsamps:
            sr = self.dev.readStream(self.stream, [buff[total:]], nsamps - total)
            if sr.ret > 0:
                total += sr.ret
            else:
                # backoff
                time.sleep(0.005)
        return buff

    def close(self):
        try:
            self.dev.deactivateStream(self.stream)
            self.dev.closeStream(self.stream)
        except Exception:
            pass


# ------------------------------
# Baseline store (SQLite)
# ------------------------------

class Store:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path, isolation_level=None)
        self._init()

    def _init(self):
        cur = self.db.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS scans (
                id INTEGER PRIMARY KEY,
                t_start_utc TEXT, t_end_utc TEXT,
                f_start_hz INTEGER, f_stop_hz INTEGER, step_hz INTEGER,
                samp_rate INTEGER, fft_size INTEGER, avg INTEGER,
                device TEXT, driver TEXT
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS detections (
                id INTEGER PRIMARY KEY,
                scan_id INTEGER, time_utc TEXT,
                f_low_hz INTEGER, f_high_hz INTEGER, f_center_hz INTEGER,
                peak_db REAL, noise_db REAL, snr_db REAL,
                service TEXT, region TEXT, notes TEXT
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS baseline (
                bin_hz INTEGER PRIMARY KEY,
                ema_occ REAL,          -- exponential moving avg of occupancy [0..1]
                ema_power_db REAL,     -- exponential moving avg of power (dB)
                last_seen_utc TEXT,
                total_obs INTEGER,
                hits INTEGER
            );
            """
        )

    def start_scan(self, meta: dict) -> int:
        cur = self.db.cursor()
        cur.execute(
            """
            INSERT INTO scans (t_start_utc, t_end_utc, f_start_hz, f_stop_hz, step_hz, samp_rate, fft_size, avg, device, driver)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                meta["t_start_utc"],
                meta["t_start_utc"],  # placeholder, will update at end
                meta["f_start_hz"],
                meta["f_stop_hz"],
                meta["step_hz"],
                meta["samp_rate"],
                meta["fft_size"],
                meta["avg"],
                meta.get("device", ""),
                meta.get("driver", ""),
            ),
        )
        return cur.lastrowid

    def end_scan(self, scan_id: int):
        cur = self.db.cursor()
        cur.execute(
            "UPDATE scans SET t_end_utc = ? WHERE id = ?",
            (utc_now_str(), scan_id),
        )

    def add_detection(self, scan_id: int, seg: Segment, svc: str, reg: str, notes: str):
        cur = self.db.cursor()
        cur.execute(
            """
            INSERT INTO detections (scan_id, time_utc, f_low_hz, f_high_hz, f_center_hz, peak_db, noise_db, snr_db, service, region, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_id,
                utc_now_str(),
                seg.f_low_hz,
                seg.f_high_hz,
                seg.f_center_hz,
                seg.peak_db,
                seg.noise_db,
                seg.snr_db,
                svc,
                reg,
                notes,
            ),
        )

    def update_baseline(self, freqs_hz: np.ndarray, psd_db: np.ndarray, occupied_mask: np.ndarray, alpha_occ=0.05, alpha_pow=0.05):
        cur = self.db.cursor()
        now = utc_now_str()
        for f, p_db, occ in zip(freqs_hz.astype(int), psd_db, occupied_mask):
            # fetch existing
            row = cur.execute("SELECT ema_occ, ema_power_db, total_obs, hits FROM baseline WHERE bin_hz = ?", (int(f),)).fetchone()
            if row:
                ema_occ, ema_pdb, total, hits = row
                total = int(total) + 1
                hits = int(hits) + (1 if occ else 0)
                ema_occ = (1 - alpha_occ) * float(ema_occ) + alpha_occ * (1.0 if occ else 0.0)
                ema_pdb = (1 - alpha_pow) * float(ema_pdb) + alpha_pow * float(p_db)
                cur.execute(
                    "UPDATE baseline SET ema_occ=?, ema_power_db=?, last_seen_utc=?, total_obs=?, hits=? WHERE bin_hz=?",
                    (ema_occ, ema_pdb, now, total, hits, int(f)),
                )
            else:
                cur.execute(
                    "INSERT INTO baseline (bin_hz, ema_occ, ema_power_db, last_seen_utc, total_obs, hits) VALUES (?, ?, ?, ?, ?, ?)",
                    (int(f), 1.0 if occ else 0.0, float(p_db), now, 1, 1 if occ else 0),
                )

    def baseline_occ(self, f_center_hz: int) -> Optional[float]:
        row = self.db.cursor().execute("SELECT ema_occ FROM baseline WHERE bin_hz = ?", (int(f_center_hz),)).fetchone()
        return float(row[0]) if row else None


# ------------------------------
# Detection logic
# ------------------------------


def detect_segments(freqs_hz: np.ndarray, psd_db: np.ndarray, thresh_db: float, guard_bins: int = 1, min_width_bins: int = 2) -> List[Segment]:
    noise_db = robust_noise_floor_db(psd_db)
    dynamic = noise_db + thresh_db
    above = psd_db > dynamic

    # Merge small gaps (guard bins) and form contiguous segments
    segs: List[Segment] = []
    i = 0
    N = len(psd_db)
    while i < N:
        if above[i]:
            start = i
            j = i + 1
            gap = 0
            while j < N and (above[j] or gap < guard_bins):
                if above[j]:
                    gap = 0
                else:
                    gap += 1
                j += 1
            end = j - 1
            if end - start + 1 >= min_width_bins:
                # compute segment metrics
                idx = slice(start, end + 1)
                peak_idx = int(np.argmax(psd_db[idx])) + start
                segs.append(
                    Segment(
                        int(freqs_hz[start]),
                        int(freqs_hz[end]),
                        int(freqs_hz[peak_idx]),
                        float(psd_db[peak_idx]),
                        float(noise_db),
                        float(psd_db[peak_idx] - noise_db),
                    )
                )
            i = end + 1
        else:
            i += 1
    return segs


# ------------------------------
# PSD computation
# ------------------------------


def compute_psd_db(samples: np.ndarray, samp_rate: float, fft_size: int, n_avg: int) -> Tuple[np.ndarray, np.ndarray]:
    """Return (freqs_hz, psd_db) for the *centered window*.
    We assume samples are centered on the tuned frequency; we compute baseband PSD and translate to RF freqs later.
    """
    if HAVE_SCIPY:
        # Welch with nperseg=fft_size, noverlap=50%
        f, Pxx = welch(
            samples,
            fs=samp_rate,
            window="hann",
            nperseg=fft_size,
            noverlap=fft_size // 2,
            nfft=fft_size,
            return_onesided=False,
            detrend=False,
            scaling="density",
        )
        psd = np.fft.fftshift(Pxx)
        freqs = np.fft.fftshift(f)
    else:
        # Simple averaged periodogram
        seg = fft_size
        step = seg // 2
        windows = []
        for start in range(0, max(len(samples) - seg + 1, 1), step):
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
    return freqs.astype(np.float64), psd_db.astype(np.float64)


# ------------------------------
# Notifications & sinks
# ------------------------------

import json
import subprocess


def maybe_notify(title: str, body: str, enable: bool):
    if not enable:
        return
    try:
        subprocess.run(["notify-send", title, body], check=False)
    except FileNotFoundError:
        pass


def maybe_emit_jsonl(path: Optional[str], record: dict):
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


# ------------------------------
# Main sweep loop
# ------------------------------


def run(args):
    bandplan = Bandplan(args.bandplan)
    store = Store(args.db)

    src = SDRSource(driver=args.driver, samp_rate=args.samp_rate, gain=args.gain)

    meta = dict(
        t_start_utc=utc_now_str(),
        f_start_hz=int(args.start),
        f_stop_hz=int(args.stop),
        step_hz=int(args.step),
        samp_rate=int(args.samp_rate),
        fft_size=int(args.fft),
        avg=int(args.avg),
        device=str(src.dev.getHardwareKey() if HAVE_SOAPY else ""),
        driver=args.driver,
    )
    scan_id = store.start_scan(meta)

    try:
        window_bw = args.samp_rate
        center = args.start
        while center <= args.stop:
            src.tune(center)
            nsamps = int(args.fft * args.avg)
            samples = src.read(nsamps)
            baseband_f, psd_db = compute_psd_db(samples, args.samp_rate, args.fft, args.avg)
            # Translate baseband freqs to RF
            rf_freqs = baseband_f + center

            # Detect segments
            segs = detect_segments(rf_freqs, psd_db, thresh_db=args.threshold_db, guard_bins=args.guard_bins, min_width_bins=args.min_width_bins)

            # Occupancy mask per bin for baseline update
            noise_db = robust_noise_floor_db(psd_db)
            dynamic = noise_db + args.threshold_db
            occupied_mask = psd_db > dynamic
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

            # Step
            center += args.step

            if args.once:
                break

        store.end_scan(scan_id)
    finally:
        src.close()


# ------------------------------
# CLI
# ------------------------------


def parse_args():
    p = argparse.ArgumentParser(description="Wideband scanner & baseline builder using SoapySDR")
    p.add_argument("--start", type=float, required=True, help="Start frequency in Hz (e.g., 88e6)")
    p.add_argument("--stop", type=float, required=True, help="Stop frequency in Hz (e.g., 108e6)")
    p.add_argument("--step", type=float, default=2.4e6, help="Center frequency step per window [Hz]")

    p.add_argument("--samp-rate", type=float, default=2.4e6, help="Sample rate [Hz]")
    p.add_argument("--fft", type=int, default=4096, help="FFT size (per Welch segment)")
    p.add_argument("--avg", type=int, default=8, help="Averaging factor (segments per PSD)")

    p.add_argument("--driver", type=str, default="rtlsdr", help="SoapySDR driver key (rtlsdr, hackrf, airspy, etc.)")
    p.add_argument("--gain", type=str, default="auto", help='Gain in dB or "auto"')

    p.add_argument("--threshold-db", type=float, default=8.0, help="Detection threshold above noise floor [dB]")
    p.add_argument("--guard-bins", type=int, default=1, help="Allow this many below-threshold bins inside a detection")
    p.add_argument("--min-width-bins", type=int, default=2, help="Minimum contiguous bins for a detection")

    p.add_argument("--db", type=str, default="sdrwatch.db", help="SQLite database path")
    p.add_argument("--bandplan", type=str, default=None, help="CSV bandplan path")
    p.add_argument("--jsonl", type=str, default=None, help="Optional path to emit one-line JSON events")
    p.add_argument("--notify", action="store_true", help="Desktop notifications for new signals")
    p.add_argument("--new-ema-occ", type=float, default=0.02, help="EMA occupancy threshold to flag a bin as NEW")
    p.add_argument("--once", action="store_true", help="Do a single sweep then exit")

    args = p.parse_args()
    if args.stop < args.start:
        p.error("--stop must be >= --start")
    if not HAVE_SOAPY:
        p.error("python3-soapysdr not installed. Install it (and device module) on Debian.")
    return args


if __name__ == "__main__":
    run(parse_args())
