# Market Lens: music release market intelligence (Phase 1: Sony Music India)

Lists the songs released on the Sony Music India YouTube channels since a start date
(default 2026-10-01), looks each one up on Spotify once for its metadata (ISRC, artists,
album, release date), and records trend readings per song: YouTube views/likes/comments
and Deezer rank. No AI. All matching is rule-based.
Python 3 standard library only, no installs.

## Market Lens Console (UI)

```bash
python3 -m lens.studio            # http://127.0.0.1:3000
```

On a remote server, open an SSH tunnel and browse to http://localhost:3000:

```bash
ssh -L 3000:localhost:3000 ubuntu@<server>
```

To listen on a public interface instead, set `STUDIO_PASSWORD` in `.env` and run
`python3 -m lens.studio --host 0.0.0.0` (browser asks for the password; any username).

| Page | What you do there |
|---|---|
| Sources | Spotify client ID/secret, API status with history and "Retry API status"; YouTube key (connector next) |
| Settings | Release window, label phrases, Sony India match rule, toggles, daily schedule, seed artists |
| Runs | Start/cancel a run, live log, run history with stats |
| Outputs | Every CSV per run (preview, filter, download) and every raw Spotify API response, including failures |
| Overview | KPIs, releases per month, label strings matched, top tracks, day-on-day risers |
| Catalogue | Search/filter/sort all tracks with YouTube views, track detail with views history, CSV export |
| Review | Spotify ↔ YouTube matches: approve/reject uncertain ones, link videos to unmatched tracks |

The daily schedule runs inside the studio process, so keep it running (e.g. under
systemd or tmux). If the studio was down at the scheduled time, it catches up on start.

## CLI

```bash
python3 -m lens.discover_spotify [--since 2026-01-01] [--until 2026-10-15]
```

## Files

