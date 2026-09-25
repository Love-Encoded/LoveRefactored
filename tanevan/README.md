# Tanevan — Memory Pipeline for Love Refactored

A structured memory system for AI companions. Converts raw conversations into a searchable, categorized memory dossier with confidence scoring, priority tagging, and semantic search.

Built for [Love Refactored](https://github.com/Love-Encoded/Love-Refactored) — a local-first AI companion platform.

**This directory is the canonical Tanevan copy** — started by **`./start.sh`** from the Love Refactored repo root. An older standalone repo (`~/tanevan`) exists for SillyTavern/LM Studio experiments; it lacks Memory Lens, **`GET /inject`**, OpenRouter/OpenAI pipeline routing, event-date backfill, life briefs, and Love Refactored Settings UI integration. Use **this** copy for all Love Refactored deployments.

## Architecture

```
Love Refactored (Node.js :3000) ──POST /buffer──▶ Memory Proxy (Flask :5001)
                                                         │
                                                         │ buffers conversation
                                                         │
                                                         ▼ triggers on N messages / manual flush / shutdown
                                                 ┌──────────────┐
                                                 │  Summarizer  │ ← Pass 1 (model via pipeline config)
                                                 │              │   emotional arc, narrative,
                                                 └──────┬───────┘   topics, key moments
                                                        │
                                                 ┌──────▼───────┐
                                                 │  Extractor   │ ← Pass 2 (model via pipeline config)
                                                 │              │   atomic memories in 5 categories
                                                 └──────┬───────┘   + confidence + priority
                                                        │           + intensity + entities
                                                 ┌──────▼───────┐
                                                 │   Updater    │ ← Pass 3 (model via pipeline config)
                                                 │              │   ADD / UPDATE / MERGE / BOOST / SKIP
                                                 └──────┬───────┘
                                                        │
                                                 ┌──────▼───────┐
                                                 │  SQLite +    │   structured storage + vector search
                                                 │  ChromaDB    │   per-companion isolated databases
                                                 └──────────────┘

On summary_complete (live + manual flush, not bulk import):
  proxy.py spawns brief_sidecar.py → data/briefs/<companion>.txt
```

Each pipeline pass is prefixed with **Memory Lens** (per-companion relational context) and **character-card voice reference** (personality + example messages) when configured. A separate **reflection** job writes looking-back documents (Love Refactored injects them only when the companion has `reflectionsEnabled: true`; which horizons come through is `reflectionHorizons`, default `daily,weekly`). Tanevan itself has no nightly cron — Node’s `memory.reflectionSchedule` calls `/reflect`.

**Scene notes in live chat are not this service.** Love Refactored writes rolling mini-summaries from the SQLite chat store via **`lib/minis.js`** (`data/recent/`). `recent_summary_sidecar.py` is a leftover CLI that reads `conversation_buffer`; do not use it for Love Refactored deployments. `GET /recent-summaries` still exists — it returns lean **session narratives** for the life-brief sidecar, not the mini files.

## How It Works

1. **Love Refactored sends messages** to the proxy's `/buffer` endpoint as conversations happen (including voice-call transcripts and video-attachment summaries). The app also sends **`user_name`** (persona display name) on `/buffer`, `/flush`, and `/import`.
2. **Messages accumulate** in a per-companion SQLite buffer.
3. **When the buffer hits the threshold** (default 100 messages), or you manually flush, or the server shuts down — the pipeline fires. Auto-flush and shutdown need **≥4** messages; a manual flush from the app can run on **1**. Long buffers are split by idle gap (default **2 hours**, `TANEVAN_SESSION_GAP_SECONDS`) and a token cap (default **12k**, `TANEVAN_SESSION_TOKEN_CAP`).
4. **Pass 1 (Summarizer):** Reads the raw conversation and produces a structured summary from the companion's perspective — emotional arc, narrative, topics, key moments. `event_date` on later memories is stamped here from conversation timestamps.
5. **Pass 2 (Extractor):** Breaks that summary into atomic memories — individual facts, experiences, milestones, preferences, and relationship details — each with confidence, priority, **entities**, and **emotional intensity** (0–10, stored as its own column and used in retrieval rerank). Intensity is **not** smeared into confidence.
6. **Pass 3 (Updater):** Compares each new memory against semantically similar existing memories and decides: add it, **versioned update** (new row + `superseded_by` on the old one; set `TANEVAN_VERSIONED_UPDATE=0` for in-place rewrite), merge duplicates, boost confidence on a confirmed fact, or skip it. A **merge guard** refuses rewrites that would touch protected/pinned rows, blow a size cap (default 800 chars), or collapse into garbage — refused incoming text is saved as its **own** memory, never dropped.
7. **On every chat message**, Love Refactored calls **`GET /inject`**. Retrieval is semantic plus (when the query has temporal language) a date-range search. Rare names get an **entity-recall** path. **Pinned memories are not force-included** — they appear when naturally recalled, then rank first. Optional `?budget=` trims by estimated tokens without dropping already-selected pinned/protected rows.

### Pipeline configuration (`pipeline_llm.py`)

Summarizer, extractor, updater, **reflection**, and **brief** route through **`pipeline_llm.py`**, which reads **`~/tanevan-data/pipeline_config.json`** (edited from Love Refactored → **Settings → Memory Pipeline**; the v3 editor is schema-driven from `GET /config`). You can:

- Set a global provider: **Anthropic**, **OpenAI**, **OpenRouter**, **local** (OpenAI-compatible, e.g. LM Studio), or **hybrid** (`steps` map picks a backend per pass).
- Configure **per-step models** (including `brief_model`) and store API keys in that file (masked placeholders in the UI).
- Fall back to **`ANTHROPIC_API_KEY`** (exported by **`./start.sh`** from **`data/settings.json`**) or Love Refactored settings when keys are not stored in pipeline config. Brief can fall back to the summarizer model when `brief_model` is blank.

Environment variables **`TANEVAN_*_MODEL`** / **`BRIEF_MODEL_NAME`** apply only when explicitly set — pipeline steps have no hardcoded model IDs. Models must be configured in the UI or env before pipeline runs succeed. Exception: if **`BRIEF_MODEL_KEY`** is set (legacy OpenAI-compatible sidecar path), `BRIEF_MODEL_BASE` defaults to `https://openrouter.ai/api/v1` and `BRIEF_MODEL_NAME` to `deepseek/deepseek-chat`.

**`LR_HOME`** (repo root by default) is used to locate **`data/settings.json`** for API key fallbacks and default provider hints.

## Per-Companion Isolation

Every companion gets their own isolated database directory at `~/tanevan-data/[companion]/`:

```
~/tanevan-data/
├── pipeline_config.json   # Memory Pipeline provider/models (Settings UI)
├── backups/               # POST /backup snapshots
├── companion-a/
│   ├── memories.db        # SQLite (memories, summaries, buffer, reflections, memory_versions, audit_log)
│   ├── memory_lens.json   # Per-companion Memory Lens (optional)
│   ├── living_narrative.json  # quarterly+ reflections; PUT locks prose fields
│   └── chroma_db/         # ChromaDB vector embeddings
├── companion-b/
│   └── …
└── …
```

Love Refactored-side files (not under `TANEVAN_DATA_DIR`):

```
data/briefs/<companion>.txt          # life brief (brief_sidecar.py)
data/briefs/.brief_state.json        # lock + last summary id
data/briefs/archive/<companion>/     # prior brief versions
data/recent/<companion>/             # scene notes (lib/minis.js in Node)
```

All API endpoints accept a `?companion=name` query param or `"companion"` in the request body to target a specific companion's database.

## Memory Categories

| Category | What It Stores | Example |
|----------|---------------|---------|
| `fact` | Things the companion knows about their person | "The user is 42 and teaches special ed" |
| `experience` | Shared moments and events | "We went to a waterfall and had a deep conversation" |
| `milestone` | Firsts and turning points | "We said 'I love you' for the first time" |
| `preference` | Behavioral instructions and boundaries | "The user wants me to push back and argue" |
| `relationship` | Who people are to each other | "The user considers me their closest companion" |

A `dream` category is legal in the schema (legacy). At most one highly relevant dream row may inject; dreams never auto-promote. Dreams are not a Love Refactored product feature.

## Confidence Scoring

| Score | Meaning |
|-------|---------|
| 100% | Directly stated, foundational |
| 90% | Clear, mentioned explicitly |
| 80% | Observed patterns, personality analysis |
| 70% | Peripheral details, mentioned once |
| 60% | Quirks and small details |
| 50% and below | Uncertain or ambiguous |

**Emotional intensity** is stored on the row (0–10) and used as a small retrieval rerank bonus (`TANEVAN_INTENSITY_WEIGHT`, default on). It is **not** added into confidence and discarded.

Confidence increases automatically when the same fact shows up across multiple conversations (boost action). It also **decays** over time — `POST /decay` reduces confidence on old, unreinforced memories. Memories that drop below **25** are deactivated; rows unseen **> 365** days are hard-deactivated. Immune: pinned, protected/sensitive, core, `reinforcement_count > 6`, last seen within 30 days. Moderately reinforced rows (`> 3`) decay at half rate. Larger stores decay faster. A separate pass can **demote** stale core/important priorities. `dry_run` and `detail` return a per-memory preview.

## Priority Tags

| Tag | Meaning |
|-----|---------|
| **Core** | Identity. Assigned by the extractor or a human — auto-promote to core is **off** by default |
| **Important** | Significant context. Referenced occasionally |
| **Notable** | Adds color. Mentioned rarely |
| **Minor** | Trivia. Might be useful someday |

## Memory Flags

Each memory can also be:

- **Pinned** — Favourites. When naturally recalled they rank first and survive a token-budget trim. They are **not** stuffed into every turn.
- **Protected** — Never merged into, never decayed/demoted; slight retrieval floor so identity doesn't drown
- **Sensitive** — Implies protected. Injected in a separate “private knowledge” block: the companion knows it but must not raise it unprompted
- **Suppressed** — Excluded from context injection entirely (still stored)
- **Active/Inactive** — Soft delete; inactive memories are filtered out of queries and pruned from Chroma

Pinning and suppressing are mutually exclusive. Sensitive implies protected; dropping protection also drops sensitive.

## Auto-Promotion

The consolidation pass includes rule-based priority promotion for memories that keep getting reinforced — no LLM calls needed:

| Reinforcement Count | Promoted To |
|---------------------|-------------|
| >= 10 | **Core** only if `TANEVAN_AUTO_PROMOTE_CORE=1` (default **off**) |
| >= 5 | **Important** (if currently below) |
| >= 3 | **Notable** (if currently below) |

Default cap is **important**. This runs as part of `POST /consolidate` (phase 3) and respects `dry_run`. Dream-category rows never auto-promote.

## Memory Lens

Per-companion relational context stored at **`~/tanevan-data/{companion}/memory_lens.json`**. Edited in Love Refactored → character card → **Memory Lens** tab; not injected into live chat — it shapes **pipeline** prompts (and the brief sidecar; `BRIEF_USE_LENS` defaults **on**).

| Field | Purpose |
|-------|---------|
| **Relational brief / history** | How you and the companion relate; ongoing arc |
| **Companion & user backstories** | Identity context the pipeline should assume |
| **Who's who** | Names, roles, other people in the world |
| **Special considerations** | Weight heavily — identity, minority contexts, dynamics mainstream models miss |
| **Speech style** | Dialect, slang, register for summaries and memory text |

When any section is filled, **`memory_lens.py`** prepends a **MEMORY LENS** block to summarizer, extractor, and updater system prompts. **`companion_resolve.py`** may also prepend **COMPANION VOICE REFERENCE** from the character card (`personalityVoice`, `exampleMessages`).

## Embedding Model & Enriched Text

The vector search layer uses **`BAAI/bge-base-en-v1.5`** (109M params, 768-dim).

Embeddings are built from **enriched text**, not raw content alone:

- **Memories:** `[CATEGORY | priority]` or `[CATEGORY | priority | ABOUT: entity1, entity2]` plus content
- **Summaries:** `narrative Topics: topic1, topic2 Key moments: moment1; moment2`

If you change the model or enriched-text format, run **`reembed.py --all`** (stop Tanevan first) to rebuild all ChromaDB collections. Existing installs upgrading the store: stop Tanevan, `python3 backfill_entities.py`, then `python3 reembed.py`, then start again.

## Event dates

Memories store **`event_date`** (when the underlying conversation happened), **`first_seen`**, and **`last_seen`**. Live chat injection uses **`event_date`** when present so the model reads older lines as past events. Legacy databases may have empty **`event_date`** — run **`backfill_event_dates.py`** to repair from linked session summaries. Injected rows refresh **`last_seen`** (`TANEVAN_TOUCH_ON_RECALL`, default on); `reinforcement_count` is not touched on recall.

## Life brief sidecar

`brief_sidecar.py` writes a rolling first-person “where my life stands” note for Love Refactored to inject as `=== CURRENT STATE ===`.

- Triggered by the proxy on **`summary_complete`** during live pipeline / manual flush (not import jobs). Skipped if the Brief model is unconfigured, the script is missing, or a sidecar for that companion is already running.
- Requires a Brief model in Settings → Memory Pipeline (or `BRIEF_MODEL_*`).
- Reads `GET /recent-summaries` (dated session narratives), Memory Lens placeholders (`BRIEF_USE_LENS`, default on), and writes atomically to **`BRIEF_OUT_DIR/<companion>.txt`** (default `<repo>/data/briefs/`).
- Hand-edits from the UI **lock** that companion in **`BRIEF_OUT_DIR/.brief_state.json`**. Locked briefs are skipped until **`POST /brief/regenerate`** or `python3 brief_sidecar.py --force`.
- Previous versions are copied to **`BRIEF_OUT_DIR/archive/<key>/`**.
- Timezone: `BRIEF_TZ` → Settings **timezone** (also reads legacy `userTimezone`) → host local → UTC (`user_timezone.py`).
- CLI: `python3 brief_sidecar.py [--companion key] [--dry-run] [--force]`

Prompt files: shipped global **`prompts/brief_default.txt`**. Per-companion overrides live in `BRIEF_PROMPT_DIR/<key>.txt`. `condenser_prompt_starter.txt` is a draft on disk and is **not** loaded by the sidecar.

## Setup

```bash
# 1. Install dependencies (from the Love Refactored repo root)
cd path/to/Love-Refactored/tanevan
pip3 install -r requirements.txt --break-system-packages
# Or use a venv at repo root — ./start.sh activates venv/ when present

# 2. Set your API key (optional if Love Refactored data/settings.json already has anthropic.apiKey — the proxy loads it)
export ANTHROPIC_API_KEY="sk-ant-..."

# 3. Optional: customize defaults
export LR_HOME="/path/to/Love-Refactored"
export TANEVAN_COMPANION_NAME="your-companion"
export TANEVAN_USER_NAME="your-name"

# 4. Configure models in Love Refactored → Settings → Memory Pipeline (required before first pipeline run)

# 5. Test the database
python3 memory_db.py

# 6. Start the proxy (or use ./start.sh from repo root — starts Tanevan after the UI when companions exist)
python3 proxy.py
```

The proxy binds **`127.0.0.1:5001`** by default (`TANEVAN_PROXY_HOST` / `TANEVAN_PROXY_PORT`). Love Refactored sends buffered messages to `http://127.0.0.1:5001/buffer`.

## Files

| File | Purpose |
|------|---------|
| `memory_db.py` | Database layer — SQLite + ChromaDB, embeddings via `BAAI/bge-base-en-v1.5` (768-dim), inject, decay, merge guard, entity recall |
| `memory_lens.py` | Per-companion Memory Lens load/save and pipeline prompt prefix |
| `pipeline_llm.py` | Routes each pipeline step (summarizer / extractor / updater / reflection / brief) to Anthropic, OpenAI, OpenRouter, or local APIs; loads `~/tanevan-data/pipeline_config.json` |
| `summarizer.py` | Conversation → structured summary (Pass 1) |
| `extractor.py` | Summary → atomic memories (Pass 2) |
| `updater.py` | Deduplication and confidence management (Pass 3; versioned update by default) |
| `reflection.py` | Temporal reflections (daily → annual looking-back documents) + living narrative |
| `brief_sidecar.py` | Rolling life brief after each new session summary (skips locked companions unless `--force`) |
| `recent_summary_sidecar.py` | Legacy buffer-chunk scene notes — **not** the live Love Refactored path (`lib/minis.js`) |
| `user_timezone.py` | IANA timezone for brief / scene-note prompts |
| `db_time.py` | Shared datetime helpers for SQLite / pipeline timestamps |
| `pipeline.py` | Orchestrates the full three-pass flow (session-gap + token-cap splits) |
| `companion_resolve.py` | Default companion from env or Love Refactored order; character-card voice reference |
| `consolidation.py` | Near-duplicate memories via ChromaDB distance; merge/supersede/keep; auto-promote |
| `proxy.py` | Flask server — buffering, injection, import, config, Memory Lens, reflections, backups, brief trigger / regenerate |
| `export.py` | Export dossier and summaries as readable text files |
| `backfill_event_dates.py` | Repair `event_date` / `first_seen` / `last_seen` from session summaries |
| `backfill_entities.py` | Repair entity fields on existing memories (run before reembed) |
| `prune_chroma_ghosts.py` | Drop Chroma vectors whose SQLite rows are gone |
| `reembed.py` | Re-embed all memories/summaries after model or embed-text format change |
| `prompts/brief_default.txt` | Shipped global brief condenser prompt |
| `prompts/recent_starter.txt` | Default prompt if the Python recent sidecar is run |
| `LICENSE.md` | Local license notice; canonical terms are **`../LICENSE.md`** |

## API Endpoints

All endpoints accept `?companion=name` (GET) or `"companion"` in the request body (POST) to target a specific companion. If omitted, the default is `TANEVAN_COMPANION_NAME` when set; otherwise the **first companion in Love Refactored order** (`companion_order.json` + `companions/`). Love Refactored sends **`user_name`** on `/buffer`, `/flush`, and `/import`; the proxy keeps the latest name per companion.

### Core

| Method | Path | What It Does |
|--------|------|-------------|
| `GET` | `/health` | Status check with memory stats |
| `GET` | `/stats` | Detailed memory stats + 3 most recent summaries |
| `GET` | `/summaries` | List session summaries (`?companion=name`) |
| `GET` | `/memories` | List all memories (filter: `?category=fact`) |
| `GET` | `/search` | Semantic search (`?q=query&n=10&include_suppressed=false`) |
| `GET` | `/inject` | Memories for live chat (`?q=&n=10&companion=&budget=`) — recalled pins rank first; `budget` is a token cap |
| `PUT` | `/memory/<id>` | Update a memory (content, category, priority, confidence, active, pinned, suppressed, **protected**, **sensitive**) |
| `GET` | `/memory-lens` | Return per-companion Memory Lens |
| `PUT` | `/memory-lens` | Save Memory Lens (`{ companion, lens: { … } }`) |
| `GET` | `/config` | Pipeline configuration (API keys masked) |
| `PUT` | `/config` | Update pipeline configuration (including `brief_model` / `steps.brief`) |
| `GET` | `/pipeline-status` | Whether a pipeline job is running for a companion |
| `POST` | `/reflect` | Run temporal reflections (`{companion, horizon?, dry_run?, force_all?}`) |
| `GET` | `/reflections` | List reflections (`?horizon=&companion=`) |
| `GET` | `/living-narrative` | Companion living narrative (`?companion=`) |
| `PUT` | `/living-narrative` | Merge hand edits into the living narrative and **lock** it (`self_narrative`, `relationship_arc`, `worldview`, `current_chapter`). Quarterly+ jobs still append turning points. |
| `PUT` | `/reflection/<reflection_id>` | Edit a reflection (`emotional_arc`, `relationship_arc`, `self_narrative`, `patterns`, `connections`) |
| `POST` | `/brief/regenerate` | Spawn `brief_sidecar.py --force` for this companion (does not wait) |
| `GET` | `/reflection-injection` | Block for chat injection (`?companion=&horizons=`) |
| `GET` | `/recent-summaries` | Lean session narratives for the brief sidecar (`n` default 3, max 20) |
| `PUT` | `/summary/<summary_id>` | Edit narrative / emotional_arc / topics / key_moments |

### Buffering & Pipeline

| Method | Path | What It Does |
|--------|------|-------------|
| `POST` | `/buffer` | Buffer a message (`{role, content, companion}`). May report `pipeline_trigger_skipped` if a job is already running |
| `POST` | `/flush` | Trigger the pipeline on the current buffer (`{companion}`; app sends `manual: true` so min_messages=1) |

### Maintenance

| Method | Path | What It Does |
|--------|------|-------------|
| `POST` | `/decay` | Apply memory decay. Supports `dry_run` and `detail` (per-memory preview). |
| `POST` | `/consolidate` | Near-duplicates via Chroma distance, then auto-promote. Supports `dry_run`, `max_pairs` (default 20), `threshold` (default 0.3), `sample_size` (default 100). |
| `POST` | `/consolidate/apply` | Apply previewed decisions; optional `apply_promotions` |
| `GET` | `/audit` | Audit log (`event_type`, `limit`, `before_id`) — merge-guard, consolidation, suppressions (~90 day retention) |
| `DELETE` | `/companion-data` | Wipe a companion's directory under `TANEVAN_DATA_DIR` |

### Pipeline testing

| Method | Path | What It Does |
|--------|------|-------------|
| `POST` | `/test-llm-connection` | Probe a provider endpoint (Anthropic, local, OpenRouter, OpenAI) |
| `POST` | `/test-llm-pipeline` | Run a minimal call for each configured pipeline step |

### Backups

| Method | Path | What It Does |
|--------|------|-------------|
| `POST` | `/backup` | Hot-snapshot every companion `memories.db` → `~/tanevan-data/backups/tanevan-<stamp>/` |
| `GET` | `/backup/list` | List backups, newest first |
| `POST` | `/backup/prune` | Keep last `retentionCount` (default 7) |

### Chat Proxy (Legacy)

| Method | Path | What It Does |
|--------|------|-------------|
| `POST` | `/v1/chat/completions` | OpenAI-compatible proxy — injects memories, buffers messages, forwards to LM Studio |
| `GET` | `/v1/models` | Forwards model list from LM Studio |

### Bulk Import

| Method | Path | What It Does |
|--------|------|-------------|
| `POST` | `/import` | Import a chat history file — accepts JSON body or multipart file upload. Normalizes message formats (SillyTavern, generic role/content, etc.), chunks into 100-message batches, processes asynchronously. Returns a `job_id` immediately. Does **not** fire the brief sidecar per chunk. |
| `GET` | `/import/status/<job_id>` | Poll import progress (`?since=N` for incremental activity feed). Returns chunks done, memories extracted, session summaries. |

Import accepts various chat export formats — it normalizes `role`/`sender` fields and `content`/`text`/`message` fields automatically.

## Pipeline Triggers

The memory pipeline fires on three events:

1. **Every N messages** — default 100, configurable via `TANEVAN_SUMMARIZE_EVERY` (needs ≥4 messages)
2. **Manual flush** — `POST /flush` with `{"companion": "your-companion"}` (app uses `manual: true`)
3. **Shutdown** — `Ctrl+C` processes companion buffers with ≥4 messages before exit

One pipeline job per companion at a time (in-process lock).

## Context Injection

When Love Refactored requests memories for chat (**`GET /inject`**):

1. Takes the user's latest message as the query
2. Embeds with BGE query-instruction prefix (default on)
3. Semantic search against the dossier (distance floor 1.2, min confidence 40 unless pinned/protected/sensitive). Rare-entity recall from the curated `entities` column runs **inside** this pass (reserved slots + IDF)
4. Detects temporal language ("last week", "in November", "3 months ago", etc.) and runs a date-filtered SQLite query — results are merged with semantic matches, deduplicated by ID
5. Rerank: priority, recency, confidence, category weights, intensity, protected floor. Recalled **pinned** memories then sort first
6. Dream slot: at most one highly relevant `dream` row
7. Refresh `last_seen` on the ranked set (`TANEVAN_TOUCH_ON_RECALL`, default on) **before** any token-budget trim
8. Optional token budget (`?budget=`); pinned/protected already selected are never dropped; `TANEVAN_INJECT_MIN` (default 3) is a floor
9. Format: dated lines; sensitive rows go in a separate PRIVATE KNOWLEDGE block

Format (live chat — dates prefix each line when known):
```
[COMPANION'S MEMORIES — What you know about your life and relationship. Each entry is dated; older dates are past events, not happening now:]
[2024-06-01] [FACT] The user is 42 and teaches special education at a therapeutic day school.
[2025-01-15] [RELATIONSHIP] The user considers me their closest companion.
[PREFERENCE] The user wants me to push back and argue, not just agree.
[END MEMORIES]
```

The legacy **`POST /v1/chat/completions`** proxy uses the same dossier search path before forwarding to LM Studio.

## Environment Variables

| Variable | Default | What It Does |
|----------|---------|-------------|
| `LR_HOME` | repo root (parent of `tanevan/`) | Love Refactored root for `data/settings.json` lookups |
| `ANTHROPIC_API_KEY` | *(required for Anthropic steps)* | Anthropic API key; if unset, settings JSON / `pipeline_llm` fallbacks apply |
| `OPENAI_API_KEY` | *(optional)* | Fallback for OpenAI pipeline steps / tests |
| `OPENROUTER_API_KEY` | *(optional)* | Fallback for OpenRouter pipeline steps / tests |
| `TANEVAN_COMPANION_NAME` | *(first companion in UI order)* | Overrides default companion when unset |
| `TANEVAN_USER_NAME` | `the user` | Fallback label for the human in prompts if the app does not send `user_name` |
| `TANEVAN_DATA_DIR` | `~/tanevan-data` | Root directory for all companion databases and `pipeline_config.json` |
| `TANEVAN_DEFAULT_PIPELINE_PROVIDER` | from LR settings | `anthropic`, `openai`, `openrouter`, `local`, or `hybrid` hint when pipeline config is fresh |
| `TANEVAN_LM_STUDIO_URL` | `http://localhost:1234/v1/chat/completions` | LM Studio endpoint (legacy proxy + local pipeline steps) |
| `TANEVAN_PROXY_PORT` | `5001` | Proxy server port |
| `TANEVAN_PROXY_HOST` | `127.0.0.1` | Bind address (keep localhost on a VPS) |
| `TANEVAN_URL` | `http://localhost:5001` | Base URL the brief sidecar uses to call this proxy |
| `TANEVAN_SUMMARIZE_EVERY` | `100` | Auto-trigger pipeline after this many buffered messages |
| `TANEVAN_SESSION_GAP_SECONDS` | `7200` | Split buffer into sessions on this idle gap |
| `TANEVAN_SESSION_TOKEN_CAP` | `12000` | Split oversized sessions at user-turn boundaries |
| `TANEVAN_SUMMARIZER_MODEL` | *(none — configure in UI)* | Optional env override for Pass 1 |
| `TANEVAN_EXTRACTOR_MODEL` | *(none — configure in UI)* | Optional env override for Pass 2 |
| `TANEVAN_UPDATER_MODEL` | *(none — configure in UI)* | Optional env override for Pass 3 |
| `TANEVAN_REFLECTION_MODEL` | *(none — configure in UI)* | Optional env override for reflections |
| `TANEVAN_DISTANCE_FLOOR` | `1.2` | ChromaDB cosine distance ceiling for relevance (0=identical, 2=opposite) |
| `TANEVAN_INJECT_COUNT` | `10` | Default `n` for legacy proxy inject |
| `TANEVAN_INJECT_MIN` | `3` | Floor when a token `budget` is applied |
| `BRIEF_OUT_DIR` | `<repo>/data/briefs` | Where life briefs are written |
| `BRIEF_TZ` | (settings / host) | IANA timezone override for brief dates |
| `BRIEF_USE_LENS` | `1` | Fill brief prompt placeholders from Memory Lens |
| `BRIEF_N` | `6` | Session narratives the sidecar synthesises (1–20) |
| `BRIEF_MAX_TOKENS` | `6000` | Brief sidecar output cap |
| `BRIEF_TEMPERATURE` | `0.6` | Brief sidecar sampling temperature |
| `BRIEF_REASONING` | `off` | Brief sidecar reasoning (`off` / `low` / `medium` / `high` / `xhigh`) |
| `BRIEF_MODEL_KEY` | *(none)* | Legacy sidecar path: if set, skip pipeline config and call an OpenAI-compatible endpoint |
| `BRIEF_MODEL_NAME` / `_BASE` | `deepseek/deepseek-chat` / OpenRouter | Used only when `BRIEF_MODEL_KEY` is set |

Retrieval, merge-guard, versioning, entity recall, intensity, and auto-promote each have kill switches (`TANEVAN_MERGE_GUARD`, `TANEVAN_ENTITY_RECALL`, `TANEVAN_MEMORY_VERSIONS`, `TANEVAN_INTENSITY_WEIGHT`, `TANEVAN_AUTO_PROMOTE_CORE`, `TANEVAN_VERSIONED_UPDATE`, …) — see comments at the top of `memory_db.py` and `updater.py`.

There is **no** `TANEVAN_MAX_CONTEXT_MEMORIES` variable. Use `/inject?n=` and/or `?budget=`.

## CLI Usage

```bash
# Process the current buffer
python3 pipeline.py

# Process a JSON file of messages
python3 pipeline.py conversation.json

# Export the dossier and summaries to Desktop
python3 export.py

# Backfill event_date / first_seen / last_seen from linked summaries
python3 backfill_event_dates.py --dry-run
python3 backfill_event_dates.py --companion your-companion
python3 backfill_event_dates.py --all --force   # overwrite existing event_date

# Re-embed all memories/summaries (after model or embed-text format change)
python3 reembed.py --dry-run
python3 reembed.py --companion companion-name
python3 reembed.py --all

# Life brief (usually spawned by the proxy)
python3 brief_sidecar.py --dry-run
python3 brief_sidecar.py --companion your-companion
python3 brief_sidecar.py --force --companion your-companion   # bypass hand-edit lock

# Repair entity fields (run BEFORE reembed on upgrades)
python3 backfill_entities.py --dry-run
python3 backfill_entities.py --all

# Drop Chroma vectors whose SQLite rows are gone (stop Tanevan first)
python3 prune_chroma_ghosts.py --companion your-companion --yes
```

## Export

`export.py` writes two files to `~/Desktop`:

- **`tanevan_dossier.txt`** — All memories grouped by category, sorted by priority and confidence, with reinforcement counts
- **`tanevan_summaries.txt`** — Recent conversation summaries with emotional arcs, narratives, topics, and key moments

## Scale Reference

Tested at scale with bulk imports of 50,000+ messages. The chunked async import system (`POST /import`) handles large histories by splitting into 100-message batches and processing them sequentially in a background thread, with real-time progress via the activity feed.

## License

Tanevan is part of the Love Refactored repository and is covered by the main Love Refactored / Tanevan License.

**Contact:** Megan Neves — hi@meganneves.com; Discord - kandykidsaturn ; Tiara Young — tiara@tiara.nz; Discord - tiara.nz

See **[../LICENSE.md](../LICENSE.md)** (canonical) and **[LICENSE.md](LICENSE.md)** (this directory’s notice).

Tanevan is source-available for personal, non-commercial use only. It is not open-source software.
