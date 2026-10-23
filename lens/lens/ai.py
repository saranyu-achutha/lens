"""AI layer over the rule-based insights: Claude explains computed numbers and answers questions with tools.

Claude never invents data: every number comes from lens/insights.py or a read-only query, and the prompts
require answers to cite them. Needs the `anthropic` package and an API key (Sources page, stored in .env as
ANTHROPIC_API_KEY; an ANTHROPIC_API_KEY environment variable or `ant auth login` profile also works).
"""
import hashlib
import json
import re
import sqlite3
import statistics
from collections import defaultdict

from . import config, db, insights, metadata

MODEL = "claude-opus-5-5"
# Server-side refusal fallback: if a request is declined by a safety classifier, the API re-runs it on a
# suitable model in the same call (opt-in; see the Claude API docs on refusals and fallbacks).
FALLBACK = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}

SYSTEM = """You are Market Lens AI, a music-market analyst for Indian record labels (Sony Music India, T-Series,
Zee Music Company, Saregama, Aditya Music, Think Music). The data is public YouTube data from the labels'
official channels (views, likes, comments, upload dates, video titles and credits), Spotify / Apple Music /
MusicBrainz metadata, and rule-based metrics computed from it.

Rules:
- Use only numbers that appear in the data or tool results you are given. Never invent figures, songs or causes.
- Public data shows WHAT changed, not WHY. Offer possible reasons only as hypotheses and say so.
- Compare fairly: prefer age-adjusted measures (percentile within the same age band, growth relative to size)
  over raw totals, because older uploads and bigger labels naturally have more views.
- Say plainly when history is too short for a conclusion (growth and acceleration need several daily readings).
- No revenue, earnings, watch time or audience demographics are available; don't estimate them.
- Write for label executives: short, specific, numbers with thousands separators, no jargon.
- Refer to a song as [Title](#song:ID) using its id, so the app can link it.

Metric glossary: velocity = views per day since the song's first YouTube upload (all its versions);
band_percentile = share of songs of the same age band that it beats on velocity; recent_rate = views per day
between its latest readings; growth_pct = recent_rate as % of its total views; acceleration = recent_rate /
the rate before; likes_per_1k = likes per 1,000 views; index = velocity / median of its age band;
score = breakout score 0-100 (percentiles of speed, growth and acceleration)."""

PANEL_TASK = {
    "emerging": "These are the Emerging Hits: songs not yet in the top 5% by views, ranked by breakout score.",
    "cross_label": "This is Cross-Label Intelligence: labels, languages and label x language groups compared by "
                   "age-adjusted percentile (not totals).",
    "catalog": "These are Catalog Opportunities: songs first released a year or more before their current YouTube "
               "upload that are drawing above-typical views.",
    "artists": "This is Artist Portfolio Health: per artist, breadth of performance vs dependence on one song.",
    "anomalies": "These are Anomalies: songs whose latest growth rate departs sharply from their previous rate.",
}


class AIUnavailable(ValueError):  # shown to the user as a 400 with this message
    pass


def _client():
    try:
        import anthropic
    except ImportError:
        raise AIUnavailable("The anthropic package is not installed (pip install anthropic).")
    key = config.read_secrets().get("ANTHROPIC_API_KEY")
    return anthropic, (anthropic.Anthropic(api_key=key) if key else anthropic.Anthropic())


