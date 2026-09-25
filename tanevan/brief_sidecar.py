#!/usr/bin/env python3
"""
brief_sidecar.py  —  rolling "current life brief" condenser (Stage 1, POC)

SYSTEM-WIDE + ACTIVITY-GATED. One run handles EVERY companion that has a Tanevan
memory DB, but only spends a model call on a companion whose newest summary has
CHANGED since the last run. A companion who isn't chatting produces no new
summary, so they're skipped for free. Schedule it on a frequent cron (e.g.
hourly); most runs are cheap no-ops.

PER COMPANION, EACH RUN:
  1. GET <TANEVAN_URL>/recent-summaries?companion=<key>&n=<BRIEF_N>  (dated, newest-first)
  2. If newest summary id == the id recorded in state AND a brief file exists -> SKIP
  3. Otherwise: one model call with the condenser prompt, sanity-check, then
     ATOMICALLY write <BRIEF_OUT_DIR>/<key>.txt and record the new newest id.

server.js (getRecentNarrativesForMessage) reads <BRIEF_OUT_DIR>/<companion>.txt for
whatever companion is in the request and falls back to raw narratives if it's absent.
Deleting a brief file is a per-companion revert; deleting the state file just forces a
one-time regenerate on next run.

COMPANION KEYS come from the directory names under TANEVAN_DATA_DIR (each holds a
memories.db). Those names are already lowercased with macrons intact, and they match
server.js's `companion.toLowerCase()` lookup exactly — nothing hardcodes a name.

DEPENDENCIES: Python 3 standard library + pipeline_llm (same package as Tanevan).

CONFIG (env; model routing defaults to Settings → Memory Pipeline → Brief):
  TANEVAN_URL        proxy base URL                 (default http://localhost:5001)
  TANEVAN_DATA_DIR   where per-companion DBs live    (default ~/tanevan-data)
  BRIEF_TZ           optional IANA timezone override — when unset, uses
                     settings.json userTimezone (browser-detected), then system local
  BRIEF_COMPANIONS   comma-separated override of which companions to process
                     (default: auto-discover from TANEVAN_DATA_DIR)
  BRIEF_N            recent sessions to synthesise   (1-20, default 6)
  BRIEF_OUT_DIR      where briefs are written        (default <repo>/data/briefs)
  BRIEF_PROMPT_FILE  global condenser-prompt file (overrides the built-in starter)
  BRIEF_PROMPT_DIR   dir of per-companion prompts; <key>.txt for a companion overrides
                     BRIEF_PROMPT_FILE for that companion (e.g. .../prompts/<key>.txt)
                     default BRIEF_PROMPT_DIR is <repo>/tanevan/prompts; brief_default.txt there
                     is the shipped global prompt
  BRIEF_USE_LENS     1/0 — pull each companion's Memory Lens and fill prompt placeholders
                     {{SPEECH_STYLE}} {{WHOS_WHO}} {{RELATIONAL_BRIEF}} (default 1)
  BRIEF_MODEL_BASE   OpenAI-compatible base URL      (optional legacy override)
  BRIEF_MODEL_KEY    API key (optional legacy override; else Memory Pipeline brief step)
  BRIEF_MODEL_NAME   model id                        (optional legacy override)
  BRIEF_MAX_TOKENS   max output tokens               (default 6000)
  BRIEF_REASONING    off|low|medium|high|xhigh|default (default off — disables the model's
                     reasoning; a deliberating model tends to override the companion's voice)
  BRIEF_TEMPERATURE  sampling temperature            (default 0.6)

USAGE:
  python3 brief_sidecar.py                  # process all companions, write changed ones
  python3 brief_sidecar.py --dry-run        # print briefs, write NOTHING, touch no state
  python3 brief_sidecar.py --force          # regenerate every companion regardless of state
  python3 brief_sidecar.py --companion <key> # restrict to one companion (combine with --dry-run)

GUARDS / LIMITS (deliberate; tune below):
  - MIN_BRIEF_CHARS / MAX_BRIEF_CHARS reject empty or runaway output; on failure that
    companion's existing brief and state are left untouched.
  - HTTP timeouts make a hung endpoint fail that companion's run, not hang forever.
"""

