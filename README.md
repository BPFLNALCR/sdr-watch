# SDR-Watch

**Wideband scanner, baseline builder, and bandplan mapper for SDR devices.**

This project is a Python-based tool to scan wide frequency ranges with an SDR, detect signals above the noise floor, build a long-term baseline of occupancy, and map detections to frequency allocations. It can run continuously to notify you when new signals appear relative to the baseline.

---

## Features

- **Wideband Sweeps**: Scan across a defined frequency range in steps using SoapySDR-compatible radios (RTL-SDR, HackRF, Airspy, LimeSDR, USRP, etc.).
- **Signal Detection**: Uses robust noise floor estimation (median + MAD) and thresholding to find signals.
- **Baseline Tracking**: Maintains an exponential moving average of occupancy per frequency bin to identify persistent vs. new signals.
- **Bandplan Mapping**: Maps detections to allocations (FCC, CEPT, ITU-R, etc.) using a CSV file or built-in defaults.
- **Data Logging**: Saves all scans, detections, and baseline info to an SQLite database.
- **Alerts & Outputs**:
  - Desktop notifications (`notify-send`) for newly detected signals.
  - JSONL log stream for easy integration with external tools (Grafana, Loki, ELK).

---

## Installation (Debian)

```bash
# Update packages
sudo apt update

# Core dependencies
sudo apt install -y python3 python3-pip python3-numpy python3-scipy \
    python3-soapysdr soapysdr-module-rtlsdr rtl-sdr

# Optional: desktop notifications
sudo apt install -y libnotify-bin
```

---

## Usage

Example: sweep the FM broadcast band once (88–108 MHz):

```bash
python3 sdrwatch.py --start 88e6 --stop 108e6 --step 1.8e6 --samp-rate 2.4e6 \
  --fft 4096 --avg 8 --driver rtlsdr --gain auto --once --bandplan bandplan.csv
```

Example: run continuously across 30 MHz – 1.7 GHz, alerting on new signals:

```bash
python3 sdrwatch.py --start 30e6 --stop 1700e6 --step 2.4e6 --samp-rate 2.4e6 \
  --fft 4096 --avg 8 --driver rtlsdr --gain auto --notify \
  --bandplan bandplan.csv --db sdrwatch.db --jsonl events.jsonl
```

---

## Database Schema (SQLite)

- **scans**: metadata about each sweep
- **detections**: records of each detected signal
- **baseline**: per-frequency occupancy statistics

---

## Bandplan CSV Format

The tool maps detections to allocations from a CSV file:

```csv
low_hz,high_hz,service,region,notes
433050000,434790000,ISM,ITU-R1 (EU),Short-range devices (SRD)
902000000,928000000,ISM,US (FCC),902-928 MHz ISM band
2400000000,2483500000,ISM,Global,2.4 GHz ISM
```

Use official frequency allocation tables (FCC, ITU, BNetzA, CEPT) to expand this list.

---

## Inspecting Collected Data

Query detections:
```bash
sqlite3 -header -column sdrwatch.db "SELECT time_utc, f_center_hz/1e6 AS MHz, snr_db, service FROM detections ORDER BY id DESC LIMIT 20;"
```

Query baseline occupancy:
```bash
sqlite3 -header -column sdrwatch.db "SELECT bin_hz/1e6 AS MHz, round(ema_occ,3) AS occ FROM baseline ORDER BY occ DESC LIMIT 20;"
```

Export to CSV:
```bash
sqlite3 -header -csv sdrwatch.db "SELECT * FROM detections;" > detections.csv
```

---

## Roadmap

- Add CFAR-style detection to reduce false positives.
- Implement duty-cycle analysis for bursty signals.
- Provide a simple web dashboard for visualization.
- Expand region-specific bandplans (FCC / CEPT / BNetzA).

---

## License

MIT License. See [LICENSE](LICENSE) for details.

---

## Acknowledgements

Inspired by existing SDR scanning tools such as `rtl_power`, `SoapyPower`, and GNU Radio’s `gr-inspector`, but built to provide persistent baselining and automated mapping to allocations.
