#!/usr/bin/env bash
# LOCAL (laptop / dev machine): restart the Market Lens Console with the current code.
# No systemd: the Console runs as a background process, logging to lens/data/console.log.
#
#   ./deploy_local.sh                  restart with your working copy (no git changes)
#   ./deploy_local.sh --pull           first pull the latest market-lens-pilot from origin
#   ./deploy_local.sh --force          don't wait for a running pipeline (it is interrupted)
#   ./deploy_local.sh --public         listen on 0.0.0.0 (needs STUDIO_PASSWORD in lens/.env)
#   ./deploy_local.sh --stop           just stop the Console
# Options can be combined, e.g. ./deploy_local.sh --pull --force
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)   # the git repository (this script's folder)
APP=$REPO/lens
BRANCH=market-lens-pilot
PORT=3000
WAIT_MINUTES=30
LOG=$APP/data/console.log

PULL=0 FORCE=0 HOST=127.0.0.1 STOP_ONLY=0
for a in "$@"; do
    case "$a" in
        --pull) PULL=1 ;;
        --force) FORCE=1 ;;
        --public) HOST=0.0.0.0 ;;
        --stop) STOP_ONLY=1 ;;
        *) echo "unknown option: $a" >&2; exit 2 ;;
    esac
done

say() { printf '\n== %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

cd "$REPO"
[[ -f "$APP/lens/studio.py" ]] || die "$APP/lens/studio.py not found: run this script from the repository root"
PASS=$(grep -E '^STUDIO_PASSWORD=' "$APP/.env" 2>/dev/null | head -1 | cut -d= -f2- || true)
[[ "$HOST" == "0.0.0.0" && -z "$PASS" ]] && die "--public needs STUDIO_PASSWORD in lens/.env"

console_pid() {  # PID of whatever listens on $PORT (empty if nothing)
    ss -ltnp 2>/dev/null | grep -E ":$PORT\b" | grep -o 'pid=[0-9]*' | head -1 | cut -d= -f2 || true
}
job_running() {  # 1 = a pipeline run is in progress, 0 = idle, ? = Console not answering
    curl -s -m 5 -u "deploy:$PASS" "http://127.0.0.1:$PORT/api/job" 2>/dev/null |
        python3 -c 'import json,sys
try: print(1 if json.load(sys.stdin).get("running") else 0)
except Exception: print("?")'
}

# 1. Wait for a running pipeline (stopping would interrupt it)
if [[ $FORCE -eq 0 && -n "$(console_pid)" ]]; then
    say "Checking for a running pipeline"
    for ((i = 0; i < WAIT_MINUTES * 2; i++)); do
        [[ "$(job_running)" != "1" ]] && break
        [[ $i -eq 0 ]] && echo "A run is in progress; waiting up to $WAIT_MINUTES min (use --force to skip)..."
        sleep 30
    done
    [[ "$(job_running)" == "1" ]] && die "a run is still in progress after $WAIT_MINUTES min; try later or use --force"
    echo "Idle: OK"
fi

# 2. Optionally pull the latest code (your local config.json and uncommitted work are protected)
TS=$(date +%Y%m%d-%H%M%S)
if [[ $PULL -eq 1 ]]; then
    say "Pulling origin/$BRANCH"
    CONFIG_SAVED=""
    if ! git diff --quiet -- lens/config.json; then
        CONFIG_SAVED="/tmp/config.json.$TS"
        cp lens/config.json "$CONFIG_SAVED"
        git checkout -- lens/config.json
    fi
    if ! git diff --quiet || ! git diff --cached --quiet; then
        [[ -n "$CONFIG_SAVED" ]] && cp "$CONFIG_SAVED" lens/config.json
        die "you have uncommitted changes (git status); commit or stash them before --pull"
    fi
    OLD=$(git rev-parse HEAD)
    git fetch origin "$BRANCH"
    git checkout -q "$BRANCH"
    git pull -q --ff-only origin "$BRANCH" || { [[ -n "$CONFIG_SAVED" ]] && cp "$CONFIG_SAVED" lens/config.json; die "pull failed (branch diverged?)"; }
    [[ -n "$CONFIG_SAVED" ]] && cp "$CONFIG_SAVED" lens/config.json && echo "Kept your local config.json"
    NEW=$(git rev-parse HEAD)
    [[ "$OLD" == "$NEW" ]] && echo "Already up to date" || git log --oneline "$OLD..$NEW"
fi

# 3. Check the code compiles before stopping anything
if [[ $STOP_ONLY -eq 0 ]]; then
    say "Checking the code"
    find "$APP" -name __pycache__ -prune -exec rm -rf {} +
    python3 -m py_compile "$APP"/lens/*.py || die "the code does not compile; the running Console was left as it is"
    echo "OK"
fi

# 4. Stop the running Console
PID=$(console_pid)
if [[ -n "$PID" ]]; then
    say "Stopping the Console (pid $PID)"
    kill "$PID"
    for i in $(seq 1 15); do [[ -z "$(console_pid)" ]] && break; sleep 1; done
    [[ -n "$(console_pid)" ]] && { kill -9 "$PID" 2>/dev/null || true; sleep 1; }
    echo "Stopped"
else
    echo "No Console running on port $PORT"
fi
[[ $STOP_ONLY -eq 1 ]] && exit 0

# 5. Back up the database
if [[ -f "$APP/data/lens.db" ]]; then
    say "Backing up the database"
    mkdir -p "$APP/data/backups"
    python3 -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close()" \
        "$APP/data/lens.db" "$APP/data/backups/lens_predeploy_$TS.db"
    gzip -f "$APP/data/backups/lens_predeploy_$TS.db"
    echo "Saved data/backups/lens_predeploy_$TS.db.gz"
    find "$APP/data/backups" -name 'lens_predeploy_*.db.gz' -mtime +7 -delete   # keep a week
fi

# 6. Start in the background and check it comes up
say "Starting the Console on $HOST:$PORT"
mkdir -p "$APP/data"
cd "$APP"
nohup python3 -m lens.studio --host "$HOST" --port "$PORT" >> "$LOG" 2>&1 &
code=000
for i in $(seq 1 30); do
    code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://127.0.0.1:$PORT/" || true)
    [[ "$code" == "401" || "$code" == "200" ]] && break
    sleep 1
done
if [[ "$code" == "401" || "$code" == "200" ]]; then
    say "Running: http://127.0.0.1:$PORT  (pid $(console_pid))"
    echo "Log: $LOG"
    echo "Stop: ./deploy_local.sh --stop"
else
    tail -30 "$LOG"
    die "the Console did not start (log above)"
fi
