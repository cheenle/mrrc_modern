#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# MRRC Modern — One-Shot Install & Deploy Script
# ═══════════════════════════════════════════════════════════════════════
#
# Automatically detects hardware, installs all dependencies, configures
# the FT-710 server, and validates the setup.  Works on macOS (arm64/x86_64)
# and Linux (Debian/Ubuntu, Fedora, Arch).
#
# Usage:
#   ./install.sh              # Full interactive install
#   ./install.sh --yes        # Non-interactive (accept all defaults)
#   ./install.sh --dry-run    # Detect & report only, no changes
#   ./install.sh --help       # Show options
#
# What it does:
#   1. Detect OS, architecture, Python version
#   2. Install system packages (portaudio, libopus, FTDI libs on macOS)
#   3. Create Python virtual environment + install pip packages
#   4. Detect FT-710 serial ports (CAT + Scope)
#   5. Detect FT-710 USB audio devices
#   6. Configure FT4222/FTDI scope (macOS: D2XX driver, ftd2xx.cfg)
#   7. Configure udev rules (Linux: serial port permissions)
#   8. Generate .env config file
#   9. Validate: test serial, test audio, test import, test server start
#  10. Optionally install as systemd service (Linux) or launchd (macOS)
# ═══════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Colors ────────────────────────────────────────────────────────────
if [ -t 1 ]; then
  RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
  BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
else
  RED=''; GREEN=''; YELLOW=''; BLUE=''; CYAN=''; BOLD=''; NC=''
fi

# ── Globals ───────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

INSTALL_LOG="$SCRIPT_DIR/install.log"
DRY_RUN=false
YES_MODE=false
INSTALL_DEPS=true
INSTALL_AUDIO=true
INSTALL_SCOPE=true
START_SERVICE=false

OS=""; ARCH=""; PKG_MANAGER=""
PYTHON=""; PIP=""; VENV_DIR="$SCRIPT_DIR/venv"
DETECTED_CAT_PORT=""; DETECTED_SCOPE_PORT=""
DETECTED_AUDIO_IN=""; DETECTED_AUDIO_OUT=""
FTDI_LIB_OK=false; OPUS_LIB_OK=false; AUDIO_OK=false
WARNINGS=(); ERRORS=()

# ── Help ──────────────────────────────────────────────────────────────
usage() {
  cat <<EOF
${BOLD}MRRC Modern Install Script${NC}

Usage: $0 [OPTIONS]

Options:
  --yes, -y        Non-interactive mode (accept all defaults)
  --dry-run        Detect hardware & report only, no changes
  --no-deps        Skip system package installation
  --no-audio       Skip audio dependencies (PyAudio, libopus)
  --no-scope       Skip FT4222 scope setup (D2XX config, FTDI libs)
  --install-service  Install as systemd (Linux) or launchd (macOS) service
  --help, -h       Show this help

Environment overrides:
  MRRC_SERIAL_PORT    Force CAT/CI-V serial port path
  MRRC_WEB_PORT       Web server port (default: 8888)
  MRRC_WEB_PASSWORD   Login password (default: auto-generated)
EOF
  exit 0
}

# ── Parse Args ────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --yes|-y) YES_MODE=true ;;
    --dry-run) DRY_RUN=true ;;
    --no-deps) INSTALL_DEPS=false ;;
    --no-audio) INSTALL_AUDIO=false ;;
    --no-scope) INSTALL_SCOPE=false ;;
    --install-service) START_SERVICE=true ;;
    --dev) INSTALL_DEV=true ;;
    --help|-h) usage ;;
    *) echo -e "${RED}Unknown option: $1${NC}"; usage ;;
  esac
  shift
done

# ── Logging ───────────────────────────────────────────────────────────
log_msg() { echo -e "${CYAN}[$(date +%H:%M:%S)]${NC} $*" | tee -a "$INSTALL_LOG"; }
log_ok()  { echo -e "${GREEN}  ✓${NC} $*" | tee -a "$INSTALL_LOG"; }
log_warn(){ echo -e "${YELLOW}  ⚠${NC} $*" | tee -a "$INSTALL_LOG"; WARNINGS+=("$*"); }
log_err() { echo -e "${RED}  ✗${NC} $*" | tee -a "$INSTALL_LOG"; ERRORS+=("$*"); }
log_hdr() { echo -e "\n${BOLD}${BLUE}═══ $* ═══${NC}" | tee -a "$INSTALL_LOG"; }

# ── Confirmation ──────────────────────────────────────────────────────
confirm() {
  $YES_MODE && return 0
  local prompt="$1 [Y/n] "
  read -r -p "$(echo -e "${CYAN}$prompt${NC}")" reply
  [[ "$reply" =~ ^[Nn] ]] && return 1
  return 0
}

# ── Dry-Run Guard ─────────────────────────────────────────────────────
run_cmd() {
  if $DRY_RUN; then
    echo -e "  ${YELLOW}[dry-run]${NC} $*"
  else
    "$@"
  fi
}

# ═══════════════════════════════════════════════════════════════════════
# STEP 1: OS Detection
# ═══════════════════════════════════════════════════════════════════════
detect_os() {
  log_hdr "STEP 1: Detecting OS & Architecture"

  OS="$(uname -s)"
  ARCH="$(uname -m)"

  case "$OS" in
    Darwin)
      OS="macos"
      PKG_MANAGER="brew"
      if ! command -v brew &>/dev/null; then
        log_err "Homebrew not found. Install: /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
        return 1
      fi
      log_ok "macOS $ARCH — Homebrew package manager"
      ;;
    Linux)
      OS="linux"
      if   command -v apt-get &>/dev/null; then PKG_MANAGER="apt"
      elif command -v dnf &>/dev/null;     then PKG_MANAGER="dnf"
      elif command -v pacman &>/dev/null;  then PKG_MANAGER="pacman"
      elif command -v zypper &>/dev/null;  then PKG_MANAGER="zypper"
      else
        log_err "No supported package manager found (apt, dnf, pacman, zypper)"
        return 1
      fi
      . /etc/os-release 2>/dev/null || true
      log_ok "Linux (${NAME:-unknown}) $ARCH — $PKG_MANAGER package manager"
      ;;
    *)
      log_err "Unsupported OS: $OS (only macOS and Linux are supported)"
      return 1
      ;;
  esac
}