def status():
    try:
        import anthropic  # noqa: F401
        sdk = True
    except ImportError:
        sdk = False
    import os
    key = bool(config.read_secrets().get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"))
    return {"sdk": sdk, "key": key, "model": MODEL, "ready": sdk and key}


def _call(**kw):
    """One Messages API call with the refusal fallback; maps SDK errors to readable messages."""
    anthropic, client = _client()
    try:
        resp = client.beta.messages.create(model=MODEL, system=[{"type": "text", "text": SYSTEM,
                                                                "cache_control": {"type": "ephemeral"}}],
                                           **FALLBACK, **kw)
    except anthropic.AuthenticationError:
        raise AIUnavailable("The LLM key is missing or invalid (Sources page).")
    except anthropic.RateLimitError:
        raise AIUnavailable("LLM rate limit reached; try again in a minute.")
    except anthropic.APIStatusError as e:
        raise AIUnavailable(f"LLM API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        raise AIUnavailable("Could not reach the LLM API (network).")
    if resp.stop_reason == "refusal":
        raise AIUnavailable("The LLM declined this request.")
    return resp


def _text(resp):
    return "\n".join(b.text for b in resp.content if b.type == "text").strip()


def explain(conn, panel_name, field=None, refresh=False):
    """Claude's reading of one insight panel. Cached per panel, data and day, so it costs one call a day."""
    if not status()["ready"]:
        _client()  # raises a clear error if the SDK is missing
        raise AIUnavailable("Add an LLM key on the Sources page to use AI explanations.")
    data = insights.panel(conn, panel_name, field=field)
    rows = data.get("rows")
    payload = {"panel": panel_name, "window": [data["meta"]["since"], data["meta"]["until"]],
               "history": data["meta"]["history_note"], "songs_measured": data["meta"]["songs"]}
    if rows is not None:
        payload["rows"] = insights.compact(rows[:20]) if panel_name != "artists" else rows[:20]
    else:
        payload.update({k: v[:20] for k, v in data.items() if k != "meta"})
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    key = f"{panel_name}:{field or ''}:{db.now_iso()[:10]}:{hashlib.sha1(body.encode()).hexdigest()[:12]}"
    if not refresh:
        hit = conn.execute("SELECT text, created_at FROM ai_note WHERE key=?", (key,)).fetchone()
        if hit:
            return {"text": hit[0], "created_at": hit[1], "cached": True}
    resp = _call(max_tokens=4000, output_config={"effort": "low"}, messages=[{"role": "user", "content": (
        f"{PANEL_TASK[panel_name]}\n\nData (JSON):\n{body}\n\nWrite: **Key findings** (3-5 bullets, each with the "
        "numbers behind it and links to songs), **Watch out** (1-2 bullets on data limits that matter here), "
        "**Suggested actions** (2-3 bullets for a label team). Under 250 words.")}])
    text = _text(resp)
    conn.execute("INSERT OR REPLACE INTO ai_note (key, created_at, panel, model, text) VALUES (?,?,?,?,?)",
                 (key, db.now_iso(), panel_name, resp.model, text))
    conn.commit()
    return {"text": text, "created_at": db.now_iso(), "cached": False}


# ---------- Ask Music AI: tools over the data ----------

SORTS = ["views", "velocity", "band_percentile", "recent_rate", "growth_pct", "acceleration", "likes_per_1k", "age_days"]
GROUP_FIELDS = ["label", "language", "category", "singer", "music_director", "film", "channel", "genre"]
SQL_SCHEMA = """Read-only SQLite. Main tables:
track(id, title, yt_title, isrc, first_published, release_status, release_id)  -- one row per song
song_meta(track_id, field, value, value_key, source)  -- field in: singer, music_director, lyricist, cast, film,
  album, language, category, occasion, label, channel, genre, director, producer, spotify_artist, apple_artist
platform_item(id, platform, platform_id, track_id, title, url, channel, asset_type, published_at, review_state)
  -- platform: youtube | spotify | apple | musicbrainz | deezer; asset_type: video | lyric | audio | short | teaser
snapshot(platform_item_id, metric, value, capture_date)  -- metric: views | likes | comments | rank; daily
release(id, title, release_date, album_type, label)  -- Spotify release of a song
Linked YouTube videos have review_state IN ('auto','approved','song')."""

TOOLS = [
    {"name": "find_songs",
     "description": "Find songs in the current window with their metrics, filtered and sorted. Returns at most 50.",
     "input_schema": {"type": "object", "additionalProperties": False, "properties": {
         "label": {"type": "string"}, "language": {"type": "string"}, "category": {"type": "string"},
         "artist": {"type": "string", "description": "singer or music director name (substring)"},
         "film": {"type": "string"}, "min_views": {"type": "integer"}, "max_age_days": {"type": "integer"},
         "exclude_top_hits": {"type": "boolean", "description": "skip songs in the top 5% by views"},
         "sort_by": {"type": "string", "enum": SORTS}, "limit": {"type": "integer"}}}},
    {"name": "insight_panel",
     "description": "Rule-based insight panels: emerging (breakout candidates), cross_label (labels/languages compared "
                    "fairly), catalog (older songs with renewed demand), artists (portfolio health; field singer or "
                    "music_director), anomalies (sudden changes).",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["panel"], "properties": {
         "panel": {"type": "string", "enum": list(insights.PANELS)},
         "field": {"type": "string", "enum": ["singer", "music_director"]}}}},
    {"name": "group_metrics",
     "description": "Aggregate songs by a metadata field (e.g. label, language, singer, film): songs, total views, "
                    "median age-adjusted percentile, median likes per 1k, top song. Optional filters.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["group_by"], "properties": {
         "group_by": {"type": "string", "enum": GROUP_FIELDS}, "label": {"type": "string"},
         "language": {"type": "string"}, "category": {"type": "string"}, "min_songs": {"type": "integer"},
         "limit": {"type": "integer"}}}},
    {"name": "song_details",
     "description": "Everything about one song: metrics, how it compares with similar songs, all metadata.",
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["song_id"],
                      "properties": {"song_id": {"type": "integer"}}}},
    {"name": "sql_query",
     "description": "Run one read-only SQL SELECT for questions the other tools can't answer (max 200 rows).\n" + SQL_SCHEMA,
     "input_schema": {"type": "object", "additionalProperties": False, "required": ["sql"],
                      "properties": {"sql": {"type": "string"}}}},
]


