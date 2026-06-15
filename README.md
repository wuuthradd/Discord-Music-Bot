# Yet Another Discord yt-dlp Music Bot

A feature rich Discord music bot built with [discord.py](https://github.com/Rapptz/discord.py). Supports YouTube, Spotify, and everything else [yt-dlp/](https://github.com/yt-dlp/yt-dlp) supports with a fully interactive music player, queue management, playlists, radio sessions, voting system, deep and highly configurable multi guild supported all with slash commands.

## Features

- **Spotify playback** - Spotify links are supported, bot will search them in YouTube to play since its copyright protected.
- **Interactive Music Player** - A persistent embed with buttons for customizable controls for playback.
- **Queue system** - Up to 10,000 tracks per queue with paginated view, search, go-to-page.
- **Playlist system** - Per user saved playlists which highly customizable, share, import and export support.
- **Radio mode** - Endless radio sessions based on a seed track using YouTube Music's recommendation mix.
- **Voting system** - Democratic control: regular users vote on actions while DJs and admins can act immediately.
- **Per server settings** - Extensive configuration through an interactive `/settings` panel.
- **Localization** - 31 locale files with full support for translating all user-facing strings, command names, and descriptions.
- **Persistent views** - Music player and queue views survive bot restarts.
- **SQLite database** - All settings and playlists stored locally with WAL mode.
- **Lightweight intents** - Only requires `guilds` and `voice_states` intents.
- **Auto-updates** - Both pip packages and bot itself has auto update capabilites can be set with intervals or fixed time.

## Documentation

- **[Setup Guide](docs/SETUP.md)** - Requirements, installation, configuration, Spotify setup, troubleshooting
- **[Commands & Features](docs/COMMANDS.md)** - All commands, settings, permission system, vote system, radio system
- **[Localization Guide](locales/README.md)** - Adding and editing locale files

## Quick Start

Requires Python 3.10+, FFmpeg, and a JS runtime (deno, node, quickjs, or quickjs-ng).

Clone the repo or download a zip/tar.gz from the [latest release](../../releases/latest).

```bash
git clone https://github.com/wuuthradd/Discord-Music-Bot.git
cd Discord-Music-Bot

# Fill in your bot token in env-template, then:
chmod +x setup-update.sh
./setup-update.sh
./run_bot.sh
```

See the [Setup Guide](docs/SETUP.md) for detailed instructions.

## Why yt-dlp instead of Lavalink?

Most Discord music bots use [Lavalink](https://github.com/lavalink-devs/Lavalink), a standalone Java audio server. This bot uses [yt-dlp](https://github.com/yt-dlp/yt-dlp) + FFmpeg directly instead. The main reason is source support, yt-dlp supports [over 1,800 sites](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md) out of the box. Any URL that yt-dlp can extract audio from works as a playback source, with no plugins or extra configuration needed. Discord links, SoundCloud, Bandcamp, anything that yt-dlp can extract excluding copyright protected can be a track to play.

## Reporting Issues & Feature Requests

If you run into a bug, please [open an issue](../../issues/new?template=bug_report.yml) using the bug report template. It asks for your environment details, bot configuration and steps to reproduce so we can diagnose the problem quickly. You can also open a blank issue for general questions and feature requests but don't be vague about them.
