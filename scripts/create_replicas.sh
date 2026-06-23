#!/usr/bin/env bash
# create_replicas.sh
# Creates bare mirror replicas of the two source repositories.
# Run this script once with appropriate GitHub credentials/SSH access.
#
# Replicas are stored under replicas/ at the project root.
# Each replica is a bare clone (--mirror) and can be used as a local
# read-only copy or as a push target for further mirroring.
#
# Usage:
#   bash scripts/create_replicas.sh [--update]
#
# Options:
#   --update   Fetch latest changes into existing replicas instead of creating
#
# Requirements:
#   - git >= 2.x
#   - SSH key or HTTPS credentials for both source repos

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
REPLICAS_DIR="$PROJECT_ROOT/replicas"

REPO_1_URL="https://github.com/Synovia-Flow/Birkdale_Production.git"
REPO_1_NAME="Birkdale_Production"

REPO_2_URL="https://github.com/Synovia-Digital/flOW_V2.git"
REPO_2_NAME="flOW_V2"

UPDATE_ONLY=false
if [[ "${1:-}" == "--update" ]]; then
  UPDATE_ONLY=true
fi

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

mirror_or_update() {
  local url="$1"
  local name="$2"
  local dest="$REPLICAS_DIR/${name}.git"

  if [[ -d "$dest" && "$UPDATE_ONLY" == false ]]; then
    log "Replica '$name' already exists at $dest — skipping (use --update to refresh)"
    return
  fi

  if [[ -d "$dest" ]]; then
    log "Updating replica '$name' ..."
    git -C "$dest" remote update --prune
    log "Replica '$name' updated."
  else
    log "Cloning mirror of '$name' from $url ..."
    mkdir -p "$REPLICAS_DIR"
    git clone --mirror "$url" "$dest"
    log "Replica '$name' created at $dest"
  fi
}

log "=== Fusion Flow V2 — Repository Replica Setup ==="
log "Replicas directory: $REPLICAS_DIR"

mirror_or_update "$REPO_1_URL" "$REPO_1_NAME"
mirror_or_update "$REPO_2_URL" "$REPO_2_NAME"

log "=== Done ==="
log ""
log "Replica locations:"
log "  1. $REPLICAS_DIR/${REPO_1_NAME}.git  (source: $REPO_1_URL)"
log "  2. $REPLICAS_DIR/${REPO_2_NAME}.git  (source: $REPO_2_URL)"
log ""
log "To inspect a replica:"
log "  git -C replicas/${REPO_1_NAME}.git log --oneline -10"
log "  git -C replicas/${REPO_2_NAME}.git log --oneline -10"
log ""
log "To push a replica to a new remote:"
log "  git -C replicas/<name>.git remote set-url --push origin <new-remote-url>"
log "  git -C replicas/<name>.git push --mirror"
