# SDR-Watch 📡

![Build Status](https://img.shields.io/badge/build-passing-brightgreen)
![Platform](https://img.shields.io/badge/platform-Raspberry%20Pi%205-red)
![SDR](https://img.shields.io/badge/SDR-RTL--SDR-blue)
![Planned SDRs](https://img.shields.io/badge/Planned-HackRF%2C%20Airspy%2C%20LimeSDR%2C%20USRP-yellow)
![WebUI](https://img.shields.io/badge/WebUI-Flask-orange)
![License](https://img.shields.io/badge/license-MIT-lightgrey)

**Wideband scanner, baseline builder, and bandplan mapper for SDR devices with a lightweight web dashboard.**

This project is a Python-based tool designed to scan wide frequency ranges with an SDR, detect signals above the noise floor, build a long-term baseline of occupancy, and map detections to frequency allocations. It now includes a simple **web GUI dashboard** for monitoring and controlling scans in real time. 🌐

At its current stage of development, SDR-Watch is:

* ✅ Optimized for **RTL-SDR** devices (support for HackRF, Airspy, LimeSDR, USRP, etc. planned).
* ✅ Intended to run on a **Raspberry Pi 5** with a **32 GB SD card** using **Raspbian Lite OS**.
* ✅ Usable as both a CLI tool and a web dashboard.

---

## ✨ Features

* **📶 Wideband Sweeps**: Scan across defined frequency ranges using RTL-SDR (SoapySDR support coming soon).
* **🔎 Signal Detection**: Robust noise floor estimation (median + MAD) and thresholding.
* **📊 Baseline Tracking**: Tracks persistent vs. new signals using exponential moving average.
* **🗺️ Bandplan Mapping**: Maps detections to frequency allocations (FCC, CEPT, ITU-R, etc.) via CSV or built-in defaults.
* **💾 Data Logging**: Saves scans, detections, and baselines to SQLite database.
* **🌍 Web Dashboard (new):**

  * 📈 Real-time graphs and histograms of detections.
  * 🎛️ Control buttons for common scan presets (FM band, full sweep, etc.).
  * 👀 Quick monitoring of activity and baseline occupancy.
* **🔔 Alerts & Outputs:**

  * Desktop notifications (`notify-send`) for new signals.
  * JSONL event log for external integrations (Grafana, Loki, ELK).

---

## 🛠️ Installation (Raspberry Pi 5 – Raspbian Lite)

```bash
# Update packages
sudo apt update && sudo apt upgrade -y

# Core dependencies
sudo apt install -y python3 python3-pip python3-numpy python3-scipy \
    python3-soapysdr soapysdr-module-rtlsdr rtl-sdr libatlas-base-dev \
    libnotify-bin sqlite3

# Install Python dependencies
pip3 install -r requirements.txt

# Optional: enable web dashboard (Flask)
pip3 install flask
```

---

## 🚀 Usage

### Command Line

Example: sweep the FM broadcast band once (88–108 MHz):

```bash
python3 sdrwatch.py --start 88e6 --stop 108e6 --step 1.8e6 --samp-rate 2.4e6 \
  --fft 4096 --avg 8 --driver rtlsdr --gain auto --once --bandplan bandplan.csv
```

Example: continuous monitoring across 30 MHz – 1.7 GHz:

```bash
python3 sdrwatch.py --start 30e6 --stop 1700e6 --step 2.4e6 --samp-rate 2.4e6 \
  --fft 4096 --avg 8 --driver rtlsdr --gain auto --notify \
  --bandplan bandplan.csv --db sdrwatch.db --jsonl events.jsonl
```

### Web Dashboard 🌐

Launch the web interface on port 8080:

```bash
python3 sdrwatch-web-simple.py --db sdrwatch.db --host 0.0.0.0 --port 8080
```

Then open `http://<raspberrypi-ip>:8080` in a browser. 🖥️

---

## 🗄️ Database Schema (SQLite)

* **scans**: metadata about each sweep
* **detections**: records of detected signals
* **baseline**: per-frequency occupancy statistics

---

## 📑 Bandplan CSV Format

```csv
low_hz,high_hz,service,region,notes
433050000,434790000,ISM,ITU-R1 (EU),Short-range devices (SRD)
902000000,928000000,ISM,US (FCC),902-928 MHz ISM band
2400000000,2483500000,ISM,Global,2.4 GHz ISM
```

Use official frequency allocation tables (FCC, ITU, BNetzA, CEPT) to expand.

---

## 🔍 Inspecting Collected Data

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

## 🛣️ Roadmap

* Expand SDR support (HackRF, Airspy, LimeSDR, USRP).
* Add CFAR-style detection for fewer false positives.
* Implement duty-cycle analysis for bursty signals.
* Enhance web dashboard with interactive charts and filters.
* Expand region-specific bandplans (FCC, CEPT, BNetzA).

---

## 📜 License

MIT License. See [LICENSE](LICENSE).

---

## 🙏 Acknowledgements

Inspired by existing SDR scanning tools such as `rtl_power`, `SoapyPower`, and GNU Radio’s `gr-inspector`, but built to provide persistent baselining, automated allocation mapping, and now a web interface for real-time use.
