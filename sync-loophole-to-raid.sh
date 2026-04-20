#!/bin/bash
# sync-loophole-to-raid.sh
# Pulls the latest therealphatt/loophole-unraid image from Docker Hub
# and restarts the container on PHATT-RAID.
#
# Usage:
#   ./sync-loophole-to-raid.sh              # pull + restart
#   ./sync-loophole-to-raid.sh --pull-only  # pull only, no restart
#
# Requirements on PHATT-RAID:
#   - docker CLI authenticated to Docker Hub (docker login)
#   - docker-compose.yml or container name "loophole" existing
#
# Add to cron for automatic updates:
#   0 4 * * * /opt/scripts/sync-loophole-to-raid.sh >> /var/log/loophole-sync.log 2>&1

set -euo pipefail

DRY_RUN="${DRY_RUN:-0}"
PULL_ONLY="${1:-}"
IMAGE="therealphatt/loophole-unraid"
CONTAINER_NAME="${CONTAINER_NAME:-loophole}"
LOG="/var/log/loophole-sync.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

notify() {
    curl -s -X POST "http://10.0.0.100:3100/api/issues/comments" \
        -H "Authorization: Bearer ${PAPERCLIP_API_KEY:-}" \
        -H "Content-Type: application/json" \
        -d "$(printf '{"body":"Loophole sync: %s"}' "$*")" \
        2>/dev/null || true
}

# ── Pre-flight ────────────────────────────────────────────────────────────────
if ! command -v docker &>/dev/null; then
    log "ERROR: docker CLI not found on this system"
    exit 1
fi

log "Starting Loophole Docker Hub sync"
log "Image: $IMAGE"

# ── Pull latest ───────────────────────────────────────────────────────────────
if [ "$DRY_RUN" = "1" ]; then
    log "[DRY RUN] Would pull $IMAGE:latest"
else
    log "Pulling $IMAGE:latest ..."
    if docker pull "$IMAGE:latest"; then
        log "Pull successful"
    else
        log "ERROR: docker pull failed"
        exit 1
    fi
fi

# ── Restart container ─────────────────────────────────────────────────────────
if [ "$PULL_ONLY" = "--pull-only" ]; then
    log "Pull-only mode, skipping container restart"
    exit 0
fi

if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    log "Container '$CONTAINER_NAME' found — checking if it's running"
    if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
        log "Restarting container '$CONTAINER_NAME' ..."
        if [ "$DRY_RUN" = "1" ]; then
            log "[DRY RUN] Would docker restart $CONTAINER_NAME"
        else
            docker restart "$CONTAINER_NAME" && log "Restart complete" || log "WARNING: restart failed"
        fi
    else
        log "Container exists but not running — starting it"
        docker start "$CONTAINER_NAME" && log "Start complete" || log "ERROR: start failed"
    fi
else
    log "Container '$CONTAINER_NAME' not found — will not auto-create"
    log "Run 'docker-compose up -d' or create the container manually"
fi

log "Sync complete"
