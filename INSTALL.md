# Installing Love Refactored (beta)

Supported: **Windows 10/11 via WSL2 (Ubuntu 24.04)**, Ubuntu 24.04, macOS with Homebrew.

## Windows

1. PowerShell **as Administrator**: `wsl --install -d Ubuntu-24.04` — restart when asked, then create a Linux username and password in the Ubuntu window that opens. (It must be 24.04; a bare `wsl --install` gives a newer Ubuntu that won't work.)
2. In that Ubuntu window, paste this one line and press Enter:

    bash <(curl -fsSL https://raw.githubusercontent.com/Love-Encoded/LoveRefactored/main/install.sh)

3. It asks for your Linux password once (for apt). Expect 20–40 minutes, mostly downloads. Two red "system bus" lines are normal in WSL. A quiet stretch of 2–5 minutes during `npm install` is SQLite compiling — do not press Ctrl+C.
4. When it prints **"Love Refactored is installed and verified"**:

    cd ~/love-refactored && ./start.sh

   then open http://localhost:3000 (it usually opens for you).

Copy/paste in the Ubuntu window: highlight text then press Enter to copy; right-click to paste. **Ctrl+C with nothing selected cancels whatever is running.**

## Mac / Ubuntu

Same one line in Terminal. Mac needs Homebrew first (https://brew.sh).

## After the first start

- First `./start.sh` downloads the speech model (~1.6 GB) and the memory model in the background. The window looks quiet; that's normal.
- In the app: Settings → Provider & model → paste a key → Test connection.
- Create a companion, then `./stop.sh` and `./start.sh` once — memory comes on after that restart.
- To update later: `cd ~/love-refactored && ./stop.sh && git pull && ./start.sh`
- Stop: `./stop.sh`. Logs: `~/.love-refactored/`. Install log: `~/lr-install.log`.

## If it stops

It stops on the first real problem and says why in plain words. Send `~/lr-install.log` with your report — it has everything.

Needs ~16 GB RAM for the default speech model. On 8 GB machines set `WHISPER_MODEL=base` in `~/love-refactored/.env` before starting.
