"""Settings: non-secret config in config.json, secrets in .env (chmod 600)."""
import copy
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.json"
ENV_FILE = ROOT / ".env"
SEED_FILE = ROOT / "seeds" / "sony_india_artists.txt"
DB_FILE = ROOT / "data" / "lens.db"
OUT_DIR = ROOT / "out"

SECRET_KEYS = ("SPOTIFY_CLIENT_ID", "SPOTIFY_CLIENT_SECRET", "YOUTUBE_API_KEY", "ANTHROPIC_API_KEY")

DEFAULTS = {
    "discovery": {
        "since": "2026-09-15",
        "until": "2026-10-15",  # blank = run date
        "market": "IN",
        # Sent as label:"..." filters; Spotify matches loosely, so results are re-checked with label_regex.
        "label_queries": ["Sony Music India", "Sony Music Entertainment India"],
        # A release counts as Sony Music India if its label or (C)/(P) line matches this.
        "label_regex": r"sony\s+music\s+(entertainment\s+)?india",
        "use_track_search": True,
        "use_artist_seeds": True,
        "store_raw": True,
        # Re-fetch albums/tracks already stored on every run (needed only if Spotify returns
        # popularity for this app; otherwise each run fetches new releases only).
        "refresh_existing": False,
        # Spotify development mode allows only roughly 1,000-1,500 calls a day per app.
        "max_calls_per_run": 900,
        "isrc_lookups_per_run": 400,
    },
    "schedule": {
        "enabled": False,
        "time": "06:00",  # server local time
    },
    "youtube": {
        "enabled": True,
        "every_hours": 6,     # sync cadence; videos in their first `fresh_days` are read every run
        "fresh_days": 7,
        "lookback_days": 31,  # also collect videos published this long before the release window
        # Record labels tracked; a label that is switched off is not collected or read.
        "labels": [
            {"name": "Sony Music India", "enabled": True},
            {"name": "Aditya Music", "enabled": True},
            {"name": "Saregama", "enabled": True},
            {"name": "Think Music", "enabled": True},
        ],
        # Official channels of each label: the channels the label's main channel features on its own
        # page (verified 2026-10-16 for Sony, 2026-10-23 for the others via channels/channelSections).
        "channels": [
            {"id": "UC56gTxNs4f9xZ7Pa2i5xNzg", "title": "Sony Music India", "label": "Sony Music India"},
            {"id": "UC3MLnJtqc_phABBriLRhtgQ", "title": "SonyMusicIndiaVEVO", "label": "Sony Music India"},
            {"id": "UCTNtRdBAiZtHP9w7JinzfUg", "title": "SonyMusicSouthVEVO", "label": "Sony Music India"},
            {"id": "UCn4rEMqKtwBQ6-oEwbd4PcA", "title": "Sony Music South", "label": "Sony Music India"},
            {"id": "UCpJmCkjsJbqIBdvTRx2zt-w", "title": "Sony Music Malayalam", "label": "Sony Music India"},
            {"id": "UCc052CoYw9cF27quifoL_mg", "title": "Sony Music Bangla", "label": "Sony Music India"},
            {"id": "UCbq-ZyyqIPOAvQGfYvkr75Q", "title": "Sony Music Bhakti", "label": "Sony Music India"},
            {"id": "UCT5VwjyDpD5m5qMNagy6Gjw", "title": "Sony Music India Classical", "label": "Sony Music India"},
            {"id": "UCNApqoVYJbYSrni4YsbXzyQ", "title": "Aditya Music", "label": "Aditya Music"},
            {"id": "UCOuNTPwea_j7l808I9DvLRQ", "title": "Aditya Music Hindi", "label": "Aditya Music"},
            {"id": "UCo_iDY0qQ4d4ad3zRAZtPsg", "title": "Aditya Music Tamil", "label": "Aditya Music"},
            {"id": "UCFfg3ORryhlm-i4ltTFe_RA", "title": "Aditya Music Kannada", "label": "Aditya Music"},
            {"id": "UCixgDUHkyH7GWePqeuNmx1g", "title": "Aditya Music Malayalam", "label": "Aditya Music"},
            {"id": "UCZQ3VR0OxbRVVy5uzfGomUg", "title": "Aditya Bhakthi", "label": "Aditya Music"},
            {"id": "UCpz5rqSZuEDbE2UTUiy9Glg", "title": "Aditya Bhakthi Hindi", "label": "Aditya Music"},
            {"id": "UC_A7K2dXFsTMAciGmnNxy-Q", "title": "Saregama Music", "label": "Saregama"},
            {"id": "UCzee67JnEcuvjErRyWP3GpQ", "title": "Saregama Tamil", "label": "Saregama"},
            {"id": "UC68nKdrLbLL0Vj7ilVkLmmg", "title": "Saregama Telugu", "label": "Saregama"},
            {"id": "UCoRF8GByEjmM_yHwUGIDGyQ", "title": "Saregama Malayalam", "label": "Saregama"},
            {"id": "UCKFnbjBQDzzh002TMJcyuZA", "title": "Saregama Kannada", "label": "Saregama"},
            {"id": "UCRh-4WUJx8M86gUYL2pyKSQ", "title": "Saregama Bengali", "label": "Saregama"},
            {"id": "UCJSX0gNr2U5lawLXE6IjRmA", "title": "Saregama Marathi", "label": "Saregama"},
            {"id": "UCvrD1eyGgo8O9JcTraCvYBg", "title": "Saregama Gujarati", "label": "Saregama"},
            {"id": "UCSdWkYk9pk3DHTXbof_R7XQ", "title": "Saregama Punjabi", "label": "Saregama"},
            {"id": "UC0J8BQahplvTY6LjlnLf_NQ", "title": "Saregama Hum Bhojpuri", "label": "Saregama"},
            {"id": "UCbZYeJWwQgGl9Iea4EeiVmA", "title": "Saregama Odia", "label": "Saregama"},
            {"id": "UCkQXxEAF-Sxb2r5ca040fng", "title": "Saregama Chhattisgarhi", "label": "Saregama"},
            {"id": "UC9qKSI1o2icugvobZfVJjXQ", "title": "Mharo Rajasthan", "label": "Saregama"},
            {"id": "UC6vQRTCxutg6fJLUGkDKynQ", "title": "Saregama Bhakti", "label": "Saregama"},
            {"id": "UCdPc54sruYz59yz9iYEHFYQ", "title": "Saregama South Devotional", "label": "Saregama"},
            {"id": "UCcfQ2IQNVoAb7znCP2dlIqw", "title": "Saregama Ghazal", "label": "Saregama"},
            {"id": "UC76TspUE4-LJgLN-7JCcUvg", "title": "Saregama Sufi", "label": "Saregama"},
            {"id": "UC6-4EXwd60J6cfJPttOHd3A", "title": "Saregama Carnatic Classical", "label": "Saregama"},
            {"id": "UCjm_2mMYXUEjWzSVsA6Wshg", "title": "Saregama Hindustani Classical", "label": "Saregama"},
            {"id": "UCLbdVvreihwZRL6kwuEUYsA", "title": "Think Music India", "label": "Think Music"},
            {"id": "UCWO7fKdX8VDcVjacgMUfI0g", "title": "Think Music Telugu", "label": "Think Music"},
            {"id": "UCAtehwA95bgdVT4c4Ukdbow", "title": "Think Music Malayalam", "label": "Think Music"},
            {"id": "UCOq0PoxzxZTYf3l65dkvmmA", "title": "Think Indie Collective", "label": "Think Music"},
            {"id": "UCBsyaEpqowt2GhrRucjWIOA", "title": "Think Divine", "label": "Think Music"},
        ],
    },
    "songs": {
        # Songs come from the YouTube channels; each is looked up on Spotify with one search call.
        "spotify_searches_per_run": 150,
        "spotify_recheck_days": 3,  # songs not found on Spotify are searched again after this
        # Artist-first lookup (artist releases -> tracks): works while search is quota-blocked.
        "spotify_artist_calls_per_run": 700,
        # Optional contact (URL or email) MusicBrainz asks for in the User-Agent; blank sends just "MarketLens/0.1".
        "musicbrainz_contact": "",
    },
    "merge": {
        # Rules that find one song stored as several (Review -> Duplicate songs). action "suggest" lists the
        # merges for one-click approval; "auto" merges during the pipeline. Every merge can be undone.
        "across_labels": False,
        "rules": {
            "same_isrc": {"enabled": True, "action": "auto"},
            "spotify_same": {"enabled": True, "action": "suggest", "max_days_apart": 60, "max_length_diff_s": 5},
            "similar_name": {"enabled": True, "action": "suggest", "min_similarity": 0.9, "max_days_apart": 30,
                             "max_length_diff_s": 8},
            "name_contains": {"enabled": True, "action": "suggest", "min_words": 2, "max_days_apart": 14,
                              "max_length_diff_s": 8},
        },
    },
    "health": {
        # While Spotify refuses requests, re-check this often; once healthy, every 6 hours.
        "recheck_minutes": 15,
        # Start discovery automatically when access comes back (if no successful run today).
        "auto_run_on_recovery": True,
    },
    # Keep the machine working between scheduled pipelines: Spotify plays, JioSaavn, song lookups, YouTube Music,
    # each only while it has work and within its source's limits (lens/jobs.py start_worker).
    "worker": {
        "enabled": True,
        "pause_seconds": 60,  # breather between jobs so the Console stays responsive
    },
}


