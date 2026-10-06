// Love Refactored — PM2 config for a VPS.
//
//   pm2 start ecosystem.config.cjs && pm2 save && pm2 startup
//
// This starts every service the app needs (UI server, Tanevan memory,
// Whisper transcription, Telegram poller) and keeps them alive across
// crashes and reboots. See README → "Running on a VPS".
//
// NEVER run start.sh under pm2. start.sh is a one-shot launcher that exits
// after starting things; pm2 would restart it in a loop, and each loop
// kills the services the previous one started.
//
// Keys and settings live in <LR>/.env, which every service loads itself.
// Change .env, then `pm2 restart all`.
const path = require("path");
const LR = __dirname;                       // the folder this file lives in
const PY = path.join(LR, "venv", "bin", "python3");   // created by install.sh

const common = {
  LR_HOME: LR,
  LR_PROFILE: "vps",
  AUTH_TRUST_PROXY: "1",
  TANEVAN_DATA_DIR: path.join(LR, "tanevan-data"),
};

module.exports = {
  apps: [
    { name: "lr-server",   script: "server.js",               cwd: LR,
      env: common, max_memory_restart: "1500M" },
    { name: "lr-tanevan",  script: "proxy.py", interpreter: PY,
      cwd: path.join(LR, "tanevan"), env: common },
    { name: "lr-whisper",  script: "voice/whisper_server.py", interpreter: PY,
      cwd: LR, env: common },
    { name: "lr-telegram", script: "telegram-poller.js",      cwd: LR,
      env: common },
    // Optional voice services — uncomment if you set up Pipecat / Fish.
    // { name: "lr-pipecat", script: "voice/pipecat/voice_server.py", interpreter: PY,
    //   cwd: path.join(LR, "voice", "pipecat"), env: common },
    // { name: "fish-asr", script: "voice/fish_asr_proxy.py", interpreter: PY, cwd: LR, env: common },
    // { name: "fish-tts", script: "voice/fish_tts_server.py", interpreter: PY, cwd: LR, env: common },
  ],
};
