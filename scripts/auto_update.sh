#!/usr/bin/env bash
# auto_update.sh — Poll GitHub for changes on main, migrate DB, and restart the service.
# Designed to run as a systemd service or directly. Polls every POLL_INTERVAL seconds.
#
# Setup:
#   chmod +x scripts/auto_update.sh
#   sudo systemctl enable ledger-update.service  (see docs/raspberry-pi-setup.md)

set -euo pipefail

# ── Configuration ────────────────────────────────────────────────────────────
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV_DIR="$REPO_DIR/venv"
BRANCH="main"
SERVICE_NAME="ledger-finance"   # systemd unit name — adjust to match your .service file
POLL_INTERVAL=3600              # seconds between polls (3600 = 1 hour)
LOG_FILE="$REPO_DIR/logs/auto_update.log"
# ─────────────────────────────────────────────────────────────────────────────

mkdir -p "$REPO_DIR/logs"

log() {
    local level="$1"; shift
    printf '[%s] [%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$level" "$*" | tee -a "$LOG_FILE"
}

activate_venv() {
    if [[ -f "$VENV_DIR/bin/activate" ]]; then
        # shellcheck source=/dev/null
        source "$VENV_DIR/bin/activate"
    else
        log ERROR "Virtual environment not found at $VENV_DIR — cannot continue."
        exit 1
    fi
}

migrations_pending() {
    # Returns 0 (true) if 'flask db upgrade' would apply at least one new revision.
    local output
    output=$(flask db upgrade --sql 2>/dev/null || true)
    [[ -n "$output" ]]
}

do_update() {
    log INFO "Changes detected on origin/$BRANCH — starting update."

    cd "$REPO_DIR"
    activate_venv

    # 1. Pull latest code
    log INFO "Pulling latest code..."
    git pull origin "$BRANCH"

    # 2. Install any new dependencies
    log INFO "Syncing Python dependencies..."
    pip install -q -r requirements.txt

    # 3. Back up DB before any migration (per CLAUDE.md)
    log INFO "Backing up database..."
    python scripts/backup_db.py

    # 4. Apply DB migrations
    log INFO "Applying database migrations..."
    flask db upgrade

    # 5. Restart the service
    log INFO "Restarting $SERVICE_NAME..."
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        sudo systemctl restart "$SERVICE_NAME"
        log INFO "Service restarted successfully."
    else
        log WARN "Service $SERVICE_NAME is not active — starting it instead."
        sudo systemctl start "$SERVICE_NAME"
    fi

    log INFO "Update complete."
}

check_for_updates() {
    cd "$REPO_DIR"

    log INFO "Checking origin/$BRANCH for updates..."
    git fetch origin "$BRANCH" 2>&1 | while IFS= read -r line; do log INFO "git: $line"; done

    local local_sha remote_sha
    local_sha=$(git rev-parse HEAD)
    remote_sha=$(git rev-parse "origin/$BRANCH")

    if [[ "$local_sha" == "$remote_sha" ]]; then
        log INFO "Already up to date ($local_sha)."
        return
    fi

    log INFO "Update available: $local_sha → $remote_sha"
    do_update
}

# ── Main loop ────────────────────────────────────────────────────────────────
log INFO "auto_update.sh started. Repo: $REPO_DIR | Service: $SERVICE_NAME | Interval: ${POLL_INTERVAL}s"

# Run immediately on start, then on each interval
while true; do
    check_for_updates || log ERROR "Update cycle failed — will retry next interval."
    log INFO "Sleeping ${POLL_INTERVAL}s until next check..."
    sleep "$POLL_INTERVAL"
done