| Path | Contents |
|---|---|
| `config.json` | Settings (written by the UI) |
| `.env` | Secrets: `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `YOUTUBE_API_KEY`, `STUDIO_PASSWORD` |
| `seeds/sony_india_artists.txt` | Seed artist IDs |
| `data/lens.db` | SQLite: release, track, artist, platform_item, snapshot, raw_capture, run_log |
| `out/` | CSV written after each run |

## API health watcher

The studio re-checks Spotify every 15 minutes while requests are failing (every 6 h once healthy)
and records each check (Sources page). When access returns it starts discovery automatically if
there was no successful run that day. Both settings are on the Settings page.

## Spotify development-mode limits (observed 2026-10-16)

This app runs in Spotify's development mode, which:

- caps search pages at 10 results and reports a misleading `total`/`next` (the client pages by offset instead);
- blocks the batch endpoints (`/albums?ids=`, `/tracks?ids=`), so every album and track is a separate call;
- omits `label` (the label is taken from the (P) line instead), `popularity`, artist `followers`/`genres`;
- requires the app owner's account to have Spotify Premium.

Because of the per-item calls, runs are incremental: albums already stored, and albums already found not
to be Sony Music India under the current match rule, are not fetched again.

## Labels

The Console tracks four Indian labels: **Sony Music India**, **Aditya Music**, **Saregama** and **Think Music**,
39 YouTube channels in all (`config.json` → `youtube.labels` / `youtube.channels`). Each label's channels are the
official ones its main channel features on its own page; movie, TV-show, karaoke, kids, remix/jhankar and
"old songs" channels are left out. Under Settings → YouTube you can add a label, add or remove channels per label,
and switch a label off ("Tracked"): a label that is off is not collected or read, and its data is kept.
Songs never merge across labels. The Overview, Songs page and CSV show each song's label.

## One song, many uploads

A song collects every upload of it on the label channels: the video song, lyric video, audio, Shorts and teasers
(Shorts and teasers only join a song the full uploads formed, since their captions are often not the song name).
Every video keeps its own views/likes/comments, and the song shows the total plus a split by type
(Songs page under Views, the song panel's "YouTube by video type" table, and `<type>_videos` / `<type>_views`
columns in the CSV). A full-length upload that is not a lyric, audio, Short or teaser counts as the video.

On Spotify, one search per song also links the other copies of the same recording in that result (same ISRC,
or same title and main artist within 3 seconds, e.g. the single and the album copy). No extra calls.

The song window is Settings → Release window (now 2026-09-15 → 2026-10-15; blank "to" = today). The Songs page
has its own first-upload date range.

## Song metadata and Explore

`lens/metadata.py` builds each song's metadata with fixed rules (no API calls), one row per value in `song_meta`
(song, field, value, normalised key, source):

| Fields | Source |
|---|---|
| ISRC, Spotify album, release date, length, Spotify artists, film from "(From "X")" / soundtrack albums | Spotify |
| Singers, music director, lyricists, cast, film / album, director, producer, arranger, mixing & mastering, choreographer, music label | the credits block in the YouTube descriptions ("Singer : X", "Music - Y", "Directed by Z" ...) |
| Language | title words, YouTube audio language, channel, script of the title, tags (in that order) |
| Category (Film, Devotional, Classical, Ghazal / Sufi, TV / Web show, Non-film / Indie), occasion (Navratri, Ganesh Chaturthi, Diwali ...) | channel and keyword rules |
| Label, channel | the channel the song was uploaded on |

The song panel shows all of it with its source; every name links to its metrics. **Explore** ranks every value of a
field (singers, music directors, lyricists, cast, films, directors, languages, categories, occasions, labels, channels,
Spotify artists) by songs, new releases, views, share, day-on-day gain, likes, comments and average views per song,
and opens a value's detail: KPIs, views gained per day, its songs and who it appears with. Songs can be filtered by
language and category, and the search box also finds credits. Each run writes `market_lens_song_metadata_<date>_run<N>.csv`
(one row per value, for Power BI) next to the songs CSV, which now also has metadata columns.

## AI Insights

`lens/insights.py` computes five panels from public data with fixed rules (no AI): **Emerging hits** (breakout score for
songs not yet in the top 5%: speed vs songs of the same age, growth relative to size, acceleration), **Cross-label
intelligence** (labels, languages and label x language by age-adjusted percentile, not totals), **Catalog
opportunities** (songs released a year+ before their upload drawing above-typical views), **Artist portfolio health**
(broad-based vs one-hit dependent) and **Anomaly watch**. Every song panel also shows how the song compares with similar
songs. Growth, acceleration and anomaly signals need several daily readings; the page says when history is thin.

The **AI assist** notes on the page are a written assessment of those panels, kept in `data/ai_assist.json` (dated; they
don't update with the data). Live AI (Claude Opus 5.5 explanations and the Ask Music AI chat, `lens/ai.py`) is built but
switched off until the Claude API integration: its endpoints are commented out in `studio.py`.

## Merging duplicate songs

Settings → Merge rules: four fixed rules (same ISRC, same Spotify title + main artist, spelling variant, one name
contained in the other), each on/off, "suggest" or "merge automatically", with thresholds (name similarity, days
apart, length difference) and an "across labels" switch. Review → Duplicate songs lists the suggestions with their
evidence: merge a group, selected groups, or every group of a rule in one click, or mark them "not the same"
(remembered). On the Songs page, tick any songs and "Merge into one song". A merge moves every YouTube video,
Spotify version and Deezer item to the kept song; it is one entry in Decision history and can be undone; and the
merged names are remembered, so the next pipeline run keeps the merge. Candidates come from blocking (shared ISRC
or a shared uncommon word), so the work grows with the number of songs, not with its square.

## YouTube connector

`python3 -m lens.studio` syncs YouTube every 6 h (Settings → YouTube). Each sync:

1. collects uploads from the configured Sony Music India channels (from 31 days before the release window);
2. reads views/likes/comments: every run for videos in their first 7 days, otherwise once a day
   (every reading is kept in `reading`; `snapshot` holds the day's latest value);
3. matches videos to Spotify tracks with fixed rules (`lens/match.py`): title (55%), artists (15%),
   film/release (10%), duration (10%), date (10%). Auto-match needs total ≥ 0.75 and title ≥ 0.85;
   0.55-0.75 goes to the Review page. Remix/male/instrumental/"X Version" variants, shorts,
   jukeboxes and mashups never match. Reviewer decisions are permanent.

A sync uses about 30-300 of the 10,000 daily quota units per key. Several keys can be stored
(`YOUTUBE_API_KEY=k1,k2,...` in `.env`, or paste them on the Sources page, which adds them to the list).
They are used in order: when Google reports a key's quota is used up (or rejects the key), it is set aside
until midnight Pacific time and the same request continues on the next key. The Sources page shows each
key's units today and status. Keys only add quota if they belong to different Google Cloud projects.

## Songs pipeline (YouTube first)

`Run pipeline` (and the 6-hourly schedule) runs two steps:

1. **YouTube sync**: new uploads from the channels; views/likes/comments for videos in the window and
   every video linked to a song (every run in a video's first week, then daily).
2. **Songs** (`lens/songs.py`):
   - groups the window's videos into songs (lyric / official / audio uploads of one song become one song;
     remix, male/female and other versions stay separate);
   - **one Spotify search per song**, scored with the same rules; uncertain ones go to Review → "Spotify links";
     songs not found are searched again only every 3 days;
   - status: *new release* (on Spotify at most 30 days before its first YouTube upload) or *older release*
     (earlier, or only on a compilation), or *not on Spotify*;
   - Deezer rank by ISRC, once a day (free API, no key).

Spotify gives no play counts or popularity to apps in development mode, so it supplies metadata only.
The old Spotify label crawl (`spotify_discovery`) is kept as a manual job; it is expensive on Spotify's quota.

---

© 2026 Saranyu Technologies. All rights reserved.
