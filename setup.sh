#!/usr/bin/env bash
# Setup script for SDRWatch on Raspberry Pi / Debian
# - Installs system deps
# - Builds and installs latest rtl-sdr from source (rtlsdrblog fork, with osmocom fallback)
# - Creates Python venv and installs pip deps
#
# Safe to re-run; uses idempotent checks where reasonable.

set -euo pipefail

# --------------- Config ---------------
: "${RTLSDR_REPO:=https://github.com/rtlsdrblog/rtl-sdr-blog.git}"  # override to use osmocom if desired
: "${RTLSDR_DIR:=rtl-sdr-src}"                                       # workspace dir
: "${VENV_DIR:=.venv}"

# --------------- Helpers ---------------
log(){ echo -e "[1;32m[+][0m $*"; }
warn(){ echo -e "[1;33m[!][0m $*"; }
err(){ echo -e "[1;31m[-][0m $*"; }

# --------------- System packages ---------------
log "Installing system packages..."
sudo apt update
sudo apt install -y \
  python3 python3-pip python3-venv python3-numpy python3-scipy \
  libusb-1.0-0 libusb-1.0-0-dev \
  git build-essential cmake pkg-config \
  libfftw3-dev \
  soapysdr-module-rtlsdr python3-soapysdr \
  libnotify-bin

# Note: We deliberately DO NOT install the repo's rtl-sdr package, since we'll build from source below.

# --------------- Build & install rtl-sdr from source ---------------
log "Preparing rtl-sdr source build from: $RTLSDR_REPO"

if [[ -d "$RTLSDR_DIR" ]]; then
  log "Updating existing source in $RTLSDR_DIR..."
  git -C "$RTLSDR_DIR" remote set-url origin "$RTLSDR_REPO" || true
  git -C "$RTLSDR_DIR" fetch --all --tags
  git -C "$RTLSDR_DIR" pull --rebase --autostash || true
else
  git clone "$RTLSDR_REPO" "$RTLSDR_DIR" || {
    warn "rtlsdrblog repo unavailable; falling back to osmocom mainline"
    RTLSDR_REPO="https://github.com/osmocom/rtl-sdr.git"
    git clone "$RTLSDR_REPO" "$RTLSDR_DIR"
  }
fi

pushd "$RTLSDR_DIR" >/dev/null

# Clean build dir
rm -rf build
mkdir -p build
pushd build >/dev/null

log "Configuring CMake..."
cmake -DDETACH_KERNEL_DRIVER=ON \
      -DCPACK_PACKAGING_INSTALL_PREFIX=/usr \
      -DCMAKE_INSTALL_PREFIX=/usr \
      ..

log "Building rtl-sdr (this can take a few minutes)..."
make -j"$(nproc)"

log "Installing rtl-sdr to /usr (requires sudo)..."
sudo make install
sudo ldconfig

# Install udev rules so non-root can access the dongle
if [[ -f ../rtl-sdr.rules ]]; then
  log "Installing udev rules for rtl-sdr..."
  sudo cp -f ../rtl-sdr.rules /etc/udev/rules.d/rtl-sdr.rules
  sudo udevadm control --reload-rules || true
  sudo udevadm trigger || true
else
  warn "udev rules file not found in repo; ensure device permissions manually if needed."
fi

popd >/dev/null  # build
popd >/dev/null  # src

# --------------- Kernel module blacklist (avoid DVB grab) ---------------
BLFILE="/etc/modprobe.d/rtl-sdr-blacklist.conf"
if ! grep -q "dvb_usb_rtl28xxu" "$BLFILE" 2>/dev/null; then
  log "Blacklisting DVB kernel modules that conflict with rtl-sdr..."
  printf "blacklist dvb_usb_rtl28xxu
blacklist rtl2832
blacklist rtl2830
" | sudo tee "$BLFILE" >/dev/null
  warn "A reboot is recommended after first install to ensure modules are detached."
fi

# --------------- Python virtual environment ---------------
log "Creating Python virtual environment at $VENV_DIR..."
python3 -m venv "$VENV_DIR"
# shellcheck disable=SC1090
source "$VENV_DIR/bin/activate"
s
log "Upgrading pip and installing Python requirements..."
pip install --upgrade pip setuptools wheel

if [[ -f requirements.txt ]]; then
  pip install -r requirements.txt
else
  warn "requirements.txt not found; installing minimal set"
  pip install numpy scipy SoapySDR pyrtlsdr
fi

log "Setup complete."
echo
echo "Next steps:"
echo "  1) Reboot (recommended on first install): sudo reboot"
echo "  2) Activate venv:  source $VENV_DIR/bin/activate"
echo "  3) Quick sanity:   rtl_test -t   (should list your tuner, no PLL spam)"
echo "  4) Run SDRWatch:   python sdrwatch.py --driver rtlsdr_native --start 95e6 --stop 97e6 --step 2.048e6 --samp-rate 2.048e6 --fft 4096 --avg 8 --gain 30 --threshold-db 6 --bandplan bandplan.csv --db sdrwatch.db"