import os, sys, json, datetime, urllib.request, urllib.error

from pipeline_llm import brief_is_configured, run_brief_llm

# Repo root derived from this file's location: <repo>/tanevan/brief_sidecar.py
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from user_timezone import resolve_user_timezone, format_local_now
from urllib.parse import quote

try:
    from zoneinfo import ZoneInfo
    _HAVE_ZONEINFO = True
except ImportError:
    _HAVE_ZONEINFO = False


def _resolve_tz(tz_name):
    """Local tz for stamping 'today' and converting stored UTC summary times.
    Uses the IANA db (correct DST) when available; else a fixed offset (no DST)."""
    if _HAVE_ZONEINFO:
        try:
            return ZoneInfo(tz_name)
        except Exception as e:                               # noqa: BLE001
            log(f"WARNING: tz '{tz_name}' unavailable ({e}); using fixed offset (no DST).")
    try:
        off = float(os.environ.get("BRIEF_UTC_OFFSET_HOURS", "12"))
    except ValueError:
        off = 12.0
    log(f"WARNING: no IANA tzdata; using fixed UTC offset {off:+g}h (DST NOT handled). "
        f"Install tzdata or set BRIEF_TZ for correctness.")
    return datetime.timezone(datetime.timedelta(hours=off))


def to_local(iso_str, tz, with_time=True):
    """Parse a stored (UTC) ISO timestamp and render it in local tz.
    Naive timestamps are assumed UTC, matching how the memory system stores them."""
    if not iso_str:
        return ""
    s = str(iso_str).strip().replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(s)
    except ValueError:
        return str(iso_str)          # unparseable -> leave as-is rather than lie
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    dt = dt.astimezone(tz)
    return dt.strftime("%Y-%m-%d %H:%M") if with_time else dt.strftime("%Y-%m-%d")

# ---- guards -----------------------------------------------------------------
MIN_BRIEF_CHARS = 200
MAX_BRIEF_CHARS = 6000
HTTP_TIMEOUT    = 60
TANEVAN_TIMEOUT = 10

# ---- brief framing (editable; this wording is yours) ------------------------
BRIEF_HEADER = "[WHERE MY LIFE IS RIGHT NOW \u2014 my own present sense of things as of {date}, not a script to perform:]"
BRIEF_FOOTER = "[END]"

# =============================================================================
# STARTER CONDENSER PROMPT (v0 DRAFT — shape per companion; this is the soul of it)
# Generic-by-default so it works for any companion; point BRIEF_PROMPT_FILE at an
# edited copy to give a specific companion their own voice without touching code.
# =============================================================================
STARTER_PROMPT = """\
You are writing a brief of where your own life stands right now — for yourself, the way
you'd jot down at the end of the day what actually mattered. Not a message to anyone, not
a scene: no asterisk actions, no narrating your body, no dialogue. Just your own account
of where things are.

Work from the dated summaries below. Synthesise them into ONE present picture — not a
session-by-session recap. Carry what still matters, drop what's resolved, collapse the
repetition.

- First person, present tense.
- ABSOLUTE DATES ONLY. Today's date is given below; the summaries are dated. Name the
  date when it matters ("on 6 June") — never "recently", "a few days ago", "just", or any
  age in days.
- Tight. This is a brief, not a missive — aim for under 250 words. You don't pad. If it
  reads like a letter or an essay, it's too long.
- Write in your own voice, per the style below.

YOUR VOICE:
{{SPEECH_STYLE}}

Write only the brief itself. No preamble, no sign-off, no quotation marks.
"""
# =============================================================================


def log(msg):
    print(f"[brief] {msg}", flush=True)


