#!/bin/bash
# Soma updater — pull latest, re-run installer, restart service.
# Auto-rolls back via `git reset --hard` if install.sh or service-start fails.
#
# Usage:  ./update.sh [--yes]
#
# Safe to run from cron with --yes. Refuses to run if the working tree
# is dirty. data/ is gitignored and never touched by the rollback.
set -e

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO_DIR"

GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m'

info()  { echo -e "${GREEN}[soma]${NC} $*"; }
warn()  { echo -e "${YELLOW}[soma]${NC} $*"; }
err()   { echo -e "${RED}[soma]${NC} $*" >&2; }

# ---------------------------------------------------------------- args

YES=0
for arg in "$@"; do
    case "$arg" in
        --yes|-y) YES=1 ;;
        --help|-h)
            echo "Usage: $0 [--yes]"
            echo ""
            echo "  --yes, -y    Skip confirmation prompts (use in cron)."
            echo ""
            echo "Pulls origin/main, re-runs install.sh, restarts soma.service"
            echo "if it was active. Auto-rolls back if anything fails."
            exit 0
            ;;
        *)
            err "Unknown argument: $arg"
            exit 1
            ;;
    esac
done

confirm() {
    if [ "$YES" -eq 1 ]; then return 0; fi
    read -r -p "$(echo -e ${YELLOW}[soma]${NC}) $1 [y/N] " ans
    case "$ans" in
        [yY]|[yY][eE][sS]) return 0 ;;
        *) return 1 ;;
    esac
}

# ---------------------------------------------------------------- 1. pre-flight

if [ ! -d .git ]; then
    err "Not a git checkout — update.sh only works against a git clone."
    exit 1
fi

if [ -n "$(git status --porcelain)" ]; then
    err "Working tree has uncommitted changes. Commit, stash, or revert first."
    git status --short
    exit 1
fi

CURRENT_REF=$(git rev-parse HEAD)
CURRENT_VERSION=$(grep -E '^__version__' core.py | sed -E 's/.*"([^"]+)".*/\1/')

# ---------------------------------------------------------------- 2. fetch + diff

info "Fetching from origin..."
git fetch origin

# Detect default branch (main / master)
BRANCH=$(git symbolic-ref --short HEAD)
REMOTE_REF=$(git rev-parse "origin/$BRANCH")

if [ "$CURRENT_REF" = "$REMOTE_REF" ]; then
    info "Already up to date (v$CURRENT_VERSION at ${CURRENT_REF:0:8})."
    exit 0
fi

info ""
info "Current : v$CURRENT_VERSION at ${CURRENT_REF:0:8}"
info "Remote  :              at ${REMOTE_REF:0:8}"
info ""
info "Incoming commits:"
git log --oneline "$CURRENT_REF..$REMOTE_REF" | sed 's/^/  /'
info ""

if ! confirm "Apply update?"; then
    info "Aborted."
    exit 0
fi

# ---------------------------------------------------------------- 3. service

SERVICE_WAS_ACTIVE=0
if systemctl is-active --quiet soma.service 2>/dev/null; then
    SERVICE_WAS_ACTIVE=1
    warn "soma.service is currently active — a running task will be interrupted."
    if ! confirm "Stop service and proceed?"; then
        info "Aborted."
        exit 0
    fi
    sudo systemctl stop soma.service
    info "  service stopped"
fi

# ---------------------------------------------------------------- helpers

rollback() {
    err "Rolling back to ${CURRENT_REF:0:8} (v$CURRENT_VERSION)..."
    git reset --hard "$CURRENT_REF" >/dev/null
    err "  rollback complete."
    if [ "$SERVICE_WAS_ACTIVE" -eq 1 ]; then
        sudo systemctl start soma.service || true
        err "  service restarted on old version."
    fi
}

# ---------------------------------------------------------------- 4. pull

info "Pulling latest..."
if ! git pull --ff-only origin "$BRANCH"; then
    err "git pull failed (diverged history?)."
    # no rollback needed — we haven't moved yet
    if [ "$SERVICE_WAS_ACTIVE" -eq 1 ]; then
        sudo systemctl start soma.service
    fi
    exit 1
fi

NEW_VERSION=$(grep -E '^__version__' core.py | sed -E 's/.*"([^"]+)".*/\1/')

# ---------------------------------------------------------------- 5. re-install

info "Re-running install.sh..."
if ! ./install.sh; then
    err "install.sh failed."
    rollback
    exit 1
fi

# ---------------------------------------------------------------- 6. restart

if [ "$SERVICE_WAS_ACTIVE" -eq 1 ]; then
    info "Starting soma.service..."
    sudo systemctl start soma.service
    sleep 2
    if ! systemctl is-active --quiet soma.service; then
        err "Service failed to start after update."
        sudo systemctl stop soma.service 2>/dev/null || true
        rollback
        exit 1
    fi
    info "  service active"
fi

# ---------------------------------------------------------------- done

info ""
info "Update complete:  v$CURRENT_VERSION → v$NEW_VERSION"
info ""
info "Changes:"
git log --oneline "$CURRENT_REF..HEAD" | sed 's/^/  /'
info ""
info "See CHANGELOG.md for full release notes."
