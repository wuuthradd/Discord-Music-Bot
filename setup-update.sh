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

# yt-dlp calls this service itself when a YouTube request needs a PO token.
# The plugin's default address is http://127.0.0.1:4416. No bot code sets it.
BGUTIL_IMAGE_REPO="brainicism/bgutil-ytdlp-pot-provider"
BGUTIL_CONTAINER="bgutil-provider"
BGUTIL_PUBLISH="127.0.0.1:4416:4416"

bgutil_plugin_version() {
    local pip_bin="" line ver=""
    if [ -n "${BGUTIL_PIP:-}" ] && [ -x "${BGUTIL_PIP}" ]; then
        pip_bin="$BGUTIL_PIP"
    elif [ -x "$SCRIPT_DIR/.venv/bin/pip" ]; then
        pip_bin="$SCRIPT_DIR/.venv/bin/pip"
    else
        return 0
    fi
    while IFS= read -r line; do
        case "$line" in
            "Version: "*) ver="${line#Version: }" ;;
        esac
    done < <("$pip_bin" show bgutil-ytdlp-pot-provider 2>/dev/null || true)
    printf '%s' "$ver"
}

bgutil_container_state() {
    docker container inspect --format '{{.Image}}|{{.State.Running}}|{{.HostConfig.RestartPolicy.Name}}|{{.HostConfig.Init}}|{{range $p, $conf := .HostConfig.PortBindings}}{{$p}}={{range $conf}}{{.HostIp}}:{{.HostPort}}{{end}};{{end}}' "$BGUTIL_CONTAINER" 2>/dev/null
}

# A running container alone does not prove that the provider is reachable.
bgutil_ping() {
    local python_bin
    if [ -x "$SCRIPT_DIR/.venv/bin/python" ]; then
        python_bin="$SCRIPT_DIR/.venv/bin/python"
    elif command -v python3 >/dev/null 2>&1; then
        python_bin=python3
    else
        return 1
    fi
    "$python_bin" - <<'PY' >/dev/null 2>&1
import json
from urllib.request import build_opener, ProxyHandler

with build_opener(ProxyHandler({})).open("http://127.0.0.1:4416/ping", timeout=2) as response:
    data = json.load(response)
    if not isinstance(data.get("version"), str) or not isinstance(data.get("server_uptime"), (float, int)):
        raise ValueError("Unexpected provider response")
PY
}

bgutil_unavailable() {
    if bgutil_ping; then
        info "The local PO token API is reachable; its container was not managed by this check."
    else
        warn "The local PO token API is unavailable. The provider is optional; bot setup can continue."
    fi
}

bgutil_service_hints() {
    local scope="$1" active enabled user load_state
    local -a ctl=(systemctl)
    local cmd="sudo systemctl"
    if ! command -v systemctl >/dev/null 2>&1; then
        return 0
    fi
    if [ "$scope" = "rootless" ]; then
        ctl+=(--user)
        cmd="systemctl --user"
    elif [ "$scope" != "system" ]; then
        return 0
    fi
    # Never assume Docker Desktop, a remote context or a non-systemd install
    # is managed by the host's docker.service.
    load_state="$("${ctl[@]}" show -p LoadState --value docker.service 2>/dev/null || true)"
    if [ "$load_state" != "loaded" ] && [ "$load_state" != "masked" ]; then
        return 0
    fi
    active="$("${ctl[@]}" is-active docker.service 2>/dev/null || true)"
    enabled="$("${ctl[@]}" is-enabled docker.service 2>/dev/null || true)"
    if [ "$active" = "active" ]; then
        info "Docker service is active."
        case "$enabled" in
            disabled|enabled-runtime)
                warn "Docker is not enabled persistently at boot. Run: $cmd enable docker.service"
                ;;
        esac
    elif [ "$enabled" = "masked" ] || [ "$enabled" = "masked-runtime" ]; then
        warn "Docker service is masked. Ask your administrator to review the service before enabling it."
    else
        warn "Docker service state: ${active:-unknown}."
        warn "To start Docker and enable it at boot, run: $cmd enable --now docker.service"
        if [ "$active" = "failed" ]; then
            if [ "$scope" = "rootless" ]; then
                warn "Inspect the service: journalctl --user -u docker.service -n 50 --no-pager"
            else
                warn "Inspect the service: sudo journalctl -u docker.service -n 50 --no-pager"
            fi
        fi
    fi
    if [ "$scope" = "rootless" ] && command -v loginctl >/dev/null 2>&1; then
        user="$(id -un)"
        if [ "$(loginctl show-user "$user" -p Linger --value 2>/dev/null || true)" = "no" ]; then
            warn "To keep rootless Docker running after logout and across boots, run: sudo loginctl enable-linger $user"
        fi
    fi
}

