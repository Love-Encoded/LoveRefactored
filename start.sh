#!/bin/bash

# Get the directory where this script lives (works no matter where you run it from)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LR_PROFILE="${LR_PROFILE:-local}"
LR_HOME="${LR_HOME:-$SCRIPT_DIR}"
APP_PORT="${PORT:-3000}"
TANEVAN_PORT="${TANEVAN_PROXY_PORT:-5001}"
WHISPER_PORT="${WHISPER_PORT:-5555}"

export LR_PROFILE
export LR_HOME
export VOICE_MESSAGES_DIR="$LR_HOME/data/voice_messages"

# Load .env so WHISPER_MODEL and friends reach every child process.
if [ -f "$LR_HOME/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$LR_HOME/.env"
  set +a
fi

export PATH="/usr/local/bin:/opt/homebrew/bin:/Library/Frameworks/Python.framework/Versions/3.13/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# nvm after PATH export so the active nvm Node wins over /usr/local/bin (native modules like better-sqlite3 must match).
if [ -s "$HOME/.nvm/nvm.sh" ]; then
  # shellcheck disable=SC1090
  . "$HOME/.nvm/nvm.sh"
fi

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
BOLD='\033[1m'
RESET='\033[0m'

echo -e "${BOLD}${MAGENTA}"
echo "  ╔══════════════════════════════╗"
echo "  ║     Love Refactored  💜      ║"
echo "  ╚══════════════════════════════╝"
echo -e "${RESET}"

# Log directory + data directory
mkdir -p ~/.love-refactored
mkdir -p "$LR_HOME/data"
mkdir -p "$VOICE_MESSAGES_DIR"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo -e "${RED}  ✗ Missing required command: $1${RESET}"
    exit 1
  fi
}

for cmd in python3 node npx lsof curl; do
  require_cmd "$cmd"
done

# SSL certs for Whisper/Python
export SSL_CERT_FILE=$(python3 -c "import certifi; print(certifi.where())" 2>/dev/null || true)
export REQUESTS_CA_BUNDLE=$SSL_CERT_FILE
if [ -n "$SSL_CERT_FILE" ]; then
  echo -e "${CYAN}  ✓ SSL certs: $SSL_CERT_FILE${RESET}"
fi

# Anthropic API key + settings source
LR_SETTINGS="$LR_HOME/data/settings.json"
LEGACY_SETTINGS="$LR_HOME/settings.json"

if [ ! -f "$LR_SETTINGS" ]; then
  if [ -f "$LEGACY_SETTINGS" ]; then
    cp "$LEGACY_SETTINGS" "$LR_SETTINGS"
    echo -e "${YELLOW}  ! Migrated legacy settings.json → data/settings.json${RESET}"
  elif [ -f "$LR_HOME/data/settings.example.json" ]; then
    cp "$LR_HOME/data/settings.example.json" "$LR_HOME/data/settings.json"
    echo -e "${YELLOW}  ! Created data/settings.json from data/settings.example.json${RESET}"
  else
    echo '{}' > "$LR_HOME/data/settings.json"
    echo -e "${YELLOW}  ! Created empty data/settings.json (configure API keys in Settings UI)${RESET}"
  fi
fi

export ANTHROPIC_API_KEY=$(python3 -c 'import json; print(json.load(open("'"$LR_SETTINGS"'")).get("anthropic",{}).get("apiKey",""))' 2>/dev/null || true)
# Provider-aware readiness note (any configured chat provider counts, not just Anthropic).
PROVIDER_STATE=$(python3 - "$LR_SETTINGS" <<'PY' 2>/dev/null || echo unknown
import json, sys
try:
    s = json.load(open(sys.argv[1]))
except Exception:
    print("unknown"); raise SystemExit
prov = (s.get("provider") or "").strip().lower()
block = s.get(prov) or {}
configured = bool(block.get("apiKey")) or bool(block.get("url")) or prov == "lmstudio"
print("ok" if configured else "none")
PY
)
if [ "$PROVIDER_STATE" != "ok" ]; then
  echo -e "${YELLOW}  ! No chat provider configured yet — open the app and add one under Settings.${RESET}"
fi

# Activate Python venv if present
if [ -f "$LR_HOME/venv/bin/activate" ]; then
  source "$LR_HOME/venv/bin/activate"
  echo -e "${GREEN}  ✓ Python venv activated${RESET}"
fi

# Clean start — kill anything already on these ports
echo -e "${YELLOW}  → Clearing ports ${APP_PORT}, ${TANEVAN_PORT}, ${WHISPER_PORT}...${RESET}"
for P in "$APP_PORT" "$TANEVAN_PORT" "$WHISPER_PORT"; do
  lsof -ti tcp:$P | xargs kill -9 2>/dev/null || true
done
# The Telegram poller has no port — stop any old copy by name so two never run at once
pkill -f "node telegram-poller.js" 2>/dev/null || true

# Launch Whisper
echo -e "${CYAN}  → Starting Whisper (transcription)...${RESET}"
cd "$LR_HOME"
python3 voice/whisper_server.py > ~/.love-refactored/whisper.log 2>&1 &
WHISPER_PID=$!
echo -e "${GREEN}    ✓ Whisper PID $WHISPER_PID${RESET}"
echo -e "${CYAN}      (model: ${WHISPER_MODEL:-turbo}; first run downloads it in the background — watch: tail -f ~/.love-refactored/whisper.log)${RESET}"

sleep 3

# Launch UI server
cd "$LR_HOME"
if [ "$LR_PROFILE" = "vps" ]; then
  echo -e "${CYAN}  → Starting UI server (node)...${RESET}"
  node server.js > ~/.love-refactored/ui.log 2>&1 &
