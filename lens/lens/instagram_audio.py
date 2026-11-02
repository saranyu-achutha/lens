"""Instagram Reels made with each song's official audio: the public audio page (instagram.com/reels/audio/<id>/),
which any logged-out visitor sees with its "80K reels" count and top Reels. The page is drawn by JavaScript, so it is
rendered with the same light headless browser as the Spotify play counts (`--dump-dom`), one page at a time, slowly,
once a day per audio. No login, no key and no Instagram API.

Finding the audio: Instagram's search needs a login and search engines don't index audio pages. Audio links come from
the labels' own YouTube descriptions (description_links) or are added in the Console (Daily data → the song →
Instagram audio), as a platform_item (platform 'instagram', platform_id = the audio id). A song can have several.
Official audio only: a page counts when it is a music-library track (not "Original audio" made by an account) whose
title matches the song. Others are kept with official=0 and never counted.

Stored in shorts_use's tables with platform 'instagram': sound_use (reels count per day, title, artists) and
sound_short (each Reel seen, id 'ig:<shortcode>': views, likes, comments). song_daily sums them into ig_reels / ig_reels_gain.
If Instagram starts showing a login wall instead of the page, the run stops for the day: nothing is retried harder.
"""
import html
import json
import os
import re
import subprocess
import tempfile
import time
from datetime import date
from pathlib import Path

from . import config, db, match, shorts_use
from .shorts_use import number
from .spotify_plays import _kill_group, find_chrome

PAGE = "https://www.instagram.com/reels/audio/{}/"
PACE = 12.0  # seconds between pages


class Cancelled(Exception):
    pass


def audio_id(text):
    """The audio id from a pasted link (instagram.com/reels/audio/<id>/) or the bare number."""
    m = re.search(r"(?:reels?/audio/)?(\d{8,25})", text or "")
    return m.group(1) if m else None


def render(url, timeout=60):
    """The page's HTML after its scripts ran (logged out, fresh profile each time)."""
    chrome = find_chrome()
    profile = tempfile.mkdtemp(prefix="lens_ig_")
    env = dict(os.environ, LD_LIBRARY_PATH=str(Path(chrome).parent))
    proc = subprocess.Popen([chrome, "--headless=new", "--disable-gpu", "--no-first-run", "--mute-audio", "--lang=en-IN",
                             "--blink-settings=imagesEnabled=false", "--disable-extensions", "--disable-sync", "--no-pings",
                             "--virtual-time-budget=15000", f"--user-data-dir={profile}", "--dump-dom", url],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, start_new_session=True,
                            preexec_fn=lambda: os.nice(19))
    try:
        out, _ = proc.communicate(timeout=timeout)
        return out.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return ""
    finally:
        _kill_group(proc)
        subprocess.run(["rm", "-rf", profile])


def parse(dom):
    """Title, artists, reels count and top Reels from a rendered audio page; None when the page didn't show (login wall)."""
    text = [html.unescape(x).strip() for x in re.split(r"<[^>]+>", re.sub(r"<(script|style)\b.*?</\1>", "", dom, flags=re.S))]
    text = [x for x in text if x]
    try:
        i = text.index("Audio")
    except ValueError:
        return None
    rest = text[i + 1:i + 8]
    count_text = next((x for x in rest if re.fullmatch(r"[\d.,]+[KMB]? reels?", x, re.I)), None)
    if not count_text:
        return None
    title = rest[0]
    artists = rest[1] if rest[1] != count_text else ""
    original = bool(re.match(r"original audio", title, re.I) or re.match(r"original audio", artists, re.I))
    reels, chunks = [], re.split(r'href="/reel/([\w-]+)/?"', dom)
    for code, chunk in zip(chunks[1::2], chunks[2::2]):
        nums = [n for n in (html.unescape(x).strip() for x in re.findall(r">([^<>]{1,12})<", chunk[:6000]))
                if re.fullmatch(r"[\d.,]+[KMB]?", n)][:3]
        if code not in {r["id"] for r in reels}:
            reels.append({"id": code, "likes": number(nums[0]) if len(nums) > 0 else None,
                          "comments": number(nums[1]) if len(nums) > 1 else None, "views": number(nums[2]) if len(nums) > 2 else None})
    return {"title": title, "artists": artists, "original": original, "count_text": count_text, "count": number(count_text), "reels": reels}


