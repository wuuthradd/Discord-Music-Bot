# Commands & Features

All commands are slash commands. Command names are localized, the names below are the en-US defaults.

---

## Playback Commands

| Command | Description |
|---|---|
| `/play <query>` | Play any link that supported by yt-dlp or search term. Supports `shuffle` and `force` options. |
| `/pause` | Pause the current track. |
| `/resume` | Resume a paused track. |
| `/stop` | Stop playback and clear the queue. Has a `current` option to cancel playlist loading without stopping playback (DJ+). |
| `/skip` | Skip the current track (or vote to skip). |
| `/previous` | Play the previous track from history. |
| `/seek <seconds> [minutes] [hours]` | Seek to a position in the current track. |
| `/select <index>` | Jump to a specific track in the queue. |
| `/loop [mode]` | Set loop mode: off, song, or queue. |
| `/join` | Summon the bot to your voice channel. |
| `/leave` | Disconnect the bot from voice. |
| `/search <query>` | Search YouTube and pick from up to 10 results. |

## Queue & History

| Command | Description |
|---|---|
| `/queue` | Open the interactive queue view with pagination. |
| `/musicplayer` | Open the interactive music player embed. |
| `/shuffle [from] [to]` | Shuffle the queue (optionally a range). |
| `/move <from> <to>` | Move a track to a new position. Optional `to_index` for multi track moves. |
| `/remove <index> [to]` | Remove a track or range from the queue. Optional `search` for string base removes. |
| `/clear [mine] [user]` | Clear the queue, your tracks, or a user's tracks. |
| `/history` | Show recently played songs with replay options. |

## Playlist Commands

| Command | Description |
|---|---|
| `/playlist` | Open the interactive playlist manager. |

The playlist view provides buttons and menus for:
- **Create** / **Delete** / **Rename** playlists
- **Add tracks** - by URL/search, current playing track or entire queue
- **Remove** / **Move** tracks within a playlist
- **Play** / **Shuffle play** a playlist
- **Import** / **Export** playlists (JSON format)
- **Copy** a playlist from another user's shared view
- **Set favourite** - the default playlist for quick add
- **Share** - send an ebmd view of the playlist to others can copy from

## Radio

| Command | Description |
|---|---|
| `/radio` | Start or manage a radio session. |