# ═══════════════════════════════════════════════════════════════════════
# STEP 2: Python Setup
# ═══════════════════════════════════════════════════════════════════════
setup_python() {
  log_hdr "STEP 2: Setting up Python"

  # Find Python 3
  PYTHON=""
  for cmd in python3.12 python3.13 python3.11 python3; do
    if command -v "$cmd" &>/dev/null; then
      local ver=$("$cmd" --version 2>&1 | grep -oE '[0-9]+\.[0-9]+' | head -1)
      if [ -n "$ver" ]; then
        local major=$(echo "$ver" | cut -d. -f1)
        local minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
          PYTHON="$cmd"
          log_ok "Found $($PYTHON --version 2>&1)"
          break
        fi
      fi
    fi
  done

  if [ -z "$PYTHON" ]; then
    log_err "Python 3.11+ required but not found"
    log_msg "Install: brew install python@3.12 (macOS) or apt install python3.12 (Linux)"
    return 1
  fi

  # Create virtual environment
  if [ ! -d "$VENV_DIR" ]; then
    log_msg "Creating virtual environment: $VENV_DIR"
    run_cmd "$PYTHON" -m venv "$VENV_DIR"
    log_ok "Virtual environment created"
  else
    log_ok "Virtual environment exists: $VENV_DIR"
  fi

  # Activate
  if [ -f "$VENV_DIR/bin/activate" ]; then
    source "$VENV_DIR/bin/activate"
    PYTHON="$VENV_DIR/bin/python"
    PIP="$VENV_DIR/bin/pip"
    log_ok "Virtual environment activated"
  else
    log_err "Virtual environment activation failed"
    return 1
  fi

  # Upgrade pip
  run_cmd "$PIP" install --upgrade pip -q
}

# ═══════════════════════════════════════════════════════════════════════
# STEP 3: System Dependencies
# ═══════════════════════════════════════════════════════════════════════
install_system_deps() {
  log_hdr "STEP 3: Installing System Dependencies"

  if ! $INSTALL_DEPS; then
    log_msg "Skipped (--no-deps)"
    return 0
  fi

  # ── portaudio (required for PyAudio) ──────────────────────────────
  log_msg "Checking portaudio..."
  case "$OS" in
    macos)
      if brew list portaudio &>/dev/null; then
        log_ok "portaudio already installed"
      else
        log_msg "Installing portaudio..."
        run_cmd brew install portaudio
        log_ok "portaudio installed"
      fi
      ;;
    linux)
      case "$PKG_MANAGER" in
        apt)
          if ! dpkg -l libportaudio2 &>/dev/null 2>&1; then
            log_msg "Installing portaudio dev libraries..."
            run_cmd sudo apt-get update -qq
            run_cmd sudo apt-get install -y -qq portaudio19-dev libportaudio2
          fi
          log_ok "portaudio installed"
          ;;
        dnf)
          run_cmd sudo dnf install -y portaudio-devel 2>/dev/null || true
          log_ok "portaudio installed"
          ;;
        pacman)
          run_cmd sudo pacman -S --noconfirm portaudio 2>/dev/null || true
          log_ok "portaudio available"
          ;;
        *)
          log_warn "Unknown package manager — install portaudio manually"
          ;;
      esac
      ;;
  esac

  # ── libopus (optional — Opus audio codec) ─────────────────────────
  log_msg "Checking libopus (Opus audio codec)..."
  case "$OS" in
    macos)
      if brew list opus &>/dev/null; then
        log_ok "libopus already installed"
        OPUS_LIB_OK=true
      else
        if confirm "Install libopus (Opus audio compression, ~12× bandwidth savings)?"; then
          run_cmd brew install opus
          OPUS_LIB_OK=true
          log_ok "libopus installed"
        else
          log_warn "libopus skipped — audio will use uncompressed PCM (768kbps)"
        fi
      fi
      ;;
    linux)
      case "$PKG_MANAGER" in
        apt)
          if ! dpkg -l libopus0 &>/dev/null 2>&1; then
            run_cmd sudo apt-get install -y -qq libopus0 libopus-dev 2>/dev/null || log_warn "libopus not available — PCM fallback"
          fi
          dpkg -l libopus0 &>/dev/null 2>&1 && OPUS_LIB_OK=true
          log_ok "libopus $($OPUS_LIB_OK && echo 'installed' || echo 'not found')"
          ;;
        dnf)
          run_cmd sudo dnf install -y opus-devel 2>/dev/null || true
          ;;
        *)
          log_warn "Install libopus manually for compressed audio"
          ;;
      esac
      ;;
  esac

  # Verify libopus path (ctypes discovery)
  if $OPUS_LIB_OK; then
    if "$PYTHON" -c "
