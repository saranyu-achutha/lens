# Market Lens: project context

Saranyu Technologies is building a release market-data platform for Times Music (client contact: Amit Bardhan; Saranyu lead: Suresh BG). The requirements are in the email thread at `lens/req..html` (the filename has two dots).

- **Phase 0 targets:** at least 95% of releases discovered within 24 hours, at least 90% matched across platforms, at least 99% of daily readings captured, and at most 10% of matches needing manual review.
- **Data rules:** daily snapshots kept indefinitely. YouTube is read every 6 hours in a video's first 7 days.
- **Output:** a model ready for Power BI.
- **No AI or ML:** all matching is rule-based.

The code lives in `lens/` and runs on the server as `/home/ubuntu/lens`. This laptop holds a copy. See `lens/README.md` for how to run it and how the pipeline works.

## Current direction (Phase 1)

- **Scope:** four Indian labels since 2026-10-23: Sony Music India, Aditya Music, Saregama and Think Music (39 YouTube channels). Each channel has a `label` in `config.json`, and a label can be switched off. Songs from different labels never merge; non-Sony song keys are prefixed `<label>::`.
- **YouTube first:** songs come from the labels' YouTube channels, for videos released from 2026-10-01 to today (`config.json` → `discovery.since`).
- **Spotify: the API only finds the track ID; everything after that is scraped** (user rule, 2026-10-29). The API is used for search (`songs.spotify_lookup`), artist-first discovery (`spotify_artists`, including one `/tracks/{id}` call when a song is linked, for its ISRC) and the health probe. Once a song has its ID, the play count, release date, duration, album and artists come from the public track page (`spotify_plays`, daily). Don't add API calls for linked tracks.
- **Trend data:**
  - YouTube views, likes and comments, every run in a video's first week and then daily.
  - Deezer rank by ISRC, from `api.deezer.com/track/isrc:{ISRC}`, daily, with no key needed.
- **Keep API calls low:**
  - A YouTube sync costs about 30 units of the 10,000 daily quota.
  - The old Spotify label crawl (`spotify_discovery`) is manual only because it exhausts Spotify's quota.
- **Python 3 standard library only, with SQLite** (`lens/data/lens.db`, WAL mode). There is no git repo.

## Songs, versions and merging (2026-10-23)

- One song = many YouTube uploads (video, lyric, audio, short, teaser); per-video stats plus totals by type. Shorts and teasers only join existing songs.
- One song can have several Spotify tracks (`match_method='same_recording'`), linked from the same search result.
- Window set to 2026-09-15 → 2026-10-15 (`config.window()`).
- `lens/merge.py`: configurable merge rules (`config.json` → `merge`). Merges are recorded in `review_decision` (action `merge`) and can be undone. `song_alias` keeps merged names, so regrouping preserves merges; `merge_rejection` remembers "not the same".

## Song metadata (2026-10-23)

- `lens/metadata.py` parses the credits blocks in YouTube descriptions and adds Spotify data, language, category and occasion rules into `song_meta` (one row per song, field and value, with `value_key` and `source`). It is rebuilt each run and after Review decisions and merges.
- The Explore page and `/api/meta/top` and `/api/meta/detail` give metrics per value. The YouTube `videos.list` call now also requests `snippet` (tags, audio language, category) at no extra quota.
- Known gaps: spelling variants of the same person aren't merged (Sublashini / Sublahshini); honorifics stay in names ("Natural Star Nani").

## AI Insights (2026-10-23)

