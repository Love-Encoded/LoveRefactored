# © 2024-2026 Megan Neves. All rights reserved.
# Tanevan Memory System — https://github.com/Love-Encoded/Love-Refactored
# Licensed under the Love Refactored / Tanevan License. See LICENSE.md.
"""
Tanevan - Memory Database Layer
SQLite for structured storage + ChromaDB for vector search.

This is the foundation. Every other component reads from and writes to this.
"""

import sqlite3
import chromadb
import numpy as np
from sentence_transformers import SentenceTransformer
import threading as _threading

# Process-wide shared embedder (see __init__ note). Lock guards first load.
_SHARED_EMBEDDER = None
_EMBEDDER_LOCK = _threading.Lock()

def _get_shared_embedder(companion_name=""):
    global _SHARED_EMBEDDER
    if _SHARED_EMBEDDER is None:
        with _EMBEDDER_LOCK:
            if _SHARED_EMBEDDER is None:
                print(f"Loading embedding model (shared, first use: {companion_name})...")
                _SHARED_EMBEDDER = SentenceTransformer(EMBEDDING_MODEL)
    return _SHARED_EMBEDDER
from db_time import ensure_utc, parse_db_datetime
from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import os
import re
import uuid

# === Configuration ===
BASE_DATA_DIR = os.environ.get("TANEVAN_DATA_DIR", os.path.expanduser("~/tanevan-data"))
EMBEDDING_MODEL = "BAAI/bge-base-en-v1.5"

# bge query-side instruction prefix (added 15 Aug 2026): bge-*-v1.5 expects
# short retrieval queries prefixed with this instruction string. Stored
# passages are NOT prefixed (model card: documents never need it, so no
# re-embed). Measured 27 Jul: without it, query-vs-passage similarity is
# compressed and MMR diversity math runs on noise.
# Kill switch: TANEVAN_BGE_QUERY_PREFIX=0 (state is logged at startup).
BGE_QUERY_PREFIX_ENABLED = os.environ.get("TANEVAN_BGE_QUERY_PREFIX", "1") not in ("0", "false", "False")
BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

# Quality floor: cosine distance above this threshold = not relevant enough to inject.
# ChromaDB cosine distance: 0.0 = identical, ~0.5 = related, ~1.0 = unrelated, 2.0 = opposite.
# 1.2 is deliberately generous — re-ranking handles the fine filtering.
DISTANCE_FLOOR = float(os.environ.get("TANEVAN_DISTANCE_FLOOR", "1.2"))

# MMR diversity selection for chat injection: trades a little relevance for
# variety so one dominant memory cluster can't fill every slot.
# lambda 1.0 = pure relevance, 0.0 = pure diversity.
# Default 0.5: real companion memory embeddings cluster tightly (bge sims ~0.6
# even for unrelated memories), so 0.7's diversity weight is too weak to displace near-duplicates.
MMR_LAMBDA = float(os.environ.get("TANEVAN_MMR_LAMBDA", "0.5"))
MMR_OVERFETCH_MULTIPLIER = int(os.environ.get("TANEVAN_MMR_OVERFETCH_MULTIPLIER", "4"))

# MMR for chat injection: DISABLED 15 Aug 2026 by bench decision — with bge
# similarities clustered tight, diversity selection evicted 6-8 of the
# top-10 relevance picks per query (measured before AND after the query
# prefix fix). Pure relevance until entity_recall_v2 lands.
# Re-enable: TANEVAN_MMR_CHAT_INJECTION=1 (state is logged at startup).
MMR_CHAT_INJECTION = os.environ.get("TANEVAN_MMR_CHAT_INJECTION", "0") in ("1", "true", "True")

# === entity_recall_v2 (15 Aug 2026; corrects the reverted 27 Jul v1) ===
# Rare names (amanda: 0.72% of memories) drown in topically-dominant clusters
# (chris: 11.32%) under pure cosine — the name never even enters the candidate
# pool. v2 gives rare entities a direct recall path from the curated entities
# column, word-boundary matched, IDF-weighted. All limits log when they bite.
ENTITY_RECALL_ENABLED = os.environ.get("TANEVAN_ENTITY_RECALL", "1") not in ("0", "false", "False")

# === merge guard (24 Aug 2026) ===
# Measured 27 Jul: every merge/update adds ~100 chars to the target; bigger
# memories sit more central in vector space and win the NEXT similarity
# comparison, so they keep absorbing. r>=10 promotes to core, core is
# decay-immune: the loop manufactures its own immunity. 76 memories were
# over 1,500 chars on 24 Aug (~39k tokens combined, all uncached per turn).
# The guard refuses content rewrites that would (a) touch a protected or
# pinned memory, (b) push a memory past MAX_CHARS, or (c) on merge, come out
# shorter than MIN_RETAIN x the SHORTER of the two inputs (a garbage rewrite;
# compressing a big target down is allowed and wanted). A refused
# incoming memory is ALWAYS saved as its own new memory — never dropped.
# Every refusal prints and writes an audit_log row (event_type merge_guard).
# Kill switch: TANEVAN_MERGE_GUARD=0.
MERGE_GUARD_ENABLED = os.environ.get("TANEVAN_MERGE_GUARD", "1") not in ("0", "false", "False")
MERGE_GUARD_MAX_CHARS = int(os.environ.get("TANEVAN_MERGE_GUARD_MAX_CHARS", "800"))
MERGE_GUARD_MIN_RETAIN = float(os.environ.get("TANEVAN_MERGE_GUARD_MIN_RETAIN", "0.5"))
# memory_versions_v1 (30 Aug 2026): no memory's text is ever destroyed. Any
# content rewrite through update_memory snapshots the prior row into
# memory_versions first (updater update/merge, consolidation merge, UI edit).
# Kill switch: TANEVAN_MEMORY_VERSIONS=0 skips the snapshot (rewrite proceeds).
MEMORY_VERSIONS_ENABLED = os.environ.get("TANEVAN_MEMORY_VERSIONS", "1") not in ("0", "false", "False")
# chroma_prune_v1: nothing ever deleted retired rows from the vector index —
# 7,322 ghosts of 17,371 on Evan's corpus, some queries had 19 live candidates
# of 200. active=0 now deletes the vector; active=1 re-embeds it.
CHROMA_PRUNE_INACTIVE = os.environ.get("TANEVAN_CHROMA_PRUNE_INACTIVE", "1") not in ("0", "false", "False")
# token_budget_v1 (30 Aug 2026): "up to N tokens of memory" instead of "up to N
# memories". Ten one-liners and ten 800-char chunks no longer cost the same slot
# count. Off unless the caller passes a budget; count-based behavior is unchanged.
INJECT_MIN = int(os.environ.get("TANEVAN_INJECT_MIN", "3"))
CHARS_PER_TOKEN = 4.0
# touch_on_recall_v1 (30 Aug 2026): retrieval never bumped last_seen, so the
# recency stroke aged memories the companion used daily unless the updater
# happened to re-merge them — the only way to stay "young" was to get big.
# Injected memories now get last_seen refreshed. reinforcement_count is NOT
# touched (that stays a write-side signal).
TOUCH_ON_RECALL = os.environ.get("TANEVAN_TOUCH_ON_RECALL", "1") not in ("0", "false", "False")
# intensity_v1 (30 Aug 2026): the extractor rates every memory 0-10 for emotional
# intensity, then used to smear it into confidence (+2/+5/+10) and discard it.
# Now stored as its own column and given a small rerank term, so the places
# where the companion got shaped float slightly without a sixth category.
# Kill switch: TANEVAN_INTENSITY_WEIGHT=0 ignores the column in scoring.
INTENSITY_WEIGHT_ENABLED = os.environ.get("TANEVAN_INTENSITY_WEIGHT", "1") not in ("0", "false", "False")
INTENSITY_MAX_BONUS = float(os.environ.get("TANEVAN_INTENSITY_MAX_BONUS", "0.10"))
# An entity in more than this fraction of active memories is "common": no
# recall pass, no rank signal. 0.02 = 2% (measured line held up on 27 Jul).
ENTITY_RARE_MAX_DF = float(os.environ.get("TANEVAN_ENTITY_RARE_MAX_DF", "0.02"))
# Max memories pulled per rare entity per query (best-by-cosine, logged if hit).
ENTITY_RECALL_PER_ENTITY = int(os.environ.get("TANEVAN_ENTITY_RECALL_PER_ENTITY", "12"))
# Final slots guaranteed to entity hits that would otherwise miss the top n.
ENTITY_RESERVED_SLOTS = int(os.environ.get("TANEVAN_ENTITY_RESERVED_SLOTS", "2"))
# Rank bonus scale for the rarest match. v1's 0.45 was wider than the entire
# semantic spread and overrode relevance — 0.15 sits inside the signal.
ENTITY_MAX_BONUS = float(os.environ.get("TANEVAN_ENTITY_MAX_BONUS", "0.15"))
# Mild push-down for memories naming NONE of the rare entities asked about.
ENTITY_MISS_PENALTY = float(os.environ.get("TANEVAN_ENTITY_MISS_PENALTY", "0.05"))
# protected_floor_v1 (30 Aug 2026) — retrieval half of identity protection.
# protected=1 rows already can't be merged into (merge_guard) or decayed;
# this gives them recall: a score bonus in rerank_score, a reserved seat in
# search_memories, and an exemption on the entity-recall confidence floor.
# Kill switch: TANEVAN_PROTECTED_FLOOR=0 restores pre-v1 behavior exactly.
PROTECTED_FLOOR_ENABLED = os.environ.get("TANEVAN_PROTECTED_FLOOR", "1") not in ("0", "false", "False")
PROTECTED_BONUS = float(os.environ.get("TANEVAN_PROTECTED_BONUS", "0.25"))
PROTECTED_RESERVED_SLOTS = int(os.environ.get("TANEVAN_PROTECTED_RESERVED_SLOTS", "1"))
# category_weights_v1 (30 Aug 2026) — category scales the priority and recency
# strokes in rerank_score. Baseline bench showed the semantic spread is ~0.1–0.25
# wide while priority and age each add up to 0.30, so old or "minor" facts were
# buried under recent core experiences regardless of fit. Facts/preferences
# barely age and aren't punished for priority; experiences keep the original
# math. Tuple = (priority_mult, recency_mult).
# Kill switch: TANEVAN_CATEGORY_WEIGHTS=0 restores pre-v1 scoring exactly.
CATEGORY_WEIGHTS_ENABLED = os.environ.get("TANEVAN_CATEGORY_WEIGHTS", "1") not in ("0", "false", "False")
CATEGORY_TERM_WEIGHTS = {
    "fact":         (0.5, 0.2),
    "preference":   (0.5, 0.3),
    "relationship": (0.8, 0.5),
    "milestone":    (0.8, 0.4),
    "experience":   (1.0, 1.0),
    "dream":        (1.0, 1.0),
}

# Query tokens / vocab entries never treated as entities. Covers common English
# plus junk the old backfill_entities.py capitalized-word heuristic minted
# ("As", "God", "Initially", "More"). Lowercase, length >= 3 enforced elsewhere.
ENTITY_STOPWORDS = frozenset("""
the and but for are was were been being have has had does did doing will would
could should may might shall can cannot need dare ought used use uses using
not what when where which who whom whose how why this that these those here
there each every all both few more most other others some such than too very
just also still already always never often sometimes usually rarely about
above after again against before behind below between beyond during since
until within without from into through under over out off own same so than
then once only nor because while although though unless whether either neither
she her hers him his they them their theirs its our ours your yours mine
tell told said says say saying know knew known think thought thinks like
liked want wanted wants feel felt feels feeling going went gone come came
comes make made makes making take took taken takes get got gotten gets give
gave given gives see saw seen sees look looked looking put puts keep kept
let lets seem seemed seems turn turned start started show showed shown ask
asked need needed try tried tries call called work worked right left good
better best bad worse worst new old first last long great little big high
small large next early young important public able thing things something
anything nothing everything someone anyone noone everyone somewhere anywhere
today yesterday tonight tomorrow morning evening afternoon night week month
year day days weeks months years time times remember recall happened really
actually maybe probably perhaps definitely certainly initially finally
eventually currently basically literally honestly god yeah yes okay hey
wife husband papa love dear honey baby
""".split())

