# Setup

## Requirements

- **Python 3.10+**
- **FFmpeg** - Must be installed and available in PATH.
- **A JavaScript runtime** - One of: `deno` (recommended), `node`, `quickjs`, or `quickjs-ng`. Required by yt-dlp for YouTube content extraction.
- **Docker** - Used only when it is already installed. The setup script then starts the YouTube PO token provider. The bot still runs without Docker.

---

## Quick Setup (Linux)

A setup script is provided that handles everything automatically:

```bash
# 1. Fill in your bot token (and optionally Spotify credentials) in env-template
# 2. Run the setup script
chmod +x setup-update.sh
./setup-update.sh
```

The script will:
- Check for Python 3.10+, FFmpeg and required system packages
- Validate your bot token
- Create `.env` from the template and clear the template keys
- Create a virtual environment and install all dependencies
- Start or update the YouTube PO token provider when Docker is available
- Generate `run_bot.sh`

To update dependencies later, run the same script again. That run also checks the provider and recreates it when the image, port, or running state does not match.

---

## Manual Setup

### 1. Get the Source

**Clone with Git:**
```bash
git clone <repository-url>
cd discord_py_clone
```

**Or** download as a ZIP from the repository page, extract it, and open the folder.

### 2. Create a Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

This installs:

| Package | Purpose |
|---|---|
| `discord.py[voice]` | Discord API + voice support (includes PyNaCl) |
| `yt-dlp[default,curl-cffi]` | source audio extraction, including browser impersonation |
| `python-dotenv` | `.env` file loading |
| `aiosqlite` | Async SQLite database |
| `psutil` | RAM/CPU monitoring in `/manage` |
| `spotapi` | Spotify track/playlist/album resolution |
| `bgutil-ytdlp-pot-provider` | yt-dlp plugin that fetches a YouTube PO token from the local provider |

### 4. Install FFmpeg

FFmpeg is required for audio playback. Install it with your package manager.

### 5. Install a JavaScript Runtime

yt-dlp requires a JavaScript runtime to extract certain YouTube content. Install **one** of the following: `deno` (recommended), `node`, `quickjs`, or `quickjs-ng`.

The bot checks for them in that order. If none are found, a warning is logged at startup and some content may fail to play.

### 6. Create the Bot on Discord

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications).
2. Create a **New Application**.
3. Go to the **Bot** tab and click **Reset Token** to get your bot token.
4. Under **Privileged Gateway Intents**, you do **not** need to enable any privileged intents. The bot only uses `guilds` and `voice_states`.
5. Go to the **OAuth2** tab, select the `bot` scope.
6. Under **Bot Permissions**, select:
   - Connect
   - Speak
   - Send Messages
   - Embed Links
   - Attach Files
   - Read Message History
   - Use Application Commands
7. Use the generated URL to invite the bot to your server.

### 7. Configure Environment Variables

Copy the included template and fill in your values:

```bash
cp env-template .env
```

Open `.env` and set your bot token:

```env
MyMusicBot_Token=your_bot_token_here
```

