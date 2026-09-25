#!/bin/bash

echo "Stopping Love Refactored..."
APP_PORT="${PORT:-3000}"
TANEVAN_PORT="${TANEVAN_PROXY_PORT:-5001}"
WHISPER_PORT="${WHISPER_PORT:-5555}"

# Kill saved PIDs
if [ -f ~/.love-refactored/pids.txt ]; then
  while IFS= read -r pid; do
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null && echo "  killed PID $pid"
    fi
  done < ~/.love-refactored/pids.txt
  rm -f ~/.love-refactored/pids.txt
fi

# Fallback — kill anything still on these ports
for P in "$APP_PORT" "$TANEVAN_PORT" "$WHISPER_PORT"; do
  PIDS=$(lsof -ti tcp:$P 2>/dev/null)
  if [ -n "$PIDS" ]; then
    echo "$PIDS" | xargs kill -9 2>/dev/null && echo "  cleared port $P"
  fi
done

# Fallback — the Telegram poller has no port; stop it by name
if pkill -f "node telegram-poller.js" 2>/dev/null; then
  echo "  stopped Telegram poller"
fi

echo "Done."
