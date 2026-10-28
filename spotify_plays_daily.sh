#!/usr/bin/env bash
# LAPTOP: read today's Spotify play counts and load them into the server's Console.
#   1. the server lists the linked Spotify tracks of the song period
#   2. this machine renders their public track pages in headless Chrome (needs Chrome, ~40 min for ~2,500 tracks)
#   3. the day's result (lens/data/spotify_plays/<date>.json) is copied to the server and imported
# Safe to rerun: tracks already read today are skipped, and an import replaces that day's values.
#
#   ./spotify_plays_daily.sh            run now
#   crontab: 30 10 * * * /path/to/spotify_plays_daily.sh >> /path/to/lens/data/spotify_plays/daily.log 2>&1
set -euo pipefail

SERVER=${LENS_SERVER:-ubuntu@54.255.142.81}
KEY=${LENS_SSH_KEY:-$HOME/SARANYU-KEY.pem}
REMOTE_APP=/home/ubuntu/lens/lens
WORKERS=${SPOTIFY_PLAYS_TABS:-3}

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
APP=$REPO/lens
DAY=$(date +%F)
SSH=(ssh -i "$KEY" -o ConnectTimeout=20 "$SERVER")

say() { printf '\n== %s  %s\n' "$(date +%H:%M:%S)" "$*"; }

cd "$APP"
mkdir -p data/spotify_plays
say "Getting the track list from the server"
"${SSH[@]}" "cd $REMOTE_APP && python3 -m lens.spotify_plays export" > data/spotify_plays/ids.json
python3 -c "import json;print(len(json.load(open('data/spotify_plays/ids.json'))), 'tracks')"

say "Reading play counts (headless Chrome, $WORKERS tabs)"
nice -n 19 ionice -c3 python3 -u -m lens.spotify_plays collect data/spotify_plays/ids.json --workers "$WORKERS"

say "Uploading and importing on the server"
scp -q -i "$KEY" "data/spotify_plays/$DAY.json" "$SERVER:$REMOTE_APP/data/spotify_plays/$DAY.json" 2>/dev/null || {
    "${SSH[@]}" "mkdir -p $REMOTE_APP/data/spotify_plays"
    scp -q -i "$KEY" "data/spotify_plays/$DAY.json" "$SERVER:$REMOTE_APP/data/spotify_plays/$DAY.json"
}
"${SSH[@]}" "cd $REMOTE_APP && python3 -m lens.spotify_plays import data/spotify_plays/$DAY.json"
say "Done"