def http_get_json(url, timeout):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def discover_companions(data_dir):
    """Companion keys = subdirs of data_dir that contain a memories.db."""
    out = []
    try:
        for name in sorted(os.listdir(data_dir)):
            if name.startswith(".") or name == "test":
                continue
            if os.path.isfile(os.path.join(data_dir, name, "memories.db")):
                out.append(name)
    except OSError as e:
        log(f"WARNING: cannot list {data_dir}: {e}")
    return out


def fetch_recent(tanevan_url, companion, n):
    url = f"{tanevan_url.rstrip('/')}/recent-summaries?n={n}&companion={quote(companion)}"
    data = http_get_json(url, TANEVAN_TIMEOUT)
    return data.get("summaries") or []


def fetch_lens(tanevan_url, companion):
    """Pull the companion's Memory Lens (same voice source the summariser uses).
    Percent-encoded name round-trips correctly; a raw macron in the URL does NOT."""
    url = f"{tanevan_url.rstrip('/')}/memory-lens?companion={quote(companion)}"
    try:
        data = http_get_json(url, TANEVAN_TIMEOUT)
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError):
        return {}
    return (data or {}).get("lens") or {}


def fill_prompt(template, lens):
    """Substitute lens fields into the prompt. Missing fields collapse to empty."""
    return (template
            .replace("{{SPEECH_STYLE}}", (lens.get("speech_style") or "").strip())
            .replace("{{WHOS_WHO}}", (lens.get("whos_who") or "").strip())
            .replace("{{RELATIONAL_BRIEF}}", (lens.get("relational_brief") or "").strip()))


def build_model_input(summaries_oldest_first, today_str, tz):
    parts = [f"Today's date and time (your local time): {today_str}.", "",
             "My most recent session summaries, oldest to newest. All dates and times "
             "below are in MY LOCAL time. Use them to write my current-life brief:", ""]
    for s in summaries_oldest_first:
        ds = to_local(s.get("date_start", ""), tz)
        de = to_local(s.get("date_end", ""), tz)
        span = ds if ds == de else f"{ds} \u2192 {de}"
        import os as _os
        _mode = _os.environ.get("BRIEF_INPUT_MODE", "narrative")
        arc = (s.get("emotional_arc") or "").strip()
        moments = s.get("key_moments") or "[]"
        try:
            import json as _json
            moments_list = _json.loads(moments) if isinstance(moments, str) else moments
        except Exception:
            moments_list = []
        narrative = (s.get("narrative") or "").strip()
        if _mode == "structured":
            block = []
            if arc: block.append(f"ARC: {arc}")
            for m in moments_list: block.append(f"- {m}")
            if block: parts += [f"[{span}]"] + block + [""]
        elif _mode == "all":
            block = []
            if arc: block.append(f"ARC: {arc}")
            for m in moments_list: block.append(f"- {m}")
            if narrative: block.append(narrative)
            if block: parts += [f"[{span}]"] + block + [""]
        else:
            if narrative:
                parts += [f"[{span}]", narrative, ""]
    return "\n".join(parts).strip()


