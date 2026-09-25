// LR beta — created 2026-09-07 (voice port + pm2 banishing).
// Design decision (Tiara + Claude): this file carries paths/interpreters ONLY.
// All keys and settings live in /opt/love-refactored/.env, which every
// service self-loads at start — the pm2 daemon never holds env, so
// fossil env cannot form. Change .env, restart, done.
const LR = "/opt/love-refactored";
const PY = `${LR}/pipecat-venv/bin/python3`;
module.exports = {
  apps: [
    { name: "lr-server",  script: `${LR}/server.js`, cwd: LR,
      env: { LR_HOME: LR } },
    { name: "lr-pipecat", script: `${LR}/voice/pipecat/voice_server.py`,
      interpreter: PY, cwd: `${LR}/voice/pipecat`, env: { LR_HOME: LR } },
    { name: "fish-asr",   script: `${LR}/voice/fish_asr_proxy.py`,
      interpreter: PY, cwd: `${LR}/voice`, env: { LR_HOME: LR } },
    { name: "fish-tts",   script: `${LR}/voice/fish_tts_server.py`,
      interpreter: PY, cwd: `${LR}/voice`, env: { LR_HOME: LR } },
  ],
};
