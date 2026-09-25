#!/usr/bin/env bash
# =============================================================================
# Love Refactored — one-shot installer (v0.2, 2026-09-25)
#
# Supported: Ubuntu 24.04 inside WSL2 (Windows), Ubuntu 24.04 (native Linux),
#            macOS with Homebrew.
# The WSL2 path is derived from a verified fresh install on 2026-09-23; macOS
# and native Linux are reasoned, not yet run.
#
# One line, nothing to download first:
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/Love-Encoded/LoveRefactored/main/install.sh)
#
# (or, from a checkout:  bash install.sh)
#
# Every step is checked. On the first failure the script stops and tells you
# what went wrong in plain words. It never prints "success" it hasn't verified.
# Re-running is safe: finished steps are detected and skipped.
#
# Overrides (env vars): LR_DIR (default ~/love-refactored)
#                       LR_REPO (default https://github.com/Love-Encoded/LoveRefactored.git)
#                       LR_SKIP_HEALTH=1  (skip the final start/stop smoke test)
#                       LR_ALLOW_ANY_OS=1 (bypass the Ubuntu 24.04 / macOS gate — unsupported)
# =============================================================================
set -euo pipefail

LR_DIR="${LR_DIR:-$HOME/love-refactored}"
LR_REPO="${LR_REPO:-https://github.com/Love-Encoded/LoveRefactored.git}"
LOG="$HOME/lr-install.log"

RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; YELLOW=$'\033[1;33m'; CYAN=$'\033[0;36m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
say()  { echo "${CYAN}→ $*${RESET}"; }
ok()   { echo "${GREEN}✓ $*${RESET}"; }
warn() { echo "${YELLOW}! $*${RESET}"; }
die()  { echo; echo "${RED}${BOLD}✗ STOPPED: $*${RESET}"; echo "${RED}  Full log: $LOG${RESET}"; exit 1; }

# Everything below is also captured to the log file.
exec > >(tee -a "$LOG") 2>&1
echo; echo "${BOLD}Love Refactored installer — $(date)${RESET}"; echo "Log: $LOG"; echo

# ---------------------------------------------------------------- 0. sanity
[ "$(id -u)" -eq 0 ] && die "Don't run this as root/sudo. Run it as your normal user; it will ask for your password when it needs sudo."
command -v curl >/dev/null 2>&1 || command -v apt-get >/dev/null 2>&1 || command -v brew >/dev/null 2>&1 || die "Need curl (or apt/brew) to begin."

# ---------------------------------------------------------------- 1. platform gate
OS="$(uname -s)"
IS_WSL=0; PLATFORM=""
if [ "$OS" = "Darwin" ]; then
  PLATFORM="mac"
elif [ "$OS" = "Linux" ]; then
  grep -qi microsoft /proc/version 2>/dev/null && IS_WSL=1
  if [ -r /etc/os-release ]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    if [ "${ID:-}" = "ubuntu" ] && [ "${VERSION_ID:-}" = "24.04" ]; then
      PLATFORM="ubuntu2404"
    elif [ "${LR_ALLOW_ANY_OS:-0}" = "1" ]; then
      warn "Unsupported Linux (${PRETTY_NAME:-unknown}) — continuing because LR_ALLOW_ANY_OS=1"; PLATFORM="ubuntu2404"
    else
      die "This is ${PRETTY_NAME:-an unknown Linux}. Supported: Ubuntu 24.04.
  On Windows: in PowerShell (as Administrator):
      wsl --unregister ${WSL_DISTRO_NAME:-Ubuntu}      (deletes this Linux install)
      wsl --install -d Ubuntu-24.04
  then re-run this installer inside the new Ubuntu-24.04."
    fi
  fi
else
  die "Unsupported OS: $OS"
fi
if [ "$IS_WSL" -eq 1 ]; then ok "Platform: Ubuntu 24.04 inside WSL2"; else ok "Platform: $PLATFORM"; fi

