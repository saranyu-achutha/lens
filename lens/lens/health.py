"""Connector health checks, recorded in api_check so the Console can show their history."""
from datetime import datetime, timedelta, timezone

from . import config, db
from .spotify import Spotify, SpotifyError

PROBE = {"q": 'label:"Sony Music India" year:2026', "type": "album", "limit": 1, "offset": 0}


def check_spotify(conn, trigger="manual"):
    s = config.read_secrets()
    cid, secret = s.get("SPOTIFY_CLIENT_ID"), s.get("SPOTIFY_CLIENT_SECRET")
    retry_at = None
    if not cid or not secret:
        ok, status, msg = False, None, "Client ID and secret are both required."
    else:
        sp = Spotify(cid, secret, market=config.load()["discovery"]["market"])
        try:
            sp._fetch_token()
        except SpotifyError as e:
            ok, status, msg = False, e.status, f"Credentials rejected by Spotify: {e.body[:300]}"
        else:
            try:
                # The pipeline only uses search (one call per song), so that is what we probe.
                # (Spotify's quota is per endpoint group: album/track lookups can be blocked separately.)
                sp.get("/search", dict(PROBE, market=sp.market))
                ok, status = True, 200
                msg = "Connected. Search works (used for song metadata)."
            except SpotifyError as e:
                ok, status = False, e.status
                if e.quota_exceeded:
                    retry_at = (datetime.now(timezone.utc) + timedelta(seconds=e.retry_after)).isoformat(timespec="seconds")
                    what = "Album/track lookups" if "/albums/" in str(e) or "/tracks/" in str(e) else "Requests"
                    msg = (f"Daily API quota exceeded. {what} are blocked by Spotify for {e.retry_after / 3600:.1f} h "
                           f"(until about {retry_at}).")
                else:
                    msg = "Credentials OK, but data requests are refused: " + _reason(e.body)
    record(conn, ok, status, msg, trigger, retry_at)
    return {"ok": ok, "status": status, "message": msg, "retry_at": retry_at}


def record(conn, ok, status, msg, trigger, retry_at=None, source="spotify"):
    conn.execute("INSERT INTO api_check (source, checked_at, ok, status, message, trigger, retry_at) "
                 "VALUES (?,?,?,?,?,?,?)", (source, db.now_iso(), int(ok), status, msg, trigger, retry_at))
    conn.commit()


def _reason(body):
    import json
    try:
        return json.loads(body)["error"]["message"]
    except Exception:
        return body[:300]


def last_check(conn, source="spotify"):
    row = conn.execute("SELECT * FROM api_check WHERE source=? ORDER BY id DESC LIMIT 1", (source,)).fetchone()
    return dict(row) if row else None