def _like(a, b):
    return b.lower() in (a or "").lower()


def _run_tool(conn, name, args, facts, cache):
    if name == "find_songs":
        rows = facts
        for k in ("label", "language", "category", "film"):
            if args.get(k):
                rows = [r for r in rows if _like(r[k], args[k])]
        if args.get("artist"):
            rows = [r for r in rows if _like(r["singer"], args["artist"]) or _like(r["music_director"], args["artist"])]
        if args.get("min_views"):
            rows = [r for r in rows if r["views"] >= args["min_views"]]
        if args.get("max_age_days"):
            rows = [r for r in rows if r["age_days"] <= args["max_age_days"]]
        if args.get("exclude_top_hits"):
            rows = [r for r in rows if not r["already_hit"]]
        key = args.get("sort_by") if args.get("sort_by") in SORTS else "views"
        rows = sorted(rows, key=lambda r: (r.get(key) is None, -(r.get(key) or 0)))
        return {"matches": len(rows), "songs": insights.compact(rows[:min(int(args.get("limit") or 20), 50)])}
    if name == "insight_panel":
        data = insights.panel(conn, args["panel"], field=args.get("field"))
        out = {"history": data["meta"]["history_note"]}
        if "rows" in data:
            out["rows"] = insights.compact(data["rows"][:20]) if args["panel"] != "artists" else data["rows"][:20]
        else:
            out.update({k: v[:20] for k, v in data.items() if k != "meta"})
        return out
    if name == "group_metrics":
        rows = facts
        for k in ("label", "language", "category"):
            if args.get(k):
                rows = [r for r in rows if _like(r[k], args[k])]
        groups = defaultdict(list)
        for r in rows:
            for v in [x.strip() for x in (r.get(args["group_by"]) or "").split(",") if x.strip()]:
                groups[v].append(r)
        out = []
        for v, rs in groups.items():
            if len(rs) < int(args.get("min_songs") or 1):
                continue
            eng = [x["likes_per_1k"] for x in rs if x["likes_per_1k"] is not None]
            top = max(rs, key=lambda x: x["views"])
            out.append({"value": v, "songs": len(rs), "total_views": sum(x["views"] for x in rs),
                        "median_percentile": round(statistics.median(x["band_percentile"] for x in rs)),
                        "median_likes_per_1k": round(statistics.median(eng), 1) if eng else None,
                        "new_releases": sum(1 for x in rs if x["release_status"] == "new"),
                        "top_song": {"id": top["id"], "title": top["title"], "views": top["views"]}})
        out.sort(key=lambda x: -x["total_views"])
        return {"groups": len(out), "rows": out[:min(int(args.get("limit") or 25), 60)]}
    if name == "song_details":
        sid = int(args["song_id"])
        b = insights.benchmark(facts, sid)
        meta = {f: [v["value"] for v in vals] for f, vals in metadata.for_song(conn, sid).items()}
        return {"song": insights.compact([b["song"]])[0] if b else None,
                "comparisons": b["comparisons"] if b else [], "metadata": meta}
    if name == "sql_query":
        return _readonly_sql(args["sql"])
    return {"error": f"unknown tool {name}"}