def link(conn, song_id, text, who="console", method="manual"):
    """Attach an Instagram audio to a song (from the Console, or a label's own link in a YouTube description).
    Returns the platform_item id."""
    aid = audio_id(text)
    if not aid:
        raise ValueError("not an Instagram audio link (instagram.com/reels/audio/<number>/)")
    ts = db.now_iso()
    conn.execute("""INSERT INTO platform_item (platform, platform_id, title, url, track_id, review_state, match_method, match_detail, first_seen_at)
                    VALUES ('instagram', ?, NULL, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(platform, platform_id) DO UPDATE SET track_id=excluded.track_id, review_state=excluded.review_state""",
                 (aid, PAGE.format(aid), song_id, "approved" if method == "manual" else "auto", method, json.dumps({"added_by": who, "at": ts}), ts))
    conn.commit()
    return conn.execute("SELECT id FROM platform_item WHERE platform='instagram' AND platform_id=?", (aid,)).fetchone()[0]


def unlink(conn, item_id):
    conn.execute("UPDATE platform_item SET track_id=NULL, review_state='rejected' WHERE id=? AND platform='instagram'", (item_id,))
    conn.commit()


def due(conn, today):
    return conn.execute("""SELECT p.id, p.platform_id, p.track_id, coalesce(t.yt_title, t.title) FROM platform_item p JOIN track t ON t.id=p.track_id
                           WHERE p.platform='instagram' AND p.review_state IN ('auto','approved')
                             AND NOT EXISTS (SELECT 1 FROM sound_use u WHERE u.platform_item_id=p.id AND u.day=?)""", (today,)).fetchall()


def run(conn, cfg, secrets, log=print, should_stop=lambda: False, run_id=None, **_):
    shorts_use.ensure(conn)
    try:
        find_chrome()
    except SystemExit as e:
        return {"status": "ok", "stop_reason": str(e)}
    today = date.today().isoformat()
    todo = due(conn, today)
    if not todo:
        return {"status": "ok", "instagram_read": 0}
    log(f"Instagram: reading {len(todo)} audio pages (one every {PACE:.0f} s)")
    read = failed = in_row = 0
    for iid, aid, tid, song in todo:
        if should_stop():
            raise Cancelled()
        dom = render(PAGE.format(aid))
        if not dom.strip():  # the browser itself didn't run (e.g. blocked by AppArmor): nothing to read on this machine
            log("Instagram: the headless browser did not start; stopping")
            return {"status": "failed", "error": "headless browser did not start", "instagram_read": read, "instagram_failed": failed + 1}
        p = parse(dom)
        if p is None:
            failed += 1
            in_row += 1
            if in_row >= 3:  # Instagram is showing a login wall to this machine: stop for today, no harder retries
                log("Instagram: 3 pages in a row not shown (login wall); stopping until the next run")
                break
            time.sleep(PACE)
            continue
        in_row = 0
        core = match.parse_spotify_title(song)[0]
        official = not p["original"] and match.title_score(core, match.parse_spotify_title(p["title"])[0]) >= 0.85
        ts = db.now_iso()
        conn.execute("UPDATE platform_item SET title=? WHERE id=?", (f"{p['title']} · {p['artists']}"[:300], iid))
        conn.execute("""INSERT OR REPLACE INTO sound_use (platform_item_id, day, platform, song_id, shorts, official, count_text, listed,
                            listed_views, sound_title, sound_owner, owner_channel, captured_at) VALUES (?, ?, 'instagram', ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)""",
                     (iid, today, tid, p["count"] or 0, int(official), p["count_text"], len(p["reels"]),
                      sum(r["views"] or 0 for r in p["reels"]), p["title"], p["artists"], ts))
        if official:
            for r in p["reels"]:
                conn.execute("""INSERT INTO sound_short (video_id, platform_item_id, song_id, platform, views, likes, comments, first_seen, last_seen)
                                VALUES (?,?,?,'instagram',?,?,?,?,?) ON CONFLICT(video_id) DO UPDATE SET views=excluded.views, likes=excluded.likes,
                                comments=excluded.comments, last_seen=excluded.last_seen""",
                             ("ig:" + r["id"], iid, tid, r["views"], r["likes"], r["comments"], today, today))
        conn.commit()
        read += 1
        log(f"  {song}: {p['count_text']}{'' if official else ' (not the official audio: not counted)'}")
        time.sleep(PACE)
    return {"status": "ok", "instagram_read": read, "instagram_failed": failed}
