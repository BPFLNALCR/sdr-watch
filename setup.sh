#!/usr/bin/env bash
set -Eeuo pipefail

# =============================================================
# SDRwatch setup for Raspberry Pi OS (Lite) — Pi 4/5 (64‑bit)
# This version is tailored to the provided sdrwatch.py, which:
#  * Works with SoapySDR (preferred) OR native rtl-sdr via pyrtlsdr
#  * Treats SciPy as optional — we avoid building it from source
#
# Key changes from the old script:
#  - Install NumPy/SciPy/Soapy via APT (system packages)
#  - Create venv with --system-site-packages so the venv can see APT pkgs
#  - Keep pip installs minimal (pyrtlsdr only) to avoid source builds
#  - Try distro RTL-SDR packages first; fall back to building from source
# =============================================================

log() { printf '%s\n' "[setup] $*"; }
die() { printf '%s\n' "[setup:ERROR] $*" >&2; exit 1; }

# --- sanity checks ---
if ! command -v python3 >/dev/null 2>&1; then
  die "python3 is required. Update your system and try again."
fi

log "Updating apt and installing base build/runtime dependencies…"
sudo apt update
sudo apt install -y \
  git cmake build-essential pkg-config \
  libusb-1.0-0 libusb-1.0-0-dev \
  python3-venv python3-dev \
  python3-numpy python3-scipy \
  python3-soapysdr libsoapysdr0.8 libsoapysdr-dev \
  librtlsdr0 librtlsdr-dev rtl-sdr

# --- (optional) Build rtl-sdr from source if rtl_test looks broken ---
log "Verifying rtl-sdr toolchain… (rtl_test)"
if ! rtl_test -t >/dev/null 2>&1; then
  log "rtl_test not working or missing — building rtl-sdr from source…"
  WORKDIR="${PWD}"
  BUILDROOT="${WORKDIR}/.build-rtl-sdr"
  SRC=""
  rm -rf "${BUILDROOT}"
  mkdir -p "${BUILDROOT}"
  cd "${BUILDROOT}"

  log "Cloning RTL-SDR Blog fork…"
  if git clone --depth=1 https://github.com/rtlsdrblog/rtl-sdr-blog.git; then
    SRC="rtl-sdr-blog"
  else
    log "Blog fork unavailable; falling back to Osmocom repo…"
    git clone --depth=1 https://github.com/osmocom/rtl-sdr.git || die "Failed to clone any rtl-sdr repo"
    SRC="rtl-sdr"
  fi

  cd "${SRC}"
  mkdir -p build
  cd build

  log "Configuring CMake…"
  cmake \
    -DDETACH_KERNEL_DRIVER=ON \
    -DCPACK_PACKAGING_INSTALL_PREFIX=/usr \
    -DCMAKE_INSTALL_PREFIX=/usr \
    ..

  log "Building rtl-sdr…"
  make -j"$(nproc)"

  log "Installing rtl-sdr (sudo)…"
  sudo make install
  sudo ldconfig

  cd ..
  if [ -f rtl-sdr.rules ]; then
    log "Installing udev rules…"
    sudo cp -v rtl-sdr.rules /etc/udev/rules.d/rtl-sdr.rules
    sudo udevadm control --reload-rules || true
    sudo udevadm trigger || true
  fi
else
  log "rtl_test works; skipping source build."
fi

# --- udev rules & kernel module blacklist (safe to (re)apply) ---
BLACKLIST="/etc/modprobe.d/rtl-sdr-blacklist.conf"
log "Ensuring DVB kernel modules are blacklisted (${BLACKLIST})…"
sudo bash -c "cat > '${BLACKLIST}'" <<'EOF'
# Prevent the DVB drivers from grabbing RTL2832U-based dongles.
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF

log "Kernel module blacklist written. A reboot is recommended."

# --- Python virtualenv that can SEE APT's NumPy/SciPy/SoapySDR ---
VENV=".venv"
if [ ! -d "${VENV}" ]; then
  log "Creating Python venv at ${VENV} (with system site packages)…"
  python3 -m venv --system-site-packages "${VENV}"
fi

# shellcheck disable=SC1091
. "${VENV}/bin/activate"
log "Upgrading pip tooling…"
pip install --upgrade pip setuptools wheel

# Keep requirements minimal: SciPy/NumPy/SoapySDR come from APT via system site-packages
REQ_FILE="requirements.txt"
log "Writing minimal ${REQ_FILE}…"
cat > "${REQ_FILE}" <<'REQS'
# Minimal Python packages (avoid heavy source builds)
# NumPy/SciPy/SoapySDR are provided by APT and visible via --system-site-packages
pyrtlsdr
# (Optional small utilities you may like)
rich>=13.0.0
REQS

log "Installing Python dependencies from ${REQ_FILE}…"
pip install -r "${REQ_FILE}"

# --- Final checks ---
log "Python can import SoapySDR? (ok if you're only using --driver rtlsdr_native)"
python - <<'PY'
try:
    import SoapySDR  # provided by python3-soapysdr (APT)
    print("[check] SoapySDR import: OK")
except Exception as e:
    print(f"[check] SoapySDR import: NOT FOUND ({e}) — you can still use --driver rtlsdr_native")
PY

log "Verify rtl_test one more time…"
if rtl_test -t >/dev/null 2>&1; then
  log "rtl_test: OK"
else
  log "rtl_test: still not working — unplug/replug the dongle and reboot after this setup."
fi

log "Done."
echo ""
echo "Next steps:"
echo "  1) Reboot to ensure blacklist + udev are applied: sudo reboot"
echo "  2) After reboot, activate venv:                  source .venv/bin/activate"
echo "  3) Quick sanity:                                 rtl_test -t"
echo "  4) Run SDRwatch (Soapy path):                    python sdrwatch.py --driver rtlsdr --start 88e6 --stop 108e6"
echo "     Or native librtlsdr path:                     python sdrwatch.py --driver rtlsdr_native --start 88e6 --stop 108e6"