def _readonly_sql(sql):
    if not re.match(r"^\s*(select|with)\b", sql, re.I) or ";" in sql.strip().rstrip(";"):
        return {"error": "only a single SELECT statement is allowed"}
    ro = sqlite3.connect(f"file:{config.DB_FILE}?mode=ro", uri=True)
    ro.set_authorizer(lambda action, *a: sqlite3.SQLITE_OK if action in (
        sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE) else sqlite3.SQLITE_DENY)
    steps = {"n": 0}

    def guard():
        steps["n"] += 1
        return 1 if steps["n"] > 2000 else 0  # abort runaway queries
    ro.set_progress_handler(guard, 10000)
    try:
        cur = ro.execute(sql.strip().rstrip(";"))
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(200)
        return {"columns": cols, "rows": rows, "truncated": len(rows) == 200}
    except sqlite3.Error as e:
        return {"error": str(e)}
    finally:
        ro.close()


def ask(conn, question, history=None, max_steps=8):
    """Answer a question with tools over the data. history: [{"role": "user"|"assistant", "content": str}]."""
    if not status()["ready"]:
        raise AIUnavailable("Add an LLM key on the Sources page to use Ask Music AI.")
    facts, meta = insights.song_facts(conn)
    messages = [{"role": m["role"], "content": m["content"]} for m in (history or [])[-10:]
                if m.get("role") in ("user", "assistant") and m.get("content")]
    messages.append({"role": "user", "content": f"{question}\n\n(Data window: songs first uploaded {meta['since']} to "
                                                f"{meta['until']}; {meta['songs']} songs. {meta['history_note']})"})
    used, cache = [], {}
    for _ in range(max_steps):
        resp = _call(max_tokens=16000, output_config={"effort": "medium"}, tools=TOOLS, messages=messages)
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        if resp.stop_reason != "tool_use":
            return {"answer": _text(resp) or "(no answer)", "tools_used": used, "stop_reason": resp.stop_reason}
        messages.append({"role": "assistant", "content": resp.content})
        results = []
        for block in resp.content:
            if block.type != "tool_use":
                continue
            try:
                out = _run_tool(conn, block.name, dict(block.input or {}), facts, cache)
                results.append({"type": "tool_result", "tool_use_id": block.id,
                                "content": json.dumps(out, ensure_ascii=False, default=str)[:60000]})
            except Exception as e:  # a failing tool is reported to Claude, not raised
                results.append({"type": "tool_result", "tool_use_id": block.id, "content": f"Error: {e}", "is_error": True})
            used.append({"tool": block.name, "input": dict(block.input or {})})
        messages.append({"role": "user", "content": results})
    return {"answer": "I needed more steps than allowed for this question; try narrowing it.", "tools_used": used,
            "stop_reason": "max_steps"}
