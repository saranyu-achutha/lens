#!/usr/bin/env bash
# SERVER: deploy the latest market-lens-pilot code and restart the systemd service.
#
#   ./deploy_server.sh            wait for a running pipeline to finish, then deploy
#   ./deploy_server.sh --force    deploy without waiting (a running pipeline is interrupted)
#
# Safe by default: keeps the server's config.json (settings changed in the Console),
# backs up the database first, checks the new code compiles, and rolls back if not.
set -euo pipefail

REPO=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)   # the git repository (this script's folder)
APP=$REPO/lens                                      # app folder (holds .env, config.json, data/)
BRANCH=market-lens-pilot
SERVICE=market-lens
PORT=3000                                           # must match --port in the systemd service (nginx proxies port 80 to it)
WAIT_MINUTES=30

FORCE=0
[[ "${1:-}" == "--force" ]] && FORCE=1

say() { printf '\n== %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

cd "$REPO"
[[ -f "$APP/lens/studio.py" ]] || die "$APP/lens/studio.py not found: run this script from the repository root"
systemctl cat "$SERVICE" >/dev/null 2>&1 || die "systemd service '$SERVICE' not found; create it first (or use deploy_local.sh)"

# /api/job needs the developer login (the client login gets 403, which would read as "idle")
env_val() { grep -E "^$1=" "$APP/.env" 2>/dev/null | head -1 | cut -d= -f2- || true; }
USER_=$(env_val DEVELOPER_USERNAME); PASS=$(env_val DEVELOPER_PASSWORD)
[[ -n "$USER_" && -n "$PASS" ]] || { USER_=deploy; PASS=$(env_val STUDIO_PASSWORD); }

job_running() {  # 1 = a job is running in either lane (API jobs or the browser crawls), 0 = idle, ? = Console not answering
    curl -s -m 5 -u "$USER_:$PASS" "http://127.0.0.1:$PORT/api/job" 2>/dev/null |
        python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
    if "error" in d: raise ValueError(d["error"])
    print(1 if d.get("running") or (d.get("browser") or {}).get("running") else 0)
except Exception: print("?")'
}

console_post() {  # POST JSON to the Console as the developer; prints the reply (empty if it does not answer)
    curl -s -m 10 -u "$USER_:$PASS" -H "Content-Type: application/json" -d "$2" "http://127.0.0.1:$PORT$1" 2>/dev/null || true
}

PAUSED_BY_US=0
resume_jobs() {  # end the deploy's pause (a pause also ends by itself after 20 minutes)
    if [[ $PAUSED_BY_US -eq 1 ]]; then
        local ok=0
        for _ in $(seq 1 12); do  # the Console can take a while to come back after the restart: try for up to a minute
            if console_post /api/jobs/resume '{"reason": "deploy finished"}' | grep -q '"resumed"'; then ok=1; break; fi
            sleep 5
        done
        if [[ $ok -eq 1 ]]; then
            echo "Jobs resumed: paused work continues where it stopped"
        else
            echo "NOTE: could not resume jobs; the deploy pause ends by itself within 20 minutes"
        fi
        PAUSED_BY_US=0
    fi
}
trap resume_jobs EXIT  # whatever happens below, jobs are not left paused

wait_idle() {  # crawl data matters: never restart in the middle of a job in either lane
    for ((i = 0; i < WAIT_MINUTES * 2; i++)); do
        local st; st=$(job_running)
        if [[ "$st" == "?" ]]; then  # idle only if the Console is really down; if it answers, the job state is unknown
            [[ "$(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://127.0.0.1:$PORT/" || true)" == "000" ]] && return 0
            die "the Console is up but its job state cannot be read (check DEVELOPER_USERNAME / DEVELOPER_PASSWORD in .env); nothing was restarted"
        fi
        [[ "$st" == "0" ]] && return 0
        [[ $i -eq 0 ]] && echo "A job is running (API or browser lane); waiting up to $WAIT_MINUTES min (use --force to skip)..."
        sleep 30
    done
    [[ "$(job_running)" != "1" ]]
}

# 1. Pause the jobs: running ones stop at their next safe point (data read so far is kept, they continue after the
#    resume), and nothing new starts. A Console without the pause feature is waited for instead.
if [[ $FORCE -eq 0 ]]; then
    say "Pausing jobs for the deploy"
    if console_post /api/jobs/pause '{"now": true, "minutes": 20, "reason": "deploy"}' | grep -q '"pause"'; then
        PAUSED_BY_US=1
        echo "Paused (running jobs stop at a safe point; the pause ends by itself within 20 minutes)"
    else
        echo "This Console has no pause feature (or does not answer); waiting for the running jobs to finish instead"
    fi
    say "Checking for running jobs (both lanes)"
    wait_idle || die "a job is still running after $WAIT_MINUTES min; try later or use --force"
    echo "Idle (or Console not running): OK"
fi

# 2. Keep the server's settings (the Console writes config.json)
OLD=$(git rev-parse HEAD)
TS=$(date +%Y%m%d-%H%M%S)
CONFIG_SAVED=""
if ! git diff --quiet -- lens/config.json; then
    CONFIG_SAVED="/tmp/config.json.$TS"
    cp lens/config.json "$CONFIG_SAVED"
    git checkout -- lens/config.json
    echo "Server settings saved to $CONFIG_SAVED (restored after the update)"
fi
git diff --quiet && git diff --cached --quiet || die "uncommitted changes in the repository (git status); commit or discard them first"

# 3. Get the new code
say "Updating to origin/$BRANCH"
git fetch origin "$BRANCH"
git checkout -q "$BRANCH"
git pull -q --ff-only origin "$BRANCH" || die "pull failed (branch diverged?); nothing was changed"
NEW=$(git rev-parse HEAD)
if [[ -n "$CONFIG_SAVED" ]]; then
    if ! git diff --quiet "$OLD" "$NEW" -- lens/config.json; then
        cp lens/config.json "lens/config.json.repo-$TS"
        echo "NOTE: this update also changes config.json; the server's settings are kept."
        echo "      The new default is saved as lens/config.json.repo-$TS for comparison."
    fi
    cp "$CONFIG_SAVED" lens/config.json
fi
if [[ "$OLD" == "$NEW" ]]; then
    echo "Already up to date ($(git log -1 --format='%h %s'))"
else
    git log --oneline "$OLD..$NEW"
fi

# 4. Check the new code compiles; roll back if not
say "Checking the code"
find "$APP" -name __pycache__ -prune -exec rm -rf {} +
if ! python3 -m py_compile "$APP"/lens/*.py; then
    git reset -q --hard "$OLD"
    [[ -n "$CONFIG_SAVED" ]] && cp "$CONFIG_SAVED" lens/config.json
    die "the new code does not compile; rolled back to $(git log -1 --format=%h). The service was not restarted."
fi
echo "OK"

# 5. Back up the database (the new code may update its structure)
if [[ -f "$APP/data/lens.db" ]]; then
    say "Backing up the database"
    mkdir -p "$APP/data/backups"
    python3 -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close()" \
        "$APP/data/lens.db" "$APP/data/backups/lens_predeploy_$TS.db"
    gzip -f "$APP/data/backups/lens_predeploy_$TS.db"
    echo "Saved data/backups/lens_predeploy_$TS.db.gz"
fi

# 6. Restart and check it comes back. A job may have started while the backup ran: check both lanes again
#    right before the restart (the new code is in place; only the restart waits).
if [[ $FORCE -eq 0 ]]; then
    wait_idle || die "a job started during the update and is still running after $WAIT_MINUTES min; the code is updated but the service was not restarted (run: sudo systemctl restart $SERVICE when idle)"
fi
say "Restarting $SERVICE"
sudo systemctl restart "$SERVICE"
code=000
for i in $(seq 1 30); do
    code=$(curl -s -o /dev/null -m 3 -w '%{http_code}' "http://127.0.0.1:$PORT/" || true)
    [[ "$code" == "401" || "$code" == "200" ]] && break
    sleep 1
done
if [[ "$code" == "401" || "$code" == "200" ]]; then
    resume_jobs
    say "Deployed $(git log -1 --format='%h %s')"
    systemctl --no-pager --lines=0 status "$SERVICE" | head -3
    echo "Rollback if needed: cd $REPO && git reset --hard $OLD && sudo systemctl restart $SERVICE"
else
    sudo journalctl -u "$SERVICE" -n 30 --no-pager
    die "the Console did not come back on port $PORT (log above). Roll back: cd $REPO && git reset --hard $OLD && sudo systemctl restart $SERVICE"
fi