def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        elif k in out:
            out[k] = v
    return out


FIRST_LABEL = "Sony Music India"  # channels saved before labels existed belong to it


def _upgrade(cfg, data):
    """Give every channel a label and every label an entry. A config.json saved before labels
    existed keeps its channel list (all Sony) and gains the default channels of the other labels."""
    y = cfg["youtube"]
    for c in y["channels"]:
        c.setdefault("label", FIRST_LABEL)
    if "labels" not in (data.get("youtube") or {}):
        have = {c["id"] for c in y["channels"]}
        y["channels"] += [dict(c) for c in DEFAULTS["youtube"]["channels"]
                          if c["id"] not in have and c["label"] != FIRST_LABEL]
    names = [lb["name"] for lb in y["labels"]]
    for c in y["channels"]:
        if c["label"] not in names:
            y["labels"].append({"name": c["label"], "enabled": True})
            names.append(c["label"])
    return cfg


def load():
    data = json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {}
    return _upgrade(_merge(DEFAULTS, data), data)


def window(cfg):
    """(since, until) of the song window; a blank `until` means today."""
    from datetime import date
    d = cfg["discovery"]
    return d["since"], d["until"] or date.today().isoformat()


def active_channels(cfg):
    """Channels whose label is switched on."""
    on = {lb["name"] for lb in cfg["youtube"]["labels"] if lb["enabled"]}
    return [c for c in cfg["youtube"]["channels"] if c["label"] in on]