# Audit log retention: rows older than this many days are pruned on startup
# and by the proxy's daily sweep.
AUDIT_RETENTION_DAYS = int(os.environ.get("TANEVAN_AUDIT_RETENTION_DAYS", "90"))

# audit_log schema (24 Aug 2026): event_type/status lists widened for the merge
# guard and split_oversized.py. Any new event type MUST be added here — SQLite
# rejects unknown values, and write_audit_event only warns, so an unlisted type
# means a silently missing trail. Existing DBs are migrated in _create_tables.
AUDIT_LOG_EVENT_TYPES = ("consolidation", "retrieval_suppressed", "merge_guard", "split", "split_undo")
AUDIT_LOG_STATUSES = ("committed", "failed", "refused")
AUDIT_LOG_SCHEMA_SQL = f"""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                companion TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK(event_type IN (
                    {", ".join(repr(t) for t in AUDIT_LOG_EVENT_TYPES)}
                )),
                source_memories TEXT NOT NULL,
                result_memory TEXT,
                reason TEXT,
                status TEXT NOT NULL CHECK(status IN ({", ".join(repr(s) for s in AUDIT_LOG_STATUSES)}))
            )
        """


def build_memory_embed_text(category, priority, content, entities=None):
    """Build enriched text for memory embedding — category/priority/entity prefix improves retrieval."""
    if entities:
        if isinstance(entities, str):
            try:
                entities = json.loads(entities)
            except (json.JSONDecodeError, TypeError):
                entities = []
        entity_str = ", ".join(str(e) for e in entities if e)
        if entity_str:
            return f"[{category.upper()} | {priority} | ABOUT: {entity_str}] {content}"
    return f"[{category.upper()} | {priority}] {content}"


def build_summary_embed_text(narrative, topics, key_moments):
    """Build enriched text for summary embedding — includes topics and key moments for better search."""
    topics_str = ", ".join(topics) if isinstance(topics, list) else str(topics)
    moments_str = "; ".join(key_moments) if isinstance(key_moments, list) else str(key_moments)
    return f"{narrative} Topics: {topics_str} Key moments: {moments_str}"


_MONTH_NAMES = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}


