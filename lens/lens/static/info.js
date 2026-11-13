/* Hover help (2026-11-12): hover any menu item, page or card title, KPI tile, column header, switch or button that has
   an entry below and a panel explains it: what it is, how the number is worked out, when it updates, why it matters,
   and (developer login only) how it works under the hood.

   Entries are keyed by the visible text, normalised (lower case; counts, dates and "(…)" removed). A key "page|text"
   applies on that page only (page = the #hash, e.g. "dashboard|songs") and wins over the plain "text" entry.
   An element can also name its entry directly: data-info="key". Toggle: the ⓘ button in the top bar (kept per browser). */
(function () {
  const I = (what, how, when, why, dev) => ({ what, how, when, why, dev });
  const LAST_DAY = "Shown for the last completed day (yesterday, server time UTC). Today is still being read, so it never appears as a low half-day.";
  const DAILY = "Read once a day for every song of the period; the watchdog re-runs any platform that misses a song for 24 hours.";
  const PERIOD = "Only songs first uploaded on the labels' YouTube channels from 1 July 2026, not released before that date. Older songs are kept for reference but left out.";
  const GROWTH = "Growth, not lifetime totals: each item's gain is its reading minus its own previous reading, counted on the day of the newer reading. A newly linked track's first reading adds nothing.";

  const INFO = {
    /* ---------- menu and pages ---------- */
    "insights": I("Pages that turn the readings into answers: how the songs and labels are doing.", "Built from the daily totals per song and platform.", "Updated after every reading job.", "Start here for business questions."),
    "data": I("Pages with the underlying numbers: every song, every platform, every day.", "Straight from the daily readings, nothing estimated.", DAILY, "Use these to check or export a figure."),
    "operations": I("Developer pages that show how the collection itself is running.", "From the job log, quotas, alerts and reading marks.", "Live: most cards refresh every few seconds to a minute.", "Shows problems before they reach the client numbers."),
    "setup": I("Developer pages that change what is collected and how often.", "Changes are saved to the server's config.json.", "Take effect on the next job run.", "One place for labels, channels, schedules and keys."),
    "overview": I("The developer home page: the client Dashboard's numbers plus label/channel detail and the top songs.", "Same figures as the Dashboard, with extra breakdowns.", LAST_DAY, "A quick health read of the whole catalogue.", "Reuses glanceTiles / audioCards / growthCharts from the Dashboard (/api/client) plus /api/overview."),
    "dashboard": I("The client home page: reach and growth of the labels' new songs on YouTube and the audio platforms.", "YouTube views and audio streams (Spotify + YouTube Music + JioSaavn) are always shown apart, never added into one number.", LAST_DAY, "The one page to read how the period's releases are performing.", "/api/client; PERIOD_ONLY songs; figures from song_daily."),
    "management": I("A weekly summary for management: top songs, new releases, artists, labels, languages and genres.", "Ranked by Spotify plays gained in the last 7 completed days; the week before is shown for change once it was fully read. YouTube views are shown alongside but never used for ranking.", "Rebuilt from the daily readings each time the page opens.", "Spotify plays gained is the cleanest measure of real listening growth.", "lens/management.py, /api/management?genre=&method=."),
    "management summary": I("A weekly summary for management: top songs, new releases, artists, labels, languages and genres.", "Ranked by Spotify plays gained in the last 7 completed days. Artists get full credit for each song they are credited on.", "Each time the page opens, from the daily readings.", "Shows where this week's growth came from.", "lens/management.py"),
    "market share": I("Each label's share of growth, comparing songs fairly at the same age.", "Every song is followed through day N of its life (day 1 = first YouTube upload). Its value at day N = views/plays gained since it started (gaps filled by straight-line interpolation). Share = sum over the label's songs ÷ sum over all songs. YouTube views and audio streams are separate.", "Daily. A song is 'final' once it reaches day N, 'provisional' before that; songs whose first reading came after day N cannot be measured and are left out.", "Labels release at different times; comparing songs at the same age removes that bias.", "lens/market_share.py, /api/market_share?days=&month=; default N in config.json → market_share.days."),
    "cast & featuring": I("The actors and featured artists credited on the songs, and how their songs grow on every platform.", "Cast is read from the credits in the YouTube descriptions (Cast, Starring, Featuring). A song with several cast members counts in full for each. " + GROWTH, "Daily, over the last 7–90 completed days (your choice).", "Shows which faces drive video reach and which drive listening, for casting, promotion and playlist pitching.", "lens/cast_report.py, /api/cast and /api/cast/detail; song_meta field 'cast', numbers from song_daily."),
    "cast|cast members": I("How many different cast members are credited on the period's songs, and how many songs credit a cast.", "Distinct names in the songs' cast credits (spelling variants of one name are not merged).", "Updated as new songs and credits arrive.", "Shows how much of the catalogue this page covers."),
    "cast|youtube views gained": I("YouTube views gained by songs that credit a cast, and their share of all songs' views.", GROWTH, "Daily, for the chosen days.", "How much of the labels' video growth comes from cast-led (mostly film) songs."),
    "cast|audio streams gained": I("Spotify + YouTube Music + JioSaavn plays gained by songs that credit a cast.", "Each platform's gain summed; never added to YouTube views.", "Daily, for the chosen days.", "Listening is a different audience from video: this is the audio side."),
    "cast|streams per 100 views": I("Audio streams gained for every 100 YouTube views gained.", "100 × audio streams ÷ YouTube views over the same days.", "Daily.", "A high figure means people go on to listen, not only watch: good for streaming playlists and audio campaigns."),
    "cast|insights": I("Findings worked out by fixed rules from the numbers on this page (no AI).", "Leaders on video vs audio; rising (week vs week before, only on a real base); audio-first; consistent (median per song, 3+ songs); carried by one song (80%+ from one); the client label's cast vs the market; strong cast not yet on the client's songs; songs with vs without a cast; cast across several labels. People credited on exactly the same songs show once, with '+N more'.", "Recomputed on every load, for the chosen days, label and language.", "A starting point for casting, promotion and A&R conversations."),
    "cast|top cast by platform": I("The 12 cast members whose songs gained the most on the chosen platform.", "Growth over the chosen days; switch between ranked bars, a donut and one stacked bar (shares of the top 12).", "Daily.", "Who leads on each platform: the leaders often differ between video and audio."),
    "cast|daily growth, top": I("Day-by-day growth of the top 5 cast members on the chosen platform.", "Each day's gain summed over the person's songs.", "Daily.", "Spikes show releases and promotions; a steady line shows lasting demand."),
    "cast|where each star's audience is": I("For the 10 biggest cast members, how their growth splits between YouTube, Spotify, YouTube Music and JioSaavn; the market's split on top for comparison.", "Each platform's gain ÷ the person's total gain over the chosen days.", "Daily.", "Shows who to promote where: a star whose bar is mostly Spotify suits audio campaigns."),
    "cast|last": I("Each top cast member's daily growth over the last 14 days on the chosen platform; darker = more.", "Daily gain summed over the person's songs; square-root shading keeps small days visible.", "Daily.", "Momentum at a glance: who is still growing and who has faded."),
    "cast|all cast members": I("Every cast member with all platforms side by side, sortable by any column.", GROWTH, "Daily.", "Click a person for their own page: daily charts, platform mix vs the market, labels, songs and co-stars."),
    "cast|week vs week before": I("Change in YouTube views gained: the last 7 completed days vs the 7 before.", "100 × (this week − week before) ÷ week before; blank when the week before had no growth.", "Daily.", "Rising or fading right now."),
    "cast|platform mix vs the market": I("This person's split of growth across platforms next to the whole market's split.", "Index = person's share ÷ market's share × 100. 130+ (green) means the platform is unusually strong for them; 70 or less (red) unusually weak.", "Daily.", "Where their audience over-indexes, so promotion goes where it works."),
    "cast|works with": I("Co-stars, singers and music directors on this person's songs, with the number of songs together.", "From the same songs' credits; hover a name for the views and streams those shared songs gained.", "Updated with the credits.", "Pairings that work, and who else to approach for a project."),
    "reports": I("Label reports and song comparisons: releases and growth by label, day by day or month by month.", GROWTH, "Daily. The last 2 days are marked ⏳ while Spotify counts are still coming in.", "Exact monthly sums of growth per label, ready to share.", "lens/label_report.py, /api/labels/report (+ .csv)."),
    "platforms": I("One page per platform: totals, daily growth and the songs on it.", "From the daily readings of that platform only.", DAILY, "See how each platform contributes on its own."),
    "daily data": I("Every song's numbers day by day on every platform, with totals and gains.", "Total = the reading at the end of the day; gain = that minus the previous day's reading.", DAILY, "The ground truth behind every chart; drill into any song."),
    "songs": I("The full list of the period's songs, with every platform's link and latest numbers.", "One song = all its YouTube uploads (video, lyric, audio, Shorts, teaser) grouped together, plus its tracks on the other platforms.", "New songs appear within hours of their first upload; numbers update daily.", "Find any song, check its links, open its details or play it.", "Grouping in songs.py; merges in merge.py (undoable)."),
    "platform data": I("Raw items per platform: every YouTube upload, Spotify track, JioSaavn track and so on, with its latest reading.", "One row per platform item as stored, with its review state.", "Updated by each reading job.", "For checking individual links and readings.", "platform_item + latest snapshot."),
    "explore": I("Metrics by metadata: singers, composers, lyricists, languages, categories and more.", "Credits are parsed from the YouTube descriptions and joined with Spotify/Apple/MusicBrainz data; each value's songs are summed.", "Rebuilt with every songs run and after review decisions.", "Answers questions like 'which composer's songs grew most'.", "lens/metadata.py → song_meta; /api/meta/top, /api/meta/detail."),
    "ai insights": I("Rule-based insight panels (no live AI) and notes written from their numbers.", "Fixed rules over the daily data: emerging hits, cross-label patterns, anomalies.", "Panels update with the data; the written notes are refreshed by hand.", "Quick talking points.", "lens/insights.py; notes in data/ai_assist.json; live AI is switched off on purpose."),
    "review": I("Matches the rules were not sure about, waiting for a person to decide.", "A match scoring 0.55–0.75 (or a title score under 0.85) waits here. Approve links it, reject drops it; every decision is logged and can be undone exactly.", "New items arrive with every Spotify search, label sweep or browser search.", "Keeps wrong links out of the numbers while staying under the 10% manual-review target.", "lens/review.py → review_decision; match.py scoring."),
    "status": I("Live state of the collection: what is running, what ran, what is due.", "From the job log and the worker's queue.", "Refreshes every few seconds.", "First place to look when something seems stale.", "/api/status, /api/job."),
    "missed readings": I("Songs not read on a platform in the last 24 hours, and why.", "For every period song and every linked platform, the time since its last reading (a read with no number, e.g. a hidden count, still counts).", "Live. The watchdog restarts the platform's job at once (every 30 min at most) until they are read.", "Guards the 99% daily-readings target.", "lens/freshness.py, /api/freshness; read_mark table."),
    "daily additions": I("How many songs were added each day, and on which platforms they were found.", "A song's day = the server day (UTC) it was first stored. A platform's day = the first day the song was linked there (one count per song and platform).", "Live; today grows during the day.", "Shows discovery speed and gaps per platform.", "lens/additions.py, /api/additions?days=&label=."),
    "crawl stats": I("Per source: runs, items read, found, not found, calls and failures over time.", "Counted by each job as it runs.", "After every run.", "Spot a source that is slowing down or failing.", "lens/crawl_stats.py."),
    "api stats": I("The same view as Crawl stats, focused on API calls and quotas.", "Counted per call.", "After every run.", "Keeps the API usage under the daily limits."),
    "runs": I("Start a job by hand, watch its live log and see every past run.", "Each run's status: ok, partial (stopped at a limit, continues next run), quota exceeded, paused, cancelled or failed.", "Live.", "Manual control and the full history.", "run_log table; jobs.py."),
    "developer": I("The developer dashboard: Phase 0 targets, jobs, quotas, storage, backlogs and failures.", "Collected from the database, the job log and the server.", "Refreshes every minute.", "Everything needed to keep the collection healthy in one page.", "/api/dev."),
    "developer dashboard": I("Phase 0 targets, jobs, quotas, storage, backlogs and failures in one page.", "Collected from the database, the job log and the server.", "Refreshes every minute.", "Keeps the collection healthy."),
    "collection schedule": I("How often each source is read.", "YouTube videos in their first 7 days every 6, 8, 12 or 24 hours (now 8), then daily; everything else once a day.", "Saved settings apply from the next run.", "Balances freshness against API quota.", "config.json → youtube.fresh_every_hours / fresh_days, songs.jiosaavn_refresh_hours."),
    "spotify chart": I("Spotify's India daily and weekly charts, and which of our songs are on them.", "Imported from Spotify Charts with a pasted session token.", "When a token is saved and the import runs.", "Chart presence is a strong hit signal.", "lens/spotify_charts.py → chart_entry."),
    "outputs": I("Every dataset straight from the database: view, filter and download as CSV.", "The CSV is built in memory when you download it; nothing is stored on disk. Downloads never contain IDs or links (ISRC and UPC stay).", "Always current.", "Feeds Excel and Power BI.", "lens/exports.py, IdFreeWriter drops *_id, *_ids, *url columns."),
    "sources": I("The data sources, their keys and what each one can and cannot give.", "Lists every API and public page used, with its status.", "Checked on open and by the health watcher.", "Shows coverage limits honestly.", "lens/health.py."),
    "settings": I("Labels, channels, discovery window, merge rules and the health watcher.", "Saved to config.json on the server.", "Applies from the next run.", "Controls the scope of the whole project."),
    "api samples": I("Sample replies from each API, for reference.", "Real responses saved from the sources.", "Static document.", "Shows exactly what each source returns."),

    /* ---------- at a glance and dashboard ---------- */
    "at a glance": I("The headline numbers for the period's songs.", "Totals at the end of the last completed day; the chip shows that day's gain.", LAST_DAY, "The four numbers to quote."),
    "youtube views": I("Total YouTube views of all uploads of the period's songs: videos, lyric videos, audio uploads and Shorts.", "Sum of each upload's latest view count from the YouTube Data API.", "Every 8 hours in an upload's first 7 days, then once a day.", "YouTube is where most Indian film music is first heard.", "sync_youtube.py → snapshot → song_daily.yt_views."),
    "audio streams": I("Spotify + YouTube Music + JioSaavn plays of the period's songs.", "Sum of each linked track's latest play count. Counted apart from YouTube views (a stream and a video view are different things).", DAILY, "Measures real listening on audio services.", "Spotify from the public track page, YouTube Music and JioSaavn from their track data."),
    "songs released": I("Songs first uploaded on the labels' YouTube channels in the period.", "One song = its uploads grouped together. 'Confirmed new on Spotify' = released on Spotify at most 30 days before its first YouTube upload.", "New songs appear within hours.", "The release pipeline of all seven labels. " + PERIOD),
    "ratio": I("One platform's number per 100 of another's (Spotify per 100 YouTube views by default).", "Total of the first ÷ total of the second × 100. Pick any two platforms or all audio streams; the choice is kept in this browser.", LAST_DAY, "Shows how audio listening converts relative to video reach."),
    "audio streams by platform": I("Each audio platform's total plays, the last day's gain and its share of all audio streams.", "Share = the platform's plays ÷ (Spotify + YouTube Music + JioSaavn) plays. 'On N songs' = songs linked there.", LAST_DAY, "Which audio platform carries the songs."),
    "youtube views by label": I("Each label's part of the period's YouTube views.", "Sum of the label's songs' views ÷ all views. Switch Donut / Bars / Stacked bar.", LAST_DAY, "Who leads on video reach."),
    "daily growth": I("Views and streams gained each day over the last 30 days.", "Each day's gain = sum of items' readings minus their previous readings. Line or Columns; audio as stacked columns (Spotify + YouTube Music + JioSaavn).", "Daily.", "Shows momentum, not just size."),
    "youtube views gained": I("YouTube views gained per day.", "Sum over all uploads of (today's reading − previous reading).", "Daily; the newest day is complete only after the day ends.", "Momentum of video reach.", "song_daily."),
    "audio streams gained": I("Audio streams gained per day, by platform.", "Sum over linked tracks of (reading − previous reading). Spotify is often read a day late, so the next day carries it.", "Daily.", "Momentum of listening."),
    "top songs": I("The period's biggest songs.", "Ranked by total YouTube views, or by audio streams with the switch.", LAST_DAY, "The current hits."),
    "fastest growing": I("Songs that gained the most on the last completed day.", "Ranked by views (or streams) gained that day.", LAST_DAY, "Catches songs taking off before they top the totals.", "Needs two days of readings."),
    "highlights": I("Four quick facts: hit songs, songs growing, top label and new releases.", "Hit = 1M+ YouTube views or 1M+ audio streams. Growing = gained views or streams on the last day. New release = on Spotify at most 30 days before its first YouTube upload.", LAST_DAY, "A summary in one glance."),
    "hit songs": I("Songs with 1 million or more YouTube views or 1 million or more audio streams.", "Counted on the latest totals.", LAST_DAY, "A simple hit threshold."),
    "songs growing": I("Songs that gained views or streams on the last completed day.", "Any positive gain on any platform.", LAST_DAY, "How much of the catalogue is still active."),
    "top label": I("The label with the most YouTube views among the period's songs.", "Ranked by total YouTube views; its share and song count are shown.", LAST_DAY, "Market leader on video."),
    "new releases": I("Songs confirmed as new releases on Spotify.", "Released on Spotify at most 30 days before the first YouTube upload.", "Updated when Spotify links are made.", "Separates genuinely new songs from re-uploads of old ones."),
    "where the songs are": I("The share of the period's songs found on each platform.", "Songs linked on the platform ÷ all period songs.", "Updated as links are found.", "Coverage: a song not found on a platform has no readings there."),
    "most used in youtube shorts": I("Songs whose official audio is used in the most YouTube Shorts.", "From each song's 'Shorts using this sound' page. Only sounds owned by the labels' channels count; re-uploads and art tracks don't. YouTube rounds counts above 1,000.", "Daily.", "Shorts usage is an early sign of a trend.", "shorts_use.py → sound_use / sound_short → song_daily.yt_shorts."),
    "most used in instagram reels": I("Songs whose official music-library audio is used in the most Instagram Reels.", "From the public audio page (e.g. '80K reels'). Only music-library audio whose title matches the song; 'Original audio' is not counted. Audio links are added by hand on the song.", "Daily.", "Reels drive discovery on Instagram.", "instagram_audio.py, headless browser, one page every 12 s."),
    "labels": I("Each label's songs, new releases, hits, YouTube views and audio streams.", "Sums over the label's period songs. Click a label to see its songs.", LAST_DAY, "Compare the labels side by side."),
    "songs by label and channel": I("Songs per label and per YouTube channel.", "Each song belongs to the label of the channel of its first upload.", "Updated as songs are found.", "Shows which channels release most."),
    "top by youtube views": I("The 10 songs with the most YouTube views.", "Total views of all their uploads.", LAST_DAY, "The current biggest songs."),
    "biggest youtube gainers": I("Songs that gained the most YouTube views on the last completed day.", "View gain that day.", LAST_DAY, "Momentum leaders."),
    "songs released each day": I("New songs per day (by first YouTube upload).", "Count of songs whose first upload falls on that day.", "Updated within hours.", "Release rhythm of the labels."),

    /* ---------- management ---------- */
    "where this week's spotify plays came from": I("How this week's Spotify plays gained split by label, language and genre.", "Sum of the week's Spotify gain per group ÷ the week's total gain. Donut / Bars / Stacked bar.", "The last 7 completed days.", "Shows which segments drive listening."),
    "by label": I("Split by label.", "Sum per label ÷ total.", "As the page's period.", "Label comparison."),
    "by language": I("Split by the song's language.", "Language comes from the description credits, YouTube audio language and title rules.", "As the page's period.", "Which languages grow."),
    "by genre": I("Split by genre.", "'Our category' (one per song, rule-based) by default, or Apple Music's genre (44% of songs). Several genres: split equally, primary only or full credit.", "As the page's period.", "Genre trends."),
    "top songs and releases": I("This week's top songs and top new releases (last 30 days).", "Ranked by Spotify plays gained in the week.", "The last 7 completed days.", "What is growing now."),
    "top new releases": I("Songs released in the last 30 days, ranked by this week's Spotify plays gained.", "Release = first YouTube upload.", "The last 7 completed days.", "New songs that are working."),
    "artists and labels": I("Top artists and labels this week.", "Artists: Spotify artist credits, else the singers; full credit to each artist on a song.", "The last 7 completed days.", "Who is driving growth."),
    "top artists this week": I("Artists ranked by Spotify plays gained this week.", "Each artist gets the full gain of every song they are credited on.", "The last 7 completed days.", "Artist momentum."),
    "top labels": I("Labels ranked by Spotify plays gained this week.", "Sum over the label's songs.", "The last 7 completed days.", "Label momentum."),
    "languages": I("Languages ranked by this week's Spotify plays gained, with a 4-week trend of the top 6.", "Sum over the language's songs.", "Daily.", "Language trends."),
    "by language, this week": I("Spotify plays gained this week per language.", "Sum per language.", "The last 7 completed days.", "Language comparison."),
    "daily, last 4 weeks": I("Daily Spotify plays gained of the top 6 languages over 4 weeks.", "Daily gain per language.", "Daily.", "Trend, not a snapshot."),
    "genre": I("Genre split.", "Our category or Apple Music genre (switch).", "As the page's period.", "Genre trends."),
    "our category": I("Our own one-per-song category from rules (e.g. romantic, devotional, party).", "Rule-based from title, credits and metadata, no AI.", "Rebuilt each songs run.", "Covers every song, unlike Apple genres."),
    "apple music genre": I("Apple Music's genre for the song.", "From the iTunes Search API; about 44% of songs, and it sometimes mixes in languages.", "When Apple data is fetched.", "An outside view of genre."),
    "split equally": I("A song with several genres gives each an equal part.", "Gain ÷ number of genres.", "", "Recommended: totals still add up."),
    "primary only": I("A song counts only for its first genre.", "Full gain to the first genre.", "", "Simplest view."),
    "full credit": I("A song counts fully for every genre it has.", "Full gain to each genre; totals can exceed 100%.", "", "Shows reach per genre."),
    "spotify plays this week": I("Spotify plays gained in the last 7 completed days.", GROWTH, "Daily.", "The ranking measure of this page."),
    "top artist": I("The artist with the most Spotify plays gained this week.", "Full credit per song.", "The last 7 completed days.", ""),
    "top language": I("The language with the most Spotify plays gained this week.", "Sum per language.", "The last 7 completed days.", ""),

    /* ---------- market share ---------- */
    "share of youtube views": I("Each label's share of the YouTube views its songs gained in their first N days.", "Σ label's songs' views at day N ÷ Σ all songs' views at day N. Bars / Donut / Stacked bar.", "Daily.", "Fair label comparison at equal song age."),
    "share of audio streams": I("Each label's share of audio streams gained in the songs' first N days.", "Same as YouTube, on Spotify + YouTube Music + JioSaavn. The number of songs each share rests on is shown (Spotify coverage differs by label).", "Daily.", "Fair label comparison on listening."),
    "share by platform": I("Label shares on each platform separately.", "Same formula per platform.", "Daily.", "A label can lead on one platform and trail on another."),
    "average song by day of life": I("How an average song of each label grows day by day after release.", "Mean of P(song, day n) over the label's songs.", "Daily.", "Shows launch strength and staying power."),
    "songs measured": I("Songs that count towards the shares.", "Songs that reached day N (final) or are still before it (provisional), and were read from their start.", "Daily.", "Shows how much data each share rests on."),
    "leader, youtube views": I("The label with the biggest share of YouTube views.", "See Share of YouTube views.", "Daily.", ""),
    "leader, audio streams": I("The label with the biggest share of audio streams.", "See Share of audio streams.", "Daily.", ""),
    "save as default": I("Makes the chosen number of days the default for everyone.", "Saved to the server's config (market_share.days).", "At once.", ""),

    /* ---------- reports and daily data ---------- */
    "labels ranked": I("Labels ranked over the chosen month.", GROWTH + " Rank by any measure.", "Daily; ⏳ on days still coming in.", "Monthly league table of labels."),
    "day by day": I("Each day's figures in a table.", "One row per day.", "Daily.", "The detail behind the charts."),
    "heatmap: labels by day": I("A grid of labels × days, darker = more of the chosen measure.", "Colour on a square-root scale so small values stay visible; hover a cell for the figure.", "Daily.", "Spot which label spiked on which day."),
    "songs: two days": I("Compare every song's numbers between two days.", "Reading on the later day minus the earlier day.", "Daily.", "Quick 'what changed' report."),
    "labels: daily": I("A month day by day for each label.", GROWTH, "Daily.", "Daily label performance."),
    "labels: monthly": I("Completed months ranked by label.", "Sum of daily growth per month (exact). Months before readings began (late October) show releases only.", "Monthly.", "Month-on-month comparison."),
    "spotify read": I("Share of Spotify tracks that had their count read that day.", "Tracks with a count that day ÷ tracks with a count that month.", "Daily.", "Under 90% means the day's Spotify gain is not complete yet (⏳)."),
    "cross-platform comparison": I("The song's numbers on every platform side by side.", "Latest reading per platform.", DAILY, "See where a song performs best."),
    "daily gain by platform": I("What the song gained each day on each platform.", "Reading − previous reading.", "Daily.", "Momentum per platform."),
    "total by day": I("The song's running total each day.", "The reading at the end of each day.", "Daily.", "Lifetime curve."),
    "gain each day": I("Day-by-day increase.", "Reading − previous reading.", "Daily.", "Shows peaks and slowdowns."),
    "shorts and reels made with the official audio": I("How many YouTube Shorts and Instagram Reels use this song's official audio.", "From the public sound/audio pages; only the labels' own sounds and matching music-library audio.", "Daily.", "Short-video usage drives discovery."),
    "youtube shorts made with the official audio": I("How many YouTube Shorts use this song's official sound.", "From YouTube's public 'Shorts using this sound' page of the song's uploads on the label channels; re-uploads are not counted. YouTube rounds counts above 1,000.", "Daily.", "Shorts usage is an early sign of a trend."),
    "day": I("Group by day.", "", "", ""), "week": I("Group by week (Monday–Sunday).", "Sum of daily gains.", "", ""), "month": I("Group by calendar month.", "Sum of daily gains.", "", ""), "year": I("Group by year.", "Sum of daily gains.", "", ""),

    /* ---------- columns ---------- */
    "song": I("The song: one name for all its YouTube uploads and its tracks elsewhere.", "Grouped from the labels' uploads by title, film and singers.", "", "Click to open its details."),
    "label": I("The record label (from the YouTube channel of the song's first upload).", "Each channel is assigned a label in the settings.", "", ""),
    "views": I("YouTube views.", "Latest view count.", "Daily (every 8 h in the first week).", ""),
    "likes": I("YouTube likes.", "Latest like count.", "With views.", "Engagement."),
    "comments": I("YouTube comments.", "Latest comment count.", "With views.", "Engagement."),
    "likes / k views": I("Likes per 1,000 views.", "Likes ÷ views × 1,000.", "Daily.", "Engagement independent of size."),
    "spotify": I("Spotify plays.", "From the song's public Spotify track page. Spotify hides counts under 1,000.", DAILY, "Main audio-listening measure."),
    "yt music": I("YouTube Music plays of the song's audio track.", "From YouTube Music's track data.", DAILY, ""),
    "youtube music": I("YouTube Music plays of the song's audio track.", "From YouTube Music's track data.", DAILY, ""),
    "jiosaavn": I("JioSaavn plays.", "From JioSaavn's track data.", DAILY, "Big in India."),
    "streams": I("Audio streams: Spotify + YouTube Music + JioSaavn.", "Sum of the linked tracks' plays.", DAILY, ""),
    "streams gained": I("Audio streams gained on the last day.", "Reading − previous reading.", LAST_DAY, ""),
    "views gained": I("YouTube views gained.", "Reading − previous reading.", LAST_DAY, ""),
    "gained": I("Increase on the day shown.", "Reading − previous reading.", LAST_DAY, ""),
    "gain last day": I("Increase on the last completed day.", "Reading − previous reading.", LAST_DAY, ""),
    "gain days": I("Increase over the last 7 days.", "Sum of the 7 daily gains.", "Daily.", ""),
    "share": I("Part of the total.", "This row's value ÷ the total of all rows.", "", ""),
    "total": I("Total at the end of the period.", "Latest reading (or sum of rows).", "", ""),
    "new": I("New in the period / on the day.", "Songs first found (or first linked) then.", "", ""),
    "hits": I("Songs with 1M+ YouTube views or 1M+ audio streams.", "", "", ""),
    "status": I("Where the song stands on Spotify, or the state of a job.", "Song: new release (on Spotify at most 30 days before the first YouTube upload), older release, or not on Spotify. Job: ok, partial, paused, cancelled or failed.", "", ""),
    "first upload": I("The day the song's first upload appeared on a label channel.", "Earliest YouTube publish time of its uploads.", "", "Day 1 of the song's life."),
    "released": I("Release date.", "Spotify's release date when linked, else the earliest reliable platform date.", "", ""),
    "first seen": I("When Market Lens first stored it.", "Server time (UTC).", "", "Shows discovery speed."),
    "last read": I("When it was last read on this platform.", "Time of the latest reading (shown in IST on Missed readings).", "", ""),
    "hours since": I("Hours since the last reading.", "Now − last read.", "Live.", "Over 24 h = missed."),
    "coverage": I("Share of songs found on the platform.", "Songs linked there ÷ songs found by then.", "Daily.", "A song not linked has no readings there."),
    "time to link": I("How long after the first YouTube upload the song was found on this platform.", "Median days from the first upload to the first link, for songs first uploaded in the range (older backfilled songs would distort it).", "Daily.", "Speed of matching."),
    "within a day": I("Share of YouTube's new songs found on the platform by the next day.", "Linked by the day after it was found ÷ songs found that day. Shown '…' until a day has passed.", "Daily.", "Phase 0 speed target."),
    "within a week": I("Share of YouTube's new songs found on the platform within 7 days.", "Linked within 7 days ÷ songs found that day. '…' until 7 days have passed.", "Daily.", ""),
    "found so far": I("Share of the day's new songs found on the platform up to now.", "Linked now ÷ songs found that day.", "Live.", ""),
    "on it now": I("Songs linked on this platform now.", "Count of songs with a link.", "Live.", ""),
    "yesterday": I("Value for yesterday (the last completed day).", "", "", ""),
    "today so far": I("Value for today up to now; still growing.", "", "Live.", ""),
    "-day avg": I("Average over the last 7 completed days.", "Sum ÷ 7.", "Daily.", "Smooths out busy and quiet days."),
    "in review": I("Spotify matches waiting for a person in Review.", "Not counted as linked until approved.", "", ""),
    "uploads": I("YouTube uploads stored.", "Videos, lyric, audio, Shorts and teasers.", "", ""),
    "yt uploads": I("YouTube uploads of the song.", "All its uploads grouped together.", "", ""),
    "uploaded that day": I("Songs whose first YouTube upload is that day.", "Usually the same as found; a backfill puts older uploads on the day they were found.", "", ""),
    "isrc": I("International Standard Recording Code: one ID per recording.", "From Spotify (or other platforms).", "", "Used to find the same recording on Deezer and others."),
    "deezer": I("Deezer rank of the track.", "Looked up by ISRC; Deezer gives a popularity rank, not plays.", DAILY, ""),
    "apple music": I("Apple Music track details (release date, genre).", "From the iTunes Search API.", "", "Metadata only; Apple gives no play counts."),
    "musicbrainz": I("Open music database entry (release dates, credits).", "MusicBrainz lookup.", "", "Metadata only."),
    "instagram reels": I("Reels made with the song's official audio.", "From the public audio page.", "Daily.", ""),
    "youtube shorts": I("Shorts made with the song's official sound.", "From 'Shorts using this sound'; YouTube rounds above 1,000.", "Daily.", ""),
    "score": I("Match score between the YouTube song and the candidate track (0–1).", "Title 0.55, artist 0.15, film 0.10, duration 0.10, date 0.10. Automatic at ≥ 0.75 with title ≥ 0.85; Review at ≥ 0.55.", "", "Rule-based, no AI."),
    "decision": I("What a person decided in Review.", "Approve, reject or merge; every decision can be undone exactly.", "", ""),
    "who": I("Who made the decision (the login used).", "", "", "Accountability."),

    /* ---------- daily additions ---------- */
    "new songs per day": I("Songs first stored each day, and songs first uploaded that day.", "Day = server day (UTC) of first_seen. More than 50 songs found above uploads = 'backfill' (e.g. a new label added).", "Live.", "Discovery speed."),
    "links made per day, by platform": I("Songs first linked on each platform each day.", "One count per song and platform, on its first link. Linked = automatic, approved or same song.", "Live.", "Matching speed per platform."),
    "songs found on each platform": I("Per platform: songs found each day, totals, coverage and time to link.", "One platform / Compare / Head to head / vs YouTube modes.", "Live.", "Shows where matching lags."),
    "one platform": I("One platform in detail.", "Yesterday, 7-day average, range total, coverage, time to link, daily bars.", "", ""),
    "compare": I("Several platforms on one chart.", "New per day, Total so far or Coverage % (linked by then ÷ found by then).", "", ""),
    "head to head": I("Two platforms against each other.", "Overlap: only A, both, only B, neither; '% of A also on B'.", "", "Find songs present on one but missing on the other."),
    "vs youtube": I("Of the songs YouTube gave us each day, the share each platform has found.", "Found so far / within a day / within a week; a wait not passed yet shows '…'.", "", ""),
    "new per day": I("Songs first linked that day.", "", "", ""),
    "total so far": I("Songs linked by the end of each day.", "Running total.", "", ""),
    "coverage %": I("Linked by then ÷ songs found by then.", "", "", ""),
    "links made that day": I("Per platform, songs first linked that day.", "", "", ""),
    "songs found that day, on each platform now": I("For the songs found each day, how many are on each platform today.", "Coverage so far, by day of discovery.", "", ""),

    /* ---------- missed readings ---------- */
    "page readers": I("Health of the readers that take numbers from public pages (Spotify, Shorts, Instagram, JioSaavn).", "Each value has several fallback ways to be read; the card shows which way worked today. If a page changes, a fallback takes over; if none works, items are not marked read and are retried.", "Live.", "A changed page can never silently turn into zeros.", "lens/heal.py; extract_stat, reading_suspect, data/heal_samples/."),
    "reading jobs: last run": I("Each reading job's last run and error.", "From the job log.", "Live.", ""),
    "not read within 24 hours": I("Items not read in the last 24 hours.", "Per platform and item; Retry now starts the job, Mark unavailable holds it off for 7 days.", "Live.", "Target: at least 99% captured."),
    "could not be read": I("Items the jobs found gone (private videos, removed pages) and manual marks.", "The jobs keep asking daily; readings resume if they come back.", "Last 7 days.", ""),
    "retry all now": I("Starts every platform's reading job now.", "", "", ""),
    "learned marker": I("The page marker a reader learned after it worked on 3 items.", "", "", ""),
    "held by guard": I("Readings held back because they looked wrong (e.g. too many hidden counts).", "Those items are read again.", "", "Protects the data."),
    "suspicious": I("Values that fell >10% or jumped >30× and were not stored.", "Kept in a quarantine table.", "", ""),

    /* ---------- developer ---------- */
    "alerts": I("Open problems: failed jobs, blocked APIs, stuck readers, long pauses.", "Raised by the jobs and the health watcher; closed when the condition clears.", "Live.", ""),
    "job control": I("Pause or resume jobs.", "Pause: running jobs finish, nothing new starts. Pause now: they stop at the next safe point, data kept. Always ends by itself.", "", "Used for deploys and maintenance."),
    "pause": I("Running jobs finish; nothing new starts.", "", "", ""),
    "pause now": I("Running jobs stop at their next safe point (between songs or pages); data read so far is kept.", "", "", ""),
    "resume": I("Ends the pause; due work continues straight away.", "", "", ""),
    "data freshness": I("How recent each platform's readings are.", "Age of the latest reading per platform.", "Live.", ""),
    "now": I("What is running at this moment on each lane.", "API lane and browser lane run in parallel.", "Live.", ""),
    "youtube quota": I("YouTube API units used today, per key.", "8 keys, one per Google project (~80,000 units a day). Reads cost 1 unit per 50 videos; searches 100.", "Resets at midnight Pacific time.", ""),
    "spotify api quota": I("Spotify API calls in the last 24 hours, per account.", "At most 700 per account per rolling 24 h on the server, one call a second. Only used to find IDs.", "Rolling.", "Staying below the limit avoids 24-hour blocks."),
    "storage": I("Database and archive sizes.", "", "Live.", ""),
    "server": I("CPU, memory, disk and uptime of the server.", "", "Live.", ""),
    "disk use": I("Disk space per folder.", "", "Live.", ""),
    "song ids found per platform": I("Share of songs with an ID on each platform.", "Linked songs ÷ period songs.", "Live.", "Phase 0 target: ≥ 90% matched."),
    "daily readings captured": I("Share of due readings actually taken each day.", "Items read ÷ items due.", "Daily.", "Phase 0 target: ≥ 99%."),
    "backlogs": I("Work still waiting: songs to search, tracks to read, items in Review.", "", "Live.", ""),
    "jobs, last 24 hours": I("Runs per job in the last 24 hours and their results.", "", "Live.", ""),
    "items read per day": I("Readings taken per day by source.", "", "Daily.", ""),
    "youtube keys": I("Each YouTube key's quota state.", "A key out of quota is set aside until midnight Pacific.", "Live.", ""),
    "recent failures": I("The latest failed runs and their errors.", "", "Live.", ""),
    "last days, hourly": I("Server load per hour over the last 7 days.", "", "Hourly.", ""),

    /* ---------- crawl stats ---------- */
    "items read by source": I("Items read per source over time.", "", "Per run.", ""),
    "run health": I("Share of runs that ended ok, per source.", "", "Per run.", ""),
    "by day": I("Per day.", "", "", ""),
    "readings captured": I("Share of due items read, per source and day.", "Green at 99%+, amber under 90%.", "Daily.", ""),
    "failures by type": I("Failures grouped by cause (timeout, block, page change…).", "", "Per run.", ""),
    "api health checks": I("Regular probes of each API: ok or blocked, and when it may be tried again.", "", "Every few minutes.", ""),
    "failure log": I("Every failure with its message.", "", "Live.", ""),
    "yield": I("Found per item tried.", "Found ÷ tried.", "", "A falling yield can mean a source changed."),

    /* ---------- runs, schedule, outputs ---------- */
    "run a job": I("Start a job by hand.", "Refused while that job is paused or already running.", "", ""),
    "live console": I("The running job's log, line by line.", "", "Live.", ""),
    "history": I("Every past run with status, duration and note.", "", "", ""),
    "youtube: new videos": I("How often new uploads and fresh videos are read.", "Videos in their first 7 days every N hours, then daily.", "", "More often = fresher, more quota."),
    "jiosaavn plays": I("JioSaavn plays and how often they are read.", "", "Daily.", ""),
    "what this costs a day": I("Estimated API units and calls a day for the chosen schedule.", "", "", "Stays within quota."),
    "every source": I("Each source's schedule.", "", "", ""),
    "datasets": I("Datasets you can view and download.", "Downloads carry no IDs or links (ISRC and UPC stay).", "Always current.", ""),
    "archived csvs": I("Older CSV files kept from before on-the-fly downloads.", "", "", ""),

    /* ---------- sources, settings ---------- */
    "spotify web api": I("Spotify's API, used only to find a song's track ID.", "After the ID is known, everything (plays, date, album) is read from the public track page.", "", "Keeps API use small and avoids blocks."),
    "youtube data api v3": I("YouTube's official API: uploads, views, likes, comments.", "", "", ""),
    "coverage of songs": I("How many songs each source covers.", "", "", ""),
    "not available from the apis": I("Data the sources do not give (e.g. Spotify genres in development mode, Apple play counts).", "", "", "Explains known gaps."),
    "all data sources": I("Every API and public page used.", "", "", ""),
    "discovery": I("The period start and how songs are found.", "From 1 July 2026, no end date.", "", ""),
    "merge rules": I("Rules for merging duplicate songs.", "Name similarity, length difference, days apart, minimum words.", "Each songs run.", "Merges are logged and can be undone."),
    "api health watcher": I("Regular probe of each API that raises an alert when one is blocked.", "", "", ""),
    "seed artists": I("Artists used to start Spotify artist-first discovery.", "", "", ""),

    /* ---------- top bar ---------- */
    "include older songs": I("Also show songs released before the period.", "Normally hidden: they are re-uploads of earlier releases and are not crawled.", "", ""),
    "run pipeline": I("Runs YouTube sync, songs, YouTube Music, JioSaavn and the daily roll-up now.", "", "", ""),
    "client view": I("See the Console exactly as the client login sees it.", "", "", ""),
    "refresh": I("Load the latest figures.", "", "", ""),
    "line": I("Show as a line chart.", "", "", "Best for trends."), "columns": I("Show as columns.", "", "", "Best for comparing days."),
    "donut": I("Show as a donut: parts of a whole.", "", "", ""), "bars": I("Show as ranked bars.", "", "", "Easiest to compare."), "stacked bar": I("Show as one bar split into parts.", "", "", ""),
    "stacked columns": I("Daily columns split by platform; the column height is the day's total.", "", "", ""),
  };

  INFO["measure each song at day"] = I("N: every song is measured at the same age, its day N (day 1 = its first YouTube upload), so old and new releases compare fairly.",
    "The choices are built from how many days of daily readings we have (shown below the buttons) plus 7/14/21/30… up to that history. Each shows how many songs are final at that day: songs that have lived N days and were read by their day N. ★ marks the day with the most measurable songs. Or type any number from 1 to 365.",
    "Recounted every time the page opens; the history grows by a day each day.",
    "A short N measures many songs but only their launch; a long N shows staying power but on fewer songs until the history builds up.",
    "market_share._presets(): H = last completed day − first YouTube reading + 1; Spotify history added as its own choice; best = most final songs.");
  INFO["sh-preset"] = I("Measure every song at this day of its life.", "Final = lived this many days and read by then; provisional = younger songs, shown with their latest value.", "Recounted on every page open.", "Pick the day with enough final songs for a fair share; ★ has the most.");
  INFO["sh-custom"] = I("Type your own day N (1–365).", "The page recounts the songs measurable at that day and recalculates every share.", "At once.", "For any comparison window you need, e.g. 10 or 45 days.");
  INFO["client access"] = I("Who used the client logins today: requests, songs opened, browsers signed in, IP addresses, last seen and blocks; on the right the latest blocked attempts.",
    "Every data request of a client login is counted. Blocks: bots or scripts, requests without the Console's browser session, too fast, over the daily caps, wrong passwords and lockouts.",
    "Live; counts reset at midnight UTC.", "Spots misuse early, e.g. a shared password (several IPs, many browsers) or bulk copying (blocks).",
    "lens/shield.py → data/shield.db (shield_access, shield_session, shield_event); settings config.json → shield.");
  INFO["ca-curve"] = I("Their songs' total YouTube views (left axis) and audio streams (right axis) at the end of each day.", "Today's total minus the growth that came after each day; Spotify + YouTube Music + JioSaavn for audio.", "Daily (last completed day).", "Shows whether their songs are still climbing or levelling off.");
  Object.assign(INFO, {
    "release timeline": I("Each of their songs on its release day (first YouTube upload); bigger dot = more YouTube growth in the period, colour = label.", "Hover a dot for the song's growth; click it to open the song.", "Updates with new releases.", "Shows how busy they are and which release carried them."),
    "best song on youtube": I("The song that brought them the most YouTube views in the period, and its share of their YouTube growth.", "", "Daily.", "A high share means one song carries them."),
    "best song in audio": I("The song with the most audio streams gained (Spotify + YouTube Music + JioSaavn) in the period.", "", "Daily.", ""),
    "biggest youtube day": I("The day their songs together gained the most YouTube views, and the audio peak.", "Sum of the songs' daily view gains.", "Daily.", "Usually a release or a viral moment."),
    "releases": I("How many of their songs are in the period, how many are new releases, and when the first and latest came out.", "", "Updates with new releases.", ""),
    "works with": I("Co-stars, singers and music directors on the same songs, with the number of songs together.", "From the YouTube credits.", "", "Click a co-star to open their page."),
    "growth so far": INFO["ca-curve"],
    "growth explorer": I("One chart, any view: pick the measure (YouTube, audio, Spotify, YouTube Music, JioSaavn, Shorts) and how to see it — daily columns, daily line, total so far, each top song as its own line, or audio split by platform.", "Daily figures are each day's growth of their songs; total so far is the running total.", "Daily (last completed day).", "Spot launches, spikes and which song drives them."),
    "songs compared": I("Each of their songs' growth on the chosen measure, as ranked bars, a donut or one stacked bar.", "Growth over the chosen days.", "Daily.", "Shows whether one song carries them or several."),
    "song × day": I("A grid of their top songs × the last 21 days; darker = more growth that day.", "Daily YouTube views or audio streams gained per song.", "Daily.", "When each song took off and how long it lasted."),
    "video vs audio": I("Each song as a bubble: further right = more YouTube views gained, higher = more audio streams gained; bubble size = total views so far; colour = label. The lines are the medians.", "Square-root scales so small songs stay visible. Click a bubble to open the song.", "Daily.", "Separates video-led songs from audio-led ones."),
    "where the growth comes from": I("Their YouTube growth split by label or by language.", "Sum of their songs' growth per label / language.", "Daily.", ""),
    "every platform, day by day": I("One card per metric: YouTube views, likes and comments, Spotify, YouTube Music and JioSaavn plays, Shorts made, the Deezer rank (and Instagram Reels for the developer) — growth in the period, total, rank among people with the same role, week-on-week change and a small daily chart.", "Sums over their songs; Deezer shows their best-ranked song each day (lower is better).", "Daily.", "Every platform at a glance; click a card to open it in the growth explorer."),
    "first days after release": I("How their songs grow in their first 30 days (day 1 = first YouTube upload): their average song against the average song of the period.", "Only songs read from their first 3 days count, so both lines use songs seen from the start; the legend shows how many.", "Daily.", "Shows launch strength: above the grey line = stronger starts than the market."),
    "engagement": I("Likes and comments per 1,000 YouTube views on their songs' uploads, against every song of the period.", "Totals so far: likes ÷ views × 1,000.", "Daily.", "Higher = a more engaged audience, not just reach."),
    "song × platform": I("Which song carries which platform: each cell is a song's share of their growth on that platform.", "Song's growth ÷ all their songs' growth on the platform.", "Daily.", "A song can lead on YouTube and another in audio."),
    "audio share over time": I("Each day's audio streams of their songs split by Spotify, YouTube Music and JioSaavn (100% columns).", "", "Daily.", "Shows whether the audio mix is shifting."),
    "deezer rank trend": I("Their best-ranked song on Deezer each day.", "Deezer gives a rank, not plays; lower is better.", "Daily.", ""),
    "nearest peers": I("The people just above and below them in the ranking for the same role (YouTube views gained).", "Click one to open their page.", "Daily.", "Puts the numbers in context."),
    "share of all growth": I("Their songs' share of the YouTube (and audio) growth of every song of the period.", "Their songs' growth ÷ all songs' growth.", "Daily.", ""),
    "audio vs video": I("Audio streams per 100 YouTube views for their songs, against the market's ratio.", "", "Daily.", "Above the market = their songs convert video reach into listening."),
  });
  /* page-specific meanings of common words */
  Object.assign(INFO, {
    "dashboard|songs": I("Top songs and fastest-growing songs, by YouTube views or audio streams.", "Switch with the buttons; views and streams are never mixed.", LAST_DAY, ""),
    "share|songs": I("Every song in the selection with its value at day N and whether it is final or provisional.", "Final = reached day N; provisional = latest completed day before N.", "Daily.", ""),
    "status|status": INFO["status"],
    "charts|status": I("Chart import status: token, last import, charts stored.", "", "", ""),
    "added|day by day": I("Every day's figures: found, uploaded, links per platform, Review.", "", "Live.", ""),
  });

  /* ---------- more pages (developer pages, Review, Explore, Outputs columns, controls) ---------- */
  Object.assign(INFO, {
    "prev": I("Previous page of rows.", "", "", ""), "next": I("Next page of rows.", "", "", ""), "first": I("First page of rows.", "", "", ""),
    "play": I("Play the song here: a docked YouTube or Spotify player opens.", "", "", "Listen without leaving the Console."),
    "save": I("Save these settings to the server.", "", "Applies from the next run.", ""), "save settings": I("Save all settings on this page to the server's config.", "", "Applies from the next run.", ""),
    "search": I("Search for it.", "", "", ""), "remove": I("Remove it (asks first).", "", "", ""), "open": I("Open the details.", "", "", ""),
    "start": I("Start the selected job now.", "Refused while that job is paused or already running.", "", ""), "cancel": I("Stop the running job at its next safe point; data read so far is kept.", "", "", ""),
    "change": I("Change from the previous reading or period.", "Newer value − older value.", "", ""),
    "gain": I("Increase over the period.", "Newer reading − older reading.", "", ""),
    "total songs": I("All songs of the period.", "Songs first uploaded on the labels' channels from 1 July 2026, not released before it.", "Updated within hours of a new upload.", ""),
    "fastest riser": I("The song that gained the most YouTube views on the last completed day.", "Largest one-day view gain. Click to play it.", LAST_DAY, ""),
    "songs with views": I("Songs past 1 million YouTube views.", "Total views of all their uploads ≥ 1M.", LAST_DAY, "A simple hit count."),
    "label / channel": I("The label and the YouTube channel of the song's first upload.", "", "", ""),
    "top youtube views": I("The 10 songs with the most YouTube views.", "Total views of all their uploads.", LAST_DAY, ""),
    "spotify plays": I("Spotify play counts.", "Read daily from each linked track's public Spotify page (no API). Spotify hides counts under 1,000.", DAILY, "The main measure of real listening."),
    "songs on spotify": I("Songs linked to a Spotify track.", "Automatic matches plus approved ones.", "As links are made.", "Only linked songs have Spotify plays."),
    "by category": I("Split by our own rule-based category.", "One category per song.", "", ""),
    "top artists": I("Artists ranked by Spotify plays gained this week.", "Full credit to every credited artist.", "The last 7 completed days.", ""),
    "artist": I("The credited artist (Spotify artist credits, else the singers from the YouTube description).", "", "", ""),
    "vs week before": I("Change against the week before.", "This week's gain ÷ last week's gain − 1. Shown only once last week was fully read.", "", ""),
    "youtube": I("YouTube: views, likes and comments of all the song's uploads.", "YouTube Data API, official counts.", "Every 8 h in a video's first 7 days, then daily.", ""),
    "language": I("The song's language.", "From the description credits, YouTube's audio language and title rules.", "", ""),
    "category": I("Our rule-based category (e.g. romantic, devotional, party).", "", "", ""),
    "days": I("N: how many days of each song's life are compared.", "Presets 7/14/30/60/90 or any number.", "", "Same age for every song = fair comparison."),
    "times music": I("Times Music, highlighted as the focus label.", "", "", ""),
    "share of youtube views by": I("Each label's share of YouTube views over the songs' first N days.", "Σ label ÷ Σ all.", "Daily.", ""),
    "share of audio streams by": I("Each label's share of audio streams over the songs' first N days.", "Σ label ÷ Σ all.", "Daily.", ""),
    "youtube music plays": I("YouTube Music plays of the song's audio track.", "From YouTube Music's track data.", DAILY, ""),
    "songs two days": INFO["songs: two days"], "labels daily": INFO["labels: daily"], "labels monthly": INFO["labels: monthly"],
    "songs in report": I("Songs counted in this report.", "Period songs matching the filters.", "", ""),
    "youtube likes": I("YouTube likes.", "Latest like count of all uploads.", "With views.", ""), "youtube comments": I("YouTube comments.", "Latest comment count of all uploads.", "With views.", ""),
    "deezer rank": I("Deezer's popularity rank for the track (higher = more popular).", "Looked up by ISRC at api.deezer.com; Deezer gives a rank, not plays.", DAILY, ""),
    "all platforms": I("Every platform together.", "", "", ""), "platforms compared": I("The platforms side by side.", "Totals, gains and songs per platform.", "Daily.", ""),
    "platform": I("The platform (YouTube, Spotify, YouTube Music, JioSaavn, Deezer…).", "", "", ""),
    "top song": I("The song with the most views or plays.", "", "", ""),
    "last days": I("The last N days.", "", "", ""),
    "videos": I("YouTube uploads grouped into this song (video, lyric, audio, Shorts, teaser).", "", "", ""),
    "views δ": I("YouTube views gained on the last day.", "", LAST_DAY, ""), "views": I("YouTube views.", "Latest view count.", "", ""),
    "paste session token": I("Spotify Charts needs a logged-in session token to download the charts.", "Paste it here; it is stored on the server only.", "", ""),
    "releases discovered within": I("Phase 0 target: at least 95% of releases discovered within 24 hours of upload.", "Songs first stored within 24 h of their first upload ÷ all songs.", "Daily.", ""),
    "songs matched to another platform": I("Phase 0 target: at least 90% of songs matched across platforms.", "Songs with a link on at least one other platform ÷ all songs.", "Live.", ""),
    "matches needing manual review": I("Phase 0 target: at most 10% of matches need a person.", "Matches sent to Review ÷ all matches.", "Live.", ""),
    "folder": I("A data folder on the server.", "", "", ""), "size": I("Size on disk.", "", "", ""), "share of disk": I("Part of the disk this uses.", "", "", ""),
    "songs with an id": I("Songs with a link on this platform.", "", "Live.", ""), "still without": I("Songs not yet found on this platform.", "They are searched again on their schedule.", "", ""),
    "today": I("So far today (server day, UTC).", "", "Live.", ""),
    "found today by route": I("Links found today, by the way they were found (label sweep, search, browser search…).", "", "Live.", ""),
    "linked items": I("Platform items linked to a song.", "", "", ""),
    "search missing spotify ids with account": I("Start a re-discovery run on Spotify account 2 for songs still without a Spotify link.", "At most one search per song per day across both accounts; within the account's daily limit.", "", ""),
    "backlog": I("Work still waiting.", "", "Live.", ""), "count": I("Number of items.", "", "", ""), "how it clears": I("Which job works this backlog down and how fast.", "", "", ""),
    "jobs, last hours": INFO["jobs, last 24 hours"], "items read per": INFO["items read per day"],
    "key": I("An API key (shown masked).", "", "", ""), "state": I("Current state.", "", "Live.", ""), "until": I("Set aside until this time.", "", "", ""),
    "snooze": I("Hide this alert for some hours; it comes back if the problem is still there.", "", "", ""),
    "success rate": I("Share of runs that ended ok.", "OK runs ÷ all runs in the period.", "", ""),
    "items read": I("Items read in the period.", "", "", ""), "api calls": I("API calls made.", "", "", ""), "links found": I("New links made.", "", "", ""),
    "busy time": I("Time the source's jobs were running.", "", "", ""), "crawl sources": I("Every crawl source with its runs, reads and failures.", "", "", ""),
    "apis": I("Every API with its calls, quota and failures.", "", "", ""), "keys and quota": I("API keys and their quota use.", "", "", ""),
    "source": I("The data source or job.", "", "", ""), "access": I("How the source is reached: API (with key), public API, or public page in a headless browser.", "", "", ""),
    "success": I("OK runs.", "", "", ""), "read": I("Items read.", "", "", ""), "found": I("Items found (linked or with a value).", "", "", ""), "not found": I("Items looked for but not found.", "", "", ""),
    "calls": I("API calls.", "", "", ""), "quota": I("Quota used.", "", "", ""), "avg time": I("Average run time.", "", "", ""),
    "last ok": I("Last run that ended ok.", "", "", ""), "last problem": I("Last failure or block.", "", "", ""), "by": I("Grouping.", "", "", ""),
    "problems": I("Failed or partial runs.", "", "", ""), "busy": I("Time spent running.", "", "", ""), "period": I("The time period.", "", "", ""),
    "type": I("The kind of item or failure.", "", "", ""), "last seen": I("Last time this happened.", "", "", ""), "example": I("An example message.", "", "", ""),
    "when": I("Time it happened.", "", "", ""), "run": I("The run number in the job log.", "", "", ""), "job": I("The job (see Runs for every job).", "", "", ""), "message": I("The message recorded.", "", "", ""),
    "api": I("The API.", "", "", ""), "checks": I("Health checks made.", "", "", ""), "blocked": I("Checks that found the API blocked.", "", "", ""),
    "last blocked": I("Last time the API was blocked.", "", "", ""), "retry at": I("When it may be tried again (Retry-After).", "", "", ""),
    "youtube key": I("One YouTube API key (one per Google Cloud project).", "", "", ""),
    "links made that": INFO["links made that day"], "new songs today": I("Songs first stored today (still growing).", "", "Live.", ""),
    "new songs yesterday": I("Songs first stored yesterday.", "", "", ""), "-day average": INFO["-day avg"], "day average": INFO["-day avg"],
    "new songs in days": I("Songs first stored in the chosen range.", "", "", ""), "new songs per": INFO["new songs per day"], "new per": INFO["new per day"],
    "new songs": I("Songs first stored that day.", "", "", ""), "uploaded that": INFO["uploaded that day"], "apple": INFO["apple music"],
    "instagram audio": I("Instagram audio pages linked to songs.", "Added by hand on the song.", "", ""), "instagram": I("Instagram Reels per audio.", "", "", ""),
    "retry now": I("Start this platform's reading job now.", "", "", ""), "reader": I("A page reader (Spotify plays, Shorts, Instagram, JioSaavn…).", "", "", ""),
    "state today": I("ok = the main way works; fallback = a backup way is in use (readings continue); held = readings held back by a guard.", "", "", ""),
    "found today": I("Values read today, by way of reading.", "", "", ""), "not readable": I("Pages read today where no way found the value; not marked read, retried.", "", "", ""),
    "page sample": I("A saved copy of a page that could not be read, for fixing the reader.", "One a day per reader, kept 7 days.", "", ""),
    "reading jobs last run": INFO["reading jobs: last run"], "not read within": INFO["not read within 24 hours"],
    "last run": I("The job's last run.", "", "", ""), "finished": I("When it finished.", "", "", ""), "error / stop reason": I("Why it failed or stopped.", "", "", ""),
    "item": I("The song's item on the platform.", "", "", ""), "mark unavailable": I("Hold this item off the missed list for 7 days (then it is checked again).", "", "", ""),
    "songs matched on spotify": I("Songs linked to a Spotify track.", "", "", ""), "spotify matches needing review": I("Spotify matches waiting for a decision.", "", "", ""),
    "videos grouped into songs": I("YouTube uploads that belong to a song.", "", "", ""), "approved / linked by you": I("Links a person approved or made.", "", "", ""),
    "spotify links to check": I("Spotify matches the rules were not sure about.", "Score 0.55–0.75, or title under 0.85, or several tracks for one song.", "", "Approve or reject each."),
    "duplicate songs": I("Songs that may be the same song.", "Found by the merge rules (name similarity, length, days apart).", "", "Merge them or mark 'not the same'."),
    "all spotify links": I("Every Spotify link with its state.", "", "", ""), "video matches to check": I("YouTube uploads that may belong to a song.", "", "", ""),
    "tracks without video": I("Spotify tracks with no YouTube song.", "", "", ""), "auto-matched": I("Links made automatically by the rules.", "Score ≥ 0.75 with title ≥ 0.85.", "", ""),
    "approved": I("Links approved by a person.", "", "", ""), "videos without track": I("Songs with no Spotify track yet.", "", "", ""),
    "decision history": I("Every Review decision with who and when; each can be undone exactly.", "", "", ""),
    "song on youtube": I("The YouTube song.", "", "", ""), "spotify candidate": I("The Spotify track proposed for it.", "", "", ""),
    "evidence": I("Why it scored as it did: title, artist, film, duration and date parts.", "", "", ""),
    "same song": I("Merge: these are the same song.", "", "", ""), "different": I("Not the same song; remembered so they are not proposed again.", "", "", ""),
    "started": I("When the run started.", "", "", ""), "took": I("How long it ran.", "", "", ""), "releases": I("Releases read.", "", "", ""), "tracks": I("Tracks read.", "", "", ""), "details": I("More about the run.", "", "", ""),
    "raw api responses": I("The raw replies from the APIs, as received (last 7 days; older ones archived).", "", "", "For checking exactly what a source said."),
    "song daily totals": I("One row per song per day: totals and gains on every platform.", "From song_daily.", "Daily.", "The main dataset for Power BI."),
    "api lane": I("The lane that runs API jobs (YouTube, Spotify search, JioSaavn, Deezer…).", "Runs beside the browser lane.", "Live.", ""),
    "browser lane": I("The lane that reads public pages in a headless browser (Spotify plays, browser search, Instagram).", "Runs beside the API lane.", "Live.", ""),
    "spotify links": I("Spotify ID finding (label sweep, search, browser search).", "", "", ""), "spotify india chart": INFO["spotify chart"],
    "daily cleanup": I("Backup, archiving and disk space, after 03:00 server time.", "Nothing is deleted.", "Daily.", ""),
    "youtube new videos": INFO["youtube: new videos"], "every hours": I("Read every N hours.", "", "", ""), "once a day": I("Read once a day.", "", "", ""),
    "what this costs a": INFO["what this costs a day"], "undo changes": I("Back to the saved schedule.", "", "", ""), "save schedule": I("Save the schedule; applies from the next run.", "", "", ""),
    "how often": I("How often it is read.", "", "", ""), "items": I("Items read each time.", "", "", ""), "reads a": I("Reads a day.", "", "", ""), "daily cost": I("API units or calls a day.", "", "", ""),
    "retry api status": I("Check every API's status again now.", "", "", ""), "api status": I("Each API's last checks.", "", "", ""),
    "http": I("The HTTP status the API returned.", "", "", ""), "result": I("What the check found.", "", "", ""),
    "youtube data api v": INFO["youtube data api v3"], "units today": I("YouTube quota units used today.", "", "", ""),
    "sync youtube now": I("Read the channels' new uploads now.", "", "", ""), "llm key": I("Key for the AI insights (live AI is switched off).", "", "", ""),
    "data coverage & gaps": I("What each source covers and what it cannot give.", "", "", ""), "not provided": I("What the source does not give.", "", "", ""),
    "workaround": I("How we get it anyway, or why we can't.", "", "", ""), "gives us": I("What the source gives.", "", "", ""), "does not give": I("What it cannot give.", "", "", ""),
    "match key": I("How songs are matched on this source (ISRC, title + artist…).", "", "", ""),
    "schedule": INFO["collection schedule"], "add channel": I("Add a YouTube channel to this label.", "", "", ""), "add label": I("Add a record label.", "", "", ""),
    "singers": I("Singers credited in the YouTube descriptions.", "", "", ""), "music director": I("The music director (composer).", "", "", ""),
    "lyricists": I("Lyricists credited.", "", "", ""), "cast / featuring": I("Actors and featured artists.", "", "", ""), "film": I("The film or album.", "", "", ""),
    "film director": I("The film's director.", "", "", ""), "occasion": I("Occasion rules (festival, wedding…).", "", "", ""),
    "youtube channel": I("The YouTube channel.", "", "", ""), "spotify artists": I("Artists as Spotify credits them.", "", "", ""),
    "name": I("The name.", "", "", ""), "avg views / song": I("Average YouTube views per song.", "Views ÷ songs.", "", ""),
    "views on": I("YouTube views on that day.", "", "", ""), "likes on": I("YouTube likes on that day.", "", "", ""), "comments on": I("YouTube comments on that day.", "", "", ""),
    "released that": I("Released that day.", "", "", ""), "upload": I("The YouTube upload.", "", "", ""), "channel": I("The YouTube channel.", "", "", ""),
    "days tracked": I("Days with readings.", "", "", ""), "change vs": INFO["change"] || I("Change.", "", "", ""),
    /* Outputs columns (CSV / Power BI) */
    "song_id": I("Internal song number (left out of CSV downloads).", "", "", ""), "label_channel": I("Label and channel of the first upload.", "", "", ""),
    "first_upload": I("Day of the song's first YouTube upload.", "", "", ""), "yt_views": I("YouTube views at the end of the day.", "", "", ""),
    "yt_likes": I("YouTube likes at the end of the day.", "", "", ""), "yt_comments": I("YouTube comments at the end of the day.", "", "", ""),
    "yt_uploads_read": I("Uploads read that day.", "", "", ""), "ytm_plays": I("YouTube Music plays.", "", "", ""), "sp_plays": I("Spotify plays.", "", "", ""),
    "js_plays": I("JioSaavn plays.", "", "", ""), "dz_rank": I("Deezer rank.", "", "", ""), "chart_rank": I("Position on the Spotify India chart.", "", "", ""),
    "yt_views_gain": I("YouTube views gained that day.", GROWTH, "", ""), "yt_likes_gain": I("Likes gained that day.", "", "", ""), "yt_comments_gain": I("Comments gained that day.", "", "", ""),
    "ytm_plays_gain": I("YouTube Music plays gained.", "", "", ""), "sp_plays_gain": I("Spotify plays gained.", "", "", ""), "js_plays_gain": I("JioSaavn plays gained.", "", "", ""),
    "yt_shorts": I("YouTube Shorts made with the official audio.", "", "", ""), "yt_shorts_gain": I("Shorts gained that day.", "", "", ""),
    "ig_reels": I("Instagram Reels made with the official audio.", "", "", ""), "ig_reels_gain": I("Reels gained that day.", "", "", ""),
  });

  const MONTHS = /\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|mon|tue|wed|thu|fri|sat|sun)\b/g;
  const norm = (s) => (s || "").toLowerCase().replace(/\([^)]*\)/g, " ").replace(/[✨📈↻▶■⏳🎵👁⇢←→‹›«»•·:✓✕#%↓↑δΔ+]/g, " ")
    .replace(MONTHS, " ").replace(/\b\d[\d,.]*\s*[kmbh]?\b/g, " ").replace(/\b\d+/g, " ").replace(/&amp;/g, "&")
    .replace(/(^|\s)[-–—]+(?=\s|$)/g, " ").replace(/(^|\s)[a-z](?=\s|$)/g, " ").replace(/\s+/g, " ").trim()
    .replace(/(\s+(on|in|of|the last day|the|this week|last|day))+$/, "").trim();
  // index every entry under its normalised key too, so a heading and its entry always meet
  Object.keys(INFO).forEach((k) => { const [pg, t] = k.includes("|") ? k.split("|") : [null, k]; const n = (pg ? pg + "|" : "") + norm(t);
    if (n && !INFO[n]) INFO[n] = INFO[k]; });
  Object.assign(INFO, {
    "by language,": INFO["by language, this week"], "daily, last weeks": INFO["daily, last 4 weeks"], "not read within hours": INFO["not read within 24 hours"],
    "once daycurrent": INFO["once a day"], "reads": INFO["reads a"], "average song by day of life youtube views, days": INFO["average song by day of life"],
    "dev|day by": I("Readings captured per day, by source.", "Items read ÷ items due.", "Daily.", "Target: 99%."),
  });
  const page = () => (location.hash.slice(1).split("?")[0]) || (document.body.classList.contains("client") ? "dashboard" : "overview");
  const isDev = () => { try { return typeof STATUS !== "undefined" && STATUS && STATUS.role === "developer" && !(typeof CLIENT_VIEW !== "undefined" && CLIENT_VIEW); } catch (e) { return false; } };
  const IG = /instagram|reels?\b/i;  // Instagram is never shown to client logins (user rule, 2026-11-12)
  const lookup = (text) => { let k = norm(text); if (!k) return null; if (!isDev() && IG.test(k)) return null;
    if (/^[a-z ]+ per [a-z ]+$/.test(k) && !INFO[k] && !INFO[page() + "|" + k]) k = "ratio";  // the ratio tile ("Spotify per 100 YouTube")
    if (!INFO[k] && !INFO[page() + "|" + k] && / change$/.test(k)) k = "change";
    if (!INFO[k] && !INFO[page() + "|" + k] && /^(views|likes|comments) on$/.test(k)) k = k.split(" ")[0];
    return INFO[page() + "|" + k] || INFO[k] || null; };
  const SEL_ALL = () => SEL;
  const clientText = (v) => isDev() ? v : v.split(/(?<=[.!?])\s+/).filter((x) => !IG.test(x)).join(" ")
    .replace(/ and (Instagram )?Reels/gi, "").replace(/\s*\/\s*Instagram Reels/gi, "");

  const SEL = "a.nav, .nav-sec, h1, h2, h3, h4, th, .kpi, .pf-card, .hl-list > li, button, .seg > *, label.pill, [data-info]";
  function keyOf(el) {
    if (el.dataset.info) return el.dataset.info;
    if (el.matches(".kpi")) return (el.querySelector(".label") || {}).textContent || "";
    if (el.matches(".pf-card")) return (el.querySelector(".pf-name") || {}).textContent || "";
    if (el.matches(".hl-list > li")) { const s = el.querySelector("span"); return s ? (s.childNodes[0] || {}).textContent || "" : ""; }
    const c = el.cloneNode(true);
    c.querySelectorAll(".nav-badge, .pill, svg, .tooltip, select, input, .muted, kbd").forEach((x) => x.remove());
    return c.textContent;
  }
  let ON = true;
  try { ON = localStorage.getItem("ml_info") !== "off"; } catch (e) {}

  function mark(root) {
    if (!ON) return;
    (root || document).querySelectorAll(SEL).forEach((el) => {
      if (el.closest(".tooltip, #info-pop, .gsearch-panel")) return;
      const hit = lookup(keyOf(el));
      el.classList.toggle("has-info", !!hit);
      if (hit && el.title && !el.dataset.infoTitle) { el.dataset.infoTitle = el.title; el.removeAttribute("title"); }
    });
  }
  let pending = null;
  const schedule = () => { if (pending) return; pending = setTimeout(() => { pending = null; mark(); }, 250); };

  const pop = document.createElement("div");
  pop.id = "info-pop"; pop.setAttribute("role", "tooltip"); pop.hidden = true;
  document.addEventListener("DOMContentLoaded", () => document.body.appendChild(pop));
  if (document.body) document.body.appendChild(pop);

  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const ROWK = { "What it is": 1, "How it's worked out": 2, "When it updates": 3, "Why it matters": 4, "Here": 1 };
  function render(el, e) {
    const title = (el.dataset.info ? (el.dataset.infoLabel || el.textContent) : keyOf(el)).replace(/\s+/g, " ").trim() || e.label || "";
    const rows = [["What it is", e.what], ["How it's worked out", e.how], ["When it updates", e.when], ["Why it matters", e.why]];
    if (isDev() && e.dev) rows.push(["Under the hood (developer)", e.dev]);
    if (el.dataset.infoTitle) rows.push(["Here", el.dataset.infoTitle]);
    if (el.dataset.tip) rows.unshift(["Here", el.dataset.tip]);
    pop.innerHTML = `<div class="ip-title">${esc(title.length > 60 ? title.slice(0, 57) + "…" : title)}</div>` +
      rows.map(([k, v]) => [k, v && clientText(v)]).filter(([, v]) => v).map(([k, v]) => `<div class="ip-row ${k.startsWith("Under the hood") ? "dev" : "k" + (ROWK[k] || 1)}"><div class="ip-k">${k.replace(" (developer)", "")}</div><div class="ip-v">${esc(v)}</div></div>`).join("");
  }
  function place(el) {
    const r = el.getBoundingClientRect(), w = Math.min(380, window.innerWidth - 24);
    pop.style.width = w + "px"; pop.hidden = false;
    const h = pop.offsetHeight;
    let top = r.bottom + 11, side = "below"; if (top + h > window.innerHeight - 8) { top = Math.max(8, r.top - h - 11); side = "above"; }
    const anchor = r.left + Math.min(28, r.width / 2);   // the arrow points at the start of the hovered item
    let left = Math.min(Math.max(8, anchor - 26), window.innerWidth - w - 8);
    pop.dataset.side = side; pop.style.setProperty("--ax", Math.max(16, Math.min(w - 16, anchor - left)) + "px");
    pop.style.top = top + "px"; pop.style.left = left + "px";
  }
  let timer = null, cur = null;
  const hide = () => { clearTimeout(timer); timer = null; cur = null; pop.hidden = true; };
  document.addEventListener("mouseover", (ev) => {
    if (!ON) return;
    const el = ev.target.closest && ev.target.closest(".has-info");
    if (el === cur) return;
    hide();
    if (!el) return;
    cur = el;
    timer = setTimeout(() => { const e = lookup(keyOf(el)); if (!e || cur !== el) return; render(el, e); place(el); }, 380);
  });
  document.addEventListener("mouseout", (ev) => { if (cur && !cur.contains(ev.relatedTarget)) hide(); });
  ["scroll", "click", "keydown"].forEach((t) => window.addEventListener(t, hide, true));
  // touch: a long press shows the panel
  let press = null;
  document.addEventListener("touchstart", (ev) => { const el = ev.target.closest && ev.target.closest(".has-info"); if (!el || !ON) return;
    press = setTimeout(() => { const e = lookup(keyOf(el)); if (e) { render(el, e); place(el); } }, 550); }, { passive: true });
  document.addEventListener("touchend", () => clearTimeout(press), { passive: true });

  function setOn(v) {
    ON = v; try { localStorage.setItem("ml_info", v ? "on" : "off"); } catch (e) {}
    document.body.classList.toggle("info-off", !v);
    const b = document.getElementById("info-toggle"); if (b) b.classList.toggle("on", v);
    if (v) mark(); else { hide(); document.querySelectorAll(".has-info").forEach((x) => x.classList.remove("has-info")); }
  }
  function start() {
    const b = document.getElementById("info-toggle");
    if (b) b.onclick = () => setOn(!ON);
    setOn(ON);
    new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
    window.addEventListener("hashchange", schedule);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start); else start();
  window.MLInfo = { INFO, lookup, mark, norm, keyOf, SEL };
})();
