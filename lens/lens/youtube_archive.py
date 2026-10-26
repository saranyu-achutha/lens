"""YouTube archive: uploads of the tracked channels for a past period, kept OUTSIDE the main database.

Files go to data/youtube_archive/<name>/ (default name = the year of --from):
  channels/<channel_id>.json   uploads found per channel (written as each channel finishes; a rerun skips them)
  videos.jsonl.gz              one JSON object per video with views, likes, comments, length, type, tags
  videos.csv                   the same as a spreadsheet
  stats.json                   totals by label, month and video type, approximate song counts, top videos

Channel scans and view-count reads run in parallel (quota is per call, not per second): a full year of the
76 channels costs a few thousand of the ~210,000 daily units. Nothing is uploaded to the server.

  python3 -m lens.youtube_archive collect --from 2025-01-01 --to 2025-12-31 [--workers 8]
  python3 -m lens.youtube_archive stats   --name 2025
"""
import argparse
import csv
import gzip
import json
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

from . import config, match
from .youtube import YouTube, iso_duration, parse_keys

FIELDS = ("video_id", "channel", "label", "published_at", "title", "asset_type", "duration_s", "views", "likes",
          "comments", "audio_language", "category", "tags", "song_key", "captured_at")


def folder(name):
    return config.DB_FILE.parent / "youtube_archive" / name


def scan_channel(yt, ch, start, end, out):
    """All uploads of one channel published in [start, end] (dates). Saved to out/channels/<id>.json."""
    path = out / "channels" / f"{ch['id']}.json"
    if path.exists():
        return json.loads(path.read_text()), 0
    token, pages, found = None, 0, []
    while True:
        page = yt.uploads(ch["id"], token)
        pages += 1
        all_old = True
        for it in page.get("items", []):
            pub = it["contentDetails"].get("videoPublishedAt")
            if not pub:  # private or deleted
                continue
            if pub[:10] >= start:
                all_old = False
            if start <= pub[:10] <= end:
                sn = it["snippet"]
                found.append({"video_id": it["contentDetails"]["videoId"], "channel": ch["title"], "label": ch.get("label"),
                              "published_at": pub, "title": sn["title"], "description": (sn.get("description") or "")[:1500]})
        token = page.get("nextPageToken")
        if not token or all_old:  # newest first: a page entirely before the period ends the scan
            break
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(found, ensure_ascii=False))
    return found, pages


