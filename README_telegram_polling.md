# Telegram polling

Love Refactored talks to Telegram by **polling**. `telegram-poller.js` (repo root) long-polls `getUpdates` for every bot in Settings and posts each update to `POST /telegram/:companion` on localhost, the same route a webhook used to hit. No public URL, no inbound port. Polling and webhooks cannot both be on for one bot; the poller deletes any webhook it finds.

`./start.sh` starts the poller after the UI. On a VPS, PM2 runs it as `lr-telegram` (`ecosystem.config.cjs`). `./stop.sh` stops it by name.

## Add a bot

1. `./stop.sh` then `./start.sh` (or `pm2 status` and confirm `lr-telegram` is online).
2. Settings → Integrations → Telegram. The **Listener** row should say it is fetching.
3. Pick a companion, paste the BotFather token, **Add bot**. The token is checked with Telegram. The poller re-reads settings about every 30 seconds.
4. Message the bot from your phone once, then **Use this chat**. That chat id is the only one the bots answer.
5. **Test** sends one line to that chat.

A secret is generated the first time a bot is added. It authenticates the poller’s posts into the app. Regenerate it only if it leaked.

## Files the poller writes

All under `data/` (gitignored; safe to delete while the poller is stopped):

| File | Role |
|------|------|
| `telegram-poll-offsets.json` | Last update id per bot. An update is never replayed. |
| `telegram-poll-status.json` | Heartbeat the Settings listener row reads. Stale after 120 seconds, or if the pid is dead. |
| `telegram-poller.pid` | Lock. A second copy exits. Two pollers make Telegram return 409. |

Log: `~/.love-refactored/telegram.log`.

A forward into the app is retried once after 5 seconds, then logged as dropped. `getUpdates` failures sleep 15 seconds. There is no tight retry loop.
