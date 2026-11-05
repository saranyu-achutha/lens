#!/usr/bin/env bash
# LAPTOP: pull every Spotify release of the shortlisted labels from the period start on one account (default account 2),
# one call a second, then load it into the server and download the list.
#
#   ./spotify_list_laptop.sh [second|primary|primary,second]
#
# 1. pause the server jobs that use the same account (label sweep, re-discovery), so the two don't share its calls
# 2. ask the server which releases it has already read (not fetched again)
# 3. python3 -m lens.spotify_list: label search + track lists, stops at the first 429, continues next time
# 4. load the list into the server (matched and linked there without calls), add the calls to its counter
# 5. download the list (Spotify song ID, title, artists, label, album, date, YouTube song) to lens/data/
# 6. lift the pause (also if anything above fails)
set -uo pipefail
ACCTS=${1:-second}
REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
APP=$REPO/lens
KEY=~/SARANYU-KEY.pem
STATE=$APP/data/spotify_list_state.json
LOG=$APP/data/spotify_list.log
exec > >(tee -a "$LOG") 2>&1
echo "== $(date -u '+%F %T') UTC start, accounts: $ACCTS"
HOST=$(getent hosts demomusic.apisaranyu.in | awk '{print $1}')
SSH="ssh -o ConnectTimeout=20 -i $KEY ubuntu@$HOST"
srv_api() {  # POST JSON (on stdin) to the server Console as the developer
    printf '%s' "$2" | $SSH "cd lens/lens && U=\$(grep -E '^DEVELOPER_USERNAME=' .env | cut -d= -f2-); P=\$(grep -E '^DEVELOPER_PASSWORD=' .env | cut -d= -f2-);
          curl -s -m 15 -u \"\$U:\$P\" -H 'Content-Type: application/json' --data-binary @- localhost:3000$1"
}
JOBS='"spotify_label_sweep"'
[[ $ACCTS == *second* ]] && JOBS="$JOBS, \"spotify_second_account\""
[[ $ACCTS == *primary* ]] && JOBS="$JOBS, \"spotify_primary_rediscover\""
lift() { echo "resume: $(srv_api /api/jobs/resume '{"reason": "laptop Spotify list finished"}' | head -c 120)"; }
trap lift EXIT
echo "pause: $(srv_api /api/jobs/pause "{\"jobs\": [$JOBS], \"minutes\": 180, \"reason\": \"Spotify list from the laptop\"}" | head -c 160)"

$SSH "cd lens/lens && sqlite3 -readonly data/lens.db \"SELECT album_id FROM spotify_label_album WHERE fetched_at IS NOT NULL\"" > "$APP/data/spotify_known_albums.txt"
echo "server has read $(wc -l < "$APP/data/spotify_known_albums.txt") releases already"

(cd "$APP" && python3 -m lens.spotify_list --accounts "$ACCTS" --state "$STATE" --known "$APP/data/spotify_known_albums.txt") > "$APP/data/spotify_list_summary.json"
cat "$APP/data/spotify_list_summary.json"

# load into the server, count the calls, record a block
scp -q -i $KEY "$STATE" ubuntu@$HOST:/tmp/spotify_list_state.json
$SSH "cd lens/lens && python3 -m lens.spotify_labels import /tmp/spotify_list_state.json && rm -f /tmp/spotify_list_state.json"
python3 - "$APP/data/spotify_list_summary.json" > "$APP/data/spotify_list_server.sql" <<'EOF'
import json, sys
from datetime import datetime, timedelta, timezone
s = json.load(open(sys.argv[1]))
hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H")
for acct, n in s["calls_this_run"].items():
    if n:
        print(f"ATTACH 'data/spotify_calls.db' AS c; INSERT INTO c.call_hour VALUES ('{acct}', '{hour}', {n}) "
              f"ON CONFLICT(account, hour) DO UPDATE SET calls=calls+{n}; DETACH c;")
for acct, st in s["stops"].items():
    if st.get("retry_after"):
        until = (datetime.now(timezone.utc) + timedelta(seconds=int(st["retry_after"]))).isoformat()
        print(f"INSERT INTO state (key, value) VALUES ('spotify_{acct}_blocked_until', '{until}') "
              f"ON CONFLICT(key) DO UPDATE SET value=excluded.value;")
EOF
$SSH "cd lens/lens && sqlite3 data/lens.db" < "$APP/data/spotify_list_server.sql" && echo "server counter and block times updated"

# the server matches the imported track lists without calls (its worker, within a few minutes); wait for it
srv_api /api/jobs/resume '{"reason": "let the sweep match the imported lists"}' > /dev/null
for i in $(seq 1 40); do
    left=$($SSH "cd lens/lens && sqlite3 -readonly data/lens.db \"SELECT count(*) FROM spotify_label_album WHERE fetched_at IS NULL AND tracks_json IS NOT NULL\"")
    [[ "$left" == "0" ]] && break
    sleep 30
done
echo "imported releases still to match on the server: ${left:-?}"

OUT=$APP/data/spotify_label_songs_$(date -u +%F).csv
$SSH "cd lens/lens && python3 -c \"
from lens import config, db, exports
body, name = exports.download(db.connect(str(config.DB_FILE)), {'ds': 'spotify_label_songs'})
import sys; sys.stdout.buffer.write(body)\"" > "$OUT"
echo "list: $OUT ($(($(wc -l < "$OUT") - 1)) songs)"
echo "== $(date -u '+%F %T') UTC done"