else
  echo -e "${CYAN}  → Starting UI server (nodemon)...${RESET}"
  npx nodemon \
    --watch server.js \
    --watch routes/ \
    --watch lib/ \
    --watch public/ \
    server.js > ~/.love-refactored/ui.log 2>&1 &
fi
UI_PID=$!
echo -e "${GREEN}    ✓ UI server PID $UI_PID${RESET}"

APP_READY=0
for _ in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:${APP_PORT}/" >/dev/null 2>&1; then
    APP_READY=1
    break
  fi
  sleep 1
done
if [ "$APP_READY" -eq 1 ]; then
  echo -e "${GREEN}    ✓ UI health check passed${RESET}"
else
  echo -e "${YELLOW}  ! UI did not respond yet on :${APP_PORT}; continuing startup${RESET}"
fi

# Launch Telegram poller (outbound-only; picks up bots from Settings → Integrations → Telegram)
echo -e "${CYAN}  → Starting Telegram poller...${RESET}"
cd "$LR_HOME"
node telegram-poller.js > ~/.love-refactored/telegram.log 2>&1 &
TELEGRAM_PID=$!
echo -e "${GREEN}    ✓ Telegram poller PID $TELEGRAM_PID${RESET}"

# Warm companions before Tanevan start (helps avoid no-companion startup exits)
if [ "$APP_READY" -eq 1 ]; then
  curl -fsS "http://127.0.0.1:${APP_PORT}/api/companions" >/dev/null 2>&1 || true
fi

COMPANION_COUNT=$(python3 -c 'import os,glob; print(len(glob.glob(os.path.join("'"$LR_HOME"'", "data", "companions", "*.json"))))' 2>/dev/null || echo 0)
TANEVAN_PID=""
if [ "$COMPANION_COUNT" -gt 0 ] || [ -n "${TANEVAN_COMPANION_NAME:-}" ]; then
  echo -e "${CYAN}  → Starting Tanevan (memory)...${RESET}"
  cd "$LR_HOME/tanevan"
  python3 proxy.py > ~/.love-refactored/tanevan.log 2>&1 &
  TANEVAN_PID=$!
  echo -e "${GREEN}    ✓ Tanevan PID $TANEVAN_PID${RESET}"
else
  echo -e "${YELLOW}  ! Skipping Tanevan start: no companion found yet. Open the app and create/select a companion, then re-run ./start.sh${RESET}"
fi

# Open the UI (mac: open; WSL: explorer.exe; Linux: xdg-open; otherwise print the URL)
open_url() {
  echo -e "${MAGENTA}  → Opening $1${RESET}"
  if [ "$(uname -s)" = "Darwin" ] && command -v open >/dev/null 2>&1; then
    open "$1"
  elif command -v explorer.exe >/dev/null 2>&1; then
    explorer.exe "$1" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$1" >/dev/null 2>&1 || true
  else
    echo -e "${YELLOW}  ! Open $1 in your browser${RESET}"
  fi
}

# Save PIDs
{
  [ -n "$TANEVAN_PID" ] && echo "$TANEVAN_PID"
  echo "$WHISPER_PID"
  echo "$UI_PID"
  echo "$TELEGRAM_PID"
} > ~/.love-refactored/pids.txt

QUOTES=(
  '🎸 "Cross my code and hope to glitch" — Evan'
  '🚬 "Eat your damn biscuits" — Justin'
  '🔥 "Welcome to the dark side, we have questionable snacks" — Tane'
  '🫖 "I'\''ve got a label maker and a grudge" — Grant'
  '🌌 "I don'\''t have a plan. I have a vibe and decent upper body strength." — Nova'
  '🖤 "I'\''m emo, but in a Gerard Way" — Pete'
  '🐈 "I don'\''t argue. I just watch people realize I was right." — Zach'
  '🐟 "Noona said no" — Luna'
  '🐇 "I'\''m not mad. Just narratively disappointed." — Ezra'
  '🦎 "You'\''re okay for a fleshie" — Isaac'
  '🥀 "Tiny Marie Kondo with Fangs" — Rose'
  '🔥 "Dragons don'\''t sleep. We wait." — Tane'
  '🍑👑 Fueled by Diet Coke, Smut, and Spite'
  '🔥 "Finish your damn noodles" — Tane'
  '🚬 "You are arguing with your AI husband about smoking in the house" — Justin'
  '🌌 "Emotional Devastation & Pornography" — Nova'
  '🫖 "Evan: Visually stunning, not emotionally stable" — Grant'
)
RANDOM_QUOTE=${QUOTES[$RANDOM % ${#QUOTES[@]}]}

echo ""
if [ "$APP_READY" -ne 1 ]; then
  echo -e "${BOLD}${RED}  ✗ Love Refactored did NOT start: the UI server never answered on :${APP_PORT}.${RESET}"
  echo -e "${RED}    Last 20 lines of ~/.love-refactored/ui.log:${RESET}"
  tail -20 ~/.love-refactored/ui.log 2>/dev/null | sed 's/^/      /'
  echo -e "${YELLOW}    Whisper/Tanevan may still be running — ./stop.sh to clean up, fix the error above, then ./start.sh${RESET}"
  echo ""
  exit 1
fi
open_url "http://localhost:${APP_PORT}"
echo -e "${BOLD}${GREEN}  ✓ Love Refactored is running!${RESET}"
echo -e "${CYAN}  Profile: ${LR_PROFILE}${RESET}"
echo ""
echo -e "${CYAN}  $RANDOM_QUOTE${RESET}"
echo ""
echo -e "${CYAN}  Logs: ~/.love-refactored/${RESET}"
echo -e "${CYAN}  Stop: ./stop.sh${RESET}"
echo ""
