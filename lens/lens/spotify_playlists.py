"""Spotify IDs from Spotify's own editorial playlists (new-release and hit lists per language) and from film albums,
read through the public embed pages: one request per playlist or album lists its tracks (up to 100) with ID, title,
artists and duration. No API, no quota, no browser.
Albums: when a song of a film is already linked to a Spotify album (the soundtrack), that album page lists the
film's other songs, so songs of the same film that are still unlinked can be matched from it. Each run, every playlist track is compared with the songs that still have no Spotify link,
using the usual rules (lens.match, automatic matches only); a match is linked and its release date and duration
are read from the track's embed page. Playlist IDs: songs.spotify_playlists in config.json (default: the list below,
Spotify-owned 37i9dQZF1D... playlists found on 2026-10-30).
"""
import json
import re
import time
import urllib.error
import urllib.request

from . import match
from .spotify_links import BROWSER_UA, attach, embed_details

PLAYLISTS = {
    "37i9dQZF1DX4ghkRUdIogy": "New Music Friday India",
    "37i9dQZF1DXaRf94BiB7fV": "Fresh Finds India",
    "37i9dQZF1DX0XUfTFmNBRM": "Hot Hits Hindi",
    "37i9dQZF1DX6XE7HRLM75P": "Hot Hits Telugu",
    "37i9dQZF1DWXVJK4aT7pmk": "Hot Hits Punjabi",
    "37i9dQZF1DX1ahAlaaz0ZE": "Hot Hits Kannada ",
    "37i9dQZF1DWZqTcNLmb3sH": "Latest Kannada",
    "37i9dQZF1DX688wU47emR9": "Hot Hits Malayalam",
    "37i9dQZF1DWSxEc2RjBCQv": "Latest Malayalam",
    "37i9dQZF1DX84EApEEEkUc": "Hot Hits Marathi",
    "37i9dQZF1DX794NyvTu6nv": "Hot Hits Bangladesh",
    "37i9dQZF1DWUu7pG6LwPJc": "Bangla Pop",
    "37i9dQZF1DWXtlo6ENS92N": "Bollywood Central",
    "37i9dQZF1DX8xfQRRX1PDm": "Bollywood Dance Music",
    "37i9dQZF1DX5cZuAHLNjGz": "Punjabi 101",
    "37i9dQZF1DX0TyiNWW7uUQ": "Kollywood Cream",
    "37i9dQZF1DWZWNsJrgFkZK": "Hot Hits Haryanvi",
    "37i9dQZF1DX5TbkaD997Cn": "Latest Haryanvi ",
    "37i9dQZF1DX1w6dPRcGVZq": "Latest Bhojpuri",
    "37i9dQZF1DX5q67ZpWyRrZ": "Indie India ",
    "37i9dQZF1DWSwxyU5zGZYe": "Bollywood Acoustic",
    "37i9dQZF1DXd8cOUiye1o2": "New Music Hindi",
    "37i9dQZF1DWWwrjLPC16W7": "Latest Telugu",
    "37i9dQZF1DX1MYRp9oolwH": "Latest Romance Telugu",
    "37i9dQZF1DWVo4cdnikh7Z": "Latest Tamil",
    "37i9dQZF1DWWWpLwNv0bd2": "Latest Love Tamil",
    "37i9dQZF1DX5baCFRgbF3x": "New Music Friday Punjabi",
    "37i9dQZF1DWZBIyOatufxH": "Latest Marathi Hits",
    "37i9dQZF1DX1i3hvzHpcQV": "Hot Hits Tamil",
}


def read_playlist(pid, kind="playlist"):
    """[{id, title, artists, duration_ms}] of a public playlist's (or album's) embed page (up to 100 tracks)."""
    try:
        with urllib.request.urlopen(urllib.request.Request(f"https://open.spotify.com/embed/{kind}/{pid}", headers=BROWSER_UA), timeout=30) as r:
            html = r.read().decode("utf-8", "ignore")
        e = json.loads(re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S).group(1))
        tracks = e["props"]["pageProps"]["state"]["data"]["entity"].get("trackList") or []
    except (urllib.error.URLError, TimeoutError, AttributeError, KeyError, TypeError, ValueError):
        return []
    return [{"id": t["uri"].rsplit(":", 1)[-1], "title": t.get("title") or "",
             "artists": [a.strip() for a in (t.get("subtitle") or "").split(",") if a.strip()], "duration_ms": t.get("duration")}
            for t in tracks if (t.get("uri") or "").startswith("spotify:track:")]