The template also includes optional Spotify API fields - see [Spotify Setup](#spotify-setup) for details.

### 8. Run the Bot

**Using the provided script:**
```bash
chmod +x run_bot.sh
./run_bot.sh
```

**Or manually:**
```bash
source .venv/bin/activate
python main.py
```

---

## Spotify Setup

If official Spotify API credentials are present, the bot uses those. Otherwise it falls back to [SpotAPI](https://github.com/Aran404/SpotAPI), which can have reliability issues. For best consistency use the official API (requires a Spotify Premium account):

1. Go to the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
2. Create an app (any name/description, no redirect URI needed).
3. Copy the **Client ID** and **Client Secret**.
4. Add them to your `.env` file:
   ```env
   SPOTIFY_CLIENT_ID=your_client_id
   SPOTIFY_CLIENT_SECRET=your_client_secret
   ```

**Without API keys (SpotAPI fallback):** Tracks, albums, and the first ~100 tracks of playlists work, but may be unreliable.
**With API keys:** Full playlist support with pagination (up to 10,000 tracks).

The bot resolves Spotify tracks by searching YouTube for the best matching result, comparing title, artist, and duration.

---

## Cookie File (Age-Restricted Content)

To play age-restricted YouTube content, provide a Netscape-format cookie file:

1. Export cookies from a logged-in YouTube session using a browser extension (e.g., "Get cookies.txt LOCALLY").
2. Place the file at `db/cookies.txt` (the default path) or set the `YTDLP_COOKIE_FILE` environment variable to a custom path.

> **Note:** The bot creates temporary copies of the cookie file for each yt-dlp operation to ensure thread safety. The original file is never modified.

> **Cookies expire.** If age-restricted content stops working, export fresh cookies from your browser and replace the file. Use a throwaway Google account, not your personal one as YouTube may flag or ban the account.

---

## YouTube PO Token Provider

The bot does not generate a PO token and does not pass one to yt-dlp. The `bgutil-ytdlp-pot-provider` package installed with the other Python dependencies is only the plugin. yt-dlp loads that plugin and, when a YouTube request needs a token, asks the provider service at `http://127.0.0.1:4416`. That address is the plugin's default. This project does not set a different URL.

The service is a separate program. It is the Docker image `brainicism/bgutil-ytdlp-pot-provider`, published only on the host's IPv4 loopback. `setup-update.sh` checks for Docker and uses it when the daemon answers:

- It reads the installed plugin version and pulls that image tag. The plain `2.0.2` tag is the Node.js image. If that tag is missing, it pulls `latest`.
- It starts a container named `bgutil-provider` with `--init`, `--restart unless-stopped`, and `-p 127.0.0.1:4416:4416`.
- On a later setup or update, it pulls again. It leaves the container alone when the image, loopback port, init process, restart policy, and running state already match. Otherwise it replaces the container.

Without Docker, setup still finishes and prints a warning. YouTube playback then depends on clients that do not ask for a token. Install Docker and run `./setup-update.sh` again to start the provider.

The same check runs from `/manage` when a package update or a bot update runs, including the scheduled ones. It calls `./setup-update.sh --bgutil-only`, which does not touch the virtualenv or `.env`.

A manual start, if you are not using the script:

```bash
docker run --name bgutil-provider -d --init --restart unless-stopped \
  -p 127.0.0.1:4416:4416 brainicism/bgutil-ytdlp-pot-provider
```

---

## Auto-Updates

The bot has a built-in auto-update system that can automatically update both **pip packages** and the **bot code** itself. Configure it from the `/manage` command by selecting **Auto-update settings** from the dropdown.

### What it does

- **Package updates** - Runs `pip install --upgrade -r requirements.txt` on schedule and restarts if any package version changed. Each check also refreshes the YouTube PO token provider.
- **Bot code updates** - Checks GitHub for a new release. If found, pulls the latest code via `git pull` (for git clones) or downloads and extracts the release tarball (for ZIP installs), then restarts. Each check also refreshes the YouTube PO token provider.

### Schedule modes

- **Interval** - Run every N hours (1-24).
- **Fixed time** - Run once daily at a specific hour (0-23) in your configured timezone.

### Process manager required

The bot restarts by exiting cleanly after applying updates. **You need a process manager** that automatically restarts the bot when it exits. Any of these work:

- **systemd**
- **pm2** (`pm2 start run_bot.sh --name music-bot`). `run_bot.sh` clears `NODE_CHANNEL_FD` and `NODE_CHANNEL_SERIALIZATION_MODE` before Python starts. pm2 exports those for its own IPC, and Deno then rejects yt-dlp's JavaScript challenge solver with `fd is not from BiPipe`. The unset does nothing when those variables are absent.
- **supervisord**
- screen/tmux or custom shell

Without a process manager bot will just stop after an update and won't come back.

### Restart notifications

When the bot restarts for an update, it sends a notification message to the text channel of any guild where it has an active voice connection, so users know why playback was interrupted.

---

## Troubleshooting

### Bot doesn't respond to commands

- Make sure the bot has `Use Application Commands` permission in the channel.
- Wait a moment after startup, bot syncs commands on first launch, which can take a few seconds.
- Check if the bot is online in the server member list.

### "No JS runtime found" warning

Install one of: `deno`, `node`, `quickjs`, or `quickjs-ng`. This is required by yt-dlp for YouTube extraction.

### Age-restricted videos are skipped

Provide a cookie file from a logged-in YouTube account. See [Cookie File](#cookie-file-age-restricted-content).

### YouTube returns HTTP 403 or asks you to sign in

Run `./setup-update.sh` again. When Docker is available, that replaces `bgutil-provider` if it is missing, stopped, or still on an older image. `docker ps` should show `bgutil-provider` published on `127.0.0.1:4416`. A PO token does not replace cookies for an age-restricted video, and a missing provider does not block clients that do not ask for a token.

### Spotify playlists are truncated at ~100 tracks

Set up Spotify API credentials. See [Spotify Setup](#spotify-setup).

### Audio cuts out or reconnects

This is usually a network issue. The bot uses FFmpeg reconnect options with up to 15-second reconnect delay. If it persists:
- Check your server's network stability.
- Try a different voice region in Discord server settings.

### Bot uses too much memory or cpu

- The bot strips heavy yt-dlp metadata fields from entries to save RAM.
- Use `/manage garbage_collect:yes` to trigger manual garbage collection.
- Use `/manage` (with no options) to view current RAM and CPU usage.
- Prefetch can be disabled in global settings if memory is tight.

### Command sync seems stuck

The bot hashes the command tree and only syncs when it changes. The hash is stored in `db/.tree_hash`. To force a re-sync, use `/manage` resync or delete the hash file and restart.