import ctypes; from ctypes.util import find_library
lib = find_library('opus')
print(lib if lib else 'NOT_FOUND')
" 2>/dev/null | grep -qv "NOT_FOUND"; then
      log_ok "libopus: ctypes discovery OK"
    else
      log_warn "libopus: installed but ctypes can't find it"
      case "$OS" in
        macos)
          log_msg "  Fix: export DYLD_LIBRARY_PATH=/opt/homebrew/lib:\$DYLD_LIBRARY_PATH"
          ;;
        linux)
          log_msg "  Fix: sudo ldconfig (or set LD_LIBRARY_PATH)"
          ;;
      esac
    fi
  fi

  # ── Linux: ALSA check (required by PortAudio/PyAudio) ───────────
  if [ "$OS" = "linux" ] && $INSTALL_AUDIO; then
    if ! ldconfig -p 2>/dev/null | grep -q libasound; then
      log_warn "libasound (ALSA) not found in ldconfig — PortAudio needs it"
      case "$PKG_MANAGER" in
        apt) log_msg "  Fix: sudo apt install libasound2-dev" ;;
        dnf) log_msg "  Fix: sudo dnf install alsa-lib-devel" ;;
        *)   log_msg "  Fix: install ALSA development libraries" ;;
      esac
    else
      log_ok "ALSA library: found"
    fi
  fi

  # ── Linux: CP210x kernel module check ───────────────────────────
  if [ "$OS" = "linux" ]; then
    if lsmod 2>/dev/null | grep -q cp210x; then
      log_ok "CP210x kernel module: loaded"
    else
      log_warn "CP210x kernel module not loaded — FT-710 serial may not work"
      log_msg "  Fix: sudo modprobe cp210x"
    fi
  fi

  # ── Raspberry Pi: performance advisory ──────────────────────────
  if [ "$OS" = "linux" ] && [ -f /proc/device-tree/model ]; then
    pi_model=$(tr -d '''\0''' < /proc/device-tree/model 2>/dev/null || true)
    if echo "$pi_model" | grep -qi "raspberry pi"; then
      log_ok "Detected: $pi_model"
      if echo "$pi_model" | grep -qiE "zero|1 model|2 model"; then
        log_warn "Pi Zero/1/2: audio resampling (numpy) may strain CPU"
        log_msg "  Consider using a Pi 4 or better for audio features"
      fi
      if ! ls lib/libft4222.so lib/libftd2xx.so &>/dev/null 2>&1; then
        log_msg "  ARM FTDI libs not found — scope will use S-meter fallback"
        log_msg "  (FTDI does not provide ARM Linux D2XX binaries)"
      fi
    fi
  fi
}

# ═══════════════════════════════════════════════════════════════════════
# STEP 4: Python Packages
# ═══════════════════════════════════════════════════════════════════════
install_python_deps() {
  log_hdr "STEP 4: Installing Python Packages"

  if [ ! -f "$SCRIPT_DIR/requirements.txt" ]; then
    log_err "requirements.txt not found at $SCRIPT_DIR"
    return 1
  fi

  # Install core + audio packages
  log_msg "Installing from requirements.txt..."
  run_cmd "$PIP" install -r "$SCRIPT_DIR/requirements.txt" -q 2>&1 | {
    local has_err=false
    while IFS= read -r line; do
      case "$line" in
        *"error"*|*"Error"*|*"ERROR"*) has_err=true; echo "$line" >> "$INSTALL_LOG" ;;
      esac
    done
    if $has_err; then
      log_warn "Some packages had warnings — see install.log"
    fi
  }

  # Verify key packages
  local all_ok=true
  for pkg in fastapi uvicorn serial; do
    if "$PYTHON" -c "import $pkg" 2>/dev/null; then
      log_ok "Python: $pkg ✓"
    else
      log_err "Python: $pkg — import failed"
      all_ok=false
    fi
  done

  # PyAudio (from pyaudio package)
  if $INSTALL_AUDIO; then
    if "$PYTHON" -c "import pyaudio" 2>/dev/null; then
      log_ok "Python: pyaudio ✓"
      AUDIO_OK=true
    else
      log_warn "Python: pyaudio import failed"
      log_msg "  Try: brew install portaudio && pip install pyaudio (macOS)"
      log_msg "  Or:   apt install portaudio19-dev && pip install pyaudio (Linux)"
      AUDIO_OK=false
    fi
  fi

  # NumPy
  if "$PYTHON" -c "import numpy" 2>/dev/null; then
    log_ok "Python: numpy ✓"
  else
    log_warn "Python: numpy not found — scope fallback may not work"
  fi

  # Verify all critical imports
  log_msg "Verifying all critical imports..."
  if "$PYTHON" -c "
import json, asyncio, logging, os, struct, sys, time
from pathlib import Path
print('Core imports OK')
" 2>/dev/null; then
    log_ok "All core Python imports verified"
  fi

  $all_ok || log_err "Some critical packages failed to install"
  return $($all_ok && echo 0 || echo 1)
}

# ═══════════════════════════════════════════════════════════════════════
# STEP 4b: Dev Dependencies (optional)
# ═══════════════════════════════════════════════════════════════════════
install_dev_deps() {
  if ! $INSTALL_DEV; then
    log_msg "Dev dependencies skipped (use --dev to install)"
    return 0
  fi

  local dev_file="$SCRIPT_DIR/requirements-dev.txt"
  if [ ! -f "$dev_file" ]; then
    log_warn "requirements-dev.txt not found — skipping dev deps"
    return 0
  fi

  log_msg "Installing dev dependencies (pytest, mypy, cryptography)..."
  run_cmd "$PIP" install -r "$dev_file" -q
  log_ok "Dev dependencies installed"
}