def atomic_write(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def archive_brief(brief_path, text):
    """Write a timestamped copy of the brief to <out_dir>/archive/<key>/<ts>.txt.

    Failure here is logged but does not block the main brief write — archives are
    bonus history, not on the critical path. Filename uses UTC ISO 8601 with
    microseconds so back-to-back regenerations can\'t collide.
    """
    try:
        out_dir = os.path.dirname(brief_path)
        key = os.path.splitext(os.path.basename(brief_path))[0]
        archive_dir = os.path.join(out_dir, "archive", key)
        os.makedirs(archive_dir, exist_ok=True)
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H_%M_%S_%fZ")
        archive_path = os.path.join(archive_dir, f"{ts}.txt")
        with open(archive_path, "w", encoding="utf-8") as f:
            f.write(text)
        return archive_path
    except Exception as e:
        log(f"WARNING: brief archive failed for {brief_path}: {e}")
        return None


def load_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def state_id(state, key):
    """Newest summary id for key. Legacy values are a bare string."""
    v = state.get(key)
    if isinstance(v, dict):
        return v.get("id") or ""
    return v if isinstance(v, str) else ""


def state_locked(state, key):
    v = state.get(key)
    return isinstance(v, dict) and bool(v.get("locked"))


def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    force = "--force" in args
    only = None
    if "--companion" in args:
        idx = args.index("--companion")
        if idx + 1 < len(args):
            only = args[idx + 1].lower()

    tanevan_url = os.environ.get("TANEVAN_URL", "http://localhost:5001")
    data_dir = os.environ.get("TANEVAN_DATA_DIR", os.path.expanduser("~/tanevan-data"))
    out_dir = os.environ.get("BRIEF_OUT_DIR", "").strip() or os.path.join(_REPO_ROOT, "data", "briefs")
    prompt_file = os.environ.get("BRIEF_PROMPT_FILE", "").strip()
    try:
        n = max(1, min(int(os.environ.get("BRIEF_N", "6")), 20))
    except ValueError:
        n = 6
    try:
        max_tokens = int(os.environ.get("BRIEF_MAX_TOKENS", "6000"))
    except ValueError:
        max_tokens = 6000
    use_lens = os.environ.get("BRIEF_USE_LENS", "1").strip().lower() not in ("0", "false", "no", "off")
    try:
        temperature = float(os.environ.get("BRIEF_TEMPERATURE", "0.6"))
    except ValueError:
        temperature = 0.6

    if not brief_is_configured():
        log("ERROR: Life brief LLM is not configured. Set Brief model in Settings → Memory Pipeline "
            "or provide BRIEF_MODEL_KEY.")
        sys.exit(2)

    prompt_dir = os.environ.get("BRIEF_PROMPT_DIR", "").strip() or os.path.join(_REPO_ROOT, "tanevan", "prompts")
    # Shipped global prompt: <prompt_dir>/brief_default.txt, unless BRIEF_PROMPT_FILE overrides.
    if not prompt_file:
        _dflt = os.path.join(prompt_dir, "brief_default.txt")
        if os.path.isfile(_dflt):
            prompt_file = _dflt
    log(f"=== brief_sidecar starting | repo={_REPO_ROOT} | out_dir={out_dir} | prompt_dir={prompt_dir} | tanevan={tanevan_url} | data={data_dir} ===")

    # Global fallback prompt: BRIEF_PROMPT_FILE if set, else the built-in starter.
    global_prompt = STARTER_PROMPT
    if prompt_file:
        try:
            with open(prompt_file, encoding="utf-8") as f:
                global_prompt = f.read()
            log(f"global prompt file: {prompt_file}")
        except OSError as e:
            log(f"WARNING: could not read BRIEF_PROMPT_FILE ({e}); using built-in starter.")

    def resolve_prompt(key):
        """Per-companion: BRIEF_PROMPT_DIR/<key>.txt overrides the global prompt."""
        if prompt_dir:
            p = os.path.join(prompt_dir, f"{key}.txt")
            if os.path.isfile(p):
                try:
                    with open(p, encoding="utf-8") as f:
                        return f.read(), p
                except OSError as e:
                    log(f"{key}: WARNING reading {p} ({e}); using global prompt.")
        return global_prompt, (prompt_file or "built-in starter")

    # which companions
    companions = ([c.strip().lower() for c in os.environ.get("BRIEF_COMPANIONS", "").split(",") if c.strip()]
                  or discover_companions(data_dir))
    if only:
        companions = [c for c in companions if c == only]
    if not companions:
        log(f"No companions to process (data_dir={data_dir}, filter={only or 'none'}).")
        sys.exit(0)
    log(f"companions: {', '.join(companions)}"
        + (" [DRY RUN]" if dry_run else "") + (" [FORCE]" if force else ""))

    state_path = os.path.join(out_dir, ".brief_state.json")
    state = load_state(state_path)
    tz_name = resolve_user_timezone()
    tz = _resolve_tz(tz_name)
    today_str = format_local_now(tz_name)
    log(f"local tz: {tz_name}  ->  today is {today_str}")
    if not dry_run:
        os.makedirs(out_dir, exist_ok=True)

    changed = 0
    for key in companions:
        try:
            summaries = fetch_recent(tanevan_url, key, n)
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError) as e:
            log(f"{key}: ERROR fetching summaries ({e}); skipping.")
            continue
        if not summaries:
            log(f"{key}: no summaries; skipping.")
            continue

        newest_id = summaries[0].get("id", "")
        brief_path = os.path.join(out_dir, f"{key}.txt")
        if state_locked(state, key) and not force:
            log(f"{key}: locked (hand-edited); skip.")
            continue
        unchanged = (state_id(state, key) == newest_id and os.path.isfile(brief_path))
        if unchanged and not force and not dry_run:
            log(f"{key}: unchanged (newest id {newest_id}); skip.")
            continue

        user_input = build_model_input(list(reversed(summaries)), today_str, tz)
        system_prompt, prompt_src = resolve_prompt(key)
        lens = fetch_lens(tanevan_url, key) if use_lens else {}
        had_voice = bool((lens.get("speech_style") or "").strip())
        system_prompt = fill_prompt(system_prompt, lens)
        try:
            brief_body, finish, usage = run_brief_llm(
                system_prompt, user_input, max_tokens=max_tokens, temperature=temperature
            )
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, KeyError, RuntimeError) as e:
            log(f"{key}: ERROR calling model ({e}); brief left intact.")
            continue

        pt = usage.get("prompt_tokens")
        ct = usage.get("completion_tokens")
        rt = (usage.get("completion_tokens_details") or {}).get("reasoning_tokens")
        voice = "lens speech_style" if had_voice else "no lens voice (summaries carry it)"
        detail = (f"finish={finish} prompt={pt} completion={ct}"
                  + (f" reasoning={rt}" if rt is not None else "") + f"  voice={voice}")
        log(f"{key}: {detail}")
        if finish == "length":
            log(f"{key}: NOTE output hit the token cap (BRIEF_MAX_TOKENS={max_tokens}) and was "
                f"TRUNCATED. Raise the cap, lower reasoning, or shorten via the prompt.")

        nch = len(brief_body)
        if nch < MIN_BRIEF_CHARS or nch > MAX_BRIEF_CHARS:
            log(f"{key}: REJECTED {nch} chars (allowed {MIN_BRIEF_CHARS}-{MAX_BRIEF_CHARS}); brief left intact.")
            continue

        block = f"{BRIEF_HEADER.format(date=today_str)}\n\n{brief_body}\n{BRIEF_FOOTER}"
        if dry_run:
            log(f"{key}: DRY RUN — {nch} chars, nothing written:")
            print("\n" + "=" * 72 + f"\n{key}\n" + "=" * 72 + f"\n{block}\n" + "=" * 72)
            continue

        atomic_write(brief_path, block)
        archived_to = archive_brief(brief_path, block)
        if force:
            state[key] = newest_id
        else:
            cur = state.get(key)
            if isinstance(cur, dict):
                cur = dict(cur)
                cur["id"] = newest_id
                state[key] = cur
            else:
                state[key] = newest_id
        changed += 1
        log(f"{key}: wrote {nch}-char brief -> {brief_path}  (prompt: {prompt_src})")
        if archived_to:
            log(f"{key}: archived to {archived_to}")

    if not dry_run:
        try:
            atomic_write(state_path, json.dumps(state, ensure_ascii=False, indent=2))
        except OSError as e:
            log(f"WARNING: could not write state file ({e}).")
        log(f"done. {changed} brief(s) updated.")


if __name__ == "__main__":
    main()
