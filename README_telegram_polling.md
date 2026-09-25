# Telegram polling for LR beta — 2026-09-25

Files in this zip (apply to a branch off origin/main):
- telegram-poller.js      NEW  → repo root. Outbound-only listener; started by start.sh.
- routes/telegram.js      FULL REPLACEMENT. Auto-generates the secret on first bot add,
                          reports poller heartbeat in /api/telegram/status, webhook
                          register route removed.
- start.sh                FULL REPLACEMENT. Launches the poller, kills stale copies,
                          fixes `open` on WSL/Linux.
- stop.sh                 FULL REPLACEMENT. Adds by-name poller kill fallback.
- patch_index_telegram.py RUN from repo root → rewrites the Telegram section of
                          public/index.html. Exact-match anchors; stops without writing
                          if anything doesn't match. Makes a .bak first.

Runtime files the poller creates under data/ (all gitignored-safe, safe to delete when stopped):
- telegram-poll-offsets.json   last update id per bot — no replays, ever
- telegram-poll-status.json    heartbeat the Settings UI reads
- telegram-poller.pid          lock so two pollers never run

Log: ~/.love-refactored/telegram.log

Tester flow after pulling: ./stop.sh → ./start.sh → Settings → Integrations → Telegram →
pick companion, paste BotFather token, Add bot → message the bot from the phone once →
"Use this chat". No terminal beyond the restart.

Not changed: server.js (route already matches), data/settings.example.json.
