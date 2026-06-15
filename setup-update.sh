#!/usr/bin/env bash
set -euo pipefail

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[+]${NC} $1"; }
warn()  { echo -e "${YELLOW}[!]${NC} $1"; }
fail()  { echo -e "${RED}[x]${NC} $1"; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# --- 1. Python ---
info "Checking Python..."

PYTHON=""
for candidate in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$candidate" &>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done

[ -z "$PYTHON" ] && fail "Python 3.10+ is required but not found. Install it with your package manager."

PY_VERSION=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$("$PYTHON" -c "import sys; print(sys.version_info.major)")
PY_MINOR=$("$PYTHON" -c "import sys; print(sys.version_info.minor)")

if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 10 ]; }; then
    fail "Python 3.10+ is required, found $PY_VERSION."
fi

info "Found Python $PY_VERSION ($PYTHON)"

# --- 2. FFmpeg ---
info "Checking FFmpeg..."

if ! command -v ffmpeg &>/dev/null; then
    fail "FFmpeg is required but not found. Install it with your package manager."
fi

info "Found FFmpeg ($(ffmpeg -version 2>&1 | head -1 | awk '{print $3}'))"

# --- 3. JavaScript runtime ---
info "Checking JavaScript runtime..."

JS_RUNTIME=""
for candidate in deno node quickjs quickjs-ng; do
    if command -v "$candidate" &>/dev/null; then
        JS_RUNTIME="$candidate"
        break
    fi
done

if [ -n "$JS_RUNTIME" ]; then
    info "Found JS runtime: $JS_RUNTIME"
    # Update deno if it was the one found
    if [ "$JS_RUNTIME" = "deno" ]; then
        deno upgrade 2>&1 || true
    fi
else
    warn "No JavaScript runtime found (deno, node, quickjs)."
    echo -e "  YouTube playback requires a JS runtime for yt-dlp extraction."
    echo -e "  Supported: deno (recommended), node (>=22), quickjs, quickjs-ng"
    echo -e "  You can also install one from your package manager instead."
    echo ""
    read -rp "  Install Deno via its official installer? [Y/n] " INSTALL_DENO
    INSTALL_DENO="${INSTALL_DENO:-Y}"
    if [[ "$INSTALL_DENO" =~ ^[Yy]$ ]]; then
        info "Installing Deno..."
        if command -v curl &>/dev/null; then
            curl -fsSL https://deno.land/install.sh | sh 2>&1 || fail "Deno installation failed."
        elif command -v wget &>/dev/null; then
            wget -qO- https://deno.land/install.sh | sh 2>&1 || fail "Deno installation failed."
        else
            fail "curl or wget is required to install Deno."
        fi
        # Add deno to PATH for the rest of this script
        export DENO_INSTALL="${DENO_INSTALL:-$HOME/.deno}"
        export PATH="$DENO_INSTALL/bin:$PATH"
        if command -v deno &>/dev/null; then
            info "Deno installed ($(deno --version 2>&1 | head -1))"
        else
            warn "Deno was installed but not found in PATH. Add ~/.deno/bin to your PATH."
        fi
    else
        warn "Skipping JS runtime installation. YouTube playback will not work without a JS runtime."
    fi
fi

# --- 4. env-template / tokens ---

FRESH_INSTALL=true
if [ -f ".env" ]; then
    FRESH_INSTALL=false
    info ".env already exists, skipping token setup"
    # Read tokens from existing .env for the Spotify check
    BOT_TOKEN=$(grep -oP '(?<=^MyMusicBot_Token=).+' .env || true)
    SPOTIFY_ID=$(grep -oP '(?<=^SPOTIFY_CLIENT_ID=).+' .env || true)
    SPOTIFY_SECRET=$(grep -oP '(?<=^SPOTIFY_CLIENT_SECRET=).+' .env || true)
else
    info "Checking env-template..."

    [ ! -f "env-template" ] && fail "env-template not found in project root."

    BOT_TOKEN=$(grep -oP '(?<=^MyMusicBot_Token=).+' env-template || true)
    SPOTIFY_ID=$(grep -oP '(?<=^SPOTIFY_CLIENT_ID=).+' env-template || true)
    SPOTIFY_SECRET=$(grep -oP '(?<=^SPOTIFY_CLIENT_SECRET=).+' env-template || true)

    if [ -z "$BOT_TOKEN" ] || [ "${#BOT_TOKEN}" -lt 59 ]; then
        fail "MyMusicBot_Token in env-template is missing or too short (must be a valid Discord bot token).\n  Get your token from https://discord.com/developers/applications"
    fi

    info "Discord bot token found (${#BOT_TOKEN} chars)"

    # Copy env-template to .env, then clear keys in template
    cp env-template .env
    info "Created .env from env-template"

    # Clear token values in env-template so they aren't committed
    sed -i 's/^\(MyMusicBot_Token=\).*/\1/' env-template
    sed -i 's/^\(SPOTIFY_CLIENT_ID=\).*/\1/' env-template
    sed -i 's/^\(SPOTIFY_CLIENT_SECRET=\).*/\1/' env-template
    sed -i 's/^\(YTDLP_COOKIE_FILE=\).*/\1/' env-template
    info "Cleared keys in env-template"
fi

HAS_SPOTIFY=false
if [ -n "$SPOTIFY_ID" ] && [ -n "$SPOTIFY_SECRET" ]; then
    HAS_SPOTIFY=true
fi

# --- 5. venv module check ---
if ! "$PYTHON" -c "import venv" &>/dev/null; then
    fail "Python venv module is not installed. Install it with your package manager (e.g. python3-venv)."
fi

# --- 6. Create .venv ---
if [ -d ".venv" ]; then
    info "Virtual environment already exists, updating packages..."
else
    info "Creating virtual environment..."
    "$PYTHON" -m venv .venv || fail "Failed to create virtual environment."
    info "Virtual environment created at .venv/"
fi

# --- 7. Install requirements.txt ---
info "Installing requirements..."

.venv/bin/pip install --upgrade pip --no-cache-dir -q || fail "Failed to upgrade pip."
.venv/bin/pip install --upgrade -r requirements.txt --no-cache-dir -q || fail "Failed to install requirements.txt. Check your internet connection and try again."

info "Requirements installed"

# --- 8. Create run_bot.sh ---
cat > run_bot.sh << 'EOF'
#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate
python main.py
EOF
chmod +x run_bot.sh

info "Created run_bot.sh"

# --- Done ---
echo ""
if [ "$FRESH_INSTALL" = true ]; then
    echo -e "${GREEN}Setup complete!${NC}"
else
    echo -e "${GREEN}Update complete!${NC}"
fi
echo ""
echo "  To start the bot:"
echo "    ./run_bot.sh"
echo ""
if [ "$HAS_SPOTIFY" = false ]; then
    echo -e "  ${YELLOW}Note:${NC} Spotify API credentials were not provided."
    echo "  Add SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET to .env for official Spotify support if you want it, not required."
    echo ""
fi