See [Radio System](#radio-system) for details.

## Administration

| Command | Description |
|---|---|
| `/settings` | Open the interactive settings panel (admin only). |
| `/manage` | Bot diagnostics and management (bot owner only). Shows status when used without options. |
| `/timeout <user> <minutes>` | Temporarily block a user from using the bot. |
| `/help` | Browse all commands and features with an interactive help menu. |

---

## Music Player View

The music player (`/musicplayer`) is a persistent interactive embed that shows:

- **Track title** with link
- **Duration** with a progress bar
- **Requester**
- **Uploader**
- **View count**
- **Loop mode**
- **Queue position**
- **Vote information** (when voting is active)
- **Thumbnail**

**Default button layout** (fully customizable via `/settings`):

| Row | Buttons |
|---|---|
| Row 1 | Pause/Resume, Previous, Skip, Stop |
| Row 2 | Add Track, Remove, Move, Select, Seek |
| Row 3 | Loop, Shuffle, Clear, Refresh |

All buttons open modals for input where needed (e.g., Remove asks for an index, Move asks for source and destination). The view is persistent, it survives bot restarts and can be recovered from any state.

---

## Queue View

The queue view (`/queue`) shows tracks in a paginated embed with:

- Track list with title, uploader, and duration
- Page navigation (first, previous, next, last, go-to-page)
- Search within the queue
- Total duration in the footer
- Playing-since timestamp

Button layout is customizable via `/settings`.

---

## Settings

The `/settings` command opens an interactive configuration panel. Only users with admin privilege can access it. Settings are organized into categories:

### General

| Setting | Description | Default |
|---|---|---|
| **Language** | Bot language for this server | `en-US` |
| **Vote Mode** | How votes are counted: `half` (50%) or `half_plus_one` (50%+1). Also controls whether deafened users are excluded from vote count (default: excluded). | `half_plus_one` |
| **DJ Role** | Role that grants DJ privileges | None |
| **DJ Users** | Individual users with DJ privileges | None |
| **Delete After** | Auto-delete bot messages after N seconds (0 = off) | `10` |
| **Silent Log** | Suppress yt-dlp console output | Off |
| **Timezone** | Server timezone offset (UTC-12 to UTC+14) for timestamps | `UTC+0` |

### Limits & Restrictions

| Setting | Description | Default |
|---|---|---|
| **Max Playlists** | Maximum playlists per user | `15` |
| **Max History** | Maximum history entries stored per server (0 = off) | `50` |
| **Track Limit (Users/DJ/Admin)** | Max tracks a user can add to the queue at once, per privilege group (0 = unlimited) | `0` |
| **Excluded Users/Roles** | Block specific users or roles from using the bot | None |
| **Queue Limit** | Maximum tracks in the queue | `5000` |
| **Playlist Track Limit** | Maximum tracks per playlist | `5000` |

### Permissions & Roles

| Setting | Description | Default |
|---|---|---|
| **Admin Privilege** | Whether `Manage Guild` permission grants admin access | On |
| **Admin Users/Roles** | Explicit admin users/roles for bot management | None |
| **Pause Permission** | Who can pause: `everyone`, `requester_dj`, `dj`, `admin`, `owner` | `requester_dj` |
| **Seek Permission** | Who can seek: `everyone`, `requester_dj`, `dj`, `admin`, `owner` | `requester_dj` |
| **Seek Limit (per track)** | Max seeks per track for regular users (0 = unlimited) | `3` |
| **Seek Limit (DJ)** | Max seeks per track for DJs (0 = unlimited) | `0` |
| **Force Play Permission** | Who can use the force-play option | `dj` |
| **Radio Permission** | Who can start radio sessions | `dj` |
| **Radio Edit Permission** | Who can modify active radio sessions | `dj` |
| **Radio Cooldown** | Minutes between radio starts | `3` |
| **Join Restriction** | Restrict which voice channels the bot can join: `none`, `users` (whitelist), `dj`, `admin` | `none` |
| **Pause Timeout** | Auto-action after being paused for N seconds | `900` (15 min) |
| **Pause Timeout Behavior** | What happens on pause timeout: `leave`, `continue`, `skip` | `leave` |
| **Idle Disconnect** | Auto-disconnect after N seconds of idle (0 = off) | `180` (3 min) |
| **Force Radio** | Allow force play to interrupt active radio: `disabled`, `enabled` | `disabled` |
| **Live Playback** | Enable/disable live stream playback | Off |
| **Live Permission** | Who can play live streams: `everyone`, `dj`, `admin`, `owner` | `admin` |
| **Live Max Hours** | Maximum duration for live stream playback in hours (0 = unlimited) | `1` |

**Permission levels:** `everyone` - anyone, `requester_dj` - track requester + DJ + admin, `dj` - DJ + admin, `admin` - admin only, `owner` - server owner only.

### Performance (App Owner Only)

| Setting | Description | Default |
|---|---|---|
| **Prefetch** | Pre-fetch next track data for gapless playback | On |
| **Safe Prefetch** | Wait for current extraction to finish before prefetching | On |
| **Max Workers** | Maximum concurrent extraction workers (1-32) | `16` |

### Embed & View Customization

| Setting | Description |
|---|---|
| **Embed Color** | Custom color for all bot embeds |
| **Music Player Buttons** | Toggle, reorder, and rearrange MP buttons across rows |
| **Music Player Fields** | Toggle and reorder which fields appear on the MP embed |
| **Queue Buttons** | Toggle and reorder queue view buttons |
| **Queue Display** | Tracks per page, compact mode, and which footer fields to show |
| **View Channel** | Lock bot views to a specific channel |
| **View Restriction** | Control who can interact with views: disabled, non-DJ blocked, DJ/users only, all blocked |

### Bot Activity Status

Configure the bot's Discord presence:

- **Static mode** - Single activity text (e.g., "Listening to /play")
- **Random mode** - Randomly cycle through a list of activities
- **Ordered mode** - Cycle through activities in order

Activity types: Playing, Listening, Watching, Competing. Customizable cycle interval. This can be set or changed only by bot application owner.

---

## Permission System

The bot has a four-tier permission hierarchy plus a separate application owner role:

| Level | Who | Capabilities |
|---|---|---|
| **App Owner** | The bot's application owner (set in Discord Developer Portal) | Access to `/manage` (bot diagnostics/actions). Can open `/settings` on any guild but only sees global bot settings (silent log, bot activity). Does **not** have guild-level control unless they also hold Admin or Owner status on that guild. |
| **Server Owner** | The Discord server owner | Full control over all guild settings, can always act on playback commands without voting |
| **Admin** | Users/roles set as admin or users with `Manage Guild` permission (if admin privilege is enabled) | Access to `/settings`, `/timeout`, bypass most restrictions |
| **DJ** | Users with the DJ role or explicitly added as DJ users | Can skip, stop, clear, move, etc. without voting. Can bypass voice channel requirement for control commands |
| **User** | Everyone else | Can play, add tracks, and vote on actions. Must be in the same voice channel |

**Voice channel rules:**

- All playback commands require the user to be in the same voice channel as the bot.
- DJs and admins can bypass the same channel requirement for any command.

---

## Vote System

When a regular user (non-DJ, non-admin) tries to use a control command (skip, stop, pause, etc.), a vote is initiated:

1. The bot announces that a vote has been started.
2. Other users in the voice channel can vote by using the same command.
3. The action is executed when the vote threshold is reached.
4. Vote threshold is configurable: `half` (50% of listeners) or `half_plus_one` (50% + 1, the default).
5. Votes are **per-command, per-song** - they reset when the track changes.
6. The server owner can always act immediately without voting.

---

## Radio System

Radio mode creates an endless playback session based on a seed track:

1. Use `/radio` to open the radio configuration panel.
2. **Choose a source:**
   - **Queue** - Uses a random track from the current queue as the seed.
   - **History** - Uses a random track from play history as the seed.
   - **Search** - Enter a YouTube search query to find a seed track.
3. **Set limits (optional):**
   - **Track limit** - Auto-stop after N tracks (15-10,000).
   - **Time limit** - Auto-stop after N minutes (1-10,080).
4. Click **Start** to begin.

The radio fetches a pool of 300-600 related tracks from YouTube Music's recommendation algorithm (`RDAMVM`). When the pool runs low (< 20 tracks), it automatically refills using a random track from the original pool as a new seed.

**Radio edit view** (visible during an active radio session):
- Shows seed track, who started it, started at time, and track count.
- **Stop** button to end the session.

Radio permissions (start, edit) are configurable in `/settings`.

---

## Localization

The bot supports **31 locales** with full support for translating all user-facing strings, command names, and descriptions. For details on adding, editing, or validating locale files, see the [Localization Guide](../locales/README.md).

**Supported languages:**

| | | | |
|---|---|---|---|
| Български (bg) | Čeština (cs) | Dansk (da) | Deutsch (de) |
| Ελληνικά (el) | English UK (en-GB) | English US (en-US) | Español (es-ES) |
| Español LATAM (es-419) | Suomi (fi) | Français (fr) | हिन्दी (hi) |
| Hrvatski (hr) | Magyar (hu) | Italiano (it) | 日本語 (ja) |
| 한국어 (ko) | Lietuvių (lt) | Nederlands (nl) | Norsk (no) |
| Polski (pl) | Português Brasil (pt-BR) | Română (ro) | Русский (ru) |
| Svenska (sv-SE) | ไทย (th) | Türkçe (tr) | Українська (uk) |
| Tiếng Việt (vi) | 中文 简体 (zh-CN) | 中文 繁體 (zh-TW) | |

Set the server language with `/settings` -> Language.