- `lens/insights.py` holds the rule-based panels. `data/ai_assist.json` holds the manual "AI assist" notes, written by Claude from the panel numbers; refresh them when the data changes.
- The user asked to keep live AI off for now. `lens/ai.py` (Claude Opus 5.5, tools, read-only SQL, server-side refusal fallback) is not routed: its endpoints are commented out in `studio.py`, and the Sources card shows "Planned".
- Metadata sources besides Spotify: the iTunes Search API (`platform='apple'`) and MusicBrainz (`platform='musicbrainz'`; the User-Agent carries no company name, at the user's request). Artist-first Spotify lookups are in `lens/spotify_artists.py`.

## Console (the web UI, formerly "Studio")

- **Branding:** "Market Lens" with a small **CONSOLE** tag. The footer reads "© <year> Saranyu Technologies. All rights reserved." The "Powered by" line was removed at the user's request.
- **Start command:** `python3 -m lens.studio --host 0.0.0.0 --port 3000`. It listens on a public address only when `STUDIO_PASSWORD` is set in `.env`; the login accepts any username with that password.
  - On the server it runs at `http://<server-public-ip>:3000`.
  - The module name `lens.studio` and the `STUDIO_PASSWORD` variable keep their old names on purpose.
- **Pages:**
  - Overview, Songs, Runs, Outputs, Sources, Settings, and an "API samples" link (`/docs/API_SAMPLES.html`).
  - Review, with these tabs: Spotify links to check, All Spotify links, Video matches, Auto-matched, Approved, Unmatched, and Decision history (each decision can be undone exactly).
- **Play buttons:** a docked YouTube or Spotify embed player. A deep link like `#catalogue?play=youtube:ID` opens a song playing.
- **Restarting:**
  1. Check that `/api/job` is idle.
  2. Find the PID with `ss -ltnp | grep ':3000 '` and kill it. Never use `pkill -f`.
  3. Clear `__pycache__`.
  4. Start the Console again.

## Main modules (`lens/lens/`)

| Module | What it does |
|---|---|
| `match.py` | Rule-based scoring: title .55, artist .15, film .10, duration .10, date .10. A match is automatic at a total ≥ .75 with title ≥ .85, and goes to review at ≥ .55. Remix, male/female and "X version" variants never match. |
| `songs.py` | Groups videos into songs, does the Spotify lookup (songs not found are rechecked every 3 days), sets the status (new release if on Spotify at most 30 days before the first YouTube upload, otherwise older release, or not on Spotify), takes Deezer readings and writes the songs CSV. |
| `sync_youtube.py` | Collects uploads, takes readings and runs matching. |
| `youtube.py` | YouTube API client. It takes a single `YOUTUBE_API_KEY` and counts quota units. |
| `spotify.py` | Spotify client with a per-run call budget and quota detection. |
| `jobs.py` | Runs jobs and pipelines, runs the scheduler (pipeline every 6 hours) and the health watcher. |
| `review.py` | Decision log with exact undo. |
| `studio.py` | HTTP server and API. |
| `static/` | The UI. |

## Spotify development-mode limits

- The app owner's account needs Premium. It has Premium now.
- Search pages are capped at 10 results.
- Batch `/albums` and `/tracks` calls return 403.
- `label`, `popularity`, `followers` and `genres` are left out of responses.
- The quota is per endpoint group. Album and track lookups can be blocked for about 24 hours while search still works.

## State as of 2026-10-16

- **Songs:** 107 in total: 11 new releases, 51 older releases and 37 not on Spotify. 8 Spotify links are waiting for review.
- **YouTube data:** 4,942 videos stored since December 2025.
- **Revert:** 5 approvals made by an unverified person were reverted. A backup from before that is at `lens/data/backups/lens_before_revert_2026-10-16.db`. Always check who made a decision before naming them.

## Open items

- Review the 8 pending Spotify links.
- Turn on the daily schedule (it is off).
- Set up a systemd service on the server (needs sudo).
- Give each reviewer their own login.
- Add HTTPS.
- Rotate the keys that were pasted in chat.
- Optional sources: Last.fm (needs a key), and JioSaavn (unofficial, needs a terms-of-service sign-off).
- YouTube keys (`YOUTUBE_API_KEY`, comma-separated, used in order; a key whose quota runs out is set aside until midnight Pacific time; a key that hits the daily search cap is set aside for searches only).
  - **Quota is per Google Cloud project, not per key.** Since 2026-10-29 there is **one key per project, 8 keys** (projects 887372396500, 421586835201, 834007049812, 701726783900, 978509973520, 1074452930644, 1058718100591, 874281993839): about 80,000 units and 800 searches a day; YouTube Music searches up to 200 per run (`songs.ytmusic_searches_per_run`). The 27 extra keys that shared those projects were removed (the full list is in the `.env.bak.*` backups). More capacity needs a key from a new project; a second key in the same project adds nothing.
  - To find a key's project: call any Google API the project has not enabled (e.g. Books); the 403 names `projects/<number>`.

## Server-only actions

The AWS metadata lookup and `sudo ufw` were denied by the permission system. The user opens ports in the EC2 security group themselves.