def run(conn, cfg, since, until, ts, log, check):
    ids = cfg["songs"].get("spotify_playlists") or list(PLAYLISTS)
    songs = conn.execute(
        """SELECT t.id, coalesce(t.yt_title, t.title) title FROM track t
           WHERE t.source='youtube' AND t.first_published >= ? AND substr(t.first_published,1,10) <= ?
             AND coalesce(t.release_check, 'unknown') != 'old' AND coalesce(t.release_status, '') != 'catalogue'
             AND NOT EXISTS (SELECT 1 FROM platform_item p WHERE p.track_id=t.id AND p.platform='spotify')""", (since, until)).fetchall()
    if not songs or not ids:
        return {}
    by_token = {}
    for s in songs:
        for tok in set(match.tokens(s["title"])):
            if len(tok) >= 3:
                by_token.setdefault(tok, set()).add(s["id"])
    titles = {s["id"]: s["title"] for s in songs}
    # Soundtrack albums of films that have an unlinked song and another song already on a Spotify album.
    albums = [r[0] for r in conn.execute(
        f"""SELECT DISTINCT r.spotify_album_id FROM song_meta m
            JOIN song_meta m2 ON m2.field='film' AND m2.value_key=m.value_key AND m2.track_id != m.track_id
            JOIN track t2 ON t2.id=m2.track_id JOIN release r ON r.id=t2.release_id
            WHERE m.field='film' AND m.track_id IN ({",".join("?" * len(titles))})
              AND r.spotify_album_id IS NOT NULL AND coalesce(r.album_type, '') != 'compilation'""", list(titles))]
    # An album's track list rarely changes: read each at most once a day (playlists change weekly; read every run).
    today = ts[:10]
    albums = [a for a in albums if (conn.execute("SELECT value FROM state WHERE key=?", (f"sp_album_read:{a}",)).fetchone() or [""])[0] != today]
    sources = [("playlist", pid) for pid in ids] + [("album", a) for a in albums]
    vids, best, seen, read = {}, {}, set(), 0
    for kind, pid in sources:
        check()
        tracks = read_playlist(pid, kind)
        if kind == "album" and tracks:
            conn.execute("INSERT OR REPLACE INTO state (key, value) VALUES (?,?)", (f"sp_album_read:{pid}", today))
        time.sleep(1)
        read += bool(tracks)
        for t in tracks:
            if t["id"] in seen:
                continue
            seen.add(t["id"])
            core = match.parse_spotify_title(t["title"])[0]
            cands = set().union(*(by_token.get(tok, set()) for tok in set(match.tokens(core)) if len(tok) >= 3)) if core else set()
            for sid in cands:
                if match.title_score(core, titles[sid]) < 0.8:
                    continue
                if sid not in vids:
                    vids[sid] = conn.execute("""SELECT title, description, published_at, duration_ms FROM platform_item WHERE track_id=?
                                                AND platform='youtube' ORDER BY asset_type IN ('audio','lyric') DESC, published_at LIMIT 3""", (sid,)).fetchall()
                for v in vids[sid]:
                    total, det = match.score(dict(t, release_title=None, release_date=None),
                                             {"title": v["title"], "description": v["description"], "published_at": v["published_at"],
                                              "duration_s": (v["duration_ms"] or 0) / 1000 or None})
                    if match.decide(total, det) == "auto" and (sid not in best or total > best[sid][0]):
                        best[sid] = (total, det, t, f"{kind}:{pid}")
    linked = 0
    for sid, (total, det, t, pid) in best.items():
        if attach(conn, sid, t["id"], pid.split(":")[0], total, dict(det, via=pid, playlist=PLAYLISTS.get(pid.split(":")[1], pid),
                                                              candidate_artists=t["artists"]), t["title"], ts):
            d = embed_details(t["id"])
            time.sleep(1)
            if d:
                conn.execute("UPDATE platform_item SET published_at=?, duration_ms=? WHERE platform='spotify' AND platform_id=?",
                             (d["release_date"], d["duration_ms"], t["id"]))
            linked += 1
    conn.commit()
    log(f"Spotify playlists and film albums: {read} of {len(sources)} pages read ({len(ids)} playlists, {len(albums)} albums; {len(seen):,} tracks); {linked} songs linked")
    return {"spotify_playlist_links": linked, "spotify_playlist_tracks": len(seen), "spotify_playlist_pages": read}