def channel_labels(cfg):
    """Channel title -> label (videos store the channel's title)."""
    return {c["title"]: c["label"] for c in cfg["youtube"]["channels"]}


def save(cfg):
    cfg = _merge(DEFAULTS, cfg)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    return cfg


def _read_env_file():
    vals = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


def read_secrets():
    vals = _read_env_file()
    for k in SECRET_KEYS:  # real environment wins over the file
        if os.environ.get(k):
            vals[k] = os.environ[k]
    return vals


def write_secrets(updates):
    """Merge non-empty values into .env; empty strings leave the existing value alone.
    YouTube keys are added to the stored list rather than replacing it. Other keys already in the file (e.g. STUDIO_PASSWORD) are preserved."""
    vals = _read_env_file()
    for k, v in updates.items():
        if k == "YOUTUBE_API_KEY" and v:
            # Several keys are kept as a comma-separated list; new ones are added after the existing ones.
            from .youtube import parse_keys
            vals[k] = ",".join(parse_keys(parse_keys(vals.get(k, "")) + parse_keys(v)))
        elif k in SECRET_KEYS and v:
            vals[k] = v.strip()
    keys = list(SECRET_KEYS) + [k for k in vals if k not in SECRET_KEYS]
    ENV_FILE.write_text("".join(f"{k}={vals.get(k, '')}\n" for k in keys))
    ENV_FILE.chmod(0o600)


def read_seeds():
    """Seed artists as [(spotify_artist_id, note)]."""
    if not SEED_FILE.exists():
        return []
    seeds = []
    for line in SEED_FILE.read_text().splitlines():
        body, _, note = line.partition("#")
        if body.strip():
            seeds.append((body.strip(), note.strip()))
    return seeds


def write_seeds(seeds):
    SEED_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Spotify artist IDs of Sony Music India artists, one per line (second discovery path)."]
    lines += [f"{sid}  # {note}" if note else sid for sid, note in seeds]
    SEED_FILE.write_text("\n".join(lines) + "\n")