bgutil_wait_ready() {
    local attempt
    for attempt in {1..5}; do
        if bgutil_ping; then
            info "YouTube PO token provider is reachable at http://127.0.0.1:4416."
            return 0
        fi
        if [ "$attempt" -lt 5 ]; then sleep 1; fi
    done
    warn "The provider did not answer its /ping endpoint. Inspect: docker logs --tail 50 $BGUTIL_CONTAINER"
    return 1
}

ensure_bgutil() {
    local ver image image_id state c_image c_running c_restart c_init c_ports
    local endpoint scope="unknown" docker_error user configured_image

    if ! command -v docker >/dev/null 2>&1; then
        if bgutil_ping; then
            info "Docker CLI was not found, but the local PO token API is reachable. Leaving it untouched."
            return 0
        fi
        warn "Docker CLI was not found. To use the optional provider, install Docker Engine: https://docs.docker.com/engine/install/"
        warn "On a standard systemd Docker Engine host, run: sudo systemctl enable --now docker.service"
        warn "Configure non-root Docker access if required: https://docs.docker.com/engine/install/linux-postinstall/"
        warn "After logging in again, run: bash setup-update.sh --bgutil-only"
        warn "The provider is optional; bot setup can continue."
        return 0
    fi

    # DOCKER_CONTEXT takes precedence over DOCKER_HOST when explicitly set.
    if [ -n "${DOCKER_CONTEXT:-}" ]; then
        endpoint="$(docker context inspect "$DOCKER_CONTEXT" --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true)"
    elif [ -n "${DOCKER_HOST:-}" ]; then
        endpoint="$DOCKER_HOST"
    else
        endpoint="$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true)"
    fi
    case "$endpoint" in
        unix:///var/run/docker.sock|unix:///run/docker.sock) scope=system ;;
        unix:///run/user/*/docker.sock) scope=rootless ;;
        tcp://*|ssh://*|http://*|https://*)
            warn "Docker uses a non-local endpoint. This script only manages a provider on the local host."
            warn "Select a local Docker context or configure the provider manually."
            bgutil_unavailable
            return 0
            ;;
    esac

    if ! docker_error="$(docker info --format '{{.ServerVersion}}' 2>&1)"; then
        warn "Docker is installed, but the bot's user cannot reach its daemon."
        printf '%s\n' "$docker_error"
        bgutil_service_hints "$scope"
        case "${docker_error,,}" in
            *"permission denied"*|*"access denied"*)
                if [ "$scope" = "system" ] && [ "$(id -u)" != "0" ]; then
                    user="$(id -un)"
                    warn "If you trust this account with root-equivalent Docker access, run: sudo usermod -aG docker $user"
                    warn "Log out and back in; restart the bot's service/process manager to refresh its groups."
                else
                    warn "Check the selected Docker socket and the bot user's permissions."
                fi
                ;;
            *) warn "Verify Docker is running and accessible with: docker info" ;;
        esac
        warn "After fixing access, rerun: bash setup-update.sh --bgutil-only"
        bgutil_unavailable
        return 0
    fi
    info "Docker daemon is reachable."
    bgutil_service_hints "$scope"

    # Do not overwrite an unrelated container or a manually managed provider.
    if state="$(bgutil_container_state)"; then
        configured_image="$(docker container inspect --format '{{.Config.Image}}' "$BGUTIL_CONTAINER")" || {
            warn "Could not inspect the existing provider container."
            return 1
        }
        case "$configured_image" in
            "$BGUTIL_IMAGE_REPO"|"$BGUTIL_IMAGE_REPO":*|"$BGUTIL_IMAGE_REPO"@*|docker.io/"$BGUTIL_IMAGE_REPO"|docker.io/"$BGUTIL_IMAGE_REPO":*|docker.io/"$BGUTIL_IMAGE_REPO"@*) ;;
            *)
                warn "A container named $BGUTIL_CONTAINER uses a different image. It was left untouched."
                warn "Inspect the existing container before resolving the name conflict: docker inspect $BGUTIL_CONTAINER"
                bgutil_unavailable
                return 0
                ;;
        esac
    else
        state=""
        if bgutil_ping; then
            info "A local PO token API is already reachable without the managed container. Leaving it untouched."
            return 0
        fi
    fi

    ver="$(bgutil_plugin_version)"
    if [ -n "$ver" ] && docker pull "${BGUTIL_IMAGE_REPO}:${ver}"; then
        image="${BGUTIL_IMAGE_REPO}:${ver}"
    else
        if [ -n "$ver" ]; then
            warn "Could not pull provider image ${ver}. Trying latest; plugin/server compatibility may differ."
        else
            warn "Plugin version was not found. Pulling the latest provider image."
        fi
        if ! docker pull "${BGUTIL_IMAGE_REPO}:latest"; then
            warn "Could not pull ${BGUTIL_IMAGE_REPO}:latest. Existing containers were left untouched."
            warn "Check registry access and rerun: bash setup-update.sh --bgutil-only"
            bgutil_unavailable
            return 1
        fi
        image="${BGUTIL_IMAGE_REPO}:latest"
    fi

    if ! image_id="$(docker image inspect --format '{{.Id}}' "$image")"; then
        warn "Could not inspect the downloaded provider image."
        return 1
    fi
    if [ -n "$state" ]; then
        IFS='|' read -r c_image c_running c_restart c_init c_ports <<< "$state"
        if [ "$c_image" = "$image_id" ] && [ "$c_init" = "true" ] \
            && [ "$c_ports" = "4416/tcp=127.0.0.1:4416;" ]; then
            if [ "$c_restart" != "unless-stopped" ]; then
                if ! docker update --restart unless-stopped "$BGUTIL_CONTAINER" >/dev/null; then
                    warn "Could not set the provider's restart policy. Run: docker update --restart unless-stopped $BGUTIL_CONTAINER"
                    return 1
                fi
                info "Provider restart policy set to unless-stopped."
            fi
            if [ "$c_running" != "true" ]; then
                if ! docker start "$BGUTIL_CONTAINER" >/dev/null; then
                    warn "Could not start the existing provider. Inspect: docker logs --tail 50 $BGUTIL_CONTAINER"
                    return 1
                fi
                info "Started the existing provider container."
            else
                info "YouTube PO token provider container is already running (${image})."
            fi
            bgutil_wait_ready
            return $?
        fi
        info "Replacing YouTube PO token provider container to update its image or configuration."
        if ! docker rm -f "$BGUTIL_CONTAINER" >/dev/null; then
            warn "Could not replace the existing provider container. Inspect: docker inspect $BGUTIL_CONTAINER"
            return 1
        fi
    else
        info "Creating YouTube PO token provider container."
    fi

    if ! docker run --name "$BGUTIL_CONTAINER" -d --init --restart unless-stopped \
        -p "$BGUTIL_PUBLISH" "$image" >/dev/null; then
        warn "Docker could not start the provider. Check port 4416, container name conflicts and Docker output."
        warn "Inspect: docker ps -a; docker logs --tail 50 $BGUTIL_CONTAINER"
        return 1
    fi
    bgutil_wait_ready
}

# pm2 puts Node's IPC descriptors in the environment. Deno treats
# NODE_CHANNEL_FD as its own pipe, and yt-dlp's JS challenge solver then
# exits with "fd is not from BiPipe". Unset is a no-op when pm2 is not used.
write_run_bot() {
    cat > "$SCRIPT_DIR/run_bot.sh" << 'EOF'
#!/bin/bash
cd "$(dirname "$0")"
source .venv/bin/activate
unset NODE_CHANNEL_FD NODE_CHANNEL_SERIALIZATION_MODE
python main.py
EOF
    chmod +x "$SCRIPT_DIR/run_bot.sh"
}

if [ "${1:-}" = "--bgutil-only" ]; then
    ensure_bgutil
    exit $?
fi

if [ "${1:-}" = "--launcher-only" ]; then
    write_run_bot
    exit 0
fi

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

# --- 7b. YouTube PO token provider ---
info "Checking YouTube PO token provider..."
if ! ensure_bgutil; then
    warn "YouTube PO token provider was not updated. Playback can continue with clients that do not need a token."
fi

# --- 8. Create run_bot.sh ---
write_run_bot

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