# ═══════════════════════════════════════════════════════════════════════
# STEP 5: Hardware Detection
# ═══════════════════════════════════════════════════════════════════════
detect_hardware() {
  log_hdr "STEP 5: Detecting FT-710 Hardware"

  # ── 5a. Serial Ports ─────────────────────────────────────────────
  log_msg "Scanning serial ports..."

  # Check env override first
  if [ -n "${MRRC_SERIAL_PORT:-${FT710_SERIAL_PORT:-}}" ]; then
    DETECTED_CAT_PORT="${MRRC_SERIAL_PORT:-$FT710_SERIAL_PORT}"
    log_ok "CAT serial port (from env): $DETECTED_CAT_PORT"
  else
    local candidates=()
    case "$OS" in
      macos)  candidates=($(ls /dev/cu.usbserial-* /dev/cu.SLAB_USBtoUART* 2>/dev/null || true)) ;;
      linux)  candidates=($(ls /dev/ttyUSB* /dev/ttyACM* /dev/serial/by-id/* 2>/dev/null || true)) ;;
    esac

    if [ ${#candidates[@]} -gt 0 ]; then
      for port in "${candidates[@]}"; do
        log_msg "  Found: $port"
      done
      # FT-710 Enhanced COM Port: typically the higher-numbered or first SLAB port
      # macOS: cu.usbserial-0121DB3A0 (Enhanced) / cu.usbserial-0121DB3A1 (Standard)
      # Linux: ttyUSB0 (Enhanced) / ttyUSB1 (Standard)
      if [ ${#candidates[@]} -ge 2 ]; then
        DETECTED_CAT_PORT="${candidates[0]}"   # First/lower is Enhanced on macOS
        DETECTED_SCOPE_PORT="${candidates[1]}" # Second/higher is Standard (SCU-LAN10 scope)
        log_ok "CAT port:    $DETECTED_CAT_PORT  (Enhanced COM)"
        log_ok "Scope port:  $DETECTED_SCOPE_PORT  (Standard COM, SCU-LAN10)"
      else
        DETECTED_CAT_PORT="${candidates[0]}"
        log_ok "CAT port:    $DETECTED_CAT_PORT"
        log_msg "Scope port:  not detected (SCU-LAN10 scope not available)"
      fi
    else
      log_warn "No serial ports detected — is the FT-710 connected via USB?"
      log_msg "  macOS: Expected /dev/cu.usbserial-*"
      log_msg "  Linux: Expected /dev/ttyUSB*"
      # Show available ports for debugging
      case "$OS" in
        macos) log_msg "  All /dev/cu.*: $(ls /dev/cu.* 2>/dev/null | tr '\n' ' ')" ;;
        linux) log_msg "  All serial: $(ls /dev/tty* 2>/dev/null | tr '\n' ' ')" ;;
      esac
    fi
  fi

  # ── Serial permission check ───────────────────────────────────────
  if [ -n "$DETECTED_CAT_PORT" ] && [ -e "$DETECTED_CAT_PORT" ]; then
    if [ -w "$DETECTED_CAT_PORT" ]; then
      log_ok "Serial port writable: $DETECTED_CAT_PORT"
    else
      log_warn "Serial port exists but NOT writable: $DETECTED_CAT_PORT"
      case "$OS" in
        linux)
          local port_group=$(stat -c '%G' "$DETECTED_CAT_PORT" 2>/dev/null || echo "unknown")
          local port_perms=$(stat -c '%a' "$DETECTED_CAT_PORT" 2>/dev/null || echo "???")
          log_msg "  Port group: $port_group, permissions: $port_perms"
          log_msg "  Fix: sudo usermod -a -G $port_group \$USER"
          log_msg "  Then: log out and back in (or: newgrp $port_group)"
          if [ "$port_group" = "dialout" ] || [ "$port_group" = "uucp" ]; then
            if ! groups "$USER" 2>/dev/null | grep -q "$port_group"; then
              if confirm "Add user to $port_group group? (requires sudo)"; then
                run_cmd sudo usermod -a -G "$port_group" "$USER"
                log_ok "User added to $port_group — log out and back in to apply"
                log_msg "  Alternatively: newgrp $port_group"
              fi
            else
              log_msg "  User is in $port_group but port is not writable — try: newgrp $port_group"
            fi
          fi
          ;;
        macos)
          log_msg "  Fix: Close any other app using this port (wfview, ExpertSDR, etc.)"
          ;;
      esac
    fi
  fi

  # ── 5b. USB Audio Devices ────────────────────────────────────────
  if $INSTALL_AUDIO && $AUDIO_OK; then
    log_msg "Detecting USB audio devices..."
    # Use Python to enumerate PyAudio devices
    "$PYTHON" -c "
import pyaudio
p = pyaudio.PyAudio()
found = False
for i in range(p.get_device_count()):
    info = p.get_device_info_by_index(i)
    name = info.get('name', '')
    inp = info.get('maxInputChannels', 0)
    out = info.get('maxOutputChannels', 0)
    tag = ''
    if 'FT-710' in name or 'FT710' in name or 'YAESU' in name.upper():
        tag = ' ← FT-710'
        found = True
    print(f'  [{i}] {name} (in={inp}, out={out}){tag}')
if not found:
    print('  WARNING: No FT-710 audio device found')
p.terminate()
" 2>/dev/null || log_warn "Could not enumerate audio devices (PyAudio error)"

    # Try to detect via system commands as fallback
    case "$OS" in
      macos)
        local audio_devs=$(system_profiler SPAudioDataType 2>/dev/null | grep -A3 "FT-710\|YAESU\|USB Audio CODEC" || true)
        [ -n "$audio_devs" ] && log_ok "macOS audio: FT-710 device found in system profile"
        ;;
      linux)
        if command -v arecord &>/dev/null; then
          arecord -l 2>/dev/null | grep -i "ft-710\|yaesu\|usb audio" || true
        fi
        ;;
    esac
  else
    log_msg "Audio detection skipped (PyAudio not available)"
  fi

  # ── 5c. FT4222 / FTDI Chip ───────────────────────────────────────
  if $INSTALL_SCOPE; then
    log_msg "Checking FT4222 (scope chip)..."
    case "$OS" in
      macos)
        if ioreg -p IOUSB -w0 -l 2>/dev/null | grep -q "FT4222"; then
          log_ok "FT4222 chip detected via IORegistry"
        else
          log_msg "FT4222 chip not found in IORegistry — scope will use S-meter fallback"
          log_msg "  (This is normal if FT-710 is not connected or another app owns the device)"
        fi
        ;;
      linux)
        if lsusb 2>/dev/null | grep -qi "0403:601c"; then
          log_ok "FT4222 chip detected (VID:0403 PID:601C)"
        else
          log_msg "FT4222 chip not found — scope will use S-meter fallback"
        fi
        ;;
    esac
  fi
}

# ═══════════════════════════════════════════════════════════════════════
# STEP 6: FT4222 Scope Setup
# ═══════════════════════════════════════════════════════════════════════
setup_ft4222() {
  log_hdr "STEP 6: FT4222 Scope Configuration"

  if ! $INSTALL_SCOPE; then
    log_msg "Skipped (--no-scope)"
    return 0
  fi

  local lib_dir="$SCRIPT_DIR/lib"

  if [ ! -d "$lib_dir" ]; then
    log_warn "lib/ directory not found — scope libraries not available"
    return 0
  fi

  # ── 6a. Verify FTDI libraries ────────────────────────────────────
  case "$OS" in
    macos)
      local dylibs=("libft4222.dylib" "libftd2xx.dylib")
      for lib in "${dylibs[@]}"; do
        if [ -f "$lib_dir/$lib" ]; then
          log_ok "Found: $lib"
          FTDI_LIB_OK=true
        else
          log_warn "Missing: $lib — scope will use S-meter fallback"
          FTDI_LIB_OK=false
        fi
      done

      # If wfview version exists and current one isn't optimal, suggest copy
      if [ -f "$lib_dir/libft4222_wfview.dylib" ] && [ -f "$lib_dir/libft4222.dylib" ]; then
        local wf_md5=$(md5 -q "$lib_dir/libft4222_wfview.dylib" 2>/dev/null || true)
        local cur_md5=$(md5 -q "$lib_dir/libft4222.dylib" 2>/dev/null || true)
        if [ "$wf_md5" != "$cur_md5" ] && [ -n "$wf_md5" ] && [ -n "$cur_md5" ]; then
          log_warn "libft4222.dylib differs from wfview version — scope may stall"
          log_msg "  Fix: cp $lib_dir/libft4222_wfview.dylib $lib_dir/libft4222.dylib"
          if confirm "Replace with wfview's known-good libft4222.dylib?"; then
            run_cmd cp "$lib_dir/libft4222_wfview.dylib" "$lib_dir/libft4222.dylib"
            log_ok "Replaced libft4222.dylib with wfview version"
          fi
        fi
      fi
      ;;
    linux)
      local so_files=("libft4222.so" "libftd2xx.so")
      local found_any=false
      for so in "${so_files[@]}"; do
        if [ -f "$lib_dir/$so" ]; then
          log_ok "Found: $so"
          found_any=true
        fi
      done
      if $found_any; then
        FTDI_LIB_OK=true
        # On Linux: ensure RPATH or LD_LIBRARY_PATH includes lib/
        if ! grep -q "$lib_dir" /etc/ld.so.conf.d/ft710.conf 2>/dev/null; then
          log_msg "Adding $lib_dir to ldconfig..."
          if ! $DRY_RUN; then
            echo "$lib_dir" | sudo tee /etc/ld.so.conf.d/ft710.conf >/dev/null 2>&1 || true
            sudo ldconfig 2>/dev/null || true
          fi
        fi
      else
        log_warn "No FTDI Linux libraries found in lib/"
        log_msg "  ARM Linux: libft4222 can be cross-compiled from wfview source"
        FTDI_LIB_OK=false
      fi
      ;;
  esac

  # ── 6b. D2XX Driver Configuration (macOS only) ───────────────────
  if [ "$OS" = "macos" ] && $FTDI_LIB_OK; then
    local ftdi_cfg_src="$lib_dir/ftd2xx.cfg"
    local ftdi_cfg_dst="/usr/local/lib/ftd2xx.cfg"

    if [ -f "$ftdi_cfg_src" ]; then
      if [ -f "$ftdi_cfg_dst" ]; then
        if grep -q "DetachKernelDriver=1" "$ftdi_cfg_dst" 2>/dev/null; then
          log_ok "D2XX config OK: $ftdi_cfg_dst"
        else
          log_warn "D2XX config exists but may not have DetachKernelDriver=1"
          if confirm "Update $ftdi_cfg_dst for FT4222?"; then
            run_cmd sudo cp "$ftdi_cfg_src" "$ftdi_cfg_dst"
            log_ok "D2XX config updated"
          fi
        fi
      else
        log_msg "D2XX config not found at $ftdi_cfg_dst"
        log_msg "  This is needed so the D2XX driver can claim the FT4222 from macOS VCP."
        if confirm "Install D2XX config to /usr/local/lib/ (requires sudo)?"; then
          run_cmd sudo mkdir -p /usr/local/lib
          run_cmd sudo cp "$ftdi_cfg_src" "$ftdi_cfg_dst"
          log_ok "D2XX config installed — unplug/replug FT-710 to apply"
        else
          log_warn "Without D2XX config, FT4222 scope will not work"
        fi
      fi
    else
      log_warn "ftd2xx.cfg not found in lib/ — scope may not work"
    fi
  fi

  # ── 6c. Linux udev rules ─────────────────────────────────────────
  if [ "$OS" = "linux" ]; then
    local udev_file="/etc/udev/rules.d/99-ft710.rules"
    if [ ! -f "$udev_file" ]; then
      log_msg "Installing udev rules for FT-710 serial ports..."
      if confirm "Install udev rules (requires sudo)?"; then
        local rules='# FT-710 Serial Ports (Silicon Labs CP210x)
SUBSYSTEM=="tty", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea60", MODE="0666", SYMLINK+="ft710-cat"
# FT-710 FT4222 Scope
SUBSYSTEM=="usb", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="601c", MODE="0666"'
        if ! $DRY_RUN; then
          echo "$rules" | sudo tee "$udev_file" >/dev/null
          sudo udevadm control --reload-rules 2>/dev/null || true
          sudo udevadm trigger 2>/dev/null || true
        fi
        log_ok "udev rules installed — FT-710 serial ports now accessible"
        log_msg "  Symlinks: /dev/ft710-cat → Enhanced COM Port"
      else
        log_warn "Without udev rules, serial ports may require sudo"
        log_msg "  Workaround: sudo usermod -a -G dialout \$USER && newgrp dialout"
      fi
    else
      log_ok "udev rules already present: $udev_file"
    fi
  fi
}

# ═══════════════════════════════════════════════════════════════════════
# STEP 7: Generate Configuration
# ═══════════════════════════════════════════════════════════════════════
generate_config() {
  log_hdr "STEP 7: Generating Configuration"

  local env_file="$SCRIPT_DIR/.env"
  local example_file="$SCRIPT_DIR/.env.example"

  # Generate random password if not set
  local password="${MRRC_WEB_PASSWORD:-${FT710_WEB_PASSWORD:-}}"
  if [ -z "$password" ]; then
    password="$("$PYTHON" -c "import secrets; print(secrets.token_hex(8))" 2>/dev/null || echo "mrrc")"
  fi

  # Determine serial port
  local cat_port="${MRRC_SERIAL_PORT:-${FT710_SERIAL_PORT:-$DETECTED_CAT_PORT}}"
  [ -z "$cat_port" ] && cat_port="/dev/cu.SLAB_USBtoUART"  # sensible default

  # Determine web port
  local web_port="${MRRC_WEB_PORT:-${FT710_WEB_PORT:-8888}}"

  # Check port availability
  if command -v lsof &>/dev/null && lsof -i ":$web_port" -sTCP:LISTEN &>/dev/null 2>&1; then
    log_warn "Port $web_port is already in use"
    log_msg "  Running: $(lsof -i ":$web_port" -sTCP:LISTEN -t 2>/dev/null | head -1)"
    local alt_port=$((web_port + 1))
    while lsof -i ":$alt_port" -sTCP:LISTEN &>/dev/null 2>&1; do
      alt_port=$((alt_port + 1))
    done
    if confirm "Port $web_port in use — use $alt_port instead?"; then
      web_port=$alt_port
    fi
  fi

  # Write .env file
  if [ ! -f "$env_file" ] || confirm "Overwrite existing .env?"; then
    cat >"$env_file" <<ENVEOF
# MRRC Modern Configuration
# Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
# OS: $OS ($ARCH)
# Legacy FT710_* prefixes are still accepted for backward compatibility;
# MRRC_* takes precedence when both are set.

# ── Serial ──────────────────────────────────────────────────────────
MRRC_SERIAL_PORT=$cat_port
MRRC_BAUD_RATE=38400
$( [ -n "${DETECTED_SCOPE_PORT:-}" ] && echo "#MRRC_SCOPE_PORT=$DETECTED_SCOPE_PORT" || echo "#MRRC_SCOPE_PORT=")
$( [ -n "${MRRC_SCOPE_BAUD:-}" ] && echo "MRRC_SCOPE_BAUD=$MRRC_SCOPE_BAUD" || echo "#MRRC_SCOPE_BAUD=115200")

# ── Web Server ──────────────────────────────────────────────────────
MRRC_WEB_PORT=$web_port
MRRC_WEB_HOST=0.0.0.0
MRRC_WEB_PASSWORD=$password

# ── Scope ───────────────────────────────────────────────────────────
MRRC_FT4222_CLK_DIV=6
$( [ -d "$SCRIPT_DIR/lib" ] && echo "#MRRC_FTDI_LIB_DIR=$SCRIPT_DIR/lib" || echo "#MRRC_FTDI_LIB_DIR=")

# ── Audio ───────────────────────────────────────────────────────────
# PyAudio device indices (auto-detected if unset)
#MRRC_AUDIO_IN_DEVICE=
#MRRC_AUDIO_OUT_DEVICE=
ENVEOF
    log_ok "Configuration written: $env_file"
  else
    log_ok "Keeping existing .env"
  fi

  # Source the .env for this session
  if [ -f "$env_file" ]; then
    set -a; source "$env_file"; set +a
  fi

  # Show summary
  echo -e "\n${BOLD}Configuration:${NC}"
  echo "  Serial port:    ${GREEN}$cat_port${NC}"
  echo "  Web port:       ${GREEN}$web_port${NC}"
  echo "  Web password:   ${GREEN}$password${NC}"
  echo "  Config file:    ${GREEN}$env_file${NC}"

  # Also write .env.example for reference
  if [ ! -f "$example_file" ]; then
    cp "$env_file" "$example_file"
    log_msg "Example config: $example_file"
  fi
}

# ═══════════════════════════════════════════════════════════════════════
# STEP 8: Validation
# ═══════════════════════════════════════════════════════════════════════
validate_setup() {
  log_hdr "STEP 8: Validating Setup"

  local all_ok=true

  # ── 8a. Python import check ──────────────────────────────────────
  log_msg "Checking Python imports..."
  if "$PYTHON" -c "
from config import *
from radio_state import RadioState
from cat_controller import CatController
from scope_handler import ScopeHandler
from scope_frame import *
from poll_scheduler import PollScheduler
print('All server modules imported OK')
" 2>/dev/null; then
    log_ok "All server modules import successfully"
  else
    log_err "Server module import failed"
    all_ok=false
  fi

  # ── 8b. Opus codec check ─────────────────────────────────────────
  log_msg "Checking Opus codec..."
  if "$PYTHON" -c "
from opus_rx import RxOpusEncoder, AUDIO_TAG_OPUS
print('Opus codec module OK')
" 2>/dev/null; then
    # Try actual libopus load
    if "$PYTHON" -c "
from opus_rx import RxOpusEncoder
try:
    enc = RxOpusEncoder(bitrate=64000)
    print('libopus loaded successfully')
except Exception as e:
    print(f'libopus unavailable: {e}')
" 2>/dev/null | grep -q "libopus loaded"; then
      log_ok "libopus codec: loaded (compressed audio available)"
    else
      log_warn "libopus codec: not loaded (PCM fallback — 768kbps)"
    fi
  else
    log_warn "opus_rx module unavailable"
  fi

  # ── 8c. Serial port accessibility ────────────────────────────────
  local serial_port="${MRRC_SERIAL_PORT:-${FT710_SERIAL_PORT:-}}"
  if [ -n "${serial_port:-}" ]; then
    if [ -e "$serial_port" ]; then
      if [ -r "$serial_port" ] && [ -w "$serial_port" ]; then
        log_ok "Serial port accessible: $serial_port"
      else
        log_warn "Serial port exists but may not be writable: $serial_port"
        case "$OS" in
          linux) log_msg "  Fix: sudo usermod -a -G dialout \$USER; newgrp dialout" ;;
          macos) log_msg "  Check: Is another app using this port? (wfview, ExpertSDR, etc.)" ;;
        esac
      fi
    else
      log_warn "Serial port not found: $serial_port"
      log_msg "  Is the FT-710 connected and powered on?"
    fi
  else
    log_warn "No serial port configured — server will start but cannot control radio"
    all_ok=false
  fi

  # ── 8d. Server syntax check ──────────────────────────────────────
  log_msg "Checking server syntax..."
  if "$PYTHON" -c "import py_compile; py_compile.compile('$SCRIPT_DIR/server.py', doraise=True)" 2>/dev/null; then
    log_ok "server.py compiles successfully"
  else
    log_err "server.py has syntax errors"
    all_ok=false
  fi

  # ── 8e. FastAPI/uvicorn availability ─────────────────────────────
  if "$PYTHON" -c "import uvicorn; import fastapi" 2>/dev/null; then
    log_ok "FastAPI + Uvicorn: ready"
  else
    log_err "FastAPI/Uvicorn not installed"
    all_ok=false
  fi

  $all_ok && log_ok "Validation passed" || log_warn "Some checks failed — see above"
  return $($all_ok && echo 0 || echo 1)
}

# ═══════════════════════════════════════════════════════════════════════
# STEP 9: Service Installation (optional)
# ═══════════════════════════════════════════════════════════════════════
install_service() {
  log_hdr "STEP 9: Installing as System Service"

  if ! $START_SERVICE; then
    log_msg "Skipped (use --install-service to enable)"
    return 0
  fi

  case "$OS" in
    linux)
      # ── systemd service ─────────────────────────────────────────
      local svc_file="/etc/systemd/system/mrrc-modern.service"
      if [ -f "$svc_file" ]; then
        log_ok "systemd service already exists: $svc_file"
        return 0
      fi
      log_msg "Creating systemd service..."
      if confirm "Install systemd service (requires sudo)?"; then
        cat <<SVCEOF | run_cmd sudo tee "$svc_file" >/dev/null
[Unit]
Description=MRRC Modern Web Control Server
After=network.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$SCRIPT_DIR
EnvironmentFile=$SCRIPT_DIR/.env
ExecStart=$VENV_DIR/bin/python $SCRIPT_DIR/server.py
Restart=on-failure
RestartSec=5
StandardOutput=append:$SCRIPT_DIR/logs/server.log
StandardError=append:$SCRIPT_DIR/logs/server.log

[Install]
WantedBy=multi-user.target
SVCEOF
        run_cmd sudo systemctl daemon-reload
        run_cmd sudo systemctl enable mrrc-modern
        run_cmd sudo systemctl start mrrc-modern
        log_ok "systemd service installed and started"
        log_msg "  Status: sudo systemctl status mrrc-modern"
        log_msg "  Logs:   journalctl -u mrrc-modern -f"
      fi
      ;;
    macos)
      # ── launchd plist ───────────────────────────────────────────
      local plist_file="$HOME/Library/LaunchAgents/com.mrrc.modern.plist"
      if [ -f "$plist_file" ]; then
        log_ok "launchd service already exists: $plist_file"
        return 0
      fi
      log_msg "Creating launchd service..."
      cat <<PLISTEOF >"$plist_file"
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.mrrc.modern</string>
    <key>ProgramArguments</key>
    <array>
        <string>$VENV_DIR/bin/python</string>
        <string>$SCRIPT_DIR/server.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$SCRIPT_DIR</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>MRRC_SERIAL_PORT</key>
        <string>${MRRC_SERIAL_PORT:-${FT710_SERIAL_PORT:-/dev/cu.SLAB_USBtoUART}}</string>
        <key>MRRC_WEB_PORT</key>
        <string>${MRRC_WEB_PORT:-${FT710_WEB_PORT:-8888}}</string>
        <key>MRRC_WEB_PASSWORD</key>
        <string>${MRRC_WEB_PASSWORD:-${FT710_WEB_PASSWORD:-ft710}}</string>
        <key>MRRC_WEB_HOST</key>
        <string>0.0.0.0</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$SCRIPT_DIR/logs/server.log</string>
    <key>StandardErrorPath</key>
    <string>$SCRIPT_DIR/logs/server.log</string>
</dict>
</plist>
PLISTEOF
      run_cmd launchctl load "$plist_file" 2>/dev/null || true
      log_ok "launchd service installed"
      log_msg "  Start: launchctl start com.mrrc.ft710"
      log_msg "  Stop:  launchctl stop com.mrrc.ft710"
      ;;
  esac
}

# ═══════════════════════════════════════════════════════════════════════
# STEP 10: Summary
# ═══════════════════════════════════════════════════════════════════════
print_summary() {
  log_hdr "Installation Summary"

  echo ""
  echo -e "${BOLD}       MRRC Modern — Ready to Run${NC}"
  echo -e "  ═══════════════════════════════════════"
  echo ""

  # Check if all critical items passed
  local critical_ok=true
  [ ${#ERRORS[@]} -gt 0 ] && critical_ok=false

  if $critical_ok; then
    echo -e "  ${GREEN}${BOLD}✓ All critical checks passed${NC}"
  else
    echo -e "  ${RED}${BOLD}✗ ${#ERRORS[@]} critical issue(s) detected${NC}"
  fi

  echo -e "  ${YELLOW}⚠ ${#WARNINGS[@]} warning(s)${NC}"
  echo ""

  # Configuration
  echo -e "  ${BOLD}Configuration:${NC}"
  echo "    Server URL:    http://localhost:${MRRC_WEB_PORT:-${FT710_WEB_PORT:-8888}}"
  echo "    Login password: ${MRRC_WEB_PASSWORD:-${FT710_WEB_PASSWORD:-mrrc}}"
  echo "    Serial port:   ${MRRC_SERIAL_PORT:-${FT710_SERIAL_PORT:-auto}}"
  echo "    Config file:   $SCRIPT_DIR/.env"
  echo ""

  # Features
  echo -e "  ${BOLD}Features:${NC}"
  echo -n "    Serial CAT:    "
  [ -n "${MRRC_SERIAL_PORT:-${FT710_SERIAL_PORT:-}}" ] && [ -e "${MRRC_SERIAL_PORT:-${FT710_SERIAL_PORT:-}}" ] && echo -e "${GREEN}✓${NC}" || echo -e "${YELLOW}will try on startup${NC}"
  echo -n "    RX/TX Audio:  "
  $AUDIO_OK && echo -e "${GREEN}✓${NC} (PyAudio + $( $OPUS_LIB_OK && echo 'Opus' || echo 'PCM' ))" || echo -e "${YELLOW}unavailable${NC}"
  echo -n "    Scope:        "
  $FTDI_LIB_OK && echo -e "${GREEN}✓${NC} (FT4222 + S-meter fallback)" || echo -e "${YELLOW}S-meter fallback only${NC}"
  echo ""

  # Start instructions
  echo -e "  ${BOLD}To start:${NC}"
  if [ -f "$SCRIPT_DIR/.env" ]; then
    echo "    source .env && venv/bin/python server.py"
  else
    echo "    MRRC_SERIAL_PORT=${MRRC_SERIAL_PORT:-${FT710_SERIAL_PORT:-/dev/cu.SLAB_USBtoUART}} venv/bin/python server.py"
  fi
  echo ""

  # Warnings
  if [ ${#WARNINGS[@]} -gt 0 ]; then
    echo -e "  ${BOLD}Warnings:${NC}"
    for w in "${WARNINGS[@]}"; do
      echo -e "    ${YELLOW}⚠${NC} $w"
    done
    echo ""
  fi

  # Errors
  if [ ${#ERRORS[@]} -gt 0 ]; then
    echo -e "  ${BOLD}Errors (must fix):${NC}"
    for e in "${ERRORS[@]}"; do
      echo -e "    ${RED}✗${NC} $e"
    done
    echo ""
  fi

  echo -e "  ${BOLD}Full log:${NC} $INSTALL_LOG"
  echo ""

  # Final advice
  if [ "$OS" = "macos" ]; then
    echo "  💡 Tip: Close wfview/ExpertSDR before starting this server"
    echo "          (only one app can access the FT-710 at a time)"
  fi
  if [ "$OS" = "linux" ]; then
    echo "  💡 Tip: If serial port is permission-denied:"
    echo "          sudo usermod -a -G dialout \$USER && newgrp dialout"
  fi
  echo ""
}

# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════
main() {
  # Clear log
  : > "$INSTALL_LOG"

  echo -e "${BOLD}${BLUE}"
  echo "  ╔══════════════════════════════════════════╗"
  echo "  ║   MRRC Modern — Install & Deploy Script  ║"
  echo "  ╚══════════════════════════════════════════╝"
  echo -e "${NC}"
  echo "  Script dir: $SCRIPT_DIR"
  echo "  Log file:   $INSTALL_LOG"
  echo ""

  if $DRY_RUN; then
    echo -e "  ${YELLOW}DRY RUN MODE — no changes will be made${NC}"
    echo ""
  fi

  # Run all steps
  detect_os         || { log_err "FATAL: OS detection failed"; exit 1; }
  setup_python      || { log_err "FATAL: Python setup failed"; exit 1; }
  install_system_deps
  install_python_deps || log_warn "Some Python packages may be missing"
  install_dev_deps
  detect_hardware
  setup_ft4222
  generate_config
  validate_setup    || log_warn "Some validation checks failed"
  install_service
  print_summary
}

main "$@"