def collect(start, end, name=None, workers=8, log=print):
    cfg = config.load()
    keys = parse_keys(config.read_secrets().get("YOUTUBE_API_KEY"))
    yt = YouTube(keys, log=log)
    channels = config.active_channels(cfg)
    out = folder(name or start[:4])
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    log(f"YouTube archive {start} .. {end}: {len(channels)} channels, {workers} in parallel, {len(keys)} keys -> {out}")

    videos, pages = [], 0
    with ThreadPoolExecutor(workers) as pool:
        futs = {pool.submit(scan_channel, yt, ch, start, end, out): ch for ch in channels}
        for n, f in enumerate(as_completed(futs), 1):
            found, p = f.result()
            videos += found
            pages += p
            log(f"  [{n}/{len(channels)}] {futs[f]['title']}: {len(found)} uploads" + (f" ({p} pages)" if p else " (saved earlier)"))
    log(f"scan done: {len(videos)} uploads, {pages} pages, {time.time() - t0:.0f}s")

    by_id = {v["video_id"]: v for v in videos}
    ids = list(by_id)
    batches = [ids[i:i + 50] for i in range(0, len(ids), 50)]
    captured = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with ThreadPoolExecutor(workers) as pool:
        for k, items in enumerate(pool.map(yt.videos, batches), 1):
            for it in items:
                v = by_id.get(it["id"])
                if not v:
                    continue
                st, sn = it.get("statistics", {}), it.get("snippet") or {}
                dur = iso_duration(it.get("contentDetails", {}).get("duration"))
                name_, quals = match.extract_song(v["title"])
                v.update(duration_s=dur, asset_type=match.asset_type(v["title"], dur),
                         views=int(st["viewCount"]) if "viewCount" in st else None,
                         likes=int(st["likeCount"]) if "likeCount" in st else None,
                         comments=int(st["commentCount"]) if "commentCount" in st else None,
                         audio_language=sn.get("defaultAudioLanguage") or sn.get("defaultLanguage"),
                         category=sn.get("categoryId"), tags=sn.get("tags") or [],
                         song_key=match.song_key(name_, quals) if name_ else None, captured_at=captured)
            if k % 100 == 0:
                log(f"  counts read for {min(k * 50, len(ids))}/{len(ids)} videos")
    log(f"counts read: {len(ids)} videos, quota used this run {yt.units} units, {time.time() - t0:.0f}s in all")

    rows = sorted(by_id.values(), key=lambda v: v["published_at"])
    with gzip.open(out / "videos.jsonl.gz", "wt", encoding="utf-8") as f:
        for v in rows:
            f.write(json.dumps(v, ensure_ascii=False) + "\n")
    with (out / "videos.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        for v in rows:
            w.writerow([("|".join(v.get(k) or []) if k == "tags" else v.get(k)) for k in FIELDS])
    s = stats(rows, start, end)
    s.update(quota_units=yt.units, scan_pages=pages, seconds=round(time.time() - t0), collected_at=captured)
    (out / "stats.json").write_text(json.dumps(s, indent=1, ensure_ascii=False))
    log(f"saved {len(rows)} videos -> {out}/videos.jsonl.gz, videos.csv, stats.json")
    return s


SONG_TYPES = ("video", "lyric", "audio", "other")


def stats(rows, start, end):
    """Totals by label, month and type. Songs are approximate: distinct song names (same rules as the main
    pipeline's grouping) per label among video/lyric/audio uploads; the main pipeline also merges variants."""
    lab = defaultdict(lambda: {"uploads": 0, "views": 0, "likes": 0, "comments": 0, "songs": set()})
    month = defaultdict(lambda: {"uploads": 0, "views": 0, "songs": set()})
    types = Counter()
    for v in rows:
        L, m = lab[v.get("label") or "?"], month[v["published_at"][:7]]
        L["uploads"] += 1; m["uploads"] += 1
        for k in ("views", "likes", "comments"):
            L[k] += v.get(k) or 0
        m["views"] += v.get("views") or 0
        types[v.get("asset_type") or "?"] += 1
        if v.get("asset_type") in SONG_TYPES and v.get("song_key"):
            L["songs"].add(v["song_key"]); m["songs"].add((v.get("label"), v["song_key"]))
    fin = lambda d: {k: (len(x) if isinstance(x, set) else x) for k, x in d.items()}
    top = sorted(rows, key=lambda v: -(v.get("views") or 0))[:25]
    return {"from": start, "to": end, "uploads": len(rows), "views": sum(v.get("views") or 0 for v in rows),
            "songs_approx": sum(len(L["songs"]) for L in lab.values()),
            "by_label": {k: fin(v) for k, v in sorted(lab.items(), key=lambda x: -x[1]["views"])},
            "by_month": {k: fin(v) for k, v in sorted(month.items())},
            "by_type": dict(types.most_common()),
            "top_videos": [{k: v.get(k) for k in ("title", "channel", "label", "published_at", "asset_type", "views")} for v in top]}


def main(argv=None):
    ap = argparse.ArgumentParser(prog="lens.youtube_archive")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("collect")
    c.add_argument("--from", dest="start", required=True)
    c.add_argument("--to", dest="end", required=True)
    c.add_argument("--name")
    c.add_argument("--workers", type=int, default=8)
    s = sub.add_parser("stats")
    s.add_argument("--name", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "collect":
        collect(a.start, a.end, a.name, a.workers)
    else:
        out = folder(a.name)
        with gzip.open(out / "videos.jsonl.gz", "rt", encoding="utf-8") as f:
            rows = [json.loads(line) for line in f]
        old = json.loads((out / "stats.json").read_text()) if (out / "stats.json").exists() else {}
        s = stats(rows, old.get("from", ""), old.get("to", ""))
        print(json.dumps({k: s[k] for k in ("uploads", "views", "songs_approx", "by_type")}, indent=1))


if __name__ == "__main__":
    main()