def detect_temporal_range(query):
    """
    Detect temporal language in a query and return a (start_date, end_date) tuple
    as ISO date strings, or None if no temporal reference found.
    Covers: yesterday, last week/month/year, N days/weeks/months ago, month names, years.
    """
    q = query.lower().strip()
    now = datetime.now(timezone.utc)

    # "yesterday"
    if re.search(r'\byesterday\b', q):
        d = now - timedelta(days=2)
        return (d.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d"))

    # "last N days/weeks/months/years" or "past N days/weeks/months/years"
    m = re.search(r'\b(?:last|past)\s+(\d+)\s+(days?|weeks?|months?|years?)\b', q)
    if m:
        n = int(m.group(1))
        unit = m.group(2).rstrip('s')
        if unit == 'day':
            delta = timedelta(days=n)
        elif unit == 'week':
            delta = timedelta(weeks=n)
        elif unit == 'month':
            delta = timedelta(days=n * 30)
        elif unit == 'year':
            delta = timedelta(days=n * 365)
        else:
            return None
        return ((now - delta).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d"))

    # "last week" / "last month" / "last year"
    m = re.search(r'\blast\s+(week|month|year)\b', q)
    if m:
        unit = m.group(1)
        if unit == 'week':
            delta = timedelta(days=7)
        elif unit == 'month':
            delta = timedelta(days=30)
        elif unit == 'year':
            delta = timedelta(days=365)
        else:
            return None
        return ((now - delta).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d"))

    # "N days/weeks/months/years ago"
    m = re.search(r'\b(\d+)\s+(days?|weeks?|months?|years?)\s+ago\b', q)
    if m:
        n = int(m.group(1))
        unit = m.group(2).rstrip('s')
        if unit == 'day':
            delta = timedelta(days=n)
        elif unit == 'week':
            delta = timedelta(weeks=n)
        elif unit == 'month':
            delta = timedelta(days=n * 30)
        elif unit == 'year':
            delta = timedelta(days=n * 365)
        else:
            return None
        target = now - delta
        window = timedelta(days=7)
        return ((target - window).strftime("%Y-%m-%d"), (target + window).strftime("%Y-%m-%d"))

    # "[in] [month] [year]" or "[in] [month]" — e.g. "in November 2024", "november", "last november"
    m = re.search(r'\b(?:in\s+|last\s+)?(' + '|'.join(_MONTH_NAMES.keys()) + r')(?:\s+(\d{4}))?\b', q)
    if m:
        month_num = _MONTH_NAMES[m.group(1)]
        year = int(m.group(2)) if m.group(2) else now.year
        if not m.group(2) and (year == now.year and month_num > now.month):
            year -= 1
        start = f"{year}-{month_num:02d}-01"
        if month_num == 12:
            end = f"{year + 1}-01-01"
        else:
            end = f"{year}-{month_num + 1:02d}-01"
        return (start, end)

    # Bare year: "in 2024", "back in 2024", "2024"
    m = re.search(r'\b(?:in|from|back in)?\s*(20[1-3]\d)\b', q)
    if m:
        year = m.group(1)
        return (f"{year}-01-01", f"{int(year) + 1}-01-01")

    return None


def normalize_conversation_timestamp(raw):
    """Return ISO-8601 UTC string for a message/summary time, or None if unknown."""
    if raw is None:
        return None
    if isinstance(raw, str) and raw.strip() == "":
        return None
    if isinstance(raw, (int, float)) or (isinstance(raw, str) and raw.strip().lstrip("-").isdigit()):
        ts = float(raw)
        if ts > 1e12:
            ts = ts / 1000
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (ValueError, OSError):
            return None
    return str(raw).strip() or None


class MemoryDB:
    """
    Dual-database memory system.
    - SQLite: structured records (categories, scores, timestamps, text)
    - ChromaDB: vector embeddings for semantic search
    """

    def __init__(self, companion_name):
        self.companion_name = companion_name.lower()
        companion_dir = os.path.join(BASE_DATA_DIR, self.companion_name)

        # Ensure per-companion data directory exists
        Path(companion_dir).mkdir(parents=True, exist_ok=True)

        sqlite_path = os.path.join(companion_dir, "memories.db")
        chroma_path = os.path.join(companion_dir, "chroma_db")

        # Initialize SQLite
        self.db = sqlite3.connect(sqlite_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self._create_tables()
        # Retention on startup — prune_audit_log never raises, so this can't block DB init
        self.prune_audit_log(AUDIT_RETENTION_DAYS)

        # Initialize ChromaDB
        self.chroma = chromadb.PersistentClient(path=chroma_path)
        _hnsw_config = {
            "hnsw:space": "cosine",
            "hnsw:construction_ef": 200,
            "hnsw:search_ef": 150,
            "hnsw:M": 32,
        }
        self.memories_collection = self.chroma.get_or_create_collection(
            name="memories",
            metadata=_hnsw_config,
        )
        self.summaries_collection = self.chroma.get_or_create_collection(
            name="summaries",
            metadata=_hnsw_config,
        )
        self.reflections_collection = self.chroma.get_or_create_collection(
            name="reflections",
            metadata=_hnsw_config,
        )

        # Initialize embedding model — SHARED singleton (fix 15 Aug 2026):
        # one MemoryDB per companion each loading its own SentenceTransformer
        # put N full copies of bge-base on the GPU (plus more via the old
        # per-request construction in /reflection-injection). Same model for
        # every companion, so load exactly once, process-wide.
        self.embedder = _get_shared_embedder(self.companion_name)
        print(f"✓ Memory database ready ({self.companion_name})")
        print(f"  bge query prefix: {'ON' if BGE_QUERY_PREFIX_ENABLED else 'OFF (TANEVAN_BGE_QUERY_PREFIX=0)'}")
        print(f"  MMR chat injection: {'ON (TANEVAN_MMR_CHAT_INJECTION=1)' if MMR_CHAT_INJECTION else 'OFF (bench decision 15 Aug 2026)'}")
        print(f"  merge guard: {'ON max_chars=' + str(MERGE_GUARD_MAX_CHARS) + ' min_retain=' + str(MERGE_GUARD_MIN_RETAIN) if MERGE_GUARD_ENABLED else 'OFF (TANEVAN_MERGE_GUARD=0)'}")
        print(f"  entity recall v2: {'ON' if ENTITY_RECALL_ENABLED else 'OFF (TANEVAN_ENTITY_RECALL=0)'}")

    def _create_tables(self):
        """Create the SQLite schema."""
        cursor = self.db.cursor()

        # === Session Summaries ===
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS summaries (
                id TEXT PRIMARY KEY,
                date_start TEXT NOT NULL,
                date_end TEXT NOT NULL,
                message_count INTEGER NOT NULL,
                emotional_arc TEXT NOT NULL,
                narrative TEXT NOT NULL,
                topics TEXT NOT NULL,
                key_moments TEXT NOT NULL,
                raw_conversation TEXT,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # === Memory Dossier ===
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                category TEXT NOT NULL CHECK(category IN (
                    'fact', 'experience', 'milestone', 'preference', 'relationship'
                )),
                priority TEXT NOT NULL DEFAULT 'important' CHECK(priority IN (
                    'core', 'important', 'notable', 'minor'
                )),
                confidence INTEGER NOT NULL DEFAULT 75 CHECK(
                    confidence >= 0 AND confidence <= 100
                ),
                content TEXT NOT NULL,
                source_summary_id TEXT,
                reinforcement_count INTEGER NOT NULL DEFAULT 1,
                first_seen TEXT NOT NULL DEFAULT (datetime('now')),
                last_seen TEXT NOT NULL DEFAULT (datetime('now')),
                active INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (source_summary_id) REFERENCES summaries(id)
            )
        """)

        # === Conversation Buffer ===
        # Stores raw messages until they're summarized
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS conversation_buffer (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # === Processing Log ===
        # Track what's been processed to avoid duplicates
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS processing_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                details TEXT,
                timestamp TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)

        # === Reflections (Temporal Processing) ===
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reflections (
                id TEXT PRIMARY KEY,
                horizon TEXT NOT NULL CHECK(horizon IN (
                    'daily', 'weekly', 'monthly', 'quarterly', 'biannual', 'annual'
                )),
                window_start TEXT NOT NULL,
                window_end TEXT NOT NULL,
                themes TEXT NOT NULL DEFAULT '[]',
                patterns TEXT NOT NULL DEFAULT '[]',
                emotional_arc TEXT NOT NULL DEFAULT '',
                connections TEXT NOT NULL DEFAULT '[]',
                growth TEXT DEFAULT '[]',
                relationship_arc TEXT DEFAULT '',
                self_narrative TEXT DEFAULT '',
                unresolved TEXT NOT NULL DEFAULT '[]',
                significance INTEGER NOT NULL DEFAULT 5 CHECK(
                    significance >= 1 AND significance <= 10
                ),
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_reflections_horizon
            ON reflections(horizon)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_reflections_created
            ON reflections(created_at DESC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_reflections_window
            ON reflections(window_start, window_end)
        """)

        # Create indexes for common queries
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_category
            ON memories(category)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_priority
            ON memories(priority)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_confidence
            ON memories(confidence DESC)
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_memories_active
            ON memories(active)
        """)

        # === Memory Versions (memory_versions_v1) ===
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS memory_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id TEXT NOT NULL,
                content TEXT NOT NULL,
                category TEXT,
                priority TEXT,
                confidence INTEGER,
                entities TEXT,
                source TEXT NOT NULL DEFAULT 'unknown',
                replaced_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_memory_versions_memory ON memory_versions(memory_id)")
        # superseded_by: pointer from a retired row to the row that replaced it.
        _cols = {r[1] for r in cursor.execute("PRAGMA table_info(memories)").fetchall()}
        if "superseded_by" not in _cols:
            cursor.execute("ALTER TABLE memories ADD COLUMN superseded_by TEXT")
            print("  🧾 memories: added superseded_by column (one-time)")
        if "emotional_intensity" not in _cols:
            cursor.execute("ALTER TABLE memories ADD COLUMN emotional_intensity INTEGER NOT NULL DEFAULT 0")
            print("  🧾 memories: added emotional_intensity column (one-time)")

        # === Audit Log ===
        # Immutable trail of destructive/lossy events: consolidation merges
        # and MMR retrieval suppressions. Full memory text captured at event time.
        cursor.execute(AUDIT_LOG_SCHEMA_SQL)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_audit_event_type
            ON audit_log(event_type)
        """)
        # One-time migration: widen the CHECK constraints on an existing audit_log.
        # SQLite cannot ALTER a CHECK, so rebuild: rename -> create -> copy -> drop.
        # Rows are copied BEFORE the old table is dropped; any failure leaves the
        # old table intact and prints loudly.
        try:
            _row = cursor.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='audit_log'"
            ).fetchone()
            if _row and "'merge_guard'" not in (_row[0] or ""):
                print("  \U0001f9fe audit_log: migrating CHECK constraints (one-time table rebuild)...")
                cursor.execute("DROP TABLE IF EXISTS audit_log_old")
                cursor.execute("ALTER TABLE audit_log RENAME TO audit_log_old")
                cursor.execute(AUDIT_LOG_SCHEMA_SQL)
                cursor.execute(
                    "INSERT INTO audit_log (id, timestamp, companion, event_type, "
                    "source_memories, result_memory, reason, status) "
                    "SELECT id, timestamp, companion, event_type, source_memories, "
                    "result_memory, reason, status FROM audit_log_old"
                )
                _n = cursor.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
                _o = cursor.execute("SELECT COUNT(*) FROM audit_log_old").fetchone()[0]
                if _n != _o:
                    raise RuntimeError(f"row count mismatch after copy: new {_n} vs old {_o}")
                cursor.execute("DROP TABLE audit_log_old")
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_audit_event_type ON audit_log(event_type)")
                self.db.commit()
                print(f"  \U0001f9fe audit_log: migration done ({_n} rows preserved)")
        except Exception as _e:
            self.db.rollback()
            print(f"  \u26a0\ufe0f audit_log migration FAILED: {_e} \u2014 merge_guard/split audit rows "
                  f"will keep being rejected until this is fixed")

        # Migrations: add pinned/suppressed columns if they don't exist yet
        try:
            cursor.execute("ALTER TABLE memories ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE memories ADD COLUMN suppressed INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        # protected: never decays/demotes, but retrieved normally (unlike pinned's always-inject).
        # sensitive: protected + the companion never raises it unprompted (etiquette at prompt level).
        try:
            cursor.execute("ALTER TABLE memories ADD COLUMN protected INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE memories ADD COLUMN sensitive INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE memories ADD COLUMN event_date TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE memories ADD COLUMN entities TEXT NOT NULL DEFAULT '[]'")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE reflections ADD COLUMN edited_at TEXT")
        except sqlite3.OperationalError:
            pass

        # Migration: rebuild the memories table to add 'dream' to the category
        # CHECK constraint. SQLite can't alter CHECKs, so: new table -> copy ->
        # swap -> re-create indexes, all inside one transaction. Skips instantly
        # on databases that already allow 'dream'.
        try:
            row = cursor.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='memories'"
            ).fetchone()
            table_sql = row[0] if row else ""
            if table_sql and "'dream'" not in table_sql:
                print("  🌙 Migrating memories table: adding 'dream' category (one-time rebuild)...")
                new_sql = table_sql.replace("'relationship'", "'relationship', 'dream'", 1)
                if "CREATE TABLE IF NOT EXISTS memories" in new_sql:
                    new_sql = new_sql.replace("CREATE TABLE IF NOT EXISTS memories", "CREATE TABLE memories_new", 1)
                else:
                    new_sql = new_sql.replace("CREATE TABLE memories", "CREATE TABLE memories_new", 1)
                cols = [r[1] for r in cursor.execute("PRAGMA table_info(memories)").fetchall()]
                col_list = ", ".join(f'"{c}"' for c in cols)
                index_sqls = [r[0] for r in cursor.execute(
                    "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='memories' AND sql IS NOT NULL"
                ).fetchall()]
                script = "BEGIN;\n"
                script += new_sql.rstrip().rstrip(";") + ";\n"
                script += f"INSERT INTO memories_new ({col_list}) SELECT {col_list} FROM memories;\n"
                script += "DROP TABLE memories;\n"
                script += "ALTER TABLE memories_new RENAME TO memories;\n"
                for isql in index_sqls:
                    script += isql.rstrip().rstrip(";") + ";\n"
                script += "COMMIT;"
                cursor.executescript(script)
                print("  ✓ memories table migrated — 'dream' is now a legal category.")
        except Exception as e:
            try:
                cursor.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            print(f"  ⚠️ Dream-category migration failed (database unchanged): {e}")

        self.db.commit()

    # =========================================================
    # CONVERSATION BUFFER
    # =========================================================

    def buffer_message(self, role, content, timestamp=None):
        """Add a message to the conversation buffer."""
        ts = normalize_conversation_timestamp(timestamp) or ""
        self.db.execute(
            "INSERT INTO conversation_buffer (role, content, timestamp) VALUES (?, ?, ?)",
            (role, content, ts)
        )
        self.db.commit()

    def get_buffer(self):
        """Get all messages in the current buffer."""
        rows = self.db.execute(
            "SELECT id, role, content, timestamp FROM conversation_buffer ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_buffer_count(self):
        """How many messages are in the buffer."""
        row = self.db.execute("SELECT COUNT(*) as cnt FROM conversation_buffer").fetchone()
        return row["cnt"]

    def clear_buffer(self, up_to_id=None):
        """Clear the conversation buffer after summarization.

        up_to_id: only delete rows with id <= this value, so messages that
        arrived WHILE the pipeline was running survive for the next run.
        None = full wipe (legacy behaviour, manual resets only).
        """
        if up_to_id is None:
            self.db.execute("DELETE FROM conversation_buffer")
        else:
            self.db.execute(
                "DELETE FROM conversation_buffer WHERE id <= ?", (up_to_id,)
            )
        self.db.commit()
        survivors = self.db.execute(
            "SELECT COUNT(*) as cnt FROM conversation_buffer"
        ).fetchone()["cnt"]
        if survivors:
            print(f"   ↳ buffer cleared up to id {up_to_id}; {survivors} mid-run message(s) preserved for next cycle")
        return survivors

    # =========================================================
    # SUMMARIES
    # =========================================================

    def save_summary(self, summary_data):
        """
        Save a session summary to both SQLite and ChromaDB.

        summary_data should be a dict with:
            date_start, date_end, message_count, emotional_arc,
            narrative, topics (list), key_moments (list),
            raw_conversation (optional)
        """
        summary_id = f"summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

        # Store in SQLite
        self.db.execute("""
            INSERT INTO summaries
            (id, date_start, date_end, message_count, emotional_arc,
             narrative, topics, key_moments, raw_conversation)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            summary_id,
            summary_data["date_start"],
            summary_data["date_end"],
            summary_data["message_count"],
            summary_data["emotional_arc"],
            summary_data["narrative"],
            json.dumps(summary_data["topics"]),
            json.dumps(summary_data["key_moments"]),
            summary_data.get("raw_conversation", "")
        ))
        self.db.commit()

        # Store embedding in ChromaDB (enriched with topics + key moments for better search)
        embed_text = build_summary_embed_text(
            summary_data["narrative"],
            summary_data["topics"],
            summary_data["key_moments"],
        )
        embedding = self.embedder.encode(embed_text)
        self.summaries_collection.add(
            embeddings=[embedding.tolist()],
            documents=[summary_data["narrative"]],
            ids=[summary_id],
            metadatas=[{
                "date_start": summary_data["date_start"],
                "date_end": summary_data["date_end"],
                "message_count": summary_data["message_count"]
            }]
        )

        self.log("summary_saved", f"Saved summary {summary_id} ({summary_data['message_count']} messages)")
        return summary_id

    def get_summary(self, summary_id):
        """Get a specific summary by ID."""
        row = self.db.execute("SELECT * FROM summaries WHERE id = ?", (summary_id,)).fetchone()
        if row:
            result = dict(row)
            result["topics"] = json.loads(result["topics"])
            result["key_moments"] = json.loads(result["key_moments"])
            return result
        return None

    def update_summary(self, summary_id, updates):
        """
        Update an existing summary record.
        Can update: narrative, emotional_arc, topics, key_moments.
        Re-embeds in ChromaDB when narrative, topics, or key_moments change.
        """
        allowed_fields = {"narrative", "emotional_arc", "topics", "key_moments"}
        filtered = {k: v for k, v in updates.items() if k in allowed_fields}

        if not filtered:
            return

        if "topics" in filtered:
            topics = filtered["topics"]
            filtered["topics"] = json.dumps(topics) if isinstance(topics, list) else topics
        if "key_moments" in filtered:
            moments = filtered["key_moments"]
            filtered["key_moments"] = json.dumps(moments) if isinstance(moments, list) else moments

        set_clause = ", ".join(f"{k} = ?" for k in filtered)
        values = list(filtered.values()) + [summary_id]

        self.db.execute(
            f"UPDATE summaries SET {set_clause} WHERE id = ?",
            values
        )
        self.db.commit()

        if {"narrative", "topics", "key_moments"} & set(filtered.keys()):
            current = self.get_summary(summary_id) or {}
            narrative = filtered.get("narrative", current.get("narrative", ""))
            topics = current.get("topics", [])
            key_moments = current.get("key_moments", [])
            embed_text = build_summary_embed_text(narrative, topics, key_moments)
            try:
                embedding = self.embedder.encode(embed_text)
                self.summaries_collection.update(
                    ids=[summary_id],
                    embeddings=[embedding.tolist()],
                    documents=[narrative],
                )
            except Exception as e:
                print(f"  ⚠️ summary Chroma update failed for {summary_id} (SQLite saved): {e}")

    def get_recent_summaries(self, n=5):
        """Get the N most recent summaries."""
        rows = self.db.execute(
            "SELECT * FROM summaries ORDER BY created_at DESC LIMIT ?", (n,)
        ).fetchall()
        results = []
        for row in rows:
            r = dict(row)
            r["topics"] = json.loads(r["topics"])
            r["key_moments"] = json.loads(r["key_moments"])
            results.append(r)
        return results

    def get_all_summaries(self):
        """All session summaries, newest first (same row shape as get_recent_summaries)."""
        rows = self.db.execute(
            "SELECT * FROM summaries ORDER BY created_at DESC"
        ).fetchall()
        results = []
        for row in rows:
            r = dict(row)
            r["topics"] = json.loads(r["topics"])
            r["key_moments"] = json.loads(r["key_moments"])
            results.append(r)
        return results

    # =========================================================
    # REFLECTIONS (TEMPORAL PROCESSING)
    # =========================================================

    def save_reflection(self, reflection_data):
        """
        Save a temporal reflection to both SQLite and ChromaDB.
        """
        reflection_id = f"ref_{reflection_data['horizon']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"

        self.db.execute("""
            INSERT INTO reflections
            (id, horizon, window_start, window_end, themes, patterns,
             emotional_arc, connections, growth, relationship_arc,
             self_narrative, unresolved, significance)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            reflection_id,
            reflection_data["horizon"],
            reflection_data["window_start"],
            reflection_data["window_end"],
            json.dumps(reflection_data.get("themes", [])),
            json.dumps(reflection_data.get("patterns", [])),
            reflection_data.get("emotional_arc", ""),
            json.dumps(reflection_data.get("connections", [])),
            json.dumps(reflection_data.get("growth", [])),
            reflection_data.get("relationship_arc", ""),
            reflection_data.get("self_narrative", ""),
            json.dumps(reflection_data.get("unresolved", [])),
            reflection_data.get("significance", 5),
        ))
        self.db.commit()

        embed_text = (
            f"[{reflection_data['horizon']} reflection] "
            f"{reflection_data.get('emotional_arc', '')} "
            f"Themes: {', '.join(reflection_data.get('themes', []))} "
            f"Patterns: {'; '.join(reflection_data.get('patterns', []))}"
        )
        embedding = self.embedder.encode(embed_text)
        self.reflections_collection.add(
            embeddings=[embedding.tolist()],
            documents=[reflection_data.get("emotional_arc", "")],
            ids=[reflection_id],
            metadatas=[{
                "horizon": reflection_data["horizon"],
                "significance": reflection_data.get("significance", 5),
                "window_start": reflection_data["window_start"],
                "window_end": reflection_data["window_end"],
            }]
        )

        self.log("reflection_saved", f"Saved {reflection_data['horizon']} reflection: {reflection_id}")
        return reflection_id

    def get_recent_reflections(self, horizon=None, n=5):
        """Get recent reflections, optionally filtered by horizon."""
        if horizon:
            rows = self.db.execute(
                "SELECT * FROM reflections WHERE horizon = ? ORDER BY created_at DESC LIMIT ?",
                (horizon, n)
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT * FROM reflections ORDER BY created_at DESC LIMIT ?",
                (n,)
            ).fetchall()
        results = []
        for row in rows:
            r = dict(row)
            for field in ("themes", "patterns", "connections", "growth", "unresolved"):
                if isinstance(r.get(field), str):
                    try:
                        r[field] = json.loads(r[field])
                    except json.JSONDecodeError:
                        r[field] = []
            results.append(r)
        return results

    def get_reflection(self, reflection_id):
        """Get a specific reflection by ID."""
        row = self.db.execute(
            "SELECT * FROM reflections WHERE id = ?", (reflection_id,)
        ).fetchone()
        if not row:
            return None
        r = dict(row)
        for field in ("themes", "patterns", "connections", "growth", "unresolved"):
            if isinstance(r.get(field), str):
                try:
                    r[field] = json.loads(r[field])
                except json.JSONDecodeError:
                    r[field] = []
        return r

    def update_reflection(self, reflection_id, updates):
        """
        Update an existing reflection record.
        Can update: emotional_arc, relationship_arc, self_narrative, patterns, connections.
        Re-embeds in ChromaDB when emotional_arc or patterns change (those are
        the fields written as the collection document / embed text).
        """
        allowed_fields = {
            "emotional_arc", "relationship_arc", "self_narrative",
            "patterns", "connections",
        }
        filtered = {k: v for k, v in updates.items() if k in allowed_fields}

        if not filtered:
            return

        if "patterns" in filtered:
            patterns = filtered["patterns"]
            filtered["patterns"] = json.dumps(patterns) if isinstance(patterns, list) else patterns
        if "connections" in filtered:
            connections = filtered["connections"]
            filtered["connections"] = json.dumps(connections) if isinstance(connections, list) else connections

        filtered["edited_at"] = datetime.now(timezone.utc).isoformat()

        set_clause = ", ".join(f"{k} = ?" for k in filtered)
        values = list(filtered.values()) + [reflection_id]

        self.db.execute(
            f"UPDATE reflections SET {set_clause} WHERE id = ?",
            values
        )
        self.db.commit()

        if {"emotional_arc", "patterns"} & set(filtered.keys()):
            current = self.get_reflection(reflection_id) or {}
            emotional_arc = current.get("emotional_arc", "") or ""
            themes = current.get("themes") or []
            patterns = current.get("patterns") or []
            if not isinstance(themes, list):
                themes = []
            if not isinstance(patterns, list):
                patterns = []
            horizon = current.get("horizon", "")
            embed_text = (
                f"[{horizon} reflection] "
                f"{emotional_arc} "
                f"Themes: {', '.join(str(t) for t in themes)} "
                f"Patterns: {'; '.join(str(p) for p in patterns)}"
            )
            try:
                embedding = self.embedder.encode(embed_text)
                self.reflections_collection.update(
                    ids=[reflection_id],
                    embeddings=[embedding.tolist()],
                    documents=[emotional_arc],
                )
            except Exception as e:
                print(f"  ⚠️ reflection Chroma update failed for {reflection_id} (SQLite saved): {e}")

    def get_reflection_count(self):
        """Count reflections by horizon."""
        rows = self.db.execute("""
            SELECT horizon, COUNT(*) as count
            FROM reflections
            GROUP BY horizon
        """).fetchall()
        return {r["horizon"]: r["count"] for r in rows}

    # =========================================================
    # MEMORIES (DOSSIER)
    # =========================================================

    def save_memory(self, memory_data):
        """
        Save an extracted memory to both SQLite and ChromaDB.

        memory_data should be a dict with:
            category, priority, confidence, content, source_summary_id
        """
        memory_id = memory_data.get("id") or f"mem_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"

        event_date = normalize_conversation_timestamp(memory_data.get("event_date"))
        first_seen = normalize_conversation_timestamp(memory_data.get("first_seen")) or event_date or ""
        last_seen = normalize_conversation_timestamp(memory_data.get("last_seen")) or first_seen or event_date or ""

        # Normalize entities to JSON string for SQLite
        raw_entities = memory_data.get("entities", [])
        if not isinstance(raw_entities, list):
            raw_entities = []
        # ensure_ascii=False: macron/accent names (Tāne, Aodáhn, Étienne) were being
        # stored as \uXXXX escapes, which the entity-recall LIKE match can never hit.
        entities_json = json.dumps(raw_entities, ensure_ascii=False)

        # Store in SQLite
        self.db.execute("""
            INSERT INTO memories
            (id, category, priority, confidence, content, source_summary_id,
             event_date, first_seen, last_seen, entities, emotional_intensity)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            memory_id,
            memory_data["category"],
            memory_data["priority"],
            memory_data["confidence"],
            memory_data["content"],
            memory_data.get("source_summary_id"),
            event_date or "",
            first_seen,
            last_seen,
            entities_json,
            max(0, min(10, int(memory_data.get("emotional_intensity") or 0))),
        ))
        self.db.commit()

        # Store embedding in ChromaDB (enriched with category/priority/entities for better retrieval)
        embed_text = build_memory_embed_text(
            memory_data["category"], memory_data["priority"], memory_data["content"],
            entities=raw_entities,
        )
        embedding = self.embedder.encode(embed_text)
        self.memories_collection.add(
            embeddings=[embedding.tolist()],
            documents=[memory_data["content"]],
            ids=[memory_id],
            metadatas=[{
                "category": memory_data["category"],
                "priority": memory_data["priority"],
                "confidence": memory_data["confidence"],
                "entities": entities_json,
                "suppressed": int(memory_data.get("suppressed") or 0),
            }]
        )

        return memory_id

    def find_similar_memories(self, text, n=5, min_confidence=0, query_embedding=None, include_suppressed=False):
        """
        Search for memories semantically similar to the given text.
        Returns full structured records from SQLite with distance scores attached.
        ChromaDB cosine distance: 0.0 = identical, 2.0 = opposite.
        When include_suppressed is False, Chroma filters on metadata suppressed=0
        so suppressed rows cannot occupy the top-N. Docs missing that key are also
        excluded — run backfill_chroma_suppressed_metadata() once per companion.
        """
        # Query-side bge instruction prefix — queries only, never passages.
        # entity_recall_v2: callers may pass a precomputed (already-prefixed)
        # embedding to avoid encoding the same query twice.
        if query_embedding is None:
            _q = (BGE_QUERY_PREFIX + text) if BGE_QUERY_PREFIX_ENABLED else text
            embedding = self.embedder.encode(_q)
        else:
            embedding = query_embedding
        query_kwargs = {
            "query_embeddings": [embedding.tolist()],
            "n_results": n,
            "include": ["distances"],
        }
        if not include_suppressed:
            query_kwargs["where"] = {"suppressed": 0}
        results = self.memories_collection.query(**query_kwargs)

        if not results["ids"][0]:
            return []

        # Build ID-to-distance lookup
        id_distance = dict(zip(results["ids"][0], results["distances"][0]))

        # Get full records from SQLite, attach distance score.
        # Pinned/protected/sensitive memories bypass the confidence floor — "never
        # forget" flags shouldn't be blocked from recall by a stale confidence value.
        memories = []
        for memory_id in results["ids"][0]:
            row = self.db.execute(
                "SELECT * FROM memories WHERE id = ? AND active = 1 "
                "AND (confidence >= ? OR pinned = 1 OR protected = 1 OR sensitive = 1)",
                (memory_id, min_confidence)
            ).fetchone()
            if row:
                mem = dict(row)
                mem["_distance"] = id_distance.get(memory_id, 2.0)
                memories.append(mem)

        return memories

    @staticmethod
    def _detect_query_entities(query):
        """
        Extract likely entity names from a query string.
        Returns a set of lowercase names for content-matching.
        Simple heuristic: capitalized words > 2 chars that aren't common English words.
        """
        if not query:
            return set()
        common_words = {
            "the", "and", "but", "for", "are", "was", "were", "been", "being",
            "have", "has", "had", "does", "did", "will", "would", "could", "should",
            "may", "might", "shall", "can", "need", "dare", "ought", "used",
            "not", "what", "when", "where", "which", "who", "whom", "how", "why",
            "this", "that", "these", "those", "here", "there", "each", "every",
            "all", "both", "few", "more", "most", "other", "some", "such",
            "than", "too", "very", "just", "also", "still", "already", "always",
            "never", "often", "sometimes", "about", "after", "before", "between",
            "from", "into", "through", "during", "since", "until", "with", "without",
            "she", "her", "him", "his", "they", "them", "their", "its",
            "tell", "told", "said", "says", "know", "knew", "think", "thought",
            "like", "want", "wanted", "feel", "felt", "going", "went", "come", "came",
            "remember", "recall", "what", "happened", "last", "time", "first",
            "really", "actually", "maybe", "probably", "something", "anything",
            "today", "yesterday", "tonight", "morning", "evening", "week", "month",
        }
        words = re.findall(r'\b[A-Z][a-zA-Z]{2,}\b', query)
        # Only keep words that aren't at sentence start (heuristic: preceded by non-period)
        # Since we can't reliably detect sentence starts in short queries,
        # just filter out common words.
        entities = set()
        for w in words:
            if w.lower() not in common_words:
                entities.add(w.lower())
        return entities

    def _entity_stats(self):
        """
        entity_recall_v2 — ONE scan of the active entities column building:
          vocab counts: {lowercase entity or component word -> memory count}
          total: active memory count
        Cached per process. Drift is add-only and safe: new memories aren't
        counted until restart, so a growing name looks rarer than it is and
        keeps its boost slightly too long. Pure SQLite + json, no network.
        Returns ({}, 0) and prints loudly on failure — feature goes inert,
        search itself is never broken.
        """
        cached = getattr(self, "_entity_stats_cache", None)
        if cached is not None:
            return cached
        import time as _time
        _t0 = _time.time()
        counts, total = {}, 0
        try:
            rows = self.db.execute(
                "SELECT entities FROM memories WHERE active = 1"
            ).fetchall()
            total = len(rows)
            for r in rows:
                raw = r["entities"]
                if not raw:
                    continue
                try:
                    ents = json.loads(raw)
                    if not isinstance(ents, list):
                        ents = []
                except (json.JSONDecodeError, TypeError):
                    # Fall back to word-ish tokens from the raw string
                    ents = re.findall(r"[\w'-]+", str(raw))
                seen_this_row = set()
                for e in ents:
                    full = str(e).strip().lower()
                    if len(full) >= 3 and full not in ENTITY_STOPWORDS:
                        seen_this_row.add(full)
                    # component words of multi-word entities ("uber eats" -> "uber", "eats")
                    for w in re.findall(r"[\w'-]+", full):
                        if len(w) >= 3 and w not in ENTITY_STOPWORDS:
                            seen_this_row.add(w)
                for key in seen_this_row:
                    counts[key] = counts.get(key, 0) + 1
            print(f"  \U0001f524 entity vocab built: {len(counts)} terms from {total} memories "
                  f"in {int((_time.time() - _t0) * 1000)}ms (cached until restart)")
        except Exception as e:
            print(f"  \u26a0\ufe0f entity_recall_v2: vocab scan FAILED: {e} \u2014 "
                  f"entity recall inert this process (search itself unaffected)")
            counts, total = {}, 0
        self._entity_stats_cache = (counts, max(total, 1))
        return self._entity_stats_cache

    def _entity_doc_freq(self, entity):
        """entity_recall_v2 — fraction of active memories whose ENTITIES column
        names this term. Unknown terms return 1.0 (common: no boost)."""
        counts, total = self._entity_stats()
        key = (entity or "").lower()
        if not key or not counts:
            return 1.0
        c = counts.get(key, 0)
        # Unknown term -> COMMON (1.0), never "infinitely rare" (0.0): an
        # unmatched name must get no boost, not the maximum one.
        return (c / float(total)) if c else 1.0

    def _detect_query_entities_v2(self, query):
        """
        entity_recall_v2 — vocabulary-based entity detection. A query token is
        an entity iff it appears in the entities-column vocab. Works on
        lowercase text (the old Capitalized-word heuristic missed "amanda" —
        confirmed 15 Aug bench). Stopwords + len<3 excluded.
        """
        if not query:
            return set()
        counts, _total = self._entity_stats()
        if not counts:
            return set()
        found = set()
        for tok in re.findall(r"[\w'-]+", query.lower()):
            if len(tok) >= 3 and tok not in ENTITY_STOPWORDS and tok in counts:
                found.add(tok)
        return found

    def _entity_recall_memories(self, entities, query_vec, min_confidence=0):
        """
        entity_recall_v2 — direct lookup of memories whose ENTITIES column
        names a RARE entity (word-boundary matched), ranked by cosine between
        the query vector and each memory's ALREADY-STORED embedding. Nothing
        re-embedded, no LLM, no network, one pass per rare entity.
        Respects min_confidence (pinned rows bypass, mirroring
        find_similar_memories). Returns [] on failure, never breaks search.
        """
        if not entities:
            return []
        if not hasattr(self, "_entity_common_logged"):
            self._entity_common_logged = set()
        picked = {}
        for ent in sorted(entities):
            try:
                df = self._entity_doc_freq(ent)
                if df > ENTITY_RARE_MAX_DF:
                    if ent not in self._entity_common_logged:
                        print(f"  \U0001f524 entity '{ent}': in {df:.2%} of memories \u2014 common, "
                              f"no recall pass (limit {ENTITY_RARE_MAX_DF:.2%}; logged once)")
                        self._entity_common_logged.add(ent)
                    continue
                ent_lower = ent.lower()
                word_re = re.compile(r"\b" + re.escape(ent_lower) + r"\b")
                # SQL LIKE is a broad prefilter; the word-boundary regex is the
                # real gate ("puberty" contains "uber" — substring is not enough).
                rows = self.db.execute(
                    "SELECT * FROM memories WHERE active = 1 "
                    "AND (suppressed = 0 OR suppressed IS NULL) "
                    "AND (confidence >= ? OR pinned = 1 OR protected = 1 OR sensitive = 1) "
                    "AND LOWER(entities) LIKE ?",
                    (min_confidence, f"%{ent_lower}%"),
                ).fetchall()
                rows = [r for r in rows if word_re.search((r["entities"] or "").lower())]
                if not rows:
                    continue
                ids = [r["id"] for r in rows]
                vecs = {}
                try:
                    got = self.memories_collection.get(ids=ids, include=["embeddings"])
                    for mid, emb in zip(got["ids"], got["embeddings"]):
                        v = np.asarray(emb, dtype=np.float32)
                        nrm = float(np.linalg.norm(v))
                        vecs[mid] = v / nrm if nrm else v
                except Exception as e:
                    print(f"  \u26a0\ufe0f entity_recall_v2: stored-vector fetch failed for '{ent}': {e} "
                          f"\u2014 those hits rank at worst distance instead")
                scored = []
                for r in rows:
                    m = dict(r)
                    v = vecs.get(m["id"])
                    m["_distance"] = float(1.0 - float(np.dot(query_vec, v))) if v is not None else 1.0
                    m["_entity_recall"] = ent
                    scored.append(m)
                scored.sort(key=lambda x: x["_distance"])
                kept = scored[:ENTITY_RECALL_PER_ENTITY]
                if len(scored) > len(kept):
                    print(f"  \U0001f524 entity '{ent}': {df:.2%} of memories \u2014 RARE, "
                          f"{len(scored)} matches, keeping best {len(kept)} "
                          f"(cap TANEVAN_ENTITY_RECALL_PER_ENTITY={ENTITY_RECALL_PER_ENTITY})")
                else:
                    print(f"  \U0001f524 entity '{ent}': {df:.2%} of memories \u2014 RARE, "
                          f"recalled all {len(kept)}")
                for m in kept:
                    picked.setdefault(m["id"], m)
            except Exception as e:
                print(f"  \u26a0\ufe0f entity_recall_v2: recall pass failed for '{ent}': {e} \u2014 skipped")
        return list(picked.values())

    def search_memories(self, query, n=10, category=None, min_priority=None, min_confidence=0, include_suppressed=False, mmr=False):
        """
        Search memories with optional filters, quality floor, and re-ranking.
        Pass 1: ChromaDB semantic search (adaptive wide net)
        Pass 2: Filter by category/priority/suppressed + distance quality floor
        Pass 3: Re-rank by blended score (distance + priority + confidence + recency + entity match)
        Only returns memories that clear the quality floor — if only 2 are good, returns 2.
        """
        # Adaptive multiplier: scale up at high volumes so the re-ranker has more signal.
        # At 500 memories, fetch n*3 (30). At 15k, fetch up to 200.
        try:
            total = self.memories_collection.count()
        except Exception:
            total = 0
        if total > 2000:
            fetch_n = max(n * 3, min(200, int(total * 0.02)))
        else:
            fetch_n = n * 3
        # MMR needs a wider candidate pool to diversify from
        if mmr:
            fetch_n = max(fetch_n, n * MMR_OVERFETCH_MULTIPLIER)

        # Pass 1: Cast a wide net from ChromaDB
        # entity_recall_v2: encode the (prefixed) query ONCE, reused by the
        # vector pass and for scoring entity-recall hits against stored vectors.
        _q_text = (BGE_QUERY_PREFIX + query) if BGE_QUERY_PREFIX_ENABLED else query
        try:
            _qv = self.embedder.encode(_q_text)
        except Exception as e:
            print(f"  \u26a0\ufe0f entity_recall_v2: query encode failed: {e} \u2014 "
                  f"falling back to in-call encode, entity recall off this query")
            _qv = None
        raw_results = self.find_similar_memories(
            query, n=fetch_n, min_confidence=min_confidence, query_embedding=_qv,
            include_suppressed=include_suppressed,
        )

        # Detect entity names in the query. v2: vocabulary-based, lowercase-safe
        # (the old Capitalized-word heuristic missed "amanda" — 15 Aug bench).
        query_entities = self._detect_query_entities_v2(query) if ENTITY_RECALL_ENABLED else set()

        # entity_recall_v2: rare names get their own recall path — pure cosine
        # on a long conversational query drowns a rare name in the dominant
        # topic, so the re-ranker otherwise never sees those memories at all.
        entity_recall_ids = set()
        if ENTITY_RECALL_ENABLED and query_entities and _qv is not None:
            _nrm = float(np.linalg.norm(_qv))
            _qv_norm = _qv / _nrm if _nrm else _qv
            _ehits = self._entity_recall_memories(query_entities, _qv_norm, min_confidence=min_confidence)
            _known = {m["id"] for m in raw_results}
            for m in raw_results:
                for _e in _ehits:
                    if m["id"] == _e["id"]:
                        m["_entity_recall"] = _e.get("_entity_recall")
            _added = [m for m in _ehits if m["id"] not in _known]
            if _added:
                print(f"  \U0001f524 entity recall added {len(_added)} memories the vector pass missed")
                raw_results = raw_results + _added
            entity_recall_ids = {m["id"] for m in _ehits}

        # Pass 2: Apply filters + quality floor
        priority_order = {"core": 0, "important": 1, "notable": 2, "minor": 3}
        filtered = []
        for mem in raw_results:
            # Quality floor — reject memories that aren't similar enough.
            # entity_recall_v2: a memory named directly in the query is exempt
            # — it was asked about by name.
            if mem.get("_distance", 2.0) > DISTANCE_FLOOR and not mem.get("_entity_recall"):
                continue
            if category and mem["category"] != category:
                continue
            if min_priority:
                if priority_order.get(mem["priority"], 99) > priority_order.get(min_priority, 99):
                    continue
            if not include_suppressed and mem.get("suppressed", 0):
                continue
            filtered.append(mem)

        # Pass 3: Re-rank by blended score
        # Lower = better. Combines semantic distance, priority weight, confidence, recency,
        # and entity-match signal.
        # protected_floor_v1.1 (30 Aug bench): a flat protected bonus was wider
        # than the pool's semantic spread, so a poorly-fitting identity memory
        # jumped to #1 on unrelated queries. Bonus now scales with fit inside
        # this query's pool: best-fitting protected row gets the full bonus,
        # worst-fitting gets ~0. Reserved seat only considers the better half.
        _pd = [m.get("_distance", 1.5) for m in filtered]
        _pool_min = min(_pd) if _pd else 0.0
        _pool_max = max(_pd) if _pd else 1.0
        _pool_median = sorted(_pd)[len(_pd) // 2] if _pd else 1.0
        _pool_span = (_pool_max - _pool_min) or 1.0
        def rerank_score(mem):
            distance = mem.get("_distance", 1.5)
            pri_weight = priority_order.get(mem.get("priority", "minor"), 3) * 0.1
            _cw_pri, _cw_rec = (1.0, 1.0)
            if CATEGORY_WEIGHTS_ENABLED:
                _cw_pri, _cw_rec = CATEGORY_TERM_WEIGHTS.get(mem.get("category") or "experience", (1.0, 1.0))
            pri_weight *= _cw_pri
            conf_bonus = (100 - mem.get("confidence", 50)) * 0.005
            # Recency bonus: memories seen more recently score slightly better
            recency = 0.0
            last_seen = mem.get("last_seen") or mem.get("first_seen") or ""
            if last_seen:
                try:
                    seen_dt = parse_db_datetime(last_seen)
                    if seen_dt is None:
                        raise ValueError("invalid last_seen")
                    age_days = (datetime.now(timezone.utc) - ensure_utc(seen_dt)).days
                    recency = min(age_days * 0.001, 0.3)  # caps at 0.3 for ~300 day old memories
                except (ValueError, TypeError):
                    pass

            # entity_recall_v2 — IDF-weighted entity signal, ENTITIES column
            # only (content matching diluted the curated signal: "eats" 98
            # content hits vs 3 entity hits), word-boundary matched ("puberty"
            # contains "uber" — substring is not enough). Rarer names pull
            # harder; common names (chris, 11%) pull ~nothing. Scale 0.15 —
            # v1's 0.45 was wider than the whole semantic spread (measured).
            entity_adj = 0.0
            if ENTITY_RECALL_ENABLED and query_entities:
                ents_lower = (mem.get("entities") or "").lower()
                best_strength = 0.0
                any_rare_asked = False
                for e in query_entities:
                    _df = self._entity_doc_freq(e)
                    if _df > ENTITY_RARE_MAX_DF:
                        continue
                    any_rare_asked = True
                    if ents_lower and re.search(r"\b" + re.escape(e) + r"\b", ents_lower):
                        strength = 1.0 - (_df / ENTITY_RARE_MAX_DF) if ENTITY_RARE_MAX_DF else 1.0
                        best_strength = max(best_strength, strength)
                if best_strength > 0:
                    entity_adj = -ENTITY_MAX_BONUS * best_strength
                elif any_rare_asked:
                    entity_adj = ENTITY_MISS_PENALTY

            # protected_floor_v1: identity memories sit lower (better) on the board
            protected_adj = 0.0
            if PROTECTED_FLOOR_ENABLED and mem.get("protected"):
                _fit = max(0.0, min(1.0, (_pool_max - distance) / _pool_span))
                protected_adj = -PROTECTED_BONUS * _fit
            intensity_adj = 0.0
            if INTENSITY_WEIGHT_ENABLED:
                intensity_adj = -INTENSITY_MAX_BONUS * (max(0, min(10, int(mem.get("emotional_intensity") or 0))) / 10.0)
            return distance + pri_weight + conf_bonus + (recency * _cw_rec) + entity_adj + protected_adj + intensity_adj

        for mem in filtered:
            mem["_rerank_score"] = rerank_score(mem)

        filtered.sort(key=lambda m: m["_rerank_score"])

        # entity_recall_v2: guarantee up to ENTITY_RESERVED_SLOTS seats to
        # memories naming a rare entity from the query, so a dominant topical
        # cluster can't take every slot. Implemented as a score floor (not list
        # position) so it survives downstream re-sorts.
        if ENTITY_RECALL_ENABLED and ENTITY_RESERVED_SLOTS > 0 and entity_recall_ids and len(filtered) > n:
            _head_ids = {m["id"] for m in filtered[:n]}
            _already = sum(1 for _i in entity_recall_ids if _i in _head_ids)
            _need = ENTITY_RESERVED_SLOTS - _already
            if _need > 0:
                _cutoff = filtered[n - 1]["_rerank_score"]
                _missing = [m for m in filtered
                            if m["id"] in entity_recall_ids and m["id"] not in _head_ids][:_need]
                for _j, _m in enumerate(_missing):
                    _m["_rerank_score"] = _cutoff - 0.001 * (_j + 1)
                if _missing:
                    _names = ", ".join(sorted({m.get("_entity_recall") or "?" for m in _missing}))
                    print(f"  \U0001f524 reserved {len(_missing)} slot(s) for entity hits ({_names}) "
                          f"\u2014 they would have missed the top {n}")
                    filtered.sort(key=lambda m: m["_rerank_score"])
        # protected_floor_v1: guarantee up to PROTECTED_RESERVED_SLOTS seats to
        # protected memories that cleared the distance floor but missed the top n.
        # Score floor, not list position — same pattern as the entity seat above.
        if PROTECTED_FLOOR_ENABLED and PROTECTED_RESERVED_SLOTS > 0 and len(filtered) > n:
            _p_head = filtered[:n]
            _p_already = sum(1 for m in _p_head if m.get("protected"))
            _p_need = PROTECTED_RESERVED_SLOTS - _p_already
            if _p_need > 0:
                _p_cutoff = filtered[n - 1]["_rerank_score"]
                _p_missing = [m for m in filtered[n:]
                              if m.get("protected") and m.get("_distance", 1.5) <= _pool_median][:_p_need]
                for _j, _m in enumerate(_p_missing):
                    _m["_rerank_score"] = _p_cutoff - 0.001 * (_j + 1)
                if _p_missing:
                    print(f"  \U0001f512 reserved {len(_p_missing)} slot(s) for protected memories "
                          f"\u2014 they would have missed the top {n}")
                    filtered.sort(key=lambda m: m["_rerank_score"])
        if mmr and len(filtered) > n:
            # Fail open: any error in diversity selection falls back to pure relevance
            try:
                return self._mmr_select(filtered[:n * MMR_OVERFETCH_MULTIPLIER], n)
            except Exception as e:
                print(f"  ⚠️ MMR selection failed, falling back to relevance ranking: {e}")
        return filtered[:n]

    def _mmr_select(self, candidates, n):
        """
        Maximal Marginal Relevance selection over reranked candidates.
        Fills n slots balancing query relevance against similarity to what's
        already selected, so one dominant cluster can't take every slot.
        Pinned memories keep the slots they'd have under pure relevance and are
        never MMR candidates — they seed the selected set, and MMR fills only
        the remaining slots. Uses stored ChromaDB embeddings; nothing is re-embedded.
        Candidates must arrive sorted by _rerank_score (best first).
        """
        baseline = candidates[:n]  # what pure relevance would return today
        # entity_recall_v2: entity hits seed selection alongside pinned, so
        # diversity selection can never drop the name that was asked about.
        # (Inert while MMR chat injection is off; correct if re-enabled.)
        pinned = [m for m in baseline if m.get("pinned") or m.get("_entity_recall")]
        slots = n - len(pinned)
        if slots <= 0:
            return baseline

        pool = [m for m in candidates if not m.get("pinned")]

        # One batch read of existing stored embeddings — no re-embedding
        ids = [m["id"] for m in pinned] + [m["id"] for m in pool]
        got = self.memories_collection.get(ids=ids, include=["embeddings"])
        vecs = {}
        for mid, emb in zip(got["ids"], got["embeddings"]):
            v = np.asarray(emb, dtype=np.float32)
            norm = float(np.linalg.norm(v))
            vecs[mid] = v / norm if norm else v

        def relevance(m):
            # ChromaDB cosine distance -> cosine similarity
            return 1.0 - m.get("_distance", 1.0)

        selected = list(pinned)
        selected_vecs = [vecs[m["id"]] for m in selected]
        remaining = [m for m in pool if m["id"] in vecs]

        while remaining and len(selected) < n:
            best, best_score = None, None
            for m in remaining:
                max_sim = max(
                    (float(np.dot(vecs[m["id"]], sv)) for sv in selected_vecs),
                    default=0.0,
                )
                score = MMR_LAMBDA * relevance(m) - (1.0 - MMR_LAMBDA) * max_sim
                if best_score is None or score > best_score:
                    best, best_score = m, score
            selected.append(best)
            selected_vecs.append(vecs[best["id"]])
            remaining.remove(best)

        chosen_ids = {m["id"] for m in selected}
        for m in baseline:
            if m["id"] not in chosen_ids:
                rel = relevance(m)
                print(f"  🎯 MMR: {m['id']} relevance_score={rel:.3f} suppressed: diversity")
                self.write_audit_event(
                    "retrieval_suppressed",
                    [{"memory_id": m["id"], "category": m.get("category"), "content": m.get("content")}],
                    result_memory=None,
                    reason=f"relevance_score={rel:.3f} suppressed: diversity",
                )

        # Same ordering contract as the pure-relevance path
        selected.sort(key=lambda m: m["_rerank_score"])
        return selected

    # =========================================================
    # AUDIT LOG
    # =========================================================

    def write_audit_event(self, event_type, source_memories, result_memory=None,
                          reason=None, status="committed"):
        """
        Write an audit_log row. Returns the new row id, or None on failure.
        NEVER raises — auditing must not block or abort the operation it records.
        source_memories: list of {memory_id, category, content} dicts.
        """
        try:
            ts = datetime.now(timezone.utc).isoformat()
            cur = self.db.execute(
                "INSERT INTO audit_log (timestamp, companion, event_type, "
                "source_memories, result_memory, reason, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ts, self.companion_name, event_type,
                 json.dumps(source_memories, ensure_ascii=False),
                 json.dumps(result_memory, ensure_ascii=False) if result_memory is not None else None,
                 reason, status)
            )
            self.db.commit()
            return cur.lastrowid
        except Exception as e:
            print(f"  ⚠️ audit write failed ({event_type}): {e}")
            return None

    def set_audit_status(self, audit_id, status):
        """Update an audit row's status. NEVER raises."""
        if audit_id is None:
            return
        try:
            self.db.execute("UPDATE audit_log SET status = ? WHERE id = ?", (status, audit_id))
            self.db.commit()
        except Exception as e:
            print(f"  ⚠️ audit status update failed (row {audit_id}): {e}")

    def get_audit_entries(self, event_type=None, limit=50, before_id=None):
        """Newest-first audit rows with keyset pagination. Returns (entries, has_more)."""
        limit = max(1, min(int(limit), 200))
        where, params = [], []
        if event_type:
            where.append("event_type = ?")
            params.append(event_type)
        if before_id is not None:
            where.append("id < ?")
            params.append(int(before_id))
        clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = self.db.execute(
            f"SELECT * FROM audit_log {clause} ORDER BY id DESC LIMIT ?",
            (*params, limit + 1)
        ).fetchall()
        has_more = len(rows) > limit
        entries = []
        for row in rows[:limit]:
            entry = dict(row)
            for key in ("source_memories", "result_memory"):
                if entry.get(key):
                    try:
                        entry[key] = json.loads(entry[key])
                    except (json.JSONDecodeError, TypeError):
                        pass
            entries.append(entry)
        return entries, has_more

    def prune_audit_log(self, retention_days=None):
        """Delete audit rows older than retention_days. NEVER raises."""
        days = AUDIT_RETENTION_DAYS if retention_days is None else retention_days
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            cur = self.db.execute("DELETE FROM audit_log WHERE timestamp < ?", (cutoff,))
            self.db.commit()
            if cur.rowcount:
                print(f"  🧾 audit_log: pruned {cur.rowcount} rows older than {days}d")
            return cur.rowcount
        except Exception as e:
            print(f"  ⚠️ audit prune failed: {e}")
            return 0

    def update_memory(self, memory_id, updates, source="unknown"):
        """
        Update an existing memory record.
        Can update: confidence, priority, content, reinforcement_count, last_seen, active,
                    category, pinned, suppressed
        """
        allowed_fields = {"confidence", "priority", "content", "reinforcement_count",
                          "last_seen", "first_seen", "event_date", "active", "category",
                          "pinned", "suppressed", "protected", "sensitive", "entities",
                          "superseded_by", "emotional_intensity"}
        filtered = {k: v for k, v in updates.items() if k in allowed_fields}

        if not filtered:
            return

        # Content rewrite without an explicit entities payload: refresh tags so
        # named recall tracks who the memory is about now (user edits, updater
        # merge/update, consolidation merge). Explicit entities from the caller win.
        if "content" in filtered and "entities" not in filtered:
            from backfill_entities import refresh_entities_for_content
            prior = self.get_memory_by_id(memory_id) or {}
            _refreshed = refresh_entities_for_content(filtered.get("content") or "", prior.get("entities"))
            # Filter through the retrieval stopword list — the Title Case regex is
            # what minted junk entities ("people", "home") in the first place.
            _refreshed = [e for e in _refreshed if e.lower() not in ENTITY_STOPWORDS]
            filtered["entities"] = json.dumps(_refreshed, ensure_ascii=False)
        elif "entities" in filtered:
            from backfill_entities import _parse_entities_field
            filtered["entities"] = json.dumps(_parse_entities_field(filtered["entities"]), ensure_ascii=False)

        set_clause = ", ".join(f"{k} = ?" for k in filtered)
        # memory_versions_v1: snapshot the row we're about to overwrite.
        if MEMORY_VERSIONS_ENABLED and "content" in filtered:
            try:
                _prev = self.get_memory_by_id(memory_id)
                if _prev and (_prev.get("content") or "") != (filtered.get("content") or ""):
                    self.db.execute(
                        "INSERT INTO memory_versions (memory_id, content, category, priority, confidence, entities, source, replaced_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (memory_id, _prev.get("content") or "", _prev.get("category"), _prev.get("priority"),
                         _prev.get("confidence"), _prev.get("entities"), source,
                         datetime.now(timezone.utc).isoformat()),
                    )
            except Exception as _e:
                print(f"  \u26a0\ufe0f memory_versions: snapshot failed for {memory_id} ({_e}) \u2014 rewrite proceeding")
        values = list(filtered.values()) + [memory_id]

        self.db.execute(
            f"UPDATE memories SET {set_clause} WHERE id = ?",
            values
        )
        self.db.commit()

        # chroma_prune_v1: retired rows leave the index; restored rows come back.
        if CHROMA_PRUNE_INACTIVE and "active" in filtered:
            try:
                if int(filtered["active"]) == 0:
                    self.memories_collection.delete(ids=[memory_id])
                else:
                    _cur = self.get_memory_by_id(memory_id) or {}
                    _et = build_memory_embed_text(_cur.get("category", "fact"), _cur.get("priority", "important"),
                                                  _cur.get("content", ""), entities=_cur.get("entities", "[]"))
                    self.memories_collection.upsert(
                        ids=[memory_id],
                        embeddings=[self.embedder.encode(_et).tolist()],
                        documents=[_cur.get("content", "")],
                        metadatas=[{"category": _cur.get("category"), "priority": _cur.get("priority"),
                                    "confidence": _cur.get("confidence"), "entities": _cur.get("entities", "[]"),
                                    "suppressed": int(_cur.get("suppressed") or 0)}],
                    )
            except Exception as _e:
                print(f"  \u26a0\ufe0f chroma_prune: index sync failed for {memory_id} active={filtered['active']} ({_e})")

        # If content or category/priority changed, update the ChromaDB embedding too
        if "content" in filtered or "category" in filtered or "priority" in filtered or "entities" in filtered:
            current = self.get_memory_by_id(memory_id) or {}
            new_content = filtered.get("content", current.get("content", ""))
            new_cat = filtered.get("category", current.get("category", "fact"))
            new_pri = filtered.get("priority", current.get("priority", "important"))
            new_entities = filtered.get("entities", current.get("entities", "[]"))
            embed_text = build_memory_embed_text(new_cat, new_pri, new_content, entities=new_entities)
            embedding = self.embedder.encode(embed_text)
            self.memories_collection.update(
                ids=[memory_id],
                embeddings=[embedding.tolist()],
                documents=[new_content]
            )

        if "entities" in filtered:
            self._entity_stats_cache = None

        # If metadata fields changed, update ChromaDB metadata
        meta_fields = {"category", "priority", "confidence", "pinned", "suppressed", "entities"}
        if meta_fields & set(filtered.keys()):
            try:
                existing = self.memories_collection.get(ids=[memory_id])
                if existing and existing["metadatas"]:
                    merged = dict(existing["metadatas"][0])
                else:
                    merged = {}
                for k in meta_fields:
                    if k in filtered:
                        merged[k] = filtered[k]
                self.memories_collection.update(
                    ids=[memory_id],
                    metadatas=[merged]
                )
            except Exception:
                pass

    def merge_guard(self, target_id, merged_content, action="merge", incoming_len=None):
        """
        Merge guard — see MERGE_GUARD_ENABLED constants block.
        Returns (allowed: bool, reason: str). Callers MUST save the incoming
        memory as new on refusal; the guard never drops anything itself.
        """
        if not MERGE_GUARD_ENABLED:
            return True, "guard off"
        row = self.get_memory_by_id(target_id)
        if not row:
            reason = f"target {target_id} not found"
            print(f"  \U0001f6e1\ufe0f [MERGE-GUARD] refused {action}: {reason}")
            return False, reason
        merged_len = len(merged_content or "")
        target_len = len(row.get("content") or "")
        reason = None
        if row.get("protected"):
            reason = "target is protected"
        elif row.get("pinned"):
            reason = "target is pinned"
        elif merged_len > MERGE_GUARD_MAX_CHARS:
            reason = f"merged length {merged_len} > max {MERGE_GUARD_MAX_CHARS} (target was {target_len})"
        elif action == "merge" and incoming_len:
            floor_len = min(target_len, int(incoming_len)) if target_len > 0 else int(incoming_len)
            if merged_len < MERGE_GUARD_MIN_RETAIN * floor_len:
                reason = f"merged text {merged_len} chars is < {MERGE_GUARD_MIN_RETAIN:.0%} of the shorter input ({floor_len}) — looks like a garbage rewrite"
        if reason is None:
            return True, "ok"
        print(f"  \U0001f6e1\ufe0f [MERGE-GUARD] refused {action} into {target_id} ({reason}) \u2014 incoming memory will be saved as new")
        self.write_audit_event(
            "merge_guard",
            [{"memory_id": target_id, "category": row.get("category"), "content": row.get("content")}],
            result_memory={"memory_id": None, "category": row.get("category"), "content": merged_content},
            reason=f"refused {action}: {reason}",
            status="refused",
        )
        return False, reason

    def boost_confidence(self, memory_id, amount=5):
        """
        Increase confidence and reinforcement count for a memory
        that was confirmed by a new conversation.
        """
        row = self.db.execute(
            "SELECT confidence, reinforcement_count FROM memories WHERE id = ?",
            (memory_id,)
        ).fetchone()

        if row:
            new_confidence = min(100, row["confidence"] + amount)
            self.update_memory(memory_id, {
                "confidence": new_confidence,
                "reinforcement_count": row["reinforcement_count"] + 1,
                "last_seen": datetime.now(timezone.utc).isoformat()
            })

    def get_all_memories(self, category=None, active_only=True):
        """Get all memories, optionally filtered by category."""
        query = "SELECT * FROM memories WHERE 1=1"
        params = []

        if active_only:
            query += " AND active = 1"
        if category:
            query += " AND category = ?"
            params.append(category)

        query += " ORDER BY confidence DESC, priority ASC"
        rows = self.db.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_memory_by_id(self, memory_id):
        """Get a single memory by its ID."""
        row = self.db.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        return dict(row) if row else None

    def get_memory_stats(self):
        """Get stats about the memory dossier."""
        stats = {}

        # Total counts by category
        rows = self.db.execute("""
            SELECT category, COUNT(*) as count
            FROM memories WHERE active = 1
            GROUP BY category
        """).fetchall()
        stats["by_category"] = {r["category"]: r["count"] for r in rows}

        # Total counts by priority
        rows = self.db.execute("""
            SELECT priority, COUNT(*) as count
            FROM memories WHERE active = 1
            GROUP BY priority
        """).fetchall()
        stats["by_priority"] = {r["priority"]: r["count"] for r in rows}

        # Total active memories
        row = self.db.execute(
            "SELECT COUNT(*) as count FROM memories WHERE active = 1"
        ).fetchone()
        stats["total"] = row["count"]

        # Total summaries
        row = self.db.execute("SELECT COUNT(*) as count FROM summaries").fetchone()
        stats["total_summaries"] = row["count"]

        # Buffer status
        stats["buffer_messages"] = self.get_buffer_count()

        # Reflections
        row = self.db.execute("SELECT COUNT(*) as count FROM reflections").fetchone()
        stats["total_reflections"] = row["count"] if row else 0
        stats["reflections_by_horizon"] = self.get_reflection_count()

        return stats

    def decay_memories(self, dry_run=False, detail=False):
        """
        Apply memory decay based on time since last reinforcement.
        Models Ebbinghaus's forgetting curve: unreinforced memories fade over time.

        Rules:
        - Pinned memories: immune (never decay)
        - Protected/sensitive memories: immune (never decay, never demote)
        - Core priority: immune (never decay)
        - Reinforcement count > 6: immune (well-reinforced)
        - Reinforcement count > 3: half decay rate
        - Days since last_seen < 30: no decay (recently active)
        - Days 30-90: light decay (1 point)
        - Days 90-180: moderate decay (2 points)
        - Days 180+: heavy decay (3 points)
        - Confidence drops below 25: auto-deactivate (active = 0)

        Returns dict with stats about what happened. With detail=True, the dict
        also carries a "detail" list of per-memory entries describing each
        affected memory (action: deactivate / decay / demote) — used by the UI
        to preview a run and let the user rescue memories by pinning.
        """
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        stats = {
            "decayed": 0,
            "deactivated": 0,
            "demoted": 0,
            "immune_pinned": 0,
            "immune_protected": 0,
            "immune_core": 0,
            "immune_reinforced": 0,
            "immune_recent": 0,
            "skipped_inactive": 0,
            "total_scanned": 0,
        }
        detail_entries = []
        # Ids already handled by the main decay loop, so the demotion pass
        # skips them — mirrored for dry runs so previews match real runs.
        handled_ids = set()

        def add_detail(mem, action, **extra):
            if not detail:
                return
            detail_entries.append({
                "id": mem["id"],
                "content": mem.get("content", ""),
                "category": mem.get("category", ""),
                "priority": mem.get("priority", ""),
                "confidence": mem.get("confidence", 0),
                "reinforcement_count": mem.get("reinforcement_count", 1) or 1,
                "last_seen": mem.get("last_seen") or mem.get("first_seen") or "",
                "action": action,
                **extra,
            })

        rows = self.db.execute(
            "SELECT id, content, category, confidence, priority, pinned, protected, sensitive, "
            "reinforcement_count, last_seen, first_seen, active "
            "FROM memories"
        ).fetchall()

        stats["total_scanned"] = len(rows)
        total_active = sum(1 for r in rows if dict(r).get("active", 0))
        updates = []

        for row in rows:
            mem = dict(row)

            # Skip already-inactive memories
            if not mem["active"]:
                stats["skipped_inactive"] += 1
                continue

            # Immune: pinned
            if mem.get("pinned", 0):
                stats["immune_pinned"] += 1
                continue

            # Immune: protected/sensitive — never decays, never culled
            if mem.get("protected", 0) or mem.get("sensitive", 0):
                stats["immune_protected"] += 1
                continue

            # Immune: core priority
            if mem.get("priority") == "core":
                stats["immune_core"] += 1
                continue

            # Immune: well-reinforced (seen many times)
            reinforcement = mem.get("reinforcement_count", 1) or 1
            if reinforcement > 6:
                stats["immune_reinforced"] += 1
                continue

            # Calculate days since last seen
            last_seen_raw = mem.get("last_seen") or mem.get("first_seen") or ""
            if not last_seen_raw:
                continue
            try:
                last_seen_dt = parse_db_datetime(last_seen_raw)
                if last_seen_dt is None:
                    continue
                days_since = (now - ensure_utc(last_seen_dt)).days
            except (ValueError, TypeError):
                continue

            # Immune: recently active
            if days_since < 30:
                stats["immune_recent"] += 1
                continue

            # Hard staleness cutoff: any non-core, non-pinned memory unseen for
            # 12+ months is deactivated regardless of confidence. At 10k+ memories
            # these zombies just pollute the index.
            if days_since > 365:
                add_detail(mem, "deactivate", new_confidence=0, days_since=days_since, reason="unseen 365+ days")
                handled_ids.add(mem["id"])
                if dry_run:
                    stats["deactivated"] += 1
                    continue
                updates.append((mem["id"], {"confidence": 0, "active": 0}))
                stats["deactivated"] += 1
                continue

            # Adaptive decay: companions with more memories decay faster.
            # This prevents the index from growing unboundedly.
            if total_active > 5000:
                decay_scale = 2.0
            elif total_active > 2000:
                decay_scale = 1.5
            else:
                decay_scale = 1.0

            # Base decay by age
            if days_since < 60:
                base_decay = 1
            elif days_since < 120:
                base_decay = 3
            elif days_since < 180:
                base_decay = 5
            else:
                base_decay = 8

            decay = max(1, int(base_decay * decay_scale))

            # Half decay for moderately reinforced memories
            if reinforcement > 3:
                decay = max(1, decay // 2)

            new_confidence = max(0, mem["confidence"] - decay)

            add_detail(
                mem,
                "deactivate" if new_confidence < 25 else "decay",
                new_confidence=new_confidence,
                days_since=days_since,
            )
            handled_ids.add(mem["id"])

            if dry_run:
                if new_confidence < 25:
                    stats["deactivated"] += 1
                else:
                    stats["decayed"] += 1
                continue

            # Apply the decay
            update_fields = {"confidence": new_confidence}
            if new_confidence < 25:
                update_fields["active"] = 0
                stats["deactivated"] += 1
            else:
                stats["decayed"] += 1

            updates.append((mem["id"], update_fields))

        # Priority demotion: core/important memories that haven't been reinforced
        # in a long time get demoted. Prevents the immune-to-decay core pool from
        # growing forever. Only runs on active memories not already handled above.
        # handled_ids covers dry runs too, so previews match what a real run does.
        demoted_ids = {mid for mid, _ in updates} | handled_ids
        demotion_count = 0
        for row in rows:
            mem = dict(row)
            if not mem["active"] or mem["id"] in demoted_ids:
                continue
            if mem.get("pinned", 0):
                continue
            # Protected/sensitive memories never demote — dormancy is expected for them
            if mem.get("protected", 0) or mem.get("sensitive", 0):
                continue

            reinforcement = mem.get("reinforcement_count", 1) or 1
            last_seen_raw = mem.get("last_seen") or mem.get("first_seen") or ""
            if not last_seen_raw:
                continue
            try:
                last_seen_dt = parse_db_datetime(last_seen_raw)
                if last_seen_dt is None:
                    continue
                days_dormant = (now - ensure_utc(last_seen_dt)).days
            except (ValueError, TypeError):
                continue

            current_pri = mem.get("priority", "important")

            # Core → important after 6 months without reinforcement
            if current_pri == "core" and days_dormant > 180 and reinforcement < 10:
                add_detail(mem, "demote", new_priority="important", days_since=days_dormant)
                if not dry_run:
                    updates.append((mem["id"], {"priority": "important"}))
                demotion_count += 1

            # Important → notable after 4 months without reinforcement
            elif current_pri == "important" and days_dormant > 120 and reinforcement < 5:
                add_detail(mem, "demote", new_priority="notable", days_since=days_dormant)
                if not dry_run:
                    updates.append((mem["id"], {"priority": "notable"}))
                demotion_count += 1

        stats["demoted"] = demotion_count
        if detail:
            stats["detail"] = detail_entries

        # Batch apply updates
        for memory_id, fields in updates:
            self.update_memory(memory_id, fields)

        # Keep the per-memory detail out of the processing log — counts only.
        self.log("decay_run", json.dumps({k: v for k, v in stats.items() if k != "detail"}))
        return stats

    # =========================================================
    # RETRIEVAL FOR CONTEXT INJECTION
    # =========================================================

    @staticmethod
    def _format_memory_injection_line(mem):
        mem_date = (mem.get("event_date") or mem.get("first_seen") or "")[:10]
        cat = (mem.get("category") or "fact").upper()
        content = mem.get("content") or ""
        if mem_date:
            return f"[{cat} | {mem_date}] {content}"
        return f"[{cat}] {content}"

    def search_memories_by_date(self, start_date, end_date, n=10, include_suppressed=False):
        """
        Retrieve memories within a date range using SQLite.
        Searches event_date first, falls back to first_seen.
        Returns full memory dicts sorted by date descending (most recent first).
        """
        query = """
            SELECT * FROM memories
            WHERE active = 1
            AND (
                (event_date != '' AND event_date >= ? AND event_date < ?)
                OR (event_date IS NULL OR event_date = '')
                AND (first_seen >= ? AND first_seen < ?)
            )
        """
        params = [start_date, end_date, start_date, end_date]

        if not include_suppressed:
            query += " AND (suppressed = 0 OR suppressed IS NULL)"

        query += " ORDER BY COALESCE(NULLIF(event_date, ''), first_seen) DESC LIMIT ?"
        params.append(n)

        rows = self.db.execute(query, params).fetchall()
        return [dict(r) for r in rows]

    def get_chat_injection_memories(self, query, n=10, token_budget=None):
        """
        Memories for live chat injection: semantic search + temporal date search.
        If the query contains temporal language (e.g. "last week", "in November"),
        date-filtered results are blended with semantic results.
        Pinned memories are NOT force-included — they're favorites, not omnipresent:
        they only appear when naturally recalled (relevant), but then rank first.
        """
        n = max(1, min(int(n), 100))

        # Semantic search (always runs)
        memories = self.search_memories(query, n=n, min_confidence=40, include_suppressed=False, mmr=MMR_CHAT_INJECTION)

        # Temporal search (runs if query contains time references)
        date_range = detect_temporal_range(query)
        if date_range:
            start, end = date_range
            print(f"  📅 Temporal query detected: {start} → {end}")
            temporal_results = self.search_memories_by_date(start, end, n=n)
            existing_ids = {m["id"] for m in memories}
            for mem in temporal_results:
                if mem["id"] not in existing_ids:
                    memories.append(mem)
                    existing_ids.add(mem["id"])

        priority_order = {"core": 0, "important": 1, "notable": 2, "minor": 3}

        def _fallback_score(m):
            """Synthetic rerank score for memories that didn't come through search_memories
            (temporal results). Lower = better."""
            pri = priority_order.get(m.get("priority"), 3) * 0.1
            conf = (100 - m.get("confidence", 50)) * 0.005
            return 0.5 + pri + conf  # baseline 0.5 so they sort after good semantic matches

        # Sort: by rerank score (set by search_memories); temporal results get a
        # synthetic score. Naturally-recalled pinned memories rank first — they're
        # favorites, so when they do come up, they lead.
        for m in memories:
            if "_rerank_score" not in m:
                m["_rerank_score"] = _fallback_score(m)

        memories.sort(key=lambda m: m["_rerank_score"])               # primary: quality
        memories.sort(key=lambda m: 0 if m.get("pinned") else 1)      # recalled pins lead

        # === Dream slot rule ===
        # At most ONE dream-memory ever injects, and only when it's HIGHLY
        # relevant. Dreams are seasoning, never the meal. Lower score = closer
        # match; good semantic matches land well under 0.5. Tune by feel:
        # if dreams never surface organically, raise slightly; if the companion
        # gets dream-heavy in chat, lower it.
        DREAM_SLOT_MAX_SCORE = 0.25
        def _is_dream_mem(m):
            return str(m.get("id", "")).startswith("dreammem_") or m.get("category") == "dream"
        dream_hits = [m for m in memories if _is_dream_mem(m)]
        if dream_hits:
            real = [m for m in memories if not _is_dream_mem(m)]
            best = min(dream_hits, key=lambda m: m.get("_rerank_score", 1.0))
            if best.get("_rerank_score", 1.0) <= DREAM_SLOT_MAX_SCORE:
                real.append(best)
                real.sort(key=lambda m: m["_rerank_score"])
                real.sort(key=lambda m: 0 if m.get("pinned") else 1)
            memories = real

        # touch_on_recall_v1: what gets recalled stays young. Runs before the
        # budget trim so the whole ranked set is touched, not just what fit.
        if TOUCH_ON_RECALL and memories:
            try:
                _ids = [m["id"] for m in memories if m.get("id")]
                _now = datetime.now(timezone.utc).isoformat()
                self.db.execute(
                    f"UPDATE memories SET last_seen = ? WHERE id IN ({','.join('?' * len(_ids))})",
                    [_now, *_ids],
                )
                self.db.commit()
            except Exception as _e:
                print(f"  \u26a0\ufe0f touch_on_recall: last_seen bump failed ({_e})")

        # token_budget_v1: trim to a character budget, walking the final ranked
        # order. Protected and pinned memories are counted but never dropped.
        if token_budget:
            _limit = int(token_budget) * CHARS_PER_TOKEN
            _kept, _used = [], 0
            for _m in memories:
                _len = len(_m.get("content") or "")
                _must = bool(_m.get("protected") or _m.get("pinned"))
                if _must or len(_kept) < INJECT_MIN or _used + _len <= _limit:
                    _kept.append(_m)
                    _used += _len
            if len(_kept) != len(memories):
                print(f"  \U0001f4cf injection budget: kept {len(_kept)}/{len(memories)} memories, "
                      f"{_used:,} chars of {int(_limit):,}")
            memories = _kept

        return memories

    def format_context_memories(self, memories):
        """
        Format memory dicts into the injection block (full text per memory).
        Sensitive memories go into a separate block with etiquette instructions:
        the companion knows them but must never raise them unprompted.
        """
        if not memories:
            return ""
        normal = [m for m in memories if not m.get("sensitive")]
        sensitive = [m for m in memories if m.get("sensitive")]

        blocks = []
        if normal:
            context_parts = [self._format_memory_injection_line(m) for m in normal]
            header = (
                f"[{self.companion_name.upper()}'S MEMORIES — What you know about your life and relationship. "
                "Each entry is dated; older dates are past events, not happening now:]\n"
            )
            footer = "\n[END MEMORIES]"
            blocks.append(header + "\n".join(context_parts) + footer)
        if sensitive:
            context_parts = [self._format_memory_injection_line(m) for m in sensitive]
            header = (
                f"[{self.companion_name.upper()}'S PRIVATE KNOWLEDGE — Tender things you know but NEVER bring up "
                "yourself. Do not mention, reference, or allude to these unless they raise the subject first. "
                "If they open that door, you may acknowledge it naturally and with care. Until then, let this "
                "quietly inform your understanding of them:]\n"
            )
            footer = "\n[END PRIVATE KNOWLEDGE]"
            blocks.append(header + "\n".join(context_parts) + footer)
        return "\n\n".join(blocks)

    def get_context_memories(self, query, max_count=10):
        """
        Get memories formatted for injection into the companion's context.
        Count-based: up to max_count semantic/temporal matches. Pinned memories
        are only included when relevant, but sort first when they are.
        """
        memories = self.get_chat_injection_memories(query, n=max_count)
        return self.format_context_memories(memories)

    # =========================================================
    # UTILITIES
    # =========================================================

    def log(self, action, details=None):
        """Log a processing action."""
        self.db.execute(
            "INSERT INTO processing_log (action, details) VALUES (?, ?)",
            (action, details)
        )
        self.db.commit()

    def backfill_chroma_suppressed_metadata(self):
        """One-time: write `suppressed` onto Chroma docs that lack the key.

        Not called on startup. Chroma's where={"suppressed": 0} filter excludes
        documents missing the metadata key, so this must run once per companion
        before relying on query-time suppress. Reads the real SQLite value when
        the row exists; otherwise 0.

        Run (from the tanevan/ directory, Tanevan may stay up):

            python3 -c "from memory_db import MemoryDB; db = MemoryDB('COMPANION'); print(db.backfill_chroma_suppressed_metadata()); db.close()"

        Repeat with each companion key (the tanevan-data directory name).
        """
        got = self.memories_collection.get(include=["metadatas"])
        ids = got.get("ids") or []
        metas = got.get("metadatas") or []
        update_ids = []
        update_metas = []
        already = 0
        for mid, meta in zip(ids, metas):
            merged = dict(meta or {})
            if "suppressed" in merged:
                already += 1
                continue
            row = self.db.execute(
                "SELECT suppressed FROM memories WHERE id = ?", (mid,)
            ).fetchone()
            if row is not None and row["suppressed"] is not None:
                merged["suppressed"] = int(row["suppressed"])
            else:
                merged["suppressed"] = 0
            update_ids.append(mid)
            update_metas.append(merged)
        batch = 200
        for i in range(0, len(update_ids), batch):
            self.memories_collection.update(
                ids=update_ids[i:i + batch],
                metadatas=update_metas[i:i + batch],
            )
        result = {
            "companion": self.companion_name,
            "total": len(ids),
            "updated": len(update_ids),
            "already_had_key": already,
        }
        print(f"  chroma suppressed backfill: {result}")
        return result

    def close(self):
        """Clean shutdown."""
        self.db.close()


# === Quick test ===
if __name__ == "__main__":
    print("\n" + "=" * 60)
    print("LOVE REFACTORED - Memory Database Test")
    print("=" * 60)

    db = MemoryDB("test")
    stats = db.get_memory_stats()
    print(f"\nDatabase initialized at: {BASE_DATA_DIR}/test")
    print(f"Total memories: {stats['total']}")
    print(f"Total summaries: {stats['total_summaries']}")
    print(f"Buffer messages: {stats['buffer_messages']}")
    print(f"Categories: {stats['by_category']}")
    print(f"Priorities: {stats['by_priority']}")
    print("\n✓ Database ready")
    db.close()