# Where are we? Cloning into /mnt/c/... (Windows drive) is slow and breaks native builds.
case "$LR_DIR" in /mnt/*) die "LR_DIR=$LR_DIR is on the Windows drive. Use the Linux home (default ~/love-refactored).";; esac
case "$PWD" in /mnt/c/WINDOWS*|/mnt/c/Windows*) warn "You're in the Windows system folder; that's fine, installing to $LR_DIR";; esac

# ---------------------------------------------------------------- 2. RAM check (WSL)
if [ "$OS" = "Linux" ]; then
  MEM_GB=$(awk '/MemTotal/ {printf "%d", $2/1024/1024}' /proc/meminfo)
  if [ "$MEM_GB" -lt 6 ]; then
    warn "Linux sees only ${MEM_GB} GB RAM. LR's memory stack needs ~4.5 GB with the default Whisper model."
    [ "$IS_WSL" -eq 1 ] && warn "WSL gives Linux half your RAM by default. If this fails with an out-of-memory kill, add a .wslconfig on Windows (see README)."
  else
    ok "RAM available to Linux: ${MEM_GB} GB"
  fi
fi

# ---------------------------------------------------------------- 3. prerequisites
if [ "$PLATFORM" = "mac" ]; then
  command -v brew >/dev/null 2>&1 || die "Homebrew is required on macOS. Install it from https://brew.sh then re-run."
  say "Installing prerequisites with Homebrew (git, python@3.12, ffmpeg, node@20)…"
  brew install git python@3.12 ffmpeg node@20 >/dev/null || die "brew install failed. Scroll up for the error."
  brew link --overwrite --force node@20 >/dev/null 2>&1 || true
  PY="$(brew --prefix python@3.12)/bin/python3.12"
else
  say "Installing prerequisites with apt (this asks for your password; lots of scrolling is normal;
   two red lines about 'system bus' are expected inside WSL)…"
  sudo apt-get update -y || die "apt-get update failed. Is the internet up?"
  sudo apt-get install -y git python3 python3-pip python3-venv ffmpeg build-essential curl lsof ca-certificates \
    || die "apt-get install failed. Scroll up for the red line."
  if ! command -v node >/dev/null 2>&1 || [ "$(node -v | cut -d. -f1)" != "v20" ]; then
    say "Installing Node 20 inside Linux (nodesource)…"
    curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash - >/dev/null || die "nodesource setup failed."
    sudo apt-get install -y nodejs || die "apt-get install nodejs failed."
  fi
  PY="python3"
fi
ok "Prerequisites installed"

# ---------------------------------------------------------------- 4. verify the toolchain is the RIGHT one
# On WSL, if Node wasn't installed in Linux, `npm` silently resolves to Windows' npm.exe via
# /mnt/c/... and every native build fails with UNC-path errors. Refuse to continue in that state.
NODE_PATH="$(command -v node || true)"; NPM_PATH="$(command -v npm || true)"
[ -n "$NODE_PATH" ] && [ -n "$NPM_PATH" ] || die "node/npm not found on PATH after install."
case "$NODE_PATH$NPM_PATH" in
  *"/mnt/c/"*|*"/mnt/d/"*) die "npm/node are resolving to WINDOWS binaries ($NPM_PATH). Node must be installed inside Ubuntu. The apt step above should have done that — scroll up for its error.";;
esac
NODE_MAJOR="$(node -v | cut -d. -f1)"
[ "$NODE_MAJOR" = "v20" ] || die "Node is $(node -v); this installer expects Node 20. (nvm or another Node on PATH?)"
ok "node $(node -v) at $NODE_PATH · npm $(npm -v)"

PYV="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
[ "$PYV" = "3.12" ] || die "Python is $PYV; this installer pins Python 3.12 (Ubuntu 24.04 / brew python@3.12)."
ok "Python $("$PY" --version 2>&1 | cut -d' ' -f2) at $PY"

for c in git ffmpeg lsof curl; do command -v "$c" >/dev/null 2>&1 || die "'$c' still missing after install."; done

# ---------------------------------------------------------------- 5. get the code
if [ -d "$LR_DIR/.git" ]; then
  say "Repo already at $LR_DIR — pulling latest (fast-forward only)…"
  ( cd "$LR_DIR" && git pull --ff-only ) || die "git pull failed (local changes?). cd $LR_DIR && git status to see. Or move the folder aside and re-run for a clean clone."
elif [ -e "$LR_DIR" ]; then
  die "$LR_DIR exists but isn't a git checkout. Move it aside (mv $LR_DIR ${LR_DIR}.old) and re-run."
else
  say "Cloning Love Refactored into $LR_DIR…"
  git clone "$LR_REPO" "$LR_DIR" || die "git clone failed. Check the internet and the repo URL."
fi
cd "$LR_DIR"
ok "Code at $LR_DIR ($(git rev-parse --short HEAD), $(git branch --show-current))"

# ---------------------------------------------------------------- 6. Node modules
if [ -f node_modules/.lr-install-ok ]; then
  ok "node_modules already built — skipping npm install"
else
  echo
  warn "npm install will look FROZEN for 2–5 minutes after a 'prebuild-install ... no longer maintained' warning."
  warn "It is compiling SQLite. Do NOT press Ctrl+C. (Ctrl+C in this terminal cancels the build.)"
  echo
  npm install --no-audit --no-fund || die "npm install failed. The real error is the first red 'npm error' block above."
  node -e 'require("better-sqlite3"); require("sharp"); console.log("native modules load OK")' \
    || die "npm finished but native modules (better-sqlite3 / sharp) don't load. That's the 'server dies silently' bug — report the line above."
  touch node_modules/.lr-install-ok
  ok "Node modules built and native modules verified"
fi

# ---------------------------------------------------------------- 7. Python venv + locked deps
if [ ! -x venv/bin/python ]; then
  say "Creating Python venv…"
  "$PY" -m venv venv || die "python -m venv failed."
fi
# shellcheck disable=SC1091
. venv/bin/activate
[ "$(python -c 'import sys;print("%d.%d"%sys.version_info[:2])')" = "3.12" ] || die "venv is not Python 3.12. Delete $LR_DIR/venv and re-run."
[ -f requirements-lock.txt ] || die "requirements-lock.txt is missing from the checkout — this repo revision predates the installer. git pull and re-run."
DEPS_OK=0
if python -c 'import torch, whisper, chromadb, sentence_transformers, flask_cors, fish_audio_sdk' 2>/dev/null; then
  TV="$(python -c 'import torch;print(torch.__version__)')"
  if [ "$TV" = "2.14.0+cpu" ] || [ "$PLATFORM" = "mac" ]; then DEPS_OK=1; fi
fi
if [ "$DEPS_OK" -eq 1 ]; then
  ok "Python deps already installed — skipping"
else
  echo
  warn "Installing pinned Python packages (~1 GB download, CPU torch — not the multi-GB CUDA stack). Quiet stretches are normal."
  echo
  pip install --upgrade pip >/dev/null || die "pip self-upgrade failed."
  pip install -r requirements-lock.txt || die "pip install failed. If the first red line mentions a version 'not found', the lock file needs updating for your platform — report it with the line."
  python -c 'import torch, whisper, chromadb, sentence_transformers, flask_cors, fish_audio_sdk, pydub' \
    || die "pip finished but a required module doesn't import (see traceback above)."
  ok "Python deps installed and verified (torch $(python -c 'import torch;print(torch.__version__)'))"
fi

# ---------------------------------------------------------------- 8. runtime config
chmod +x start.sh stop.sh
if [ ! -f .env ]; then
  cat > .env <<'EOF'
# Love Refactored local settings (loaded by start.sh)
# Whisper speech-to-text model. turbo ≈ 3 GB RAM, fast, near large-v3 quality.
# Options: tiny base small medium turbo large-v3   (large-v3 ≈ 6 GB RAM; needs a 32 GB machine under WSL)
WHISPER_MODEL=turbo
EOF
  ok "Wrote .env (WHISPER_MODEL=turbo)"
else
  grep -q '^WHISPER_MODEL=' .env || { echo 'WHISPER_MODEL=turbo' >> .env; ok "Added WHISPER_MODEL=turbo to existing .env"; }
fi

# ---------------------------------------------------------------- 9. smoke test
if [ "${LR_SKIP_HEALTH:-0}" = "1" ]; then
  warn "Skipping smoke test (LR_SKIP_HEALTH=1)"
else
  echo
  say "Smoke test: starting the UI server only and checking it answers…"
  # Start just server.js so this doesn't trigger the multi-GB Whisper download during install.
  mkdir -p ~/.love-refactored
  lsof -ti tcp:3000 | xargs kill -9 2>/dev/null || true
  ( node server.js > ~/.love-refactored/install-smoke.log 2>&1 & echo $! > ~/.love-refactored/install-smoke.pid )
  SMOKE=0
  for _ in $(seq 1 30); do   # bounded: 30 tries × 1 s
    if curl -fsS -o /dev/null http://127.0.0.1:3000/ 2>/dev/null; then SMOKE=1; break; fi
    sleep 1
  done
  kill "$(cat ~/.love-refactored/install-smoke.pid)" 2>/dev/null || true
  rm -f ~/.love-refactored/install-smoke.pid
  if [ "$SMOKE" -eq 1 ]; then
    ok "UI server answered on http://localhost:3000"
  else
    echo "${RED}  Last 20 lines of the server log:${RESET}"; tail -20 ~/.love-refactored/install-smoke.log | sed 's/^/    /'
    die "UI server did not answer within 30 s. The reason is in the log lines above."
  fi
fi

# ---------------------------------------------------------------- done
echo
echo "${BOLD}${GREEN}════════════════════════════════════════════════════${RESET}"
echo "${BOLD}${GREEN}  Love Refactored is installed and verified.${RESET}"
echo "${BOLD}${GREEN}════════════════════════════════════════════════════${RESET}"
echo
echo "  To run it:      cd $LR_DIR && ./start.sh"
echo "  Then open:      http://localhost:3000"
echo "  To stop:        ./stop.sh"
echo
echo "  First ./start.sh downloads the Whisper '${WHISPER_MODEL:-turbo}' model (~1.6 GB) and the memory"
echo "  embedding model in the background. The screen will look quiet. That's normal."
echo "  In the app: Settings → Provider & model → paste a key → Test connection."
echo "  Then create a companion, ./stop.sh, ./start.sh — memory comes on after that restart."
echo "  Settings → Pipeline needs its own key + models for memory."
echo
echo "  Log of this install: $LOG"
