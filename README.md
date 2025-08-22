# SDRwatch 📡

**Wideband scanner, baseline builder, and bandplan mapper for SDR devices.**

SDRwatch is a Python-based tool to sweep wide frequency ranges with an SDR, detect signals above the noise floor, build a long-term baseline of occupancy, and map detections to frequency allocations. It can run continuously to notify you when new signals appear relative to the baseline, and includes a simple web dashboard for visualization.

---

## ✨ Features

* **📶 Wideband Sweeps**: Scan across defined frequency ranges using SoapySDR-compatible radios (RTL-SDR, HackRF, Airspy, LimeSDR, USRP, etc.) or native RTL-SDR via `--driver rtlsdr_native`.
* **🔎 Signal Detection**: Robust noise floor estimation (median + MAD) and CFAR-style detection (Order-Statistic / Cell-Averaging).
* **📊 Baseline Tracking**: Maintains an exponential moving average of occupancy and power per frequency bin to identify persistent vs. new signals.
* **🗺 Bandplan Mapping**: Maps detections to allocations (FCC, CEPT, ITU-R, etc.) from a CSV file or built-in defaults.
* **💾 Data Logging**: All scans, detections, and baselines are stored in SQLite.
* **🔔 Alerts & Outputs**:

  * Desktop notifications (`notify-send`) for newly detected signals.
  * JSONL log output for integration with external tools (Grafana, Loki, ELK).
* **📈 Web Dashboard**: A lightweight Flask app (`sdrwatch-web-simple.py`) to view:

  * Summary stats (scans, detections, baseline bins)
  * SNR histograms and strongest signals
  * Top services and frequency distributions
  * Detections over time and CSV export

---

## ⚙️ Installation

Run the included setup script:

```bash
chmod +x setup.sh
./setup.sh
```

This will:

* 📦 Install system dependencies (NumPy, SciPy, SoapySDR, RTL-SDR)
* 🐍 Set up a Python virtual environment with minimal pip packages
* 🛠 Verify RTL-SDR toolchain (`rtl_test`)
* 🔒 Add udev rules + kernel module blacklist for SDR dongles

After reboot:

```bash
source .venv/bin/activate
```

---

## 🚀 Usage

Sweep the FM broadcast band once:

```bash
python3 sdrwatch.py --driver rtlsdr --start 88e6 --stop 108e6 --step 2.4e6
```

Continuous scanning for 30 minutes, with 5s delay between sweeps:

```bash
python3 sdrwatch.py --driver rtlsdr --start 88e6 --stop 108e6 \
  --duration 30m --sleep-between-sweeps 5 --db sdrwatch.db
```

Native librtlsdr path (no Soapy):

```bash
python3 sdrwatch.py --driver rtlsdr_native --start 88e6 --stop 108e6
```

Enable CFAR detection:

```bash
python3 sdrwatch.py --start 400e6 --stop 420e6 \
  --cfar os --cfar-train 24 --cfar-guard 4 --cfar-quantile 0.75
```

Run the web dashboard:

```bash
python3 sdrwatch-web-simple.py --db sdrwatch.db --host 0.0.0.0 --port 8080
```

Then open [http://localhost:8080](http://localhost:8080). 🌐

---

## 🗄 Database Schema (SQLite)

* **scans**: metadata about each sweep
* **detections**: records of each detected signal
* **baseline**: per-frequency EMA occupancy and power statistics

---

## 📑 Bandplan CSV Format

```csv
low_hz,high_hz,service,region,notes
433050000,434790000,ISM/SRD,ITU-R1 (EU),Short-range devices
902000000,928000000,ISM,US (FCC),902-928 MHz ISM band
2400000000,2483500000,ISM,Global,2.4 GHz ISM
```

Extend this file with allocations from official tables (FCC, ITU, CEPT, BNetzA, etc.).

---

## 🔍 Inspecting Collected Data

Query detections:

```bash
sqlite3 -header -column sdrwatch.db \
  "SELECT time_utc,f_center_hz/1e6 AS MHz,snr_db,service FROM detections ORDER BY id DESC LIMIT 20;"
```

Query baseline occupancy:

```bash
sqlite3 -header -column sdrwatch.db \
  "SELECT bin_hz/1e6 AS MHz,round(ema_occ,3) AS occ FROM baseline ORDER BY occ DESC LIMIT 20;"
```

Export to CSV:

```bash
sqlite3 -header -csv sdrwatch.db "SELECT * FROM detections;" > detections.csv
```

---

## 🛠 Roadmap

* More advanced CFAR modes & presets
* Duty-cycle analysis for bursty signals
* Expanded region-specific bandplans (FCC / CEPT / BNetzA)
* Enhanced dashboard with interactive graphs

---

## 📜 License

MIT License. See [LICENSE](LICENSE).

---

## 🙏 Acknowledgements

Inspired by tools such as `rtl_power`, `SoapyPower`, and GNU Radio’s `gr-inspector`, but designed for persistent baselining, CFAR detection, and allocation mapping.
