#!/usr/bin/env bash
set -Eeuo pipefail

# --- helper logging ---
log() { printf '%s\n' "[setup] $*"; }
die() { printf '%s\n' "[setup:ERROR] $*" >&2; exit 1; }

# --- prereqs (apt) ---
log "Updating apt and installing build/runtime dependencies…"
sudo apt update
sudo apt install -y \
  git cmake build-essential pkg-config \
  libusb-1.0-0 libusb-1.0-0-dev \
  python3-venv python3-dev \
  # optional but handy:
  libatlas-base-dev libfftw3-dev \
  libnotify-bin

# --- fetch & build rtl-sdr from source (RTL-SDR Blog fork first) ---
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

# --- udev rules & kernel module blacklist ---
cd ..
if [ -f rtl-sdr.rules ]; then
  log "Installing udev rules…"
  sudo cp -v rtl-sdr.rules /etc/udev/rules.d/rtl-sdr.rules
  sudo udevadm control --reload-rules || true
  sudo udevadm trigger || true
fi

BLACKLIST="/etc/modprobe.d/rtl-sdr-blacklist.conf"
log "Ensuring DVB kernel modules are blacklisted (${BLACKLIST})…"
sudo bash -c "cat > '${BLACKLIST}'" <<'EOF'
# Prevent the DVB drivers from grabbing RTL2832U-based dongles.
blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
EOF

log "Kernel module blacklist written. A reboot is recommended."

# --- create & populate Python virtualenv ---
cd "${WORKDIR}"
VENV=".venv"
if [ ! -d "${VENV}" ]; then
  log "Creating Python venv at ${VENV}…"
  python3 -m venv "${VENV}"
fi

# shellcheck disable=SC1091
. "${VENV}/bin/activate"
log "Upgrading pip tooling…"
pip install --upgrade pip setuptools wheel

# If requirements.txt is missing, create a sane default
if [ ! -f requirements.txt ]; then
  log "requirements.txt not found; creating a default one…"
  cat > requirements.txt <<'REQS'
# Core scientific stack
numpy
scipy

# Native RTL-SDR Python bindings (for --driver rtlsdr_native)
pyrtlsdr

# Optional: keep Soapy path available if you want
SoapySDR
REQS
fi

log "Installing Python dependencies from requirements.txt…"
pip install -r requirements.txt

log "Done."
printf '%s\n' ""
printf '%s\n' "Next steps:"
printf '  1) Reboot to ensure blacklist + udev are applied: %s\n' "sudo reboot"
printf '  2) After reboot, activate venv:                 %s\n' "source .venv/bin/activate"
printf '  3) Quick sanity:                                %s\n' "rtl_test -t"
printf '  4) Run SDRwatch (native path):                  %s\n' 'python sdrwatch.py --driver rtlsdr_native ...'
