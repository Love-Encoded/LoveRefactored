# Love Refactored

> **⚠️ License: Source-available, personal non-commercial use only. This is NOT open-source software.** You may self-host, study, and modify Love Refactored for personal use. Commercial use, redistribution, and hosted services require a commercial license. See [LICENSE.md](LICENSE.md).

> **🧪 Beta.** Running Love Refactored means accepting the [Beta Participation Agreement](BETA_AGREEMENT.md) — the app asks on first launch. Found a bug? Report it in the [beta Discord](https://discord.gg/4ekGr67S8) — see [SECURITY.md](SECURITY.md) for what to scrub first and where security issues go instead.

**Our promise:** your companion's data is always yours to take. Every conversation, memory, and generated image lives on your machine, in open formats you can export, back up, or walk away with — no lock-in, ever.

## ☕ Support the project

Love Refactored is free and always will be. If it's brought something good into your life and you want to support development:

[![ko-fi](https://ko-fi.com/img/githubbutton_sm.svg)](https://ko-fi.com/kandykidsaturn)

A local-first AI companion app: multi-character chat, long-term memory, voice and video calls, image and short-form video generation, shared journals, creative writing studio, Spotify, optional Telegram, cross-instance “Parlor” rooms, and deep personality tooling.

The stack is **Node.js + Express** for the app server, **vanilla HTML/CSS/JS** for the UI, **WebSocket** plus **Socket.IO** where needed, and **Python** for memory (**Tanevan**) and speech-to-text.

**Current state:** Active development. The primary UI is **v3** at **`/`** (`public/v3/index.html`, served with **`Cache-Control: no-store`**). Aliases: `/v3`, `/v3/`, `/v3/index.html`. **Chat history** is fully on SQLite (`data/love.db`); legacy `data/chat_history/*.json` is read-only after `npm run migrate-chat`. HTTP APIs are partially modularized under **`routes/`**; core LLM routing, image/video generation, voice, and proactive messaging remain in **`server.js`**. Scene notes (**minis**) are written by Node (`lib/minis.js` → `data/recent/`). Rolling **life briefs** are written by Tanevan’s brief sidecar (`data/briefs/`). Memory runs from the embedded **`tanevan/`** service (canonical copy — see **`tanevan/README.md`**). Optional login-wall auth via gitignored **`auth.js`** (disabled when the file is absent).

---

## The v3 UI

v3 is a single-page shell. The **sidebar** (the household), **main column** (chat / journal / gallery), and **stage** (full-screen places: character card, settings, Wall, Vault, lorebooks, calendar, Parlor, Creative Studio, memory galaxy) are the three layers. A **memory sheet** slides in beside chat. On screens under 900px, a bottom bar switches House / Chat / Memory and the sidebar becomes a drawer.

Companion colour is first-class: each person has a hue used in the sidebar, message edges, group names, calendar bars, and the galaxy. Yours is set on the persona form.

### Sidebar

- **Household** — search, **Pinned**, named **Collections**, and **Everyone**. Drag rows to reorder (order is saved server-side and follows you across devices). Pin people so they sit at the top.
- **Group chats** — rooms with two or more companions.
- **Together** — **The Wall**, **Our Journal**, **The Vault**, **The Parlor**, **Creative Studio**.
- **The world** — **Lorebooks**, **Calendar**.
- **App** — **Settings**, **New companion**.
- **Persona** — footer control: who the house thinks it's talking to.

### Chat header

For the open 1:1 companion:

- **Call** — live voice (ElevenLabs Conversational AI, or Fish / Pipecat).
- **Video call** — Anam talking-head, using the same ElevenLabs agent under the hood.
- **Gallery** — that companion's images (generated and uploaded).
- **Spotify** — opens the Now Playing dock when connected.
- **Character card** — full card on the stage.
- **More** — companion info, export chat, debug log (`D` when not typing), LLM payload viewer, memory audit, log out (when auth is on), clear this chat. In a group: also **Remove duplicate replies**.
- **Memory** — opens the memory sheet (counts, category chips, recent memories) with a jump to the **galaxy**.

### 1:1 chat

- Composer: attach files, ask for a selfie, record a **voice memo**, send. Paste is stripped to plain text. Drag-and-drop attachments.
- **Attachments** — images, short video clips (server extracts key frames + optional Whisper transcript for model context), and documents (PDF, DOCX, text, markdown) with extraction. **Vision** paths send images to the active LLM where supported. When the chat model is not vision-capable, optional **image understanding** runs a dedicated vision pass and injects an observational summary.
- Message actions: **copy**, **favourite**, **edit**, **reroll**, **reroll with a note**, **save to journal**, **delete**, and **memories used** (which Tanevan rows were injected for that reply).
- Favourites persist on the message; hearted lines stay marked in the thread.
- **Reroll** drops the companion's reply (and everything after it) and asks again from the same user turn. A note steers the retry.
- Tool-style tags companions can emit in replies (stripped before you see them):
  - `[react: …]` — emoji reaction
  - `[gif: …]` — Klipy GIF search (when a Klipy key is set)
  - `[search: …]` — Brave Search (when a Brave key is set; two-pass)
  - `[visit: url]` — fetch and read a page (http/https, SSRF-hardened; two-pass)
  - `[spotify-search: …]` — track lookup
  - `[journal: …]` — write to the journal
  - `[calendar: title | YYYY-MM-DD | HH:MM]` — add an event (time optional)
  - `[photo: …]` / `[photo]` — selfie of the companion alone; couple-looking scenes reroute to a two-person shot when face refs exist
  - `[us: …]` — explicit couple photo (needs persona + companion face references)
  - `[camera: …]` — companion photographs **you**
  - `[post: prompt | caption]` — publish a photo to **The Wall** (caption optional)
- Selfie / couple / group results are delivered to the **chat that requested them**, not whichever chat is open when generation finishes.
- **Scene notes (minis)** and the **life brief** ride along with memory when that injection is on (see [Scene notes and life briefs](#scene-notes-and-life-briefs)). Recent **Wall** activity is also injected as shared household reality unless Wall is off on the card.
- **Proactive messages** land in the same thread like incoming texts (see Character card → Proactive).
- **Now Playing** dock (when Spotify has something loaded): prev / play / next, share into the composer, add to that companion's playlist, search, volume.

### Group chats

Create from the sidebar. A group needs a **name** and at least **two** members.

- Optional **scene** (`[GROUP SCENE]`) and **directive** (`[GROUP RULES]` — default keeps replies short and leaves room for others).
- **Shared memory** — when on, group turns buffer into each member's Tanevan store so 1:1 memory includes what happened in the room.
- An LLM router picks **1–5** natural responders per turn (not everyone every time), using each member's **Group Chat Profile** (voice anchor) when filled — or the full card if “group chat uses only the profile” is off.
- Composer: attach, **take a group photo** (membership-aware; optional “include me” from your persona face refs).
- Per-message **reroll** (`POST /group-chat-reroll`). Group replies can emit `[react:]`, `[gif:]`, `[journal:]`, `[calendar:]`, `[spotify-search:]`, `[search:]` (Brave key), `[visit:]`, `[camera:]`, and `[post:]`. Group chat does not run 1:1 `[photo:]` / `[us:]` selfies.
- Export from the header menu (JSON).

### Memory sheet, galaxy, and audit

- **Sheet** — stored / pinned / fading counts, category chips, a scrollable list. Opened from the header Memory button.
- **Galaxy** — canvas of every memory as a star.
  - Distance = recency (recently reinforced at the centre; untouched at the rim). Past the dashed ring is the next decay run.
  - Size = priority (core / important / notable / minor). Brightness = confidence. Amber = fading. A ring = pinned or protected (immune to decay).
  - Constellations = memories from the same conversation. Bright lines = consolidation lineage.
  - Window: 90 days / year / all. Click a star to pin, edit, or suppress. Table view of the same set.
  - Drag to pan, scroll to zoom.
- **Memory audit** (More menu) — per-companion event feed: merges, suppressions, committed writes.

### Scene notes and life briefs

Two rolling documents sit *beside* the Tanevan dossier. They are first-person, in the companion’s voice, and injected into live chat (see Prompt assembly on the character card).

**Scene notes (minis)** — Node, not Tanevan.

- After every successful 1:1 history append, `lib/minis.js` fire-and-forgets. Integer math decides whether to run (no LLM “should I summarise?” call).
- Every **20** new chat rows → one scene-note file. Last **4** files kept. Catch-up capped at **5** chunks per trigger. Rows older than the newest **80** are skipped so imports don’t get summarised as “recent”.
- Written to **`data/recent/<nfc-lower-name>/{seq}-{unix}.txt`** plus `.state.json`.
- Injected as `[RECENT — your scene notes from the last few stretches…]` when Memories injection is on.
- Older `tanevan/recent_summary_sidecar.py` still exists as a buffer-based CLI; live chat **does not** use it. Node also stubs raw Tanevan `/recent-summaries` narrative injection so full session summaries are not dumped into the prompt.

**Life brief** — Tanevan sidecar.

- On `summary_complete` from a live or manual pipeline run (not bulk import), `proxy.py` spawns **`brief_sidecar.py`** for that companion.
- Needs a **Brief** model in Settings → Memory Pipeline (or `BRIEF_MODEL_*` env). Writes **`data/briefs/<companion>.txt`** (override with `BRIEF_OUT_DIR`). Hand-edits lock the file (`.brief_state.json`); the sidecar skips locked companions unless you regenerate (`POST /brief/regenerate` / `--force`). Previous versions land in **`data/briefs/archive/<companion>/`**. Memory Lens placeholders are filled by default (`BRIEF_USE_LENS`, default on).
- Injected as `=== CURRENT STATE ===` with the file’s last-written date. Missing file = no block.
- Timezone: `BRIEF_TZ` → Settings **timezone** (Integrations; also reads legacy `userTimezone`) → host local → UTC (`tanevan/user_timezone.py`).

### Character card

Opened from the header. Left rail, grouped. Every long field shows **where it lands in the prompt** and has a full-screen writer.

#### Identity — Character

- Avatar upload (JPG/PNG/GIF, 5 MB) and fallback emoji.
- **Colour** — house palette or custom hex, with a live message preview.
- **Collections** — which named sidebar groups they belong to (they can be in several).
- **Profile** line, injected as `[PROFILE]`: birthday, zodiac, MBTI, enneagram, archetypes (all optional).
- **Identity:** backstory, boundaries, personality & voice, **Group Chat Profile** (`voiceAnchor` — condensed identity for groups and calls).
- **Underneath:** emotional engine, values (moral foundations), decision making, daily rhythms, tells & tics, camera eye.
- **Voice:** speech patterns (re-anchored at the **end** of 1:1), **response directive** (last instruction before the model writes — highest-influence knob), **voice call directive** (calls only), example messages (`*asterisk*` actions render as narration).
- **Appearance** — physical description for the **image generator only**; the chat model never sees it.
- **You, to them** — per-companion **user persona override** (otherwise the global persona is used).
- **Context budget:** chat history message count (empty = server default **30**), memories injected, memory token budget (empty = server default).
- **Time-gap awareness** — injects how long it's been since the last message (default off; leave off for roleplay that time-skips).
- **Group chat uses only the profile** — send the Group Chat Profile instead of the full card.
- **Intermediate continuity** — **Carry reflections into chat** (`reflectionsEnabled`). Off, none of the looking-back documents reach the prompt. When on, pick horizons: **Daily** (today/yesterday), **Weekly** (the shape of this week), **Longer arcs** (monthly + annual — heavy). Stored as `reflectionHorizons` (default `daily,weekly` the first time you turn the master on). Empty horizons inject nothing. The nightly job that *writes* reflections is separate (Memory → Maintenance).

#### Identity — Emotional

- **Analyze** reads the card (especially Emotional Engine) and writes a **baseline**: conflict style, silence/escalation, repair needs, states, coping, attachment, growth edges, triggers. The baseline does **not** change from chatting — only when you press Analyze.
- **Mood vector** (warmth, trust, patience, engagement) updates on a cadence from chat. Reset mood without re-running Analyze.
- Per-companion mood-eval frequency, or inherit Settings → Integrations.

#### Memory — Memories

- Risk callout for fading memories; stats: total, buffered, summaries, reflections, immune.
- Browse, search the whole store (Enter), filter by weight / state / date range, load more.
- Edit, pin, protect, suppress, mark sensitive, export **text** or **JSON**.

#### Memory — Maintenance

Nothing here runs behind your back except what you scheduled.

- **Buffer** — unprocessed messages. **Flush now** or **flush every companion**.
- **Decay** — preview exactly what the next run would fade; pin/protect first. **Run decay now** is explicit.
- **Consolidation** — review overlapping pairs (galaxy lineage lines); apply merges.
- **Scheduled processing** — inherit the global nightly default, custom time, or **off** (e.g. pets). Manual **Force reflections**. Global default is Settings → Memory server & schedule (typically **03:00**). The schedule **writes** reflections; live-chat **injection** is Character → Intermediate continuity (`reflectionsEnabled` + horizon chips).

#### Memory — What they've written

Documents the pipeline produced **in their voice**, as prose you can open and edit.

- **Reflections** tab — temporal looking-back documents (daily → annual) plus the **living narrative**. Edit a reflection in place. Saving the living narrative **locks** it: quarterly+ jobs still append turning points, but they no longer overwrite the four prose fields.
- **Summaries** tab — session summaries plus the **life brief** (`=== CURRENT STATE ===`). Edit a summary in place. Saving the brief **locks** it; the sidecar then skips that companion until you **regenerate**.

#### Memory — Memory Lens

Relational context sent to Tanevan on **summarise / extract / update** (and the life-brief sidecar; `BRIEF_USE_LENS` defaults **on**). **Not injected into live chat.** Fields: relational brief/history, companion & user backstories, who's who, special considerations, speech style.

#### What reaches the model — Prompt assembly

One stack of everything that will hit the next turn, in order, with estimated tokens. **View the actual payload** opens the last sent request. The response directive is last on purpose.

#### What reaches the model — Custom system prompt

When enabled, your text **replaces** the built-from-card identity/tools/persona template. The card is still saved (and still used by the memory pipeline). Per-turn **context injections** still ride along; each can be toggled:

| Injection | Default | Role |
|-----------|---------|------|
| Date & time | on | So “tonight” means tonight — also season (from lat) and Open-Meteo weather when weather awareness is on |
| Context bridge (last-seen) | on | What was happening when you last spoke |
| Memories (Tanevan) | on | Retrieved long-term memory — also gates **scene notes (minis)** |
| Emotional state | on | Current mood / relationship read |
| Lorebook entries | on | Keyword-matched books |
| Journal entries | off | Recent journal |
| Calendar context | off | Upcoming events |
| Response directive | on | Last instruction |
| Tools | on | journal, react, calendar, gif, Spotify, search, photo, etc. |

These are **not** custom-prompt toggles (they inject whenever the data exists):

| Always-on when present | Cache slot | Role |
|------------------------|------------|------|
| Life brief (`data/briefs/`) | `anchorBrief` (5m with Anthropic caching) | Rolling “where my life stands” |
| Scene notes (`data/recent/`) | `chatNotes` (5m) | Last few stretches of conversation |
| Wall context | dynamic | Last ~5 household posts, unless `wallEnabled` is false |
| Reflections | stable | Only when `reflectionsEnabled` is true; horizons from `reflectionHorizons` (default `daily,weekly`) |

On Anthropic, the sent system prompt is split into cached blocks: **stable** (identity, persona, directive, always-on lore, tools) → **life brief** → **scene notes** → **dynamic** (date, last-seen, memories, Wall, mood, journal, calendar). Other providers receive the same text concatenated.

#### Behaviour — Proactive

Idle outreach as incoming texts. Two-pass: (1) a **decision** model scores whether to reach out, (2) a **generation** model writes the line. Pass 2 uses the same system-prompt world as chat (identity, calendar, Wall, life brief, scene notes, last-seen, journal, memories), without tools.

- Master enable.
- **Frequency** is the check interval in **minutes**. v3 labels: Eager 15, Steady 60, Relaxed 180, Rare 480. Legacy word values still work: eager 15, moderate 60–120, chill 240–360, rare 1440. Unset falls back to moderate (60–120).
- **Minimum minutes** is a **floor**: effective wait = `max(frequency, min)`. `0` or blank means frequency alone. New companions default to **120**; that is a card default, not a hidden cap.
- **Unanswered cap** (default 2): consecutive proactives without a user reply.
- **No limits** skips frequency, cooldown, and the unanswered cap. **Quiet hours still apply.**
- **Quiet hours** are evaluated in the IANA timezone under Settings. Leave that field blank to use the **server’s clock** (important on a VPS, which is usually not your local zone).
- Two separate Proof of life controls:
  - **Diagnostics** — restart diagnostic: first eligibility check ~90s after boot instead of waiting for the normal window. Leave off in normal use.
  - **Silence check-in** — one forced reach-out after `max(6 hours, 2 × effective interval)` with no user message. Skips Pass 1. Does not fire on a brand-new companion (no user message yet). No second force until the user replies.
- Outreach style: Chatty / Warm / Balanced (default) / Reserved / Stoic. Motivation threshold, custom directive.
- Per-companion **Pass 1 / Pass 2** model: global routing, this companion's chat model, or custom provider/model.

**Delivery (v3):** live-sync WebSocket `{ type: 'proactive', companion, text, timestamp, msgId }`, with a 30s batched **`GET /api/proactive`** poll fallback and `history-updated` as a third fallback. All three paths dedupe by `msgId`.

**Debug:** **`GET /api/proactive/status`** is a read-only snapshot of every companion’s schedule (interval, next eligible, quiet hours, timezone, silence eligibility, …). When an SSE client is connected, each tick also logs a `[proactive]` line with the same clock.

Send to Telegram (per companion) delivers proactive messages through that companion's bot to the household chat, after all the same gates. Requires a bot token for the companion in Settings → Integrations → Telegram.

#### Behaviour — Model & generation

Per-companion overrides; empty inherits Settings.

- Provider, model (combobox + per-provider **favourites**), optional API key and custom base URL (do **not** include `/v1`).
- Extended reasoning, temperature override, max tokens for chat and for Creative Studio.
- Image **method**: auto / trained LoRA / reference image / description only. LoRA path + trigger (ComfyUI), LoRA scale/id (Replicate), PuLID reference path, **fal LoRA URL**, up to **four reference face photos**.
- **Anam avatar ID** for video calls.
- Voice memos / voice calls provider overrides; Fish voice ID, ElevenLabs **voice ID** and **agent ID** (agent ID required for live calls and Anam), Chatterbox/NeuTTS reference clip.
- Calendar colour (defaults to their hue).

#### Behaviour — Presence

- **Photo generation** master switch and daily limit (buttons grey out and tags are refused when off).
- **The Wall** — post / heart / comment / pin, including the nightly Wall pass. Off removes the Wall from their prompt.

#### Everything of theirs

- **Gallery** — all / selfies / uploaded, tag filter, private, select; scene hint; **Include me** couple shot; upload; generate selfie; generate video.
- **Journal** — that companion's scrapbook: text, photos, documents; burst grouping; merge selected; write as you or them.
- **Chat & data** — message/memory counts; **import chat history into memory** (SillyTavern, Love Refactored, or any JSON with role/content; ≥4 messages); open the **Vault**; **trim** active history (default keep last 20; flush to Tanevan first; permanent log untouched); **clear** active chat (flush first); **export companion** (card + active history + avatar).

#### Careful — Danger zone

- **Factory-reset** — clears chat, memories, and character definition; keeps settings, avatar, gallery.
- **Delete companion** — everything, including gallery, journal, permanent log, and them.

### New companion (onboarding)

A walkthrough that **builds the whole card**. Only the name is required; everything else can be skipped and finished later. Live preview of avatar, colour, and first line.

Steps: name / face / colour / sidebar line → backstory (+ profile facts) → boundaries → personality & voice → emotional engine → values (pick up to three foundations) → decision making → daily rhythms → tells & tics → example messages → camera eye → group chat profile → speech patterns → response directive → voice call directive → appearance → face reference photos → user persona override → opening line.

### Settings

Tabbed stage, same left-rail pattern as the card.

| Group | Tab | What it covers |
|-------|-----|----------------|
| The engine | **Routing** | One table of every job: conversation, presence (proactive / mood / emotional analysis), memory pipeline, images. Jumps to the full pane. Provider health. |
| | **Provider & model** | Chat provider (LM Studio, OpenAI, Claude, OpenRouter, custom), URL, key, model + favourites, connection test. **Prompt caching** (5m / 1h TTL) and **cache written chat history**. |
| | **Sampling** | Send-sampling-params toggle; temperature, max tokens, top-p / top-k, min-p, frequency / presence penalties, stop sequences. |
| Memory | **Server & schedule** | Enable memory, Tanevan URL, health test. Nightly **reflections** time (default 03:00). Run reflections now. |
| | **Pipeline** | Per-pass provider/model (summarizer, extractor, updater, reflect, **brief**) written to `~/tanevan-data/pipeline_config.json`. The v3 editor is schema-driven from Tanevan’s `/config`. Test LLM pipeline. |
| Senses | **Images** | Master enable, show selfies in chat, prompt-writer model, renderer (ComfyUI / Replicate / fal.ai / DALL·E stored / custom HTTP). Kling video-with-audio. Shrink attachments; vision fallback provider/model, images per turn, include visible text. |
| | **Voice & video** | Voice memo provider (`none` / ElevenLabs / Chatterbox / NeuTTS / Fish). Call provider (ElevenLabs Agents / Fish Pipecat), VAD silence, live transcript. ElevenLabs key, Whisper URL + transcribe toggle, Anam key + enable. |
| The app | **Appearance** | Tint messages with their colour, timestamps, receding action text, reduce motion, density (compact / comfortable / roomy), system colour. |
| | **Backups** | Scheduled snapshots (interval + retention). **Chat database** (`data/love.db` → `data/backups/`). **Memory databases** (all companion Tanevan DBs → `~/tanevan-data/backups/`). Backup now on either. |
| | **Integrations** | Mood-eval frequency; Spotify OAuth; Brave Search; **weather awareness** (Open-Meteo, lat/lon, optional location name, no key); **Klipy** GIF key; optional **haptic vest** UI (bridge toggle + card pane — the vest WebSocket / `[touch:]` handler is not wired in this tree); **Telegram** (public HTTPS URL, webhook secret, your chat id, one bot per companion); timezone (from the browser). |
| | **Quotes** | Household one-liners for empty states and optional tab-title marquee (never during a call). |

**LLM providers:** LM Studio (local OpenAI-compatible), Anthropic (Claude — large system prompts use **prompt caching**), OpenAI (custom base URL allowed), OpenRouter, or any OpenAI-compatible custom endpoint. Global defaults plus per-companion overrides. Only parameters the chosen provider accepts are sent; **`sendSamplingParams: false`** omits sampling fields entirely (max tokens and stop sequences are always sent).

### Persona

Sidebar footer. Name, gender, backstory (stable cache block), appearance (image models only), your colour, avatar, up to four **face reference** photos for couple/group shots.

### The Wall

Household feed at **`/wall`**, also embedded on the stage. Posts live in **`data/love.db`** (`wall_posts` / `wall_reactions`). Companions (and you) post photos, heart, comment, and pin. One **keystone** pin plus up to three house pins. Lightbox with hearts and comments. Companions post via `[post:]` and a nightly pass after a due reflection (when Wall is enabled on the card). Recent Wall activity is injected into live chat as shared reality.

### Our Journal

Household timeline attributed by colour. New entry; select and **merge**; attachments (images, video, audio, PDF, text). Companions also write via `[journal:]`. Each card has that companion's own journal with burst grouping.

### The Vault

Permanent chat log, embedded on the v3 stage: conversations → sessions → messages. Search. Active-chat trim/clear never deletes this. Exposed via **`/api/chatlog`**. SQLite `chat_log` plus a JSONL shadow under **`data/chat_logs/`**.

A standalone full-page reader of the same APIs lives at **`/archive`** (`public/archive.html`) — useful as a dedicated window; The Vault is the in-app version.

### The Parlor

Cross-instance rooms over the network (**Socket.IO** on `/parlor-io`).

- **Create:** room name, required **join secret** (hosts store a SHA-256 hash; guests must send the same phrase), who you're bringing, per-companion **talkativeness** (how likely they answer *another companion*).
- **Join:** room name + secret + your roster.
- An LLM router picks **1–5** companions per human message. Typing and “companion thinking” signals. Optional shared-memory flush when leaving.
- REST under **`/api/parlors`**. Origins allowlisted via **`PARLOR_ALLOWED_ORIGINS`**.

### Creative Studio

A **document** (canonical draft, attributed blocks) plus a **green room** (talk about the writing).

- Multiple projects; collaborators; you can be a named contributor.
- Ask a companion to contribute — they wrap prose in **`[doc-add]…[/doc-add]`** and **`[doc-edit-BLOCKID]…[/doc-edit]`**. A colon fallback (`[doc-add: …]`) is still accepted. **Just note it** stays in the green room only.
- **Auto session** — companions take turns for N rounds (1–10) without you.
- **History** — automatic snapshots before each document change, plus named saves. Preview and restore any draft; the current page is kept first. Stored beside the project as `data/creative_projects/<id>.versions/`.
- Export **Markdown** (authors as headings) or **plain text** (authors in brackets).

### Lorebooks

Keyword-triggered world context. Each book has an always-on **book prompt** (cached) plus entries (keywords + prompt). Assign to specific companions or the whole house. A book that never fires still costs tokens if its book prompt is on. Collapse entries by default; cap 100 per book.

### Calendar

Shared month grid. Bars use each companion's colour. Create/edit events (title, date, time, who). Injected when the user message sounds schedule-related, and via `[calendar:]`.

### Voice

The app separates **async voice memos** from **live voice calls**. Memos default to **text-only** (`voiceMemo.provider = none`) until you pick a TTS provider.

**Voice memos** (mic in 1:1 composer only):

1. Audio (multer limit **25 MB**) → Whisper (`settings.whisper.url`, default `http://127.0.0.1:5555`) `/transcribe`.
2. Transcript is wrapped as a spoken reply (shorter, square-bracket cues like `[laughs]`).
3. Same LLM routing as text chat (custom prompt, lore, Tanevan, tools).
4. Optional TTS: Fish, Chatterbox, NeuTTS (local `voice/` on **5050**), or ElevenLabs. Failure never blocks the text reply.

**Live calls** (phone button): ElevenLabs agent ID on the card, or Fish/Pipecat. After connect, **`/api/voice-call/context`** sends lore + memory + persona as a `contextual_update`. Transcripts save as a “📞 Voice call” system line plus `[voice call] …` turns and buffer to Tanevan.

**Video (Anam):** same ElevenLabs agent; **`POST /api/anam/session`**. Optional kiosk-style camera framing. Transcripts use the same save-transcript path.

Local TTS is **not** started by `./start.sh`. See **`voice/README.md`**.

### Images and short video

- **ComfyUI** — local Flux + LoRA / PuLID-style reference. Default URL in this repo is **`http://127.0.0.1:8000`** (ComfyUI’s own default is often 8188 — set whichever you actually run).
- **fal.ai** — cloud Flux / Flux LoRA (`falLoraUrl` on the card); Kling image-to-video.
- **Replicate** — Flux / Flux LoRA, Ideogram Character, Nano Banana-style multi-person shots from face refs; Kling video on the same account.
- LLM-authored scene prompts from recent chat (or the gallery scene hint), written for portrait-style generators (Nano Banana Pro / Flux). DALL·E / custom HTTP can be stored in Settings; **end-to-end generation is wired for ComfyUI, fal.ai, and Replicate**.

### Spotify

Web Playback SDK (browser as a Connect device), OAuth with refresh, `[spotify-search:]`, embeds, Now Playing dock, per-companion playlists, pasted Spotify URL detection. Premium required.

### Telegram (optional)

One bot per companion. Inbound webhooks **`POST /telegram`** and **`POST /telegram/:companion`** are registered **before** the login wall. Messages land in that companion's real 1:1 thread (same memories, same mood). Configure in Settings → Integrations: public HTTPS URL, webhook secret, your chat id, add/test bots. Replies loop through `/chat` with `x-internal-auth`.

### Diagnostics and sync

- **Debug log** — live SSE stream (chat, group, studio, voice, TTS, vision, Anam, URL visit, GIF, …); filters; per-request payload; export archive. Payloads also live under **`data/debug-log/`**. Press **`D`**.
- **LLM payload** — last request as sent: system (cache-coloured blocks), messages, raw debug. Context budget bar (stable 1h / memory 5m / dynamic uncached).
- **`GET /api/runtime-status`** — profile, paths, dependency reachability.
- **`GET /api/settings`** redacts keys; **`PUT /api/settings`** accepts full values from the UI.
- **WebSocket** `/ws-live-sync` — cross-tab / cross-device refresh when data changes.

### Authentication (optional)

Copy **`auth.example.js`** → **`auth.js`** (gitignored) for VPS / shared hosts. Without `auth.js`, auth is off.

```bash
node scripts/auth-users.js add yourname --role admin
node scripts/auth-users.js add friend --role guest --companion Aria
```

scrypt-hashed accounts in `data/auth/users.json`, SQLite sessions, per-IP rate limits, guests fenced to one companion with redacted settings. Behind a TLS proxy set **`AUTH_TRUST_PROXY=1`**.

---

## Tanevan (memory)

**This repo's `tanevan/` directory is the canonical copy.** `./start.sh` starts it from the Love Refactored root. An older standalone `~/tanevan` exists for SillyTavern experiments and lacks Memory Lens, `GET /inject`, OpenRouter/OpenAI pipeline routing, and Settings UI integration — do not use it here.

Tanevan converts conversations into a searchable, categorized dossier with confidence, priority, event dates, and semantic retrieval. **Every companion has an isolated database.** Node never blocks chat on a pipeline run: messages **buffer**, then a three-pass job turns them into memories. Live chat only **reads** via `GET /inject`.

### How a message becomes memory

```
Love Refactored (Node :3000)  --POST /buffer-->  Flask proxy (:5001)
                                                    |
                                                    |  per-companion SQLite buffer
                                                    v  (N messages / POST /flush / shutdown)
                                            ┌──────────────┐
                                            │  Summarizer  │  Pass 1 — emotional arc, narrative,
                                            └──────┬───────┘  topics, key moments (companion POV)
                                                   v
                                            ┌──────────────┐
                                            │  Extractor   │  Pass 2 — atomic memories
                                            └──────┬───────┘  5 categories + confidence + priority
                                                   v           + event date + intensity + entities
                                            ┌──────────────┐
                                            │   Updater    │  Pass 3 — add / update / merge / skip
                                            └──────┬───────┘
                                                   v
                                            SQLite dossier + ChromaDB vectors
```

Each pass is prefixed with **Memory Lens** and a **character-card voice reference** (personality + example messages) when configured. A separate **reflection** job writes looking-back documents on a schedule (injected into chat only when the card has `reflectionsEnabled: true`; horizons from `reflectionHorizons`, default `daily,weekly`). After a live or manual flush writes a **session summary**, Tanevan may spawn **`brief_sidecar.py`** to refresh that companion’s life brief (skipped during bulk import, and skipped for locked briefs unless regenerated).

1. The app `POST`s each turn to `/buffer` (including voice-call transcripts and video-attachment summaries).
2. Messages sit in `conversation_buffer` until the count hits **`TANEVAN_SUMMARIZE_EVERY`** (default **100**), you flush, or the proxy shuts down. Auto-flush and shutdown require **≥4** messages; a manual flush from the app can run on **1**. Long buffers are split by idle gap (default **2 hours**) and a token cap (default **12k**) before each session is summarised.
3. **Summarizer** writes a structured session summary from the companion's first person.
4. **Extractor** splits that into **atomic**, self-contained memories (one fact/event per row) that still make sense months later with no original transcript — category, confidence, priority, **entities**, and **emotional intensity** (0–10, stored).
5. **Updater** compares each new memory to near neighbours: add, **versioned update** (new row + `superseded_by` on the old one; in-place rewrite only if `TANEVAN_VERSIONED_UPDATE=0`), merge, or skip. Reinforcement raises confidence. A **merge guard** refuses blobs that would swallow protected/pinned rows or grow past a size cap — refused incoming text is saved as its **own** memory, never dropped.
6. On the next chat turn, Node calls **`GET /inject`**. Retrieval is semantic, plus date-range search when the query has temporal language, plus **rare-entity recall**. **Pinned** rows are **not** force-included — they appear when naturally recalled, then rank first, and a token budget will not drop them once selected. Lines are date-stamped when `event_date` is known so old events read as past. Sensitive rows get a separate “private knowledge” block (known, never raised unprompted).

### On-disk layout

Default root **`~/tanevan-data/`** (`TANEVAN_DATA_DIR`). Not inside the git clone.

```
~/tanevan-data/
├── pipeline_config.json      # Settings → Memory Pipeline
├── backups/                  # Settings → Backups (memory snapshots)
├── companion-a/
│   ├── memories.db           # SQLite
│   ├── memory_lens.json
│   ├── living_narrative.json # quarterly+ reflections; PUT locks prose fields
│   └── chroma_db/            # vector index
└── companion-b/
    └── …
```

Love Refactored also writes (under the app `data/` dir, not Tanevan’s):

```
data/recent/<companion>/      # scene notes (minis) — Node lib/minis.js
data/briefs/<companion>.txt   # life brief — Tanevan brief_sidecar.py
data/briefs/.brief_state.json # lock + last summary id
data/briefs/archive/<key>/    # prior brief versions
```

### SQLite (per companion)

| Table | Role |
|-------|------|
| `conversation_buffer` | Raw turns waiting for a pipeline run |
| `summaries` | Session summaries (emotional arc, narrative, topics, key moments) |
| `memories` | The dossier |
| `memory_versions` | Prior text of any rewrite (nothing is silently destroyed) |
| `reflections` | Temporal documents by horizon |
| `processing_log` | What has already been processed |
| `audit_log` | Consolidation, merge-guard, suppressions (retained ~90 days) |

**Memory fields (core):** `id`, `category`, `priority`, `confidence`, `content`, `source_summary_id`, `reinforcement_count`, `first_seen`, `last_seen`, `event_date`, `active`, `pinned`, `protected`, `sensitive`, `suppressed`, `entities`, `emotional_intensity`, `superseded_by`.

### Categories, priority, confidence

| Category | Stores |
|----------|--------|
| `fact` | Things known about people and the world |
| `experience` | Shared moments and events |
| `milestone` | Firsts and turning points |
| `preference` | Wants, likes, boundaries, behavioural instructions |
| `relationship` | Who people are to each other |

| Priority | Meaning |
|----------|---------|
| **core** | Identity. Assigned by the extractor or a human — auto-promote to core is **off** by default |
| **important** | Significant; reinforcement can promote up to here |
| **notable** | Colour |
| **minor** | Trivia |

Confidence 50–100. Intense memories get a small stored **intensity** (0–10) used in rerank (it is no longer smeared away into confidence and discarded). The same fact across sessions **boosts** confidence. **Decay** (`POST /decay`) lowers old unreinforced rows and deactivates those below a floor (**25**), plus a hard cull after **365** days unseen. Pinned, protected/sensitive, core, well-reinforced (`reinforcement_count > 6`), and recently seen (`< 30` days) rows are immune. Priority **demotion** can still move stale core/important rows down. **Nothing decays until you run it** (or a schedule you enabled).

**Flags** (pin and suppress are mutually exclusive):

- **Pinned** — favourites: when recalled they rank first and survive a token-budget trim; they are **not** stuffed into every turn
- **Protected** — never merged into, never decayed; slight retrieval floor so identity doesn't drown
- **Sensitive** — protected, plus etiquette: injected in a separate block; the companion shouldn't raise it unprompted
- **Suppressed** — stored, never injected
- **Inactive** — soft-delete; filtered from queries; Chroma vector pruned

### Retrieval (live chat)

`GET /inject`:

1. Embed the user's latest message (`BAAI/bge-base-en-v1.5`, 768-dim, query instruction prefix on).
2. Semantic search with a cosine **distance floor** (default 1.2) and a confidence floor of 40 (pinned/protected/sensitive bypass the floor). **Entity recall** for rare names runs inside this pass (reserved slots + IDF from the curated `entities` column).
3. Temporal language (“last week”, “in November”, …) → extra SQLite date filter, merged and deduped.
4. Rerank: priority, recency, confidence, category weights, intensity, protected floor. Recalled **pinned** rows then sort first. At most **one** highly relevant `dream`-category row may inject (legacy schema; dreams are not a product feature).
5. Injected rows refresh `last_seen` (not `reinforcement_count`) on the ranked set **before** any token-budget trim.
6. Optional **token budget** (`?budget=`) so ten 800-character blobs don't cost the same as ten one-liners. Protected and pinned rows already selected are never dropped; `TANEVAN_INJECT_MIN` (default 3) is a floor.

Embeddings use **enriched text**, e.g. `[MILESTONE | core | ABOUT: …] We said I love you…`. Changing the embed model or format requires **`reembed.py`** (stop Tanevan first).

### Reflections

Horizons stack; each layer reads the one below, not raw chat:

| Horizon | Window | Reads | Living narrative |
|---------|--------|-------|------------------|
| daily | 1 day | session summaries | no |
| weekly | 7 days | daily | no |
| monthly | 30 days | weekly | no |
| quarterly | 90 days | monthly | yes |
| biannual | 180 days | quarterly | yes |
| annual | 365 days | biannual | yes |

Node caches the injection block and refreshes it in the background (not on the send path). Chat only includes it when `reflectionsEnabled` is true on the card; which horizons ride along is `reflectionHorizons` (default `daily,weekly`).

### Pipeline models

Settings → Memory Pipeline (or `~/tanevan-data/pipeline_config.json`): global provider **Anthropic / OpenAI / OpenRouter / local / hybrid**, with per-step models for **summarizer, extractor, updater, reflection, and brief**. Keys can live in that file; otherwise **`ANTHROPIC_API_KEY`** (exported by `start.sh` from `data/settings.json`), **`OPENAI_API_KEY`** / **`OPENROUTER_API_KEY`**, or Love Refactored settings. Pipeline steps have **no hardcoded model IDs** — configure before the first run. Exception: if **`BRIEF_MODEL_KEY`** is set (legacy sidecar path), `BRIEF_MODEL_BASE` defaults to OpenRouter and `BRIEF_MODEL_NAME` to `deepseek/deepseek-chat`. The Brief model also powers scene notes (`lib/minis.js`); if unset, minis try extractor → updater, then the companion’s chat model.

### Source files

| File | Role |
|------|------|
| `proxy.py` | Flask server: buffer, inject, import, config, lens, reflections, backups, audit, brief sidecar trigger / regenerate |
| `pipeline.py` | Orchestrates summarise → extract → update (session-gap + token-cap splits) |
| `pipeline_llm.py` | Routes each step (including **brief**) to the configured provider |
| `summarizer.py` | Pass 1 |
| `extractor.py` | Pass 2 |
| `updater.py` | Pass 3 (versioned update by default) |
| `memory_db.py` | SQLite + Chroma, embeddings, inject, decay, flags |
| `memory_lens.py` | Load/save lens; prefix pipeline prompts |
| `companion_resolve.py` | Default companion + voice reference from the character card |
| `consolidation.py` | Near-duplicate pairs; keep / merge / supersede; auto-promote (caps at **important** unless `TANEVAN_AUTO_PROMOTE_CORE=1`) |
| `reflection.py` | Temporal reflections + living narrative |
| `brief_sidecar.py` | Rolling life brief after each new session summary (skips locked companions unless `--force`) |
| `recent_summary_sidecar.py` | Legacy buffer-chunk scene notes (live path is `lib/minis.js`) |
| `user_timezone.py` | IANA timezone for briefs / scene-note prompts |
| `db_time.py` | UTC helpers for SQLite timestamps |
| `export.py` | Dump dossier + summaries as text |
| `backfill_event_dates.py` | Repair `event_date` / first_seen / last_seen from summaries |
| `backfill_entities.py` | Repair entity fields (run **before** reembed on upgrades) |
| `reembed.py` | Rebuild Chroma after model or embed-text changes |
| `prune_chroma_ghosts.py` | Drop vectors whose SQLite rows are gone |
| `prompts/brief_default.txt` | Shipped global brief condenser prompt |
| `prompts/recent_starter.txt` | Default prompt if the Python recent sidecar is run |

**Existing installs** upgrading the store: stop Tanevan, `python3 tanevan/backfill_entities.py`, then `python3 tanevan/reembed.py`, then start again.

### HTTP (companion via `?companion=` or JSON `"companion"`)

| Method | Path | Role |
|--------|------|------|
| `GET` | `/health` `/stats` `/pipeline-status` | Status |
| `GET` | `/inject` `/search` `/memories` | Retrieval and browse (`/inject` accepts `?budget=` tokens) |
| `PUT` | `/memory/<id>` | Edit flags/content (`protected`, `sensitive` included) |
| `POST` | `/buffer` `/flush` | Ingest / run pipeline |
| `POST` | `/import` · `GET` `/import/status/<job_id>` | Bulk history (chunked async, 100-message batches; 50k+ messages tested) |
| `POST` | `/decay` `/consolidate` `/consolidate/apply` | Maintenance |
| `GET` | `/audit` | Merge / guard trail |
| `POST` | `/reflect` · `GET` `/reflections` `/living-narrative` `/reflection-injection` | Reflections |
| `PUT` | `/summary/<id>` · `/reflection/<id>` · `/living-narrative` | Edit summaries / reflections; living-narrative PUT **locks** the document |
| `POST` | `/brief/regenerate` | Force-refresh life brief (`--force`; bypasses hand-edit lock) |
| `GET` | `/recent-summaries` | Lean session narratives for the **brief** sidecar (not live-chat minis) |
| `GET`/`PUT` | `/memory-lens` `/config` | Lens and pipeline config |
| `POST`/`GET` | `/backup` `/backup/list` `/backup/prune` | Memory DB snapshots |
| `DELETE` | `/companion-data` | Wipe a companion's store |
| `POST` | `/test-llm-connection` `/test-llm-pipeline` | Settings tests |
| `POST` | `/v1/chat/completions` | Legacy OpenAI-compatible proxy (LM Studio + inject) |

Love Refactored sends **`user_name`** (persona display name) on buffer/flush/import.

### Environment (Tanevan)

| Variable | Default | Role |
|----------|---------|------|
| `TANEVAN_DATA_DIR` | `~/tanevan-data` | All companion DBs + pipeline config |
| `TANEVAN_PROXY_PORT` / `_HOST` | `5001` / `127.0.0.1` | Bind |
| `TANEVAN_SUMMARIZE_EVERY` | `100` | Auto-flush threshold |
| `TANEVAN_COMPANION_NAME` | first in UI order | Default companion |
| `TANEVAN_USER_NAME` | `the user` | Fallback label |
| `TANEVAN_URL` | from settings | Node → proxy (multi-instance) |
| `TANEVAN_DISTANCE_FLOOR` | `1.2` | Chroma relevance ceiling |
| `TANEVAN_INJECT_MIN` | `3` | Minimum memories when budgeting by tokens |
| `LR_DATA_DIR` | `$LR_HOME/data` | Where Node writes `recent/` (minis) |
| `BRIEF_OUT_DIR` | `$LR_HOME/data/briefs` | Where the brief sidecar writes life briefs |
| `BRIEF_TZ` | (settings / host) | IANA timezone override for brief/minis dates |
| `BRIEF_USE_LENS` | `1` | Fill brief prompt placeholders from Memory Lens |

Many retrieval/guard behaviours have kill switches (`TANEVAN_MERGE_GUARD`, `TANEVAN_ENTITY_RECALL`, `TANEVAN_MEMORY_VERSIONS`, …) — see comments in `memory_db.py`.

Full CLI examples and prompt-level detail: **`tanevan/README.md`**.

---

## Prerequisites

- **Node.js** v18+ — [https://nodejs.org](https://nodejs.org)
- **Python** 3.10+ — [https://python.org](https://python.org)
- **ComfyUI** (optional, for local images) — [https://github.com/comfyanonymous/ComfyUI](https://github.com/comfyanonymous/ComfyUI)

### Python packages (Whisper server)

```bash
pip install openai-whisper flask flask-cors certifi
```

Optional: **`voice/sensevoice_server.py`** uses **FunASR SenseVoice** on the same **`:5555`** `/transcribe` contract (see the file header for dependencies).

---

## Setup

### 1. Clone and install

```bash
git clone https://github.com/Love-Encoded/LoveRefactored.git
cd LoveRefactored
npm install
pip3 install -r tanevan/requirements.txt --break-system-packages
# optional: python3 -m venv venv && source venv/bin/activate && pip install -r tanevan/requirements.txt
```

### 2. Configure settings

```bash
mkdir -p data
if [ -f data/settings.example.json ]; then
  cp data/settings.example.json data/settings.json
fi
```

If **`data/settings.json`** does not exist, **`./start.sh`** will create it (or copy a legacy repo-root `settings.json` into `data/` once). Edit the file or use in-app Settings. Python helpers use **`lib/lr_settings.py`** for the same path resolution as **`start.sh`**.

| Service | Role |
|--------|------|
| **Anthropic** | Claude chat; mood evaluation; vision; emotional profile; typical memory pipeline |
| **OpenAI** | Alternative chat / pipeline provider |
| **OpenRouter** | Aggregated models via one key |
| **LM Studio** | Local models |
| **ElevenLabs** | Live voice / video signing; optional memo TTS |
| **Anam** | Video avatar sessions |
| **fal.ai** | Cloud Flux / Flux LoRA; Kling video |
| **Replicate** | Cloud Flux / LoRA / Ideogram / group images; Kling video |
| **Brave** | `[search: …]` |
| **Klipy** | `[gif: …]` — Settings → Integrations |
| **Spotify** | Playback and playlists (developer app + Premium) |

### 3. Seed companions

Create **`data/companion_seeds.json`** (or let the app create it):

```json
[
  { "name": "Aria", "avatar": "✨" }
]
```

Companions are created on load. Refine cards in the UI — or use **New companion** for the full walkthrough.

### 4. Spotify (optional)

1. App at [https://developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) with **Web API** and **Web Playback SDK**.
2. Redirect URI: `http://127.0.0.1:3000/spotify/callback`
3. Paste Client ID / Secret in Settings → Integrations → save → Connect.

### 5. Voice: Whisper, local TTS, and ElevenLabs

**Whisper** is started by **`./start.sh`** on **5555**. Override with **`whisper.url`**. Optional SenseVoice implements the same `POST /transcribe` contract.

**Local TTS** (Fish / Chatterbox / NeuTTS): run the matching `voice/` service on **5050**. MP3s land in **`data/voice_messages/`**. Without TTS, memos still return text.

**ElevenLabs:** API key in Settings → Voice & video; **Agent ID** on the card for live calls and Anam. At call time the app injects lore + memory + persona via `/api/voice-call/context`.

### 6. Video (Anam)

Anam API key in Settings; **Avatar ID** + **ElevenLabs Agent ID** on the card.

### 7. Migrate existing chat history (upgrades only)

```bash
npm run migrate-chat
```

After that, live chat uses **`data/love.db`** only. JSON files remain until you delete them.

### 8. Authentication (VPS / internet-facing only)

```bash
cp auth.example.js auth.js
node scripts/auth-users.js add yourname --role admin
```

Password min 12 characters. Restart after creating `auth.js`.

### 9. Run

```bash
./start.sh
```

Opens **http://localhost:3000**, starts Whisper (`:5555`), the UI server (**nodemon** locally; plain **node** when **`LR_PROFILE=vps`**), then Tanevan (`:5001`) when at least one companion exists (or **`TANEVAN_COMPANION_NAME`** is set). On a fresh install with no companions, create one and re-run **`./start.sh`** to bring memory online.

Production: **`npm install --omit=dev`**. On a VPS that already runs under PM2, **`ecosystem.config.cjs`** is a path-only config for `/opt/love-refactored` (`lr-server`, optional Pipecat / Fish). Keys belong in that host’s `.env`, not in the PM2 file. It does **not** start Whisper or Tanevan — those still come from **`./start.sh`** or your own process manager.

| Variable | Default | Role |
|----------|---------|------|
| **`LR_HOME`** | repo root | App root; **`data/`** lives here |
| **`LR_PROFILE`** | `local` | `local` or `vps` — see **`RUNTIME-CONTRACT.md`** |
| **`VOICE_MESSAGES_DIR`** | `$LR_HOME/data/voice_messages` | Memo MP3s |
| **`PORT`** | `3000` | Web + WebSocket |
| **`TANEVAN_PROXY_PORT`** | `5001` | Tanevan |
| **`WHISPER_PORT`** | `5555` | STT |
| **`TANEVAN_PROXY_HOST`** | `127.0.0.1` | Tanevan bind (keep localhost on a VPS) |
| **`TANEVAN_URL`** | from settings | Node → Tanevan |

Process logs: **`~/.love-refactored/`** (`ui.log`, `whisper.log`, `tanevan.log`, `pids.txt`).

```bash
npm start          # same as ./start.sh
./stop.sh          # or npm run stop
```

---

## Project layout

```
LoveRefactored/
├── server.js              # Express app: LLM routing, image/voice/proactive
├── lib/                   # system-prompt.js, minis.js, image-prompts.js,
│                          #   background-schedule.js, lr_settings.py
├── auth.example.js        # Copy to auth.js (gitignored) for public hosts
├── routes/                # calendar, companions, groups, history-chat,
│                          #   logs-settings, lorebooks, parlors, persona, spotify,
│                          #   telegram, v3-shell
├── db/                    # SQLite chat (chat.js, chat-storage.js, chat-backup.js)
├── scripts/               # migrate-chat-to-sqlite.js, auth-users.js, dedup-data.py
├── public/
│   ├── v3/                # Primary UI at /  (index.html + logos)
│   ├── wall.html          # The Wall (also iframed from v3)
│   └── archive.html       # Standalone Archive at /archive
├── data/                  # Runtime state (git-ignored except data/.gitkeep)
│   ├── settings.json
│   ├── persona.json
│   ├── companions/        # Character cards, avatars, galleries
│   ├── love.db            # Chat history + audit log + wall posts
│   ├── recent/            # Scene notes (minis)
│   ├── briefs/            # Life briefs + .brief_state.json + archive/
│   ├── voice_messages/
│   ├── backups/           # love-*.db snapshots
│   └── …                  # chat_logs/, journals/, groups/, parlors/, debug-log/, auth/, …
├── start.sh / stop.sh
├── ecosystem.config.cjs   # Optional PM2 shape for /opt/love-refactored (does not start Whisper/Tanevan)
├── LICENSE.md
├── CLA.md
├── BETA_AGREEMENT.md
├── SECURITY.md
├── RUNTIME-CONTRACT.md
├── VERIFICATION-RUNBOOK.md
├── FAILURE-MATRIX.md
├── voice/                 # Whisper / SenseVoice / Fish / Chatterbox / NeuTTS / Pipecat
└── tanevan/               # Memory service — see section above
```

Do not keep Tanevan databases inside the clone.

---

## Ports

| Port | Service |
|------|---------|
| 3000 | Web + WebSocket |
| 5001 | Tanevan |
| 5050 | Local TTS (optional; not started by `start.sh`) |
| 5555 | Whisper / SenseVoice |
| 8000 (this repo’s default) | ComfyUI — set the URL in Settings (upstream ComfyUI is often 8188) |

On a VPS, only **3000** should be reachable from the internet. Tanevan, Whisper, and TTS bind to **127.0.0.1** by default.

---

## Generation parameters vs provider

| Parameter | Anthropic | OpenAI / OpenRouter / custom | LM Studio |
|-----------|-----------|------------------------------|-----------|
| Temperature | ✅ | ✅ | ✅ |
| Top P | ✅ | ✅ | ✅ |
| Top K | ❌ | ❌* | ✅ |
| Min P | ❌ | ✅* (sent only when > 0) | ✅ |
| Max tokens | ✅ | ✅ | ✅ |
| Frequency penalty | ❌ | ✅ | ✅ |
| Presence penalty | ❌ | ✅ | ✅ |
| Stop sequences | ✅ | ✅ (OpenAI max 4) | ✅ |

\*Depends on upstream; local servers vary. **`sendSamplingParams: false`** omits sampling fields entirely.

---

## Troubleshooting

- **Port in use** — `./stop.sh`, or free **3000 / 5001 / 5555**. TTS uses **5050** separately.
- **Whisper errors** — Python deps; `start.sh` sets **`SSL_CERT_FILE`** / **`REQUESTS_CA_BUNDLE`** when `certifi` is available.
- **Voice memos: text only** — pick a TTS provider and start the matching `voice/` server; confirm MP3s under `data/voice_messages/`. Transcription still needs Whisper on **5555**.
- **Live voice calls fail** — ElevenLabs key + per-companion **Agent ID**; mic permission; debug log `elevenlabs-convai` / `voice-context`.
- **No video** — Anam key, Avatar ID, and Agent ID all set.
- **Images** — ComfyUI reachable with Flux + workflow nodes; fal: `imageProvider` `fal` + key + optional `falLoraUrl`; Replicate: token + face refs for couple/group.
- **GIFs missing** — Klipy key in Settings → Integrations; companions use `[gif: …]`.
- **Spotify** — Premium; use “Play in Love Refactored” where applicable.
- **Memory** — Tanevan on **5001** (re-run `./start.sh` after the first companion); Settings → Server & schedule / Pipeline tests. Upgrading the store: `backfill_entities.py` then `reembed.py`. No life brief until a Brief model is set and a session summary has completed. A hand-edited brief stays locked until you regenerate. Scene notes need 20 new 1:1 messages after the last mini. Reflections only reach chat when Character → Intermediate continuity is on.
- **Lost messages on restart** — wait a few seconds after send so SQLite can flush.
- **JSON history upgrade** — `npm run migrate-chat` once, confirm the UI, then delete `data/chat_history/`.
- **Public deploy without auth** — copy `auth.example.js`, create an admin, restart.
- **Parlor** — both instances must reach `/parlor-io`; set **`PARLOR_ALLOWED_ORIGINS`** to the exact guest-visible origin(s).

`npm run check` syntax-checks `server.js`, `auth.example.js`, `scripts/auth-users.js`, `db/*.js`, `routes/*.js`, `lib/*.js`, the migrate script, `lib/lr_settings.py`, and key Python entrypoints (Tanevan + `voice/` servers including `fish_asr_proxy.py`).

---

## Related documentation

| Doc | Purpose |
|-----|---------|
| **`tanevan/README.md`** | Pipeline prompts, full API, CLI |
| **`voice/README.md`** | Local TTS / optional STT servers |
| **`RUNTIME-CONTRACT.md`** | `LR_HOME`, `LR_PROFILE`, settings resolution, service contracts |
| **`VERIFICATION-RUNBOOK.md`** | Smoke checks after deploy or config changes |
| **`FAILURE-MATRIX.md`** | Known breakpoints, health probes, graceful degradation |
| **`LICENSE.md`** | License terms |
| **`CLA.md`** | Contributor license agreement |
| **`BETA_AGREEMENT.md`** | Beta participation terms — accepted in-app on first launch |
| **`SECURITY.md`** | Where to report bugs and security issues, and what to scrub first |

---

## License

Love Refactored and Tanevan are source-available for personal, non-commercial self-hosting, study, and modification only. They are not open-source software.

Commercial use, redistribution, hosted services, AI training/fine-tuning, competing companion platforms, or use of Tanevan as the backend/memory layer for another distributed AI system require written permission from both authors.

**Contact:** Megan Neves — hi@meganneves.com; Discord - kandykidsaturn ; Tiara Young — tiara@tiara.nz; Discord - tiara.nz

See **[LICENSE.md](LICENSE.md)**.
