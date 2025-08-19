# 📡 SDRwatch

**Wideband spectrum scanner, baseline builder, and bandplan mapper for SDR devices.**

---

## ✨ Overview

SDRwatch is a Python-based toolchain for **spectrum monitoring and analysis**. It allows you to:

* Sweep wide frequency ranges with SDRs.
* Detect and classify signals above the noise floor.
* Maintain a **long-term baseline** of spectrum occupancy.
* Map detections to **regulatory bandplans**.
* Visualize and query results via a **CLI tool** or optional **web dashboard**.

---

## 🚀 Features

* **Wideband Sweeps**: Scan defined frequency ranges using SoapySDR-compatible radios (RTL-SDR, HackRF, Airspy, LimeSDR, USRP, etc.).
* **Noise Floor Estimation**: Robust algorithms (median + MAD) with upcoming CFAR support.
* **Signal Detection & Baseline Tracking**:

  * Threshold-based detection above noise floor.
  * EMA-based tracking to distinguish persistent vs transient signals.
* **Bandplan Mapping**: Match detections against allocations (FCC, CEPT, ITU-R, etc.) via CSV.
* **Data Persistence**: Store all scans, detections, and baselines in **SQLite**.
* **Outputs & Notifications**:

  * Desktop notifications for new signals (`notify-send`).
  * JSONL logging for Grafana, Loki, ELK, or custom integrations.
* **Query & Web Tools**:

  * `query-sdrwatch.py` for summaries, histograms, and service-level stats.
  * Lightweight Flask-based web interface.

---

## 🛠️ Installation (Debian/Raspberry Pi)

```bash
# Update system
sudo apt update

# Core dependencies
sudo apt install -y python3 python3-pip python3-numpy python3-scipy \
    python3-soapysdr soapysdr-module-rtlsdr rtl-sdr sqlite3

# Optional: desktop notifications
sudo apt install -y libnotify-bin

# Optional: Flask web UI
pip install flask
```

---

## ▶️ Usage

### Example 1: Sweep FM broadcast band once (88–108 MHz)

```bash
python3 sdrwatch.py --start 88e6 --stop 108e6 --step 2.0e6 --samp-rate 2.0e6 \
  --fft 4096 --avg 8 --driver rtlsdr --gain auto --once --bandplan bandplan.csv
```

### Example 2: Continuous monitoring (30 MHz – 1.7 GHz)

```bash
python3 sdrwatch.py --start 30e6 --stop 1700e6 --step 2.4e6 --samp-rate 2.4e6 \
  --fft 4096 --avg 8 --driver rtlsdr --gain auto --notify \
  --bandplan bandplan.csv --db sdrwatch.db --jsonl events.jsonl
```

---

## 📊 Querying the Database

Summarize contents:

```bash
python3 query-sdrwatch.py --db sdrwatch.db summary
```

List recent scans:

```bash
python3 query-sdrwatch.py --db sdrwatch.db scans --limit 10
```

Show service-level statistics:

```bash
python3 query-sdrwatch.py --db sdrwatch.db services
```

---

## 📑 Bandplan CSV Example

```csv
low_hz,high_hz,service,region,notes
433050000,434790000,ISM,ITU-R1 (EU),Short-range devices (SRD)
902000000,928000000,ISM,US (FCC),902-928 MHz ISM band
2400000000,2483500000,ISM,Global,2.4 GHz ISM
```

---

## 🗺️ Roadmap

* ✅ CLI query tools
* ✅ SQLite database integration
* 🔄 CFAR algorithms for adaptive detection
* 🔄 Duty cycle analysis for bursty signals
* 🔄 Expanded bandplan datasets (FCC, CEPT, BNetzA)
* 🔄 Enhanced Flask web UI with visualizations
* 🔄 Multi-SDR support (sweeping + monitoring roles)

---

## 📜 License

MIT License. See [LICENSE](LICENSE) for details.

---

## 🙌 Acknowledgements

Inspired by `rtl_power`, `SoapyPower`, and GNU Radio’s `gr-inspector` — reimagined for **persistent baselining** and **automated regulatory mapping**.
