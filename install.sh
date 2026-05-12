#!/bin/bash
# Soma installer — bootstraps a fresh checkout to a runnable state.
#
# Steps:
#   1. Check prerequisites (python3 >=3.10, docker, pip)
#   2. Install host-side Python deps (aiogram, httpx, pyyaml)
#   3. Create data/ tree
#   4. Pull the sandbox image and create the sandbox container
#   5. If .env is missing: copy .env.example to .env and prompt the user to edit it
#   6. Run the test suite
#
# Idempotent: safe to re-run.
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

# ---------------------------------------------------------------- 1. checks

info "Checking prerequisites..."

if ! command -v python3 >/dev/null 2>&1; then
    err "python3 not found. Install Python 3.10+ first."
    exit 1
fi

PY_MAJOR=$(python3 -c 'import sys; print(sys.version_info[0])')
PY_MINOR=$(python3 -c 'import sys; print(sys.version_info[1])')
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    err "python3 must be >= 3.10 (found ${PY_MAJOR}.${PY_MINOR})."
    exit 1
fi
info "  python3 ${PY_MAJOR}.${PY_MINOR}"

if ! command -v docker >/dev/null 2>&1; then
    err "docker not found. Install Docker first: https://docs.docker.com/engine/install/"
    exit 1
fi

if ! docker info >/dev/null 2>&1; then
    err "docker is installed but the daemon is not reachable (or you lack permission)."
    err "Try: sudo usermod -aG docker \$USER  and re-login."
    exit 1
fi
info "  docker reachable"

if ! python3 -c "import pip" >/dev/null 2>&1; then
    err "pip not available for python3. Install python3-pip and re-run."
    exit 1
fi
info "  pip available"

# ---------------------------------------------------------------- 2. host deps

VENV_DIR="$REPO_DIR/.venv"
info "Installing host Python dependencies into $VENV_DIR ..."

if [ ! -d "$VENV_DIR" ]; then
    if ! python3 -m venv "$VENV_DIR" 2>/dev/null; then
        err "Failed to create venv. Install the venv module first:"
        err "  Debian/Ubuntu:  sudo apt install python3-venv"
        err "  Fedora/RHEL:    sudo dnf install python3-virtualenv"
        exit 1
    fi
    info "  created venv"
fi

"$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
"$VENV_DIR/bin/pip" install --upgrade -r requirements.txt >/dev/null
info "  aiogram, httpx, pyyaml installed"

# ---------------------------------------------------------------- 3. data tree

info "Creating data/ tree..."
mkdir -p data/sandbox-home data/sandbox/workspace data/sandbox/inbox
info "  data/sandbox-home/, data/sandbox/workspace/, data/sandbox/inbox/"

# ---------------------------------------------------------------- 4. sandbox container

SANDBOX_NAME="${SOMA_CONTAINER:-soma-sandbox}"
SANDBOX_IMAGE="python:3.12-slim"

info "Preparing sandbox container ($SANDBOX_NAME)..."

if ! docker image inspect "$SANDBOX_IMAGE" >/dev/null 2>&1; then
    info "  pulling $SANDBOX_IMAGE (one-time download)..."
    docker pull "$SANDBOX_IMAGE"
fi

if docker ps -a --format '{{.Names}}' | grep -qx "$SANDBOX_NAME"; then
    info "  container $SANDBOX_NAME already exists — keeping it"
else
    info "  creating container $SANDBOX_NAME..."
    docker run -d \
        --name "$SANDBOX_NAME" \
        --network bridge \
        --restart unless-stopped \
        -v "$REPO_DIR/data/sandbox-home:/root" \
        -v "$REPO_DIR/data/sandbox/workspace:/workspace" \
        -v "$REPO_DIR/data/sandbox/inbox:/inbox" \
        -w /workspace \
        "$SANDBOX_IMAGE" \
        sleep infinity >/dev/null
    info "  container created"
fi

# ---------------------------------------------------------------- 5. .env

if [ ! -f .env ] && [ ! -L .env ]; then
    info "Creating .env from template..."
    cp .env.example .env
    chmod 600 .env
    warn ""
    warn "  Edit .env and fill in:"
    warn "    TELEGRAM_TOKEN   (from @BotFather)"
    warn "    OWNER_CHAT_ID    (your numeric chat-id)"
    warn "    VLLM_BASE_URL    (your LLM endpoint)"
    warn "    VLLM_MODEL       (model name served at that endpoint)"
    warn ""
else
    info ".env exists — leaving it alone"
fi

# ---------------------------------------------------------------- 6. tests

info "Running test suite..."
FAILED=0
for t in test_memory.py test_events.py test_crystallize.py test_state.py \
         test_self_model.py test_telegram.py test_background.py test_core.py; do
    if python3 "$t" >/dev/null 2>&1; then
        info "  ✓ $t"
    else
        err "  ✗ $t"
        FAILED=1
    fi
done

if [ "$FAILED" -ne 0 ]; then
    err "Some tests failed. Re-run a single suite to see details:  python3 test_memory.py"
    exit 1
fi

# ---------------------------------------------------------------- done

info ""
info "Installation complete."
info ""
info "Next steps:"
info "  1. Edit .env (if you haven't yet)"
info "  2. Run:  ./start_soma.sh"
info "     or install soma.service (edit the User= and WorkingDirectory= placeholders first)"
info ""
info "Check status any time with:  python3 status.py"
