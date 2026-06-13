import asyncio
import io
import itertools
import time
import traceback
import math
import sys
from pathlib import Path

import discord
from discord.ext import commands

from locales.localization import t, init_locales_cache, guild_locales, DEFAULT_LOCALE
from core.playback import PlaybackManager, CommandCheckError, GuildMusicState, _task_done_cb
from core.media import log_youtube_status
from core.spotify import is_spotify_url
from core.safe_embed import SafeEmbed
from core.radio import fetch_radio_pool

DEFAULT_MP_LAYOUT = {
    "toggle":    {"row": 0, "col": 0, "enabled": True},
    "prev":      {"row": 0, "col": 1, "enabled": True},
    "skip":      {"row": 0, "col": 2, "enabled": True},
    "stop":      {"row": 0, "col": 3, "enabled": True},
    "add_track": {"row": 1, "col": 0, "enabled": True},
    "remove":    {"row": 1, "col": 1, "enabled": True},
    "move":      {"row": 1, "col": 2, "enabled": True},
    "select":    {"row": 1, "col": 3, "enabled": True},
    "seek":      {"row": 1, "col": 4, "enabled": True},
    "loop":      {"row": 2, "col": 0, "enabled": True},
    "shuffle":   {"row": 2, "col": 1, "enabled": True},
    "clear":     {"row": 2, "col": 2, "enabled": True},
    "refresh":   {"row": 2, "col": 3, "enabled": True},
}

DEFAULT_QUEUE_LAYOUT = {
    "first":     {"row": 0, "col": 0, "enabled": True},
    "prev_page": {"row": 0, "col": 1, "enabled": True},
    "next_page": {"row": 0, "col": 2, "enabled": True},
    "last":      {"row": 0, "col": 3, "enabled": True},
    "goto":      {"row": 0, "col": 4, "enabled": True},
    "search":    {"row": 1, "col": 0, "enabled": True},
    "refresh":   {"row": 1, "col": 1, "enabled": True},
}

_MP_BUTTON_LABELS = {
    "toggle": "BUTTON_TOGGLE", "prev": "BUTTON_PREV_TRACK",
    "skip": "BUTTON_SKIP", "stop": "BUTTON_STOP",
    "add_track": "BUTTON_ADD_TRACK", "remove": "BUTTON_REMOVE",
    "move": "BUTTON_MOVE", "select": "BUTTON_SELECT",
    "loop": "BUTTON_LOOP", "shuffle": "BUTTON_SHUFFLE",
    "seek": "BUTTON_SEEK", "clear": "BUTTON_CLEAR_QUEUE",
    "refresh": "BUTTON_REFRESH",
}

_QUEUE_BUTTON_LABELS = {
    "first": "BUTTON_FIRST", "prev_page": "BUTTON_PREV",
    "next_page": "BUTTON_NEXT", "last": "BUTTON_LAST",
    "goto": "BUTTON_GOTO_PAGE", "search": "BUTTON_SEARCH",
    "refresh": "BUTTON_REFRESH",
}

DEFAULT_MP_FIELDS = {
    "duration":  {"order": 0, "enabled": True},
    "requester": {"order": 1, "enabled": True},
    "loop":      {"order": 2, "enabled": True},
    "url":       {"order": 3, "enabled": True},
    "views":     {"order": 4, "enabled": True},
    "uploader":  {"order": 5, "enabled": True},
    "queue":     {"order": 6, "enabled": True},
    "thumbnail": {"order": 7, "enabled": True},
    "vote_info": {"order": 8, "enabled": True},
}

_MP_FIELD_LABELS = {
    "duration":  "DURATION",
    "requester": "REQUESTER",
    "loop":      "LOOP",
    "url":       "URL_LABEL",
    "views":     "VIEWS",
    "uploader":  "UPLOADER",
    "queue":     "QUEUE_LABEL",
    "thumbnail": "MP_FIELD_THUMBNAIL",
    "vote_info": "MP_FIELD_VOTE_INFO",
}
_MP_REORDERABLE_FIELDS = ("duration", "requester", "loop", "url", "views", "uploader")

DEFAULT_QUEUE_FIELDS = {
    "total_duration": {"enabled": True},
    "playing_since":  {"enabled": True},
}

_QUEUE_FIELD_LABELS = {
    "total_duration": "QUEUE_FOOTER_TOTAL_DURATION",
    "playing_since":  "QUEUE_FOOTER_PLAYING_SINCE",
}

from db.db import db

PLACEHOLDER_IMAGE_PATH = (Path(__file__).resolve().parent.parent / "resources" / "no_thumbnail.png").resolve()
_PLACEHOLDER_EXISTS = PLACEHOLDER_IMAGE_PATH.exists()
_PLACEHOLDER_BYTES = PLACEHOLDER_IMAGE_PATH.read_bytes() if _PLACEHOLDER_EXISTS else None

DEFAULT_VOTE_MODE = "half_plus_one"
_VOTE_EMOJI = {
    "skip": "⏭", "previous": "⏮", "stop": "⏹", "select": "▶", "remove": "🗑",
    "move": "↕", "shuffle": "🔀", "clear": "🧹", "loop": "🔁",
    "pause": "⏸",
}
MAX_QUEUE = 5000                        # tracks, max items in the play queue
EMBED_COLOR = 5053538
README_URL = "https://github.com/OWNER/REPO#spotify-setup"

_MISSING = object()


class RadioSession:
    __slots__ = (
        "guild_id", "starter_id", "source_type", "source_query",
        "seed_track", "original_pool", "used_seeds",
        "track_limit", "tracks_played", "timeout_minutes",
        "started_at", "active", "refill_failed", "_fetch_lock",
        "_initial_fetch", "_timeout_task", "_refill_task",
    )

    def __init__(
        self,
        guild_id: int,
        starter_id: int,
        source_type: str,
        seed_track: dict,
    ):
        self.guild_id = guild_id
        self.starter_id = starter_id
        self.source_type = source_type     # "query" | "history" | "queue" | "playlist"
        self.source_query: str | None = None  # original query text when source_type == "query"
        self.seed_track = seed_track       # {id, title, url, uploader, ...}
        self.original_pool: list[dict] = []  # tracks from first fetch (for refill seed picking)
        self.used_seeds: set[str] = set()    # IDs already used as refill seeds
        self.track_limit: int = 0          # 0 = infinite, 15–5000
        self.tracks_played: int = 0
        self.timeout_minutes: int = 0      # 0 = no timeout
        self.started_at: float = 0.0
        self.active: bool = True
        self.refill_failed: bool = False
        self._fetch_lock: asyncio.Lock = asyncio.Lock()
        self._initial_fetch: asyncio.Task | None = None
        self._timeout_task: asyncio.Task | None = None
        self._refill_task: asyncio.Task | None = None


def _fmt_dur(seconds):
    if not isinstance(seconds, (int, float)):
        return ""
    s = int(seconds)
    if s >= 3600:
        return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"
    return f"{s // 60:02d}:{s % 60:02d}"


def _fmt_uptime(ctx, elapsed: int | float) -> str:
    elapsed = int(elapsed)
    days, rem = divmod(elapsed, 86400)
    hrs, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    _d = t(ctx, 'ABBR_DAYS')
    _h = t(ctx, 'ABBR_HOURS')
    _m = t(ctx, 'ABBR_MINUTES')
    _s = t(ctx, 'ABBR_SECONDS')
    if days:
        return f"{days}{_d} {hrs}{_h} {mins}{_m}"
    if hrs:
        return f"{hrs}{_h} {mins}{_m}"
    return f"{mins}{_m} {secs}{_s}"


def _fmt_track_lines(tracks, ctx_or_interaction, *, start_index: int = 1):
    lines = []
    for i, tr in enumerate(tracks, start=start_index):
        title = tr.get("title") or t(ctx_or_interaction, "UNKNOWN")
        uploader = tr.get("uploader") or t(ctx_or_interaction, "UNKNOWN")
        dur_str = _fmt_dur(tr.get("duration"))
        url = tr.get("url", "")
        if url:
            safe_title = title.replace("[", "⌜").replace("]", "⌝")
            linked = f"[{safe_title}]({url})"
        else:
            linked = title
        dur_part = f" - `{dur_str}`" if dur_str else ""
        lines.append(f"`{i}.` {linked} - *{uploader}*{dur_part}")
    return lines


def count_listeners(channel, *, exclude_deafened: bool = False) -> int:
    if exclude_deafened:
        return sum(1 for m in channel.members if not m.bot and not (m.voice and (m.voice.deaf or m.voice.self_deaf)))
    return sum(1 for m in channel.members if not m.bot)


async def respond_with_error(interaction: discord.Interaction, error: CommandCheckError):
    cog = interaction.client.get_cog("MusicCog")
    if cog:
        await cog.send_reply(interaction, error.message, ephemeral=error.ephemeral)
    else:
        try:
            if interaction.response.is_done():
                await interaction.followup.send(error.message, ephemeral=error.ephemeral)
            else:
                await interaction.response.send_message(error.message, ephemeral=error.ephemeral)
        except (discord.NotFound, discord.HTTPException):
            pass


class MusicHandlers(commands.Cog):
    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot
        self.guild_states: dict[int, GuildMusicState] = {}
        self.dj_roles: dict[int, int] = {}
        self.command_votes = {
            "skip": {},
            "previous": {},
            "select": {},
            "stop": {},
            "clear": {},
            "remove": {},
            "move": {},
            "shuffle": {},
            "loop": {},
            "pause": {},
        }
        self.active_searches: dict[tuple[int, int], discord.Message] = {}
        self.active_helps: dict[tuple[int, int], discord.Message] = {}
        self.active_mp: dict[int, tuple[discord.Message, discord.Interaction, discord.ui.View]] = {}
        self.active_queues: dict[int, tuple] = {}  # (msg, view, ViewCls)
        self._silent_log: bool = True
        self.vote_modes: dict[int, str] = {}
        self.guild_delete_after: dict[int, int] = {}
        self.guild_embed_colors: dict[int, int] = {}
        self.guild_compact_modes: dict[int, bool] = {}
        self.guild_max_playlists: dict[int, int] = {}
        self.guild_queue_per_page: dict[int, int] = {}
        self.guild_queue_compact: dict[int, bool] = {}
        self.guild_view_channels: dict[int, int] = {}
        self.guild_view_restricts: dict[int, int] = {}
        self._owner_override_views: set[tuple[int, str]] = set()
        self._intentional_disconnect: set[int] = set()
        self.dj_users: dict[int, set[int]] = {}  # guild_id -> set of user_ids
        self.guild_max_history: dict[int, int] = {}
        self.guild_max_user_tracks: dict[int, int] = {}
        self.excluded_users: dict[int, set[int]] = {}
        self.excluded_roles: dict[int, set[int]] = {}
        self.admin_users: dict[int, set[int]] = {}
        self.admin_roles: dict[int, set[int]] = {}
        self.guild_admin_priv: dict[int, int] = {}
        self.guild_queue_button_compact: dict[int, int] = {}
        self.guild_track_limit_target: dict[int, str] = {}
        self.guild_track_limit_users: dict[int, int] = {}
        self.guild_track_limit_dj: dict[int, int] = {}
        self.guild_track_limit_admin: dict[int, int] = {}
        self.guild_pause_permission: dict[int, str] = {}
        self.guild_pause_timeout: dict[int, int] = {}
        self.guild_pause_timeout_behavior: dict[int, str] = {}
        self.guild_timezones: dict[int, int] = {}
        self.guild_queue_limit: dict[int, int] = {}
        self.guild_playlist_track_limit: dict[int, int] = {}
        self.guild_seek_permission: dict[int, str] = {}
        self.guild_max_seeks_per_track: dict[int, int] = {}
        self.guild_max_seeks_dj: dict[int, int] = {}
        self.guild_idle_disconnect: dict[int, int] = {}
        self.guild_join_restrict_level: dict[int, str] = {}
        self.guild_join_restrict_channels: dict[int, set[int]] = {}
        self.guild_embed_layouts: dict[int, dict] = {}
        self._timeouts: dict[tuple[int, int], float] = {}  # (guild_id, user_id) -> expires_at
        self.radio_sessions: dict[int, RadioSession] = {}  # guild_id -> active session
        self._radio_config_interactions: dict[tuple[int, int], discord.Interaction] = {}  # (guild_id, user_id) -> interaction
        self._radio_cooldowns: dict[tuple[int, int], float] = {}  # (guild_id, user_id) -> earliest reuse
        self.guild_radio_permissions: dict[int, str] = {}
        self.guild_radio_edit_permissions: dict[int, str] = {}
        self.guild_radio_cooldowns: dict[int, int] = {}
        self._radio_initializing: set[int] = set()
        self.guild_force_play_permission: dict[int, str] = {}
        self.guild_force_radio: dict[int, str] = {}
        self.guild_vote_exclude_deafened: dict[int, int] = {}
        self.guild_live_enabled: dict[int, int] = {}
        self.guild_live_permission: dict[int, str] = {}
        self.guild_live_max_hours: dict[int, int] = {}
        self._prefetch: bool = True
        self._safe_prefetch: bool = True
        self._extract_active: int = 0
        self._max_workers: int = 16
        self.bot_activity_type: int = 2
        self.bot_activity_text: str = "/play"
        self.bot_activity_list: list[dict] = []
        self.bot_activity_mode: str = "static"
        self.bot_activity_interval: int = 120
        self.bot_activity_selected: int = 0
        self._activity_cycle_task: asyncio.Task | None = None
        self.active_playlists: dict[tuple[int, int], tuple] = {}  # (guild_id, user_id) -> (msg, view)
        self.playlist_busy: set[tuple[int, int]] = set()  # (guild_id, viewer_id) - blocks view re-creation while an action is running
        self.active_settings: dict[tuple[int, int], discord.Message] = {}  # (guild_id, user_id) -> msg
        self.active_history: dict[tuple[int, int], discord.Message] = {}  # (guild_id, user_id) -> msg
        self._vote_user_keys: dict[str, dict[int, dict[int, object]]] = {}
        self._bg_fetch_tasks: dict[int, asyncio.Task] = {}
        self._bg_fetch_forced_tasks: dict[int, asyncio.Task] = {}
        self._command_locks: dict[int, asyncio.Lock] = {}
        self._pending_refresh: dict[int, asyncio.Task] = {}
        self._last_refresh: dict[int, float] = {}
        self._refresh_active: set[int] = set()
        self._refresh_again: dict[int, bool] = {}
        self._active_copy_pickers: dict[tuple[int, int], discord.Interaction] = {}  # (guild_id, user_id) -> origin interaction
        self._refresh_backoff: dict[int, float] = {}
        self._ui_cooldowns: dict[tuple, float] = {}
        self.playback = PlaybackManager(bot, self.guild_states, self.command_votes, self._vote_user_keys, self.is_silent_log, self.is_prefetch_enabled)
        self.playback._intentional_disconnect = self._intentional_disconnect
        self.playback.on_track_change = lambda gid: self._schedule_refresh(gid, immediate=True)
        self.playback.on_disconnect = self._cleanup_views
        self.playback.on_track_played = self._record_history
        self.playback.on_radio_track_finished = self._on_radio_track_finished
        self.playback.guild_pause_timeout_behavior = self.guild_pause_timeout_behavior
        self.playback.guild_idle_disconnect = self.guild_idle_disconnect
        self.playback.guild_live_max_hours = self.guild_live_max_hours
        self._start_time = time.monotonic()

    def _cancel_bg_fetch(self, guild_id: int, *, forced_only: bool = False):
        if not forced_only:
            task = self._bg_fetch_tasks.pop(guild_id, None)
            if task and not task.done():
                task.cancel()
        ftask = self._bg_fetch_forced_tasks.pop(guild_id, None)
        if ftask and not ftask.done():
            ftask.cancel()

    def _cleanup_views(self, guild_id: int):
        self.playback._cancel_live_timer(guild_id)
        self._cancel_bg_fetch(guild_id)
        pending = self._pending_refresh.pop(guild_id, None)
        if pending and not pending.done():
            pending.cancel()
        self._refresh_active.discard(guild_id)
        self._refresh_again.pop(guild_id, None)
        old_mp = self.active_mp.pop(guild_id, None)
        if old_mp:
            view = old_mp[2] if isinstance(old_mp, tuple) else old_mp
            if view and hasattr(view, "stop"):
                view.stop()
        old_q = self.active_queues.pop(guild_id, None)
        if old_q:
            view = old_q[1] if isinstance(old_q, tuple) else old_q
            if view and hasattr(view, "stop"):
                view.stop()
        session = self.radio_sessions.pop(guild_id, None)
        if session:
            session.active = False
            for task in (session._timeout_task, session._initial_fetch, session._refill_task):
                if task and not task.done():
                    task.cancel()
            session._timeout_task = None
            session._initial_fetch = None
            session._refill_task = None

    def _record_history(self, guild_id: int, entry: dict):
        max_h = self.guild_max_history.get(guild_id, 50)
        if max_h <= 0:
            return
        url = entry.get("webpage_url") or entry.get("spotify_url") or entry.get("url") or ""
        if url and getattr(self, "_last_history_url", {}).get(guild_id) == url:
            return
        if not hasattr(self, "_last_history_url"):
            self._last_history_url = {}
        self._last_history_url[guild_id] = url
        self._create_task(db.add_history_entry(
            guild_id,
            entry.get("title"), entry.get("uploader"),
            entry.get("duration"),
            url,
            entry.get("requester", 0),
            max_entries=max_h,
        ), name=f"history-{guild_id}")

    def get_max_history(self, guild_id: int) -> int:
        return self.guild_max_history.get(guild_id, 50)

    def check_join_restriction(self, guild_id: int, channel_id: int, member) -> bool:
        """Return True if the member is allowed to make the bot join the given channel."""
        level = self.guild_join_restrict_level.get(guild_id, "none")
        if level == "none":
            return True
        channels = self.guild_join_restrict_channels.get(guild_id, set())
        if not channels:
            return True
        if channel_id in channels:
            return True
        if self._is_effective_owner(guild_id, member):
            return True
        if level == "users":
            return self.has_control_privilege(guild_id, member)
        if level == "dj":
            return self.has_admin_privilege(guild_id, member)
        if level == "admin":
            return False
        return True

    def is_excluded(self, guild_id: int, member) -> bool:
        if member.id == member.guild.owner_id:
            return False
        owner_id = getattr(self.bot, 'owner_id', None)
        if owner_id is not None and member.id == owner_id:
            return False
        if member.id in self.excluded_users.get(guild_id, set()):
            return True
        excluded_roles = self.excluded_roles.get(guild_id, set())
        return bool(excluded_roles & {r.id for r in member.roles})

    @staticmethod
    def _check_expiry(store: dict, guild_id: int, user_id: int) -> float | None:
        key = (guild_id, user_id)
        expires = store.get(key)
        if expires is None:
            return None
        if time.time() >= expires:
            store.pop(key, None)
            return None
        return expires

    def is_timed_out(self, guild_id: int, user_id: int) -> float | None:
        return self._check_expiry(self._timeouts, guild_id, user_id)

    # -- radio helpers --

    def _check_tiered_permission(self, guild_id: int, member, perm: str, *, allow_everyone: bool = False) -> bool:
        if allow_everyone and perm == "everyone":
            return True
        if perm == "dj":
            return self.has_control_privilege(guild_id, member) or self.has_admin_privilege(guild_id, member)
        if perm == "requester_dj":
            return self.has_control_privilege(guild_id, member) or self.has_admin_privilege(guild_id, member)
        if perm == "admin":
            return self.has_admin_privilege(guild_id, member)
        if perm == "owner":
            return self._is_effective_owner(guild_id, member)
        return False

    def has_radio_permission(self, guild_id: int, member) -> bool:
        perm = self.guild_radio_permissions.get(guild_id, "dj")
        return self._check_tiered_permission(guild_id, member, perm, allow_everyone=True)

    def has_radio_edit_permission(self, guild_id: int, member) -> bool:
        perm = self.guild_radio_edit_permissions.get(guild_id, "dj")
        return self._check_tiered_permission(guild_id, member, perm)

    def check_radio_cooldown(self, guild_id: int, user_id: int) -> float | None:
        return self._check_expiry(self._radio_cooldowns, guild_id, user_id)

    def set_radio_cooldown(self, guild_id: int, user_id: int, seconds: float):
        self._radio_cooldowns[(guild_id, user_id)] = time.time() + seconds

    def get_radio_session(self, guild_id: int) -> RadioSession | None:
        session = self.radio_sessions.get(guild_id)
        if session and not session.active:
            self.radio_sessions.pop(guild_id, None)
            return None
        return session

    def is_radio_active(self, guild_id: int) -> bool:
        return self.get_radio_session(guild_id) is not None

    def _schedule_radio_timeout(self, guild_id: int):
        session = self.get_radio_session(guild_id)
        if not session:
            return
        if session._timeout_task and not session._timeout_task.done():
            session._timeout_task.cancel()
            session._timeout_task = None
        if session.timeout_minutes <= 0:
            return
        remaining = session.timeout_minutes * 60 - (time.time() - session.started_at)
        if remaining <= 0:
            session._timeout_task = self._create_task(self._end_radio(guild_id, "timeout"), name=f"radio-end-timeout-{guild_id}")
            return

        async def _timeout_waiter():
            await asyncio.sleep(remaining)
            if session.active:
                await self._end_radio(guild_id, "timeout")

        session._timeout_task = self._create_task(_timeout_waiter(), name=f"radio-timeout-{guild_id}")

    async def _on_radio_track_finished(self, guild_id: int):
        session = self.get_radio_session(guild_id)
        if not session:
            state = self.guild_states.get(guild_id)
            if state:
                state._seeking = False
            return
        state = self.guild_states.get(guild_id)
        if state and state._seeking:
            state._seeking = False
            return
        if state and state.now and state.now.get("_radio_forced"):
            return
        session.tracks_played += 1
        if session.track_limit > 0 and session.tracks_played >= session.track_limit:
            await self._end_radio(guild_id, "limit")
            return
        # If queue is empty and the initial background fetch is still running, wait for it
        state = self.guild_states.get(guild_id)
        if state and not state.queue and session._initial_fetch and not session._initial_fetch.done():
            try:
                await session._initial_fetch
            except Exception:
                pass
        # Refill when queue drops to 20 or fewer tracks
        if state and len(state.queue) <= 20 and not session._fetch_lock.locked():
            old = session._refill_task
            if old and not old.done():
                old.cancel()
            session._refill_task = self._create_task(self._radio_refill(guild_id), name=f"radio-refill-{guild_id}")

    async def _radio_refill(self, guild_id: int):
        """Refill radio queue by picking a new seed from the original pool."""
        import random
        session = self.get_radio_session(guild_id)
        if not session or not session.original_pool:
            return
        state = self.guild_states.get(guild_id)
        if not state:
            return
        async with session._fetch_lock:
            # Already refilled by another call
            if len(state.queue) > 20:
                return
            # Pick a seed from original pool that hasn't been used
            candidates = [tk for tk in session.original_pool if tk["id"] not in session.used_seeds]
            if not candidates:
                # All exhausted, reset and re-use original seed
                session.used_seeds.clear()
                candidates = list(session.original_pool)
            seed = random.choice(candidates)
            session.used_seeds.add(seed["id"])
            self._schedule_refresh(guild_id)
            try:
                tracks = await fetch_radio_pool(seed["id"])
            except Exception as e:
                print(f"[Radio refill] guild {guild_id}: {e}")
                session.refill_failed = True
                remaining = len(state.queue)
                effective = session.tracks_played + remaining
                if session.track_limit <= 0 or effective < session.track_limit:
                    session.track_limit = effective
                text_ch = state.text_channel
                if text_ch:
                    try:
                        await text_ch.send(
                            t(text_ch, "RADIO_REFILL_ERROR", remaining=remaining),
                            delete_after=state.effective_delete_after,
                        )
                    except discord.HTTPException:
                        pass
                self._schedule_refresh(guild_id)
                return
            if not tracks or not session.active:
                return
            # Filter out the refill seed, tracks already in queue, and tracks longer than 60 min
            existing_ids = {tr.get("id") for tr in state.queue if tr.get("id")}
            if state.now and state.now.get("id"):
                existing_ids.add(state.now["id"])
            existing_ids.add(seed["id"])
            tracks = [tr for tr in tracks if tr.get("id") not in existing_ids and (tr.get("duration") or 0) <= 3600]
            random.shuffle(tracks)
            q_limit = self.guild_queue_limit.get(guild_id, MAX_QUEUE)
            room = q_limit - len(state.queue)
            if room <= 0:
                return
            tracks = tracks[:room]
            for tr in tracks:
                tr["requester"] = self.bot.user.id
            state.queue.extend(tracks)
            self._schedule_refresh(guild_id)

    async def _end_radio(self, guild_id: int, reason: str, *, skip_cooldown: bool = False):
        session = self.radio_sessions.pop(guild_id, None)
        if not session:
            return
        session.active = False
        current = asyncio.current_task()
        for task in (session._timeout_task, session._initial_fetch, session._refill_task):
            if task and not task.done() and task is not current:
                task.cancel()
        session._timeout_task = None
        session._initial_fetch = None
        session._refill_task = None
        if not skip_cooldown:
            cooldown_secs = self.guild_radio_cooldowns.get(guild_id, 3) * 60
            self.set_radio_cooldown(guild_id, session.starter_id, cooldown_secs)

        if reason == "restart":
            self._cancel_bg_fetch(guild_id)
            self._schedule_refresh(guild_id)
            return

        self._cancel_bg_fetch(guild_id)
        state = self.guild_states.get(guild_id)
        if state:
            vc = state.vc
            if vc and (vc.is_playing() or vc.is_paused()):
                state.suppress_after_callback = True
                vc.stop()
            state.queue.clear()
            state.now = None
            state.playing = False
            state.playing_since = None
            state.paused_at = None
            state._total_paused = 0.0
            state.loop_mode = "off"
            self.playback.cleanup_guild_votes(guild_id)
            self.playback._cancel_live_timer(guild_id)
            state.cancel_tasks()
            idle_timeout = self.guild_idle_disconnect.get(guild_id, 180)
            if idle_timeout > 0:
                state.idle_disconnect_task = self._create_task(
                    self.playback.auto_disconnect_after(guild_id, idle_timeout),
                    name=f"idle-dc-{guild_id}",
                )
            if state.text_channel:
                key = {
                    "limit": "RADIO_ENDED_LIMIT",
                    "timeout": "RADIO_ENDED_TIMEOUT",
                    "stopped": "RADIO_ENDED_STOPPED",
                    "stopped_self": "RADIO_ENDED_STOPPED",
                    "fetch_failed": None,
                }.get(reason, "RADIO_ENDED_STOPPED")
                if key:
                    try:
                        await state.text_channel.send(
                            t(state.text_channel, key),
                            delete_after=self._resolve_delete_after(guild_id),
                        )
                    except Exception:
                        pass
        self._schedule_refresh(guild_id)

    def _get_layout(self, guild_id: int, key: str, default: dict) -> dict:
        full = self.guild_embed_layouts.get(guild_id)
        if full and key in full:
            merged = {}
            for k, v in default.items():
                override = full[key].get(k)
                if override is not None and isinstance(v, dict) and isinstance(override, dict):
                    merged[k] = {**v, **override}
                elif override is not None:
                    merged[k] = override
                else:
                    merged[k] = v if not isinstance(v, dict) else dict(v)
            return merged
        return {k: (dict(v) if isinstance(v, dict) else v) for k, v in default.items()}

    def get_mp_layout(self, guild_id: int) -> dict:
        return self._get_layout(guild_id, "mp", DEFAULT_MP_LAYOUT)

    def get_queue_layout(self, guild_id: int) -> dict:
        return self._get_layout(guild_id, "queue", DEFAULT_QUEUE_LAYOUT)

    def get_mp_fields(self, guild_id: int) -> dict:
        return self._get_layout(guild_id, "mp_fields", DEFAULT_MP_FIELDS)

    def get_queue_fields(self, guild_id: int) -> dict:
        return self._get_layout(guild_id, "queue_fields", DEFAULT_QUEUE_FIELDS)

    def has_admin_privilege(self, guild_id: int, member) -> bool:
        if member.id == member.guild.owner_id:
            return True
        if self.is_excluded(guild_id, member):
            return False
        if self.guild_admin_priv.get(guild_id, 1) and member.guild_permissions.manage_guild:
            return True
        au = self.admin_users.get(guild_id, set())
        ar = self.admin_roles.get(guild_id, set())
        if member.id in au:
            return True
        return bool(ar & {r.id for r in member.roles})

    def _is_effective_owner(self, guild_id: int, member) -> bool:
        if member.id == member.guild.owner_id:
            return True
        owner_id = getattr(self.bot, 'owner_id', None)
        return owner_id is not None and member.id == owner_id and self.has_admin_privilege(guild_id, member)

    _ACTIVITY_TYPE_MAP = {
        0: discord.ActivityType.playing,
        1: discord.ActivityType.streaming,
        2: discord.ActivityType.listening,
        3: discord.ActivityType.watching,
        5: discord.ActivityType.competing,
    }

    async def _set_presence(self, atype_int: int, text: str):
        atype = self._ACTIVITY_TYPE_MAP.get(atype_int, discord.ActivityType.listening)
        await self.bot.change_presence(activity=discord.Activity(type=atype, name=text))

    async def update_presence(self):
        if not self.bot_activity_list:
            await self._set_presence(self.bot_activity_type, self.bot_activity_text)
            return
        mode = self.bot_activity_mode
        lst = self.bot_activity_list
        if mode == "static":
            idx = max(0, min(self.bot_activity_selected, len(lst) - 1))
            item = lst[idx]
            await self._set_presence(item["type"], item["text"])
        elif mode == "random":
            import random
            if len(lst) > 1:
                last = getattr(self, '_activity_last_id', None)
                candidates = [x for x in lst if x.get("id") != last]
                item = random.choice(candidates) if candidates else random.choice(lst)
            else:
                item = lst[0]
            self._activity_last_id = item.get("id")
            await self._set_presence(item["type"], item["text"])
        elif mode == "ordered":
            idx = getattr(self, '_activity_order_idx', 0) % len(lst)
            item = lst[idx]
            self._activity_order_idx = idx + 1
            self._activity_last_id = item.get("id")
            await self._set_presence(item["type"], item["text"])

    def start_activity_cycle(self):
        if self._activity_cycle_task and not self._activity_cycle_task.done():
            self._activity_cycle_task.cancel()
        if not self.bot_activity_list or self.bot_activity_mode == "static":
            return
        self._activity_cycle_task = self._create_task(self._activity_cycle_loop(), name="activity-cycle")

    async def _activity_cycle_loop(self):
        try:
            while True:
                await asyncio.sleep(self.bot_activity_interval * 60)
                if not self.bot_activity_list or self.bot_activity_mode == "static":
                    break
                await self.update_presence()
        except asyncio.CancelledError:
            pass

    def _settings_loaders(self):
        from db.db import db
        return [
            ("DJ roles", db.get_all_dj_roles, self.dj_roles),
            ("vote_modes", db.get_all_vote_modes, self.vote_modes),
            ("delete_afters", db.get_all_delete_after, self.guild_delete_after),
            ("embed_colors", db.get_all_embed_colors, self.guild_embed_colors),
            ("compact_modes", db.get_all_compact_modes, self.guild_compact_modes),
            ("max_playlists", db.get_all_max_playlists, self.guild_max_playlists),
            ("queue_per_page", db.get_all_queue_per_page, self.guild_queue_per_page),
            ("queue_compact", db.get_all_queue_compact, self.guild_queue_compact),
            ("view_channels", db.get_all_view_channels, self.guild_view_channels),
            ("view_restricts", db.get_all_view_restricts, self.guild_view_restricts),
            ("max_history", db.get_all_max_history, self.guild_max_history),
            ("max_user_tracks", db.get_all_max_user_tracks, self.guild_max_user_tracks),
            ("embed_layouts", db.get_all_embed_layouts, self.guild_embed_layouts),
            ("dj_users", db.get_all_dj_users, self.dj_users),
            ("excluded_users", db.get_all_excluded_users, self.excluded_users),
            ("excluded_roles", db.get_all_excluded_roles, self.excluded_roles),
            ("admin_users", db.get_all_admin_users, self.admin_users),
            ("admin_roles", db.get_all_admin_roles, self.admin_roles),
            ("admin_priv", db.get_all_admin_priv, self.guild_admin_priv),
            ("queue_button_compact", db.get_all_queue_button_compact, self.guild_queue_button_compact),
            ("track_limit_target", db.get_all_track_limit_target, self.guild_track_limit_target),
            ("track_limit_users", db.get_all_track_limit_users, self.guild_track_limit_users),
            ("track_limit_dj", db.get_all_track_limit_dj, self.guild_track_limit_dj),
            ("track_limit_admin", db.get_all_track_limit_admin, self.guild_track_limit_admin),
            ("pause_permission", db.get_all_pause_permission, self.guild_pause_permission),
            ("pause_timeout", db.get_all_pause_timeout, self.guild_pause_timeout),
            ("pause_timeout_behavior", db.get_all_pause_timeout_behavior, self.guild_pause_timeout_behavior),
            ("timezone", db.get_all_timezones, self.guild_timezones),
            ("queue_limit", db.get_all_queue_limits, self.guild_queue_limit),
            ("playlist_track_limit", db.get_all_playlist_track_limits, self.guild_playlist_track_limit),
            ("seek_permission", db.get_all_seek_permissions, self.guild_seek_permission),
            ("max_seeks_per_track", db.get_all_max_seeks_per_track, self.guild_max_seeks_per_track),
            ("max_seeks_dj", db.get_all_max_seeks_dj, self.guild_max_seeks_dj),
            ("idle_disconnect", db.get_all_idle_disconnect_timeout, self.guild_idle_disconnect),
            ("join_restrict_level", db.get_all_join_restrict_level, self.guild_join_restrict_level),
            ("join_restrict_channels", db.get_all_join_restrict_channels, self.guild_join_restrict_channels),
            ("radio_permission", db.get_all_radio_permissions, self.guild_radio_permissions),
            ("radio_edit_permission", db.get_all_radio_edit_permissions, self.guild_radio_edit_permissions),
            ("radio_cooldown", db.get_all_radio_cooldowns, self.guild_radio_cooldowns),
            ("force_play_permission", db.get_all_force_play_permission, self.guild_force_play_permission),
            ("force_radio", db.get_all_force_radio, self.guild_force_radio),
            ("vote_exclude_deafened", db.get_all_vote_exclude_deafened, self.guild_vote_exclude_deafened),
            ("live_enabled", db.get_all_live_enabled, self.guild_live_enabled),
            ("live_permission", db.get_all_live_permission, self.guild_live_permission),
            ("live_max_hours", db.get_all_live_max_hours, self.guild_live_max_hours),
        ]

    _GUILD_ROW_MAP = [
        ("dj_role_id",          int,  "dj_roles"),

        ("vote_mode",           str,  "vote_modes"),
        ("delete_after",        int,  "guild_delete_after"),
        ("embed_color",         int,  "guild_embed_colors"),
        ("compact_mode",        bool, "guild_compact_modes"),
        ("max_playlists",       int,  "guild_max_playlists"),
        ("queue_per_page",      int,  "guild_queue_per_page"),
        ("queue_compact",       bool, "guild_queue_compact"),
        ("view_channel",        int,  "guild_view_channels"),
        ("view_restrict",       int,  "guild_view_restricts"),
        ("max_history",         int,  "guild_max_history"),
        ("max_user_tracks",     int,  "guild_max_user_tracks"),
        ("admin_priv",          int,  "guild_admin_priv"),
        ("queue_button_compact",int,  "guild_queue_button_compact"),
        ("track_limit_target",  str,  "guild_track_limit_target"),
        ("pause_permission",    str,  "guild_pause_permission"),
        ("pause_timeout",       int,  "guild_pause_timeout"),
        ("timezone",            int,  "guild_timezones"),
        ("queue_limit",         int,  "guild_queue_limit"),
        ("playlist_track_limit", int, "guild_playlist_track_limit"),
        ("seek_permission",     str,  "guild_seek_permission"),
        ("max_seeks_per_track", int,  "guild_max_seeks_per_track"),
        ("max_seeks_dj",        int,  "guild_max_seeks_dj"),
        ("radio_permission",    str,  "guild_radio_permissions"),
        ("radio_edit_permission", str, "guild_radio_edit_permissions"),
        ("radio_cooldown",      int,  "guild_radio_cooldowns"),
        ("track_limit_users",   int,  "guild_track_limit_users"),
        ("track_limit_dj",      int,  "guild_track_limit_dj"),
        ("track_limit_admin",   int,  "guild_track_limit_admin"),
        ("pause_timeout_behavior", str, "guild_pause_timeout_behavior"),
        ("idle_disconnect_timeout", int, "guild_idle_disconnect"),
        ("join_restrict_level", str,  "guild_join_restrict_level"),
        ("force_play_permission", str, "guild_force_play_permission"),
        ("force_radio",           str, "guild_force_radio"),
        ("vote_exclude_deafened", int, "guild_vote_exclude_deafened"),
        ("live_enabled",          int, "guild_live_enabled"),
        ("live_permission",       str, "guild_live_permission"),
        ("live_max_hours",        int, "guild_live_max_hours"),
    ]

    _GUILD_ENTITY_MAP = [
        ("dj_users",       "user_id", "dj_users"),
        ("excluded_users", "user_id", "excluded_users"),
        ("excluded_roles", "role_id", "excluded_roles"),
        ("admin_users",    "user_id", "admin_users"),
        ("admin_roles",    "role_id", "admin_roles"),
        ("join_restrict_channels", "channel_id", "guild_join_restrict_channels"),
    ]

    async def _reload_guild_from_db(self, guild_id: int):
        import json as _json
        from db.db import db
        from locales.localization import init_locales_cache

        row = await db.get_guild_settings_row(guild_id)

        for col, cast, attr in self._GUILD_ROW_MAP:
            target = getattr(self, attr)
            val = row.get(col) if row else None
            if val is not None:
                target[guild_id] = cast(val)
            else:
                target.pop(guild_id, None)

        raw_layout = row.get("embed_layout") if row else None
        if raw_layout:
            try:
                self.guild_embed_layouts[guild_id] = _json.loads(raw_layout)
            except Exception:
                self.guild_embed_layouts.pop(guild_id, None)
        else:
            self.guild_embed_layouts.pop(guild_id, None)

        for table, entity_col, attr in self._GUILD_ENTITY_MAP:
            target = getattr(self, attr)
            s = await db.get_guild_entity_set(table, entity_col, guild_id)
            if s:
                target[guild_id] = s
            else:
                target.pop(guild_id, None)

        await init_locales_cache(force=True)
        state = self.guild_states.get(guild_id)
        if state:
            state.delete_after = self.guild_delete_after.get(guild_id, 10)

    def _resolve_delete_after(self, guild_id: int | None) -> int | None:
        if not guild_id:
            return 10
        state = self.guild_states.get(guild_id)
        val = state.delete_after if state else self.guild_delete_after.get(guild_id, 10)
        return val if val else None

    def _maybe_restart_prefetch(self, guild_id: int, prev_head_id: int | None):
        """Restart prefetch if queue[0] identity (id()) changed after a mutation."""
        state = self.guild_states.get(guild_id)
        if not state or not state.playing or not state.queue:
            return
        if id(state.queue[0]) != prev_head_id:
            self.playback._start_prefetch(state, guild_id)

    def _schedule_refresh(self, guild_id: int, *, immediate: bool = False):
        """Schedule a debounced embed refresh.

        immediate=True  → refresh now, reset the 5-second cooldown timer.
                          Used for skip, previous, and natural track changes.
                          Still respects backoff from 429 errors.
        immediate=False → if no cooldown active, refresh now and start timer.
                          If cooldown active, reset timer to now+5 s (coalesce).
        """
        now = asyncio.get_event_loop().time()
        last = self._last_refresh.get(guild_id, 0)
        backoff_until = self._refresh_backoff.get(guild_id, 0)

        # If a refresh is actively editing (mid-HTTP), don't cancel it.
        # Mark that we need another refresh after it finishes.
        if guild_id in self._refresh_active:
            self._refresh_again[guild_id] = immediate or self._refresh_again.get(guild_id, False)
            return

        existing = self._pending_refresh.get(guild_id)
        if existing and not existing.done():
            existing.cancel()

        if now < backoff_until:
            do_now = False
        elif immediate:
            do_now = True
        else:
            do_now = now >= last + 5.0

        if do_now:
            async def _run():
                self._pending_refresh.pop(guild_id, None)
                self._refresh_active.add(guild_id)
                try:
                    await self._refresh_embeds(guild_id)
                    self._last_refresh[guild_id] = asyncio.get_event_loop().time()
                finally:
                    self._refresh_active.discard(guild_id)
                    again = self._refresh_again.pop(guild_id, None)
                    if again is not None:
                        self._schedule_refresh(guild_id, immediate=again)
        else:
            delay = max(last + 5.0, backoff_until) - now

            async def _run():
                await asyncio.sleep(delay)
                self._pending_refresh.pop(guild_id, None)
                self._refresh_active.add(guild_id)
                try:
                    await self._refresh_embeds(guild_id)
                    self._last_refresh[guild_id] = asyncio.get_event_loop().time()
                finally:
                    self._refresh_active.discard(guild_id)
                    again = self._refresh_again.pop(guild_id, None)
                    if again is not None:
                        self._schedule_refresh(guild_id, immediate=again)

        self._pending_refresh[guild_id] = self._create_task(_run(), name=f"refresh-{guild_id}")

    async def _refresh_embeds(self, guild_id: int):
        """Refreshes active musicplayer and queue embeds for a guild."""
        results = await asyncio.gather(
            self._refresh_mp(guild_id),
            self._refresh_queue(guild_id),
            return_exceptions=True,
        )
        for label, r in zip(("MP", "Queue"), results):
            if isinstance(r, Exception):
                print(f"[{label} refresh fatal] guild {guild_id}: {type(r).__name__}: {r}")

    async def _refresh_mp(self, guild_id: int):
        entry = self.active_mp.get(guild_id)
        if not entry:
            row = await db.get_active_view(guild_id, "mp")
            if not row:
                return
            ch_id, msg_id = row
            ch = self.bot.get_channel(ch_id)
            if not ch:
                await db.delete_active_view(guild_id, "mp")
                return
            from core.music_cog import _GuildCtx
            guild = ch.guild
            msg = ch.get_partial_message(msg_id)
            ctx = _GuildCtx(guild)
            entry = (msg, ctx, None)
            self.active_mp[guild_id] = entry

        msg, ctx, old_view = entry

        # Channel restriction: if a view_channel is set and msg is not in it, delete
        # (skip if owner explicitly confirmed sending to this channel)
        view_ch = self.guild_view_channels.get(guild_id)
        if view_ch and msg.channel.id != view_ch and (guild_id, "mp") not in self._owner_override_views:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
            self.active_mp.pop(guild_id, None)
            await db.delete_active_view(guild_id, "mp")
            return

        state = self.guild_states.get(guild_id)
        from core.music_cog import MusicCog
        if state:
            state._mp_rev += 1
            my_rev = state._mp_rev
        else:
            my_rev = 0
        view = None
        try:
            if not state or not state.now:
                embed, file = self.build_idle_embed(ctx)
                view = MusicCog.MusicPlayerView(ctx, self, idle=True)
                await msg.edit(embed=embed, view=view, attachments=[file] if file else [])
            else:
                embed, file = self.build_mp_embed(ctx, state, attach_placeholder=True)
                view = MusicCog.MusicPlayerView(ctx, self)
                await msg.edit(embed=embed, view=view, attachments=[file] if file else [])
            if old_view is not None and old_view is not view:
                old_view.stop()
            self.active_mp[guild_id] = (msg, ctx, view)
            try:
                self.bot.add_view(view, message_id=msg.id)
            except Exception:
                pass
            if state and state._mp_rev != my_rev:
                self._schedule_refresh(guild_id)
        except discord.NotFound:
            if old_view is not None:
                old_view.stop()
            if view is not None:
                view.stop()
            self.active_mp.pop(guild_id, None)
            await db.delete_active_view(guild_id, "mp")
        except discord.HTTPException as e:
            if view is not None and view is not old_view:
                view.stop()
            if e.code == 30046 or e.status == 429:
                self._refresh_backoff[guild_id] = asyncio.get_event_loop().time() + 10
                self._schedule_refresh(guild_id)
            print(f"[MP refresh] guild {guild_id}: {type(e).__name__}: {e}")
        except Exception as e:
            if view is not None and view is not old_view:
                view.stop()
            print(f"[MP refresh] guild {guild_id}: {type(e).__name__}: {e}")

    async def _refresh_queue(self, guild_id: int):
        entry = self.active_queues.get(guild_id)
        if not entry:
            row = await db.get_active_view(guild_id, "queue")
            if not row:
                return
            ch_id, msg_id = row
            ch = self.bot.get_channel(ch_id)
            if not ch:
                await db.delete_active_view(guild_id, "queue")
                return
            from core.music_cog import _GuildCtx
            guild = ch.guild
            msg = ch.get_partial_message(msg_id)
            ctx = _GuildCtx(guild)
            session = self.get_radio_session(guild_id)
            if session and session.active:
                view, view_cls = self._build_radio_queue_view(ctx)
            else:
                view, view_cls = self._build_queue(ctx)
            entry = (msg, view, view_cls)
            self.active_queues[guild_id] = entry
        msg, old_view, view_cls = entry[0], entry[1], entry[2]

        # Channel restriction: if a view_channel is set and msg is not in it, delete
        # (skip if owner explicitly confirmed sending to this channel)
        view_ch = self.guild_view_channels.get(guild_id)
        if view_ch and msg.channel.id != view_ch and (guild_id, "queue") not in self._owner_override_views:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
            self.active_queues.pop(guild_id, None)
            await db.delete_active_view(guild_id, "queue")
            return

        # If radio mode changed, rebuild with the correct view class
        session = self.get_radio_session(guild_id)
        radio_active = bool(session and session.active)
        was_radio = getattr(old_view, 'is_radio', False)
        if radio_active != was_radio:
            from core.music_cog import _GuildCtx
            guild = self.bot.get_guild(guild_id)
            if guild:
                ctx = _GuildCtx(guild)
                if radio_active:
                    _, view_cls = self._build_radio_queue_view(ctx)
                else:
                    _, view_cls = self._build_queue(ctx)

        state = self.guild_states.get(guild_id)
        if getattr(old_view, '_busy', False):
            self._schedule_refresh(guild_id)
            return
        if state:
            state._queue_rev += 1
            my_rev = state._queue_rev
        else:
            my_rev = 0
        view = None
        try:
            queue = state.queue if state else []
            view = view_cls(queue, self.guild_states, self)
            view.page = min(old_view.page, max(0, view.total_pages - 1))
            view.update_buttons()
            await msg.edit(embed=view.get_embed(), view=view)
            if old_view is not None and old_view is not view:
                old_view.stop()
            self.active_queues[guild_id] = (msg, view, view_cls)
            try:
                self.bot.add_view(view, message_id=msg.id)
            except Exception:
                pass
            if state and state._queue_rev != my_rev:
                self._schedule_refresh(guild_id)
        except discord.NotFound:
            if old_view is not None:
                old_view.stop()
            if view is not None:
                view.stop()
            self.active_queues.pop(guild_id, None)
            await db.delete_active_view(guild_id, "queue")
        except discord.HTTPException as e:
            if view is not None and view is not old_view:
                view.stop()
            if e.code == 30046 or e.status == 429:
                self._refresh_backoff[guild_id] = asyncio.get_event_loop().time() + 10
                self._schedule_refresh(guild_id)
            print(f"[Queue refresh] guild {guild_id}: {type(e).__name__}: {e}")
        except Exception as e:
            if view is not None and view is not old_view:
                view.stop()
            print(f"[Queue refresh] guild {guild_id}: {type(e).__name__}: {e}")

    async def send_reply(self, interaction: discord.Interaction, content=None, **kwargs):
        """Helper to send a reply that safely handles deferrals and applies delete_after delays."""
        delete_after = kwargs.pop('delete_after', _MISSING)
        
        if delete_after is _MISSING:
            delete_after = self._resolve_delete_after(interaction.guild_id)

        try:
            if not interaction.response.is_done():
                resp = await interaction.response.send_message(content, delete_after=delete_after, **kwargs)
                return resp.resource if resp.resource else await interaction.original_response()
            else:
                msg = await interaction.followup.send(content, wait=True, **kwargs)
                if delete_after:
                    try:
                        await msg.delete(delay=delete_after)
                    except discord.HTTPException:
                        pass
                return msg
        except (discord.NotFound, discord.HTTPException):
            return None

    async def _edit_or_reply(self, interaction, msg, content, **kwargs):
        delete_after = kwargs.pop('delete_after', _MISSING)
        if delete_after is _MISSING:
            delete_after = self._resolve_delete_after(interaction.guild_id)
        if msg:
            try:
                await msg.edit(content=content)
                if delete_after:
                    await msg.delete(delay=delete_after)
                return msg
            except discord.HTTPException:
                pass
        return await self.send_reply(interaction, content, delete_after=delete_after, **kwargs)

    def get_state(self, guild_id: int) -> GuildMusicState:
        """Retrieves or creates the music state object for a specific guild."""
        state = self.guild_states.get(guild_id)
        if not state:
            state = GuildMusicState(guild_id)
            state.loop_mode = "off"
            state.delete_after = self.guild_delete_after.get(guild_id, 10)
            self.guild_states[guild_id] = state
        return state

    def has_control_privilege(self, guild_id: int, member: discord.Member) -> bool:
        """Checks if a member has DJ privileges. Excluded users are rejected first."""
        if self.is_excluded(guild_id, member):
            return False
        if member.guild_permissions.manage_guild:
            return True
        dj_user_set = self.dj_users.get(guild_id)
        if dj_user_set and member.id in dj_user_set:
            return True
        role_id = self.dj_roles.get(guild_id)
        if not role_id:
            return False
        return any(r.id == role_id for r in member.roles)

    def has_force_play_privilege(self, guild_id: int, member: discord.Member) -> bool:
        perm = self.guild_force_play_permission.get(guild_id, "dj")
        if perm == "everyone":
            return True
        if perm == "owner":
            return self._is_effective_owner(guild_id, member)
        if perm == "admin":
            return self.has_admin_privilege(guild_id, member)
        return self.has_control_privilege(guild_id, member) or self.has_admin_privilege(guild_id, member)

    def get_playlist_track_limit(self, guild_id: int) -> int:
        return self.guild_playlist_track_limit.get(guild_id, 5000)

    def get_embed_color(self, guild_id: int) -> int:
        return self.guild_embed_colors.get(guild_id, EMBED_COLOR)

    def is_compact(self, guild_id: int) -> bool:
        return self.guild_compact_modes.get(guild_id, False)

    def is_queue_button_compact(self, guild_id: int) -> bool:
        return bool(self.guild_queue_button_compact.get(guild_id, 0))

    def get_queue_per_page(self, guild_id: int) -> int:
        return self.guild_queue_per_page.get(guild_id, 10)

    def is_queue_compact(self, guild_id: int) -> bool:
        return self.guild_queue_compact.get(guild_id, True)

    def get_max_playlists(self, guild_id: int) -> int:
        return self.guild_max_playlists.get(guild_id, 15)

    def get_view_channel(self, guild_id: int) -> int | None:
        return self.guild_view_channels.get(guild_id)

    def get_view_restrict(self, guild_id: int) -> int:
        return self.guild_view_restricts.get(guild_id, 0)

    def check_view_restriction(self, guild_id: int, channel_id: int, member: discord.Member) -> tuple[str, int | None] | None:
        level = self.guild_view_restricts.get(guild_id, 0)
        view_ch = self.guild_view_channels.get(guild_id)
        is_owner = self._is_effective_owner(guild_id, member)
        if not is_owner:
            if level == 3:
                return ("user_blocked", view_ch)
            if level == 2 and not self.has_admin_privilege(guild_id, member):
                return ("user_blocked", view_ch)
            if level == 1 and not self.has_control_privilege(guild_id, member):
                return ("user_blocked", view_ch)
        if view_ch and channel_id != view_ch:
            if is_owner:
                return ("owner_wrong_channel", view_ch)
            return ("wrong_channel", view_ch)
        return None

    async def _save_view(self, guild_id: int, view_type: str, channel_id: int, message_id: int):
        try:
            await db.save_active_view(guild_id, view_type, channel_id, message_id)
        except Exception:
            pass

    async def _delete_stale_view(self, guild_id: int, view_type: str, bot):
        try:
            row = await db.get_active_view(guild_id, view_type)
            if row:
                ch_id, msg_id = row
                ch = bot.get_channel(ch_id)
                if ch:
                    try:
                        msg = ch.get_partial_message(msg_id)
                        await msg.delete()
                    except discord.HTTPException:
                        pass
                await db.delete_active_view(guild_id, view_type)
        except Exception:
            pass

    def is_silent_log(self, guild_id: int) -> bool:
        """Checks if FFmpeg/yt-dlp/discord-voice output should be suppressed (global setting)."""
        return self._silent_log

    def is_prefetch_enabled(self, guild_id: int) -> bool:
        if not self._prefetch:
            return False
        if self._safe_prefetch and self._extract_active > 0:
            return False
        return True

    def _acquire_extract(self):
        self._extract_active += 1
        if self._safe_prefetch:
            for state in self.guild_states.values():
                if state._prefetch_task and not state._prefetch_task.done():
                    state._prefetch_task.cancel()
                    state._prefetch_task = None

    def _release_extract(self):
        self._extract_active = max(0, self._extract_active - 1)
        if self._extract_active == 0 and self._prefetch:
            for gid, state in self.guild_states.items():
                if state.playing and state.queue and not (state._prefetch_task and not state._prefetch_task.done()):
                    self.playback._start_prefetch(state, gid)

    async def _check_cooldown(self, interaction, action: str, seconds: float, *, per_guild: bool = False) -> bool:
        """Returns True if the action is allowed. Sends ephemeral warning and returns False if on cooldown."""
        now = time.monotonic()
        if per_guild:
            key = (interaction.guild_id, action)
        else:
            key = (interaction.guild_id, interaction.user.id, action)
        last = self._ui_cooldowns.get(key, 0.0)
        if now - last < seconds:
            remaining = seconds - (now - last)
            title = t(interaction, "COOLDOWN_TITLE")
            desc = t(interaction, "COOLDOWN_DESC", seconds=remaining)
            da = self._resolve_delete_after(interaction.guild_id)
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        f"**{title}** {desc}", ephemeral=True, delete_after=da)
                else:
                    _msg = await interaction.followup.send(
                        f"**{title}** {desc}", ephemeral=True, wait=True)
                    if da and _msg:
                        await _msg.delete(delay=da)
            except Exception:
                pass
            return False
        self._ui_cooldowns[key] = now
        # Incremental eviction of stale entries
        if len(self._ui_cooldowns) > 5000:
            cutoff = now - 60
            stale = [k for k in itertools.islice(self._ui_cooldowns, 500) if self._ui_cooldowns[k] <= cutoff]
            for k in stale:
                del self._ui_cooldowns[k]
        return True

    def _create_task(self, coro, *, name: str | None = None):
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(_task_done_cb)
        return task

    def _update_discord_loggers(self):
        """Set discord.py voice/player log levels based on the global silent_log setting."""
        import logging
        level = logging.ERROR if self._silent_log else logging.WARNING
        for name in ("discord.voice_state", "discord.player"):
            logging.getLogger(name).setLevel(level)
        for name in ("discord.gateway", "discord.http"):
            logging.getLogger(name).setLevel(logging.ERROR)

    def get_vote_mode(self, guild_id: int) -> str:
        """Retrieves the current vote mode for the guild (e.g., 'half_plus_one')."""
        return self.vote_modes.get(guild_id, DEFAULT_VOTE_MODE)

    def _required_votes(self, guild_id: int, channel) -> int:
        exclude_deaf = bool(self.guild_vote_exclude_deafened.get(guild_id, 1))
        listener_count = count_listeners(channel, exclude_deafened=exclude_deaf) if channel else 1
        if listener_count <= 1:
            return 1
        mode = self.get_vote_mode(guild_id)
        if mode == "half":
            return math.ceil(listener_count / 2)
        return max(1, (listener_count // 2) + 1)

    def _prune_user_votes(self, guild_id: int, user_id: int):
        """Remove a user's votes from all active vote sets for the given guild."""
        for cmd_name, store in self.command_votes.items():
            if not isinstance(store, dict):
                continue
            for vote_key, entry in list(store.items()):
                if not PlaybackManager.is_vote_key_for_guild(vote_key, guild_id):
                    continue
                voters = entry.get("voters") if isinstance(entry, dict) else entry if isinstance(entry, set) else None
                if voters and user_id in voters:
                    voters.discard(user_id)
                    if not voters:
                        store.pop(vote_key, None)
            user_map = self._vote_user_keys.get(cmd_name)
            if user_map:
                guild_map = user_map.get(guild_id)
                if guild_map:
                    guild_map.pop(user_id, None)

    _FOOTER_MAX_PER_TYPE = 3

    def _vote_footer_text(self, guild_id: int, vc) -> str | None:
        if not vc or not getattr(vc, "channel", None):
            return None
        required = self._required_votes(guild_id, vc.channel)
        groups: dict[str, list[str]] = {}
        for cmd_name, store in self.command_votes.items():
            if not isinstance(store, dict) or not store:
                continue
            entries = []
            for vote_key, entry in list(store.items()):
                if not PlaybackManager.is_vote_key_for_guild(vote_key, guild_id):
                    continue
                voters = entry.get("voters") if isinstance(entry, dict) else entry if isinstance(entry, set) else None
                if not voters:
                    continue
                count = len(voters)
                tag = f"{count}/{required}"
                if not isinstance(vote_key, tuple) or len(vote_key) < 2:
                    entries.append(tag)
                elif cmd_name == "skip":
                    entries.append(tag)
                elif cmd_name == "loop":
                    entries.append(f"{vote_key[1]} | {tag}")
                elif cmd_name == "select":
                    entries.append(f"#{vote_key[1] + 1} | {tag}")
                elif cmd_name == "move" and len(vote_key) == 4:
                    entries.append(f"#{vote_key[1]}-#{vote_key[2]}→#{vote_key[3]} | {tag}")
                elif cmd_name == "move" and len(vote_key) == 3:
                    entries.append(f"#{vote_key[1]}→#{vote_key[2]} | {tag}")
                elif cmd_name == "remove" and len(vote_key) >= 3:
                    idx_parts = vote_key[1:]
                    label = f"#{idx_parts[0]}-#{idx_parts[-1]}"
                    entries.append(f"{label} | {tag}")
                elif cmd_name == "shuffle" and len(vote_key) == 3:
                    entries.append(f"#{vote_key[1]}-#{vote_key[2]} | {tag}")
                elif isinstance(vote_key[1], int):
                    entries.append(f"#{vote_key[1]} | {tag}")
                else:
                    entries.append(tag)
            if entries:
                groups[cmd_name] = entries
        if not groups:
            return None
        parts = []
        for cmd_name, entries in groups.items():
            emoji = _VOTE_EMOJI.get(cmd_name, "🗳")
            shown = entries[:self._FOOTER_MAX_PER_TYPE]
            overflow = len(entries) - self._FOOTER_MAX_PER_TYPE
            text = f"{emoji} {', '.join(shown)}"
            if overflow > 0:
                text += f" +{overflow}"
            parts.append(text)
        locale = guild_locales.get(guild_id) or DEFAULT_LOCALE
        return t(locale, "MP_VOTES_FOOTER") + " " + " · ".join(parts)

    def ensure_voice_and_state(
        self,
        interaction: discord.Interaction,
        *,
        same_channel: bool = True,
        admin_ok: bool = True,
        require_queue: bool = False,
        require_now: bool = False,
        queue_message: str | None = None,
        now_message: str | None = None
    ):
        """
        Validates voice connection and music state requirements for a command.
        Raises CommandCheckError if conditions are not met.
        """
        vc = interaction.guild.voice_client if interaction.guild else None
        if not vc:
            raise CommandCheckError(t(interaction, "BOT_NOT_IN_VOICE"), ephemeral=True)
        if same_channel and not (admin_ok and (self.has_control_privilege(interaction.guild_id, interaction.user) or self.has_admin_privilege(interaction.guild_id, interaction.user))):
            user_voice = getattr(interaction.user, "voice", None)
            if not user_voice or user_voice.channel != vc.channel:
                raise CommandCheckError(t(interaction, "SAME_CHANNEL_REQUIRED"), ephemeral=True)
        state = self.guild_states.get(interaction.guild_id)
        if require_queue and (not state or not state.queue):
            raise CommandCheckError(queue_message or t(interaction, "QUEUE_EMPTY"), ephemeral=True)
        if require_now and (not state or not state.now):
            raise CommandCheckError(now_message or t(interaction, "NOTHING_PLAYING"), ephemeral=True)
        return vc, state

    def has_pause_permission(self, guild_id: int, member, requester_id: int | None = None) -> bool:
        perm = self.guild_pause_permission.get(guild_id, "requester_dj")
        if perm == "owner":
            return self._is_effective_owner(guild_id, member)
        if perm == "admin":
            return self.has_admin_privilege(guild_id, member)
        if perm == "dj":
            return self.has_control_privilege(guild_id, member) or \
                   self.has_admin_privilege(guild_id, member)
        if perm == "everyone":
            return True
        # default "requester_dj"
        return (requester_id is not None and requester_id == member.id) or \
               self.has_control_privilege(guild_id, member) or \
               self.has_admin_privilege(guild_id, member)

    def check_playback_permissions(self, interaction: discord.Interaction):
        """
        Checks if the user has permission to control playback (pause/resume).
        Returns vc, state, and current song if successful, otherwise raises CommandCheckError.
        """
        vc, state = self.ensure_voice_and_state(interaction, same_channel=True, admin_ok=True, require_now=True)
        current = state.now
        if not self.has_pause_permission(interaction.guild_id, interaction.user, current.get("requester")):
            raise CommandCheckError(t(interaction, "PAUSE_NO_PERMISSION"), ephemeral=True)
        return vc, state, current

    def register_vote(self, command_name: str, vote_key, user_id: int):
        """
        Registers a vote for a command, handling internal vote storage initialization.
        Enforces one active vote per user per command type per guild.
        Returns (already_voted, vote_set).
        """
        vote_store = self.command_votes[command_name]
        guild_id = vote_key[0] if isinstance(vote_key, tuple) else vote_key

        # Remove user's previous vote on a different key for this command
        user_keys = self._vote_user_keys.setdefault(command_name, {}).setdefault(guild_id, {})
        old_key = user_keys.get(user_id)
        if old_key is not None and old_key != vote_key:
            old_entry = vote_store.get(old_key)
            if old_entry is not None:
                old_voters = old_entry.get("voters") if isinstance(old_entry, dict) else old_entry if isinstance(old_entry, set) else None
                if old_voters is not None:
                    old_voters.discard(user_id)
                    if not old_voters:
                        vote_store.pop(old_key, None)

        entry = vote_store.get(vote_key)

        if isinstance(entry, dict):
            vote_set = entry.setdefault("voters", set())
        elif isinstance(entry, set):
            vote_set = entry
        else:
            if isinstance(vote_key, tuple):
                vote_set = set()
                vote_store[vote_key] = {"voters": vote_set}
            else:
                vote_set = set()
                vote_store[vote_key] = vote_set

        already_voted = user_id in vote_set
        vote_set.add(user_id)
        user_keys[user_id] = vote_key
        return already_voted, vote_set

    async def handle_vote(
        self,
        interaction: discord.Interaction,
        command_name: str,
        vote_key,
        *,
        success_message: str,
        vote_message: str,
        already_voted_message: str | None = None,
        is_owner: bool = False,
        requires_same_channel: bool = True,
        send_success: bool = True,
    ) -> bool:
        """
        Manages voting logic for commands like skip, stop, shuffle.
        Returns True if the vote passed (or forced by DJ/admin/owner), False otherwise.
        When send_success=False, the caller is responsible for sending the success message.
        """
        is_admin = self.has_control_privilege(interaction.guild_id, interaction.user) or self.has_admin_privilege(interaction.guild_id, interaction.user)
        vc = interaction.guild.voice_client if interaction.guild else None
        user_voice = getattr(interaction.user, "voice", None)
        author_channel = user_voice.channel if user_voice else None
        bot_channel = vc.channel if vc else None

        if requires_same_channel:
            if not bot_channel:
                await self.send_reply(interaction, t(interaction, "BOT_NOT_IN_VOICE"), ephemeral=True)
                return False
            if not is_admin and author_channel != bot_channel:
                await self.send_reply(interaction, t(interaction, "SAME_CHANNEL_REQUIRED"), ephemeral=True)
                return False

        listener_channel = author_channel or bot_channel
        if not listener_channel:
            await self.send_reply(interaction, t(interaction, "VOTE_NEED_JOIN"), ephemeral=True)
            return False

        already_voted, vote_set = self.register_vote(command_name, vote_key, interaction.user.id)

        required_votes = self._required_votes(interaction.guild_id, listener_channel)
        current_votes = len(vote_set)
        already_voted_message = already_voted_message or t(interaction, "ALREADY_VOTED")

        if current_votes >= required_votes or is_admin or is_owner:
            if send_success:
                await self.send_reply(interaction, success_message)
            self.playback.cleanup_guild_votes(interaction.guild_id, [command_name])
            return True
        else:
            if already_voted:
                await self.send_reply(interaction, f"{already_voted_message} ({current_votes}/{required_votes})")
            else:
                await self.send_reply(interaction, f"{vote_message} ({current_votes}/{required_votes})")
            self._schedule_refresh(interaction.guild_id)
            return False

    def _start_bg_fetch(self, guild_id: int, coro, *, forced: bool = False):
        d = self._bg_fetch_forced_tasks if forced else self._bg_fetch_tasks
        existing = d.get(guild_id)
        if existing and not existing.done():
            existing.cancel()
        task = self._create_task(coro, name=f"bg-fetch-{guild_id}{'-f' if forced else ''}")
        d[guild_id] = task
        task.add_done_callback(lambda t, _d=d: _d.pop(guild_id, None) if _d.get(guild_id) is t else None)

    async def _await_bg_fetch(self, guild_id: int):
        task = self._bg_fetch_tasks.get(guild_id)
        if task and not task.done():
            try:
                await task
            except BaseException:
                pass

    async def _loading_msg(self, channel, guild_id):
        try:
            return await channel.send(t(channel, "PLAYLIST_LOADING"))
        except discord.HTTPException:
            return None

    async def _finish_loading_msg(self, msg, channel, guild_id, added, hit_limit, *, append=False, skip_counts=None):
        delete_after = self._resolve_delete_after(guild_id)
        if hit_limit:
            q_limit = self.guild_queue_limit.get(guild_id, MAX_QUEUE)
            text = t(channel, "QUEUE_LIMIT_REACHED", added=added, limit=q_limit)
        elif added:
            text = t(channel, "PLAYLIST_LOADED", added=added)
        else:
            text = None
        if skip_counts:
            skip_lines = []
            if skip_counts.get("deleted"):
                skip_lines.append(t(channel, "SKIPPED_DELETED", count=skip_counts["deleted"]))
            if skip_counts.get("private"):
                skip_lines.append(t(channel, "SKIPPED_PRIVATE", count=skip_counts["private"]))
            if skip_counts.get("members_only"):
                skip_lines.append(t(channel, "SKIPPED_MEMBERS_ONLY", count=skip_counts["members_only"]))
            if skip_lines:
                text = (text + "\n" if text else "") + "\n".join(skip_lines)
        try:
            if msg and text and append:
                loading = t(channel, "PLAYLIST_LOADING_REMAINING")
                original = (msg.content or "") if hasattr(msg, "content") else ""
                cleaned = original.replace("\n" + loading, "").replace(loading, "")
                await msg.edit(content=(cleaned + "\n" + text) if cleaned else text)
                if delete_after:
                    await msg.delete(delay=delete_after)
            elif msg and text:
                await msg.edit(content=text)
                if delete_after:
                    await msg.delete(delay=delete_after)
            elif msg and not append:
                await msg.delete()
            elif msg and append and not text:
                # Strip loading suffix from the existing message
                loading = t(channel, "PLAYLIST_LOADING_REMAINING")
                content = (msg.content or "") if hasattr(msg, "content") else ""
                cleaned = content.replace("\n" + loading, "").replace(loading, "")
                if cleaned != content:
                    if not cleaned.strip():
                        await msg.delete()
                    else:
                        await msg.edit(content=cleaned)
                        if delete_after:
                            await msg.delete(delay=delete_after)
            elif text and channel:
                m = await channel.send(text)
                if delete_after:
                    await m.delete(delay=delete_after)
        except discord.HTTPException:
            pass

    async def _bg_fetch_error_notify(self, msg, channel, guild_id, hint_text):
        """Clean up loading message and send an error hint with auto-delete."""
        if msg:
            try:
                await msg.delete()
            except discord.HTTPException:
                pass
        if channel:
            da = self._resolve_delete_after(guild_id)
            try:
                m = await channel.send(hint_text)
                if da:
                    await m.delete(delay=da)
            except discord.HTTPException:
                pass

    async def _run_bg_fetch(self, guild_id: int, fetch_coro, channel=None, *, reply_msg=None, first_batch=None, insert_at: int | None = None):
        state = self.guild_states.get(guild_id)
        if not state:
            return
        pending = list(first_batch) if first_batch else []
        if reply_msg:
            msg = reply_msg
        else:
            msg = await self._loading_msg(channel, guild_id) if channel else None
        self._acquire_extract()
        try:
            collected, hit_limit, skip_counts = await fetch_coro(state, msg, pending)
            to_add = pending + collected
            if to_add:
                q_limit = self.guild_queue_limit.get(guild_id, MAX_QUEUE)
                to_add = to_add[:max(0, q_limit - len(state.queue))]
                if insert_at is not None:
                    pos = min(insert_at, len(state.queue))
                    state.queue[pos:pos] = to_add
                else:
                    state.queue.extend(to_add)
                self._schedule_refresh(guild_id)
                if not state.playing and state.queue:
                    state.playing = True
                    state.cancel_tasks()
                    try:
                        await self.playback.play_next(guild_id)
                    except Exception as e:
                        print(f"_run_bg_fetch play_next error: {e}")
                        self.playback._mark_idle(state, guild_id)
            added = len(to_add)
            if channel or reply_msg:
                await self._finish_loading_msg(msg, channel, guild_id, added, hit_limit, append=bool(reply_msg), skip_counts=skip_counts)
        except asyncio.CancelledError:
            try:
                if msg and not reply_msg:
                    await msg.delete()
                elif msg and reply_msg:
                    loading = t(channel, "PLAYLIST_LOADING_REMAINING")
                    content = (msg.content or "") if hasattr(msg, "content") else ""
                    cleaned = content.replace("\n" + loading, "").replace(loading, "")
                    if cleaned != content:
                        if not cleaned.strip():
                            await msg.delete()
                        else:
                            await msg.edit(content=cleaned)
                            delete_after = self._resolve_delete_after(guild_id)
                            if delete_after:
                                await msg.delete(delay=delete_after)
            except discord.HTTPException:
                pass
        finally:
            self._release_extract()

    def _bg_user_limit_remaining(self, guild_id: int, requester_id: int, state) -> int | None:
        """Return the number of tracks the requester can still add, considering per-user limits. None = unlimited."""
        if requester_id == self.bot.user.id:
            return None
        if guild_id in self.guild_states:
            guild = self.bot.get_guild(guild_id)
            if guild:
                member = guild.get_member(requester_id)
                if member and self._is_effective_owner(guild_id, member):
                    return None
        guild = self.bot.get_guild(guild_id)
        member = guild.get_member(requester_id) if guild else None
        if member and self.has_admin_privilege(guild_id, member):
            max_user = self.guild_track_limit_admin.get(guild_id, 0)
        elif member and self.has_control_privilege(guild_id, member):
            max_user = self.guild_track_limit_dj.get(guild_id, 0)
        else:
            max_user = self.guild_track_limit_users.get(guild_id, 0)
        if max_user <= 0:
            return None
        user_count = sum(1 for e in state.queue if e.get("requester") == requester_id)
        if state.now and state.now.get("requester") == requester_id:
            user_count += 1
        return max(max_user - user_count, 0)

    async def _fetch_remaining_spotify(self, guild_id: int, entity_type: str, entity_id: str, offset: int, requester_id: int, channel=None, reply_msg=None, first_batch=None, forced=False, insert_at: int | None = None):
        from core.spotify import fetch_remaining_tracks

        async def _do_fetch(state, msg, pending):
            collected = []
            hit_limit = False
            base_count = len(state.queue) + len(pending)
            u_remaining = self._bg_user_limit_remaining(guild_id, requester_id, state)
            if u_remaining is not None:
                pending_user = sum(1 for e in pending if e.get("requester") == requester_id)
                u_remaining = max(0, u_remaining - pending_user)
            try:
                async for batch in fetch_remaining_tracks(entity_type, entity_id, offset):
                    _fd = self._bg_fetch_forced_tasks if forced else self._bg_fetch_tasks
                    if guild_id not in self.guild_states or guild_id not in _fd:
                        break
                    q_limit = self.guild_queue_limit.get(guild_id, MAX_QUEUE)
                    for entry in batch:
                        if base_count + len(collected) >= q_limit:
                            hit_limit = True
                            break
                        if u_remaining is not None and len(collected) >= u_remaining:
                            hit_limit = True
                            break
                        entry["requester"] = requester_id
                        collected.append(entry)
                    if hit_limit:
                        break
            except Exception as e:
                print(f"[Spotify API] Background fetch failed: {e}")
                if not collected and not pending:
                    await self._bg_fetch_error_notify(msg, channel, guild_id,
                        t(channel, "SPOTIFY_API_ERROR", error=str(e), readme_url=README_URL))
            return collected, hit_limit, {}

        await self._run_bg_fetch(guild_id, _do_fetch, channel, reply_msg=reply_msg, first_batch=first_batch, insert_at=insert_at)

    async def _fetch_remaining_spotapi(self, guild_id: int, entity_type: str, entity_id: str, requester_id: int, channel=None, reply_msg=None, initial_urls=None, first_batch=None, forced=False, insert_at: int | None = None):
        from core.spotify import fetch_all_via_spotapi

        async def _do_fetch(state, msg, pending):
            collected = []
            try:
                all_tracks = await fetch_all_via_spotapi(entity_type, entity_id)
            except Exception as e:
                print(f"[SpotAPI] Background fetch failed: {e}")
                if not collected and not pending:
                    await self._bg_fetch_error_notify(msg, channel, guild_id,
                        t(channel, "SPOTIFY_ERROR"))
                return collected, False, {}
            seen = set(initial_urls) if initial_urls else set()
            hit_limit = False
            base_count = len(state.queue) + len(pending)
            u_remaining = self._bg_user_limit_remaining(guild_id, requester_id, state)
            if u_remaining is not None:
                pending_user = sum(1 for e in pending if e.get("requester") == requester_id)
                u_remaining = max(0, u_remaining - pending_user)
            q_limit = self.guild_queue_limit.get(guild_id, MAX_QUEUE)
            _fd = self._bg_fetch_forced_tasks if forced else self._bg_fetch_tasks
            for entry in all_tracks:
                if guild_id not in self.guild_states or guild_id not in _fd:
                    break
                if base_count + len(collected) >= q_limit:
                    hit_limit = True
                    break
                if u_remaining is not None and len(collected) >= u_remaining:
                    hit_limit = True
                    break
                if entry["spotify_url"] not in seen:
                    entry["requester"] = requester_id
                    collected.append(entry)
                    seen.add(entry["spotify_url"])
            return collected, hit_limit, {}

        await self._run_bg_fetch(guild_id, _do_fetch, channel, reply_msg=reply_msg, first_batch=first_batch, insert_at=insert_at)

    async def _fetch_remaining_youtube(self, guild_id: int, query: str, offset: int, requester_id: int, channel=None, silent: bool = False, reply_msg=None, first_batch=None, forced=False, insert_at: int | None = None):
        from core.media import extract_entries_from, is_playable_entry, unavailable_reason

        async def _do_fetch(state, msg, pending):
            collected = []
            try:
                remaining = await extract_entries_from(query, silent=silent, playliststart=offset)
            except Exception as e:
                print(f"[YouTube] Background fetch failed: {e}")
                if not collected and not pending:
                    await self._bg_fetch_error_notify(msg, channel, guild_id,
                        t(channel, "YOUTUBE_FETCH_ERROR"))
                return collected, False, {}
            skip_counts = {}
            hit_limit = False
            base_count = len(state.queue) + len(pending)
            u_remaining = self._bg_user_limit_remaining(guild_id, requester_id, state)
            if u_remaining is not None:
                pending_user = sum(1 for e in pending if e.get("requester") == requester_id)
                u_remaining = max(0, u_remaining - pending_user)
            _fd = self._bg_fetch_forced_tasks if forced else self._bg_fetch_tasks
            for entry in remaining:
                if guild_id not in self.guild_states or guild_id not in _fd:
                    break
                reason = unavailable_reason(entry)
                if reason:
                    skip_counts[reason] = skip_counts.get(reason, 0) + 1
                    continue
                if not is_playable_entry(entry):
                    skip_counts["other"] = skip_counts.get("other", 0) + 1
                    continue
                q_limit = self.guild_queue_limit.get(guild_id, MAX_QUEUE)
                if base_count + len(collected) >= q_limit:
                    hit_limit = True
                    break
                if u_remaining is not None and len(collected) >= u_remaining:
                    hit_limit = True
                    break
                if not entry.get("uploader"):
                    entry["uploader"] = None
                entry["requester"] = requester_id
                collected.append(entry)
            return collected, hit_limit, skip_counts

        await self._run_bg_fetch(guild_id, _do_fetch, channel, reply_msg=reply_msg, first_batch=first_batch, insert_at=insert_at)

    async def handle_play(self, interaction: discord.Interaction, entries: list[dict], *, forced: bool = False) -> tuple[bool | str | None, int]:
        """
        Adds entries to the queue and starts playback if idle.
        Returns (status, added_count):
          status: True if playback started, False if queued, None if rejected,
                  "restricted" if channel join restricted, "user_limit" if per-user track limit hit,
                  "connect_failed" if voice connection failed, "live_blocked" if live streams not permitted.
          added_count: number of entries actually added.
        If forced=True, entries are prepended to the queue and current track is skipped.
        """
        guild_id = interaction.guild_id
        user_voice = getattr(interaction.user, "voice", None)
        voice_channel = user_voice.channel if user_voice else None
        vc = interaction.guild.voice_client if interaction.guild else None
        if vc and not vc.is_connected():
            self._intentional_disconnect.add(guild_id)
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
            vc = None
        if not vc and voice_channel:
            if not self.check_join_restriction(guild_id, voice_channel.id, interaction.user):
                return "restricted", 0
            try:
                vc = await voice_channel.connect()
            except (discord.ClientException, TimeoutError):
                zombie = interaction.guild.voice_client if interaction.guild else None
                if zombie:
                    self._intentional_disconnect.add(guild_id)
                    try:
                        await zombie.disconnect(force=True)
                    except Exception:
                        pass
                try:
                    vc = await voice_channel.connect()
                except Exception:
                    vc = None
        elif vc and voice_channel and vc.channel != voice_channel:
            if not self.check_join_restriction(guild_id, voice_channel.id, interaction.user):
                return "restricted", 0
            try:
                await vc.move_to(voice_channel)
            except Exception:
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
                try:
                    vc = await voice_channel.connect()
                except Exception:
                    vc = None

        if not vc:
            return "connect_failed", 0

        state = self.get_state(guild_id)

        state.vc = vc
        state.text_channel = interaction.channel

        q_limit = self.guild_queue_limit.get(guild_id, MAX_QUEUE)
        available = q_limit - len(state.queue)
        if available <= 0 and not forced:
            return None, 0
        if forced:
            available = max(available, len(entries))

        is_owner = self._is_effective_owner(guild_id, interaction.user)
        if not is_owner:
            if self.has_admin_privilege(guild_id, interaction.user):
                max_user = self.guild_track_limit_admin.get(guild_id, 0)
            elif self.has_control_privilege(guild_id, interaction.user):
                max_user = self.guild_track_limit_dj.get(guild_id, 0)
            else:
                max_user = self.guild_track_limit_users.get(guild_id, 0)
            if max_user > 0:
                user_count = sum(1 for e in state.queue if e.get("requester") == interaction.user.id)
                if state.now and state.now.get("requester") == interaction.user.id:
                    user_count += 1
                user_available = max_user - user_count
                if user_available <= 0:
                    return "user_limit", 0
                available = min(available, user_available)

        # Filter live tracks based on guild settings
        live_enabled = self.guild_live_enabled.get(guild_id, 0)
        if not is_owner:
            live_entries = [e for e in entries if e.get("is_live")]
            if live_entries:
                if not live_enabled:
                    entries = [e for e in entries if not e.get("is_live")]
                else:
                    live_perm = self.guild_live_permission.get(guild_id, "admin")
                    can_play_live = self._check_tiered_permission(
                        guild_id, interaction.user, live_perm, allow_everyone=True
                    )
                    if not can_play_live:
                        entries = [e for e in entries if not e.get("is_live")]
            if not entries:
                return "live_blocked", 0

        to_add = entries[:available]
        for entry in to_add:
            entry["requester"] = interaction.user.id

        _was_empty = not state.queue

        if forced:
            state.queue[0:0] = to_add
        else:
            state.queue.extend(to_add)

        added = len(to_add)
        if forced and state.playing:
            state.suppress_after_callback = True
            vc = state.vc
            if vc and (vc.is_playing() or vc.is_paused()):
                if state.pause_disconnect_task and not state.pause_disconnect_task.done():
                    state.pause_disconnect_task.cancel()
                state.pause_disconnect_task = None
                vc.stop()
            try:
                await self.playback.play_next(guild_id, manual=True)
            except Exception as e:
                print(f"handle_play forced play_next error: {e}")
                self.playback._mark_idle(state, guild_id)
                return False, added
            return True, added
        if not state.playing:
            state.playing = True
            state.cancel_tasks()
            try:
                await self.playback.play_next(guild_id, manual=True)
            except Exception as e:
                print(f"handle_play initial play_next error: {e}")
                self.playback._mark_idle(state, guild_id)
                return False, added
            return (state.playing and state.now is not None), added
        if _was_empty and state.queue:
            self.playback._start_prefetch(state, guild_id)
        return False, added

    def ensure_queue_data(self, interaction: discord.Interaction, require_queue=False, require_now=False, queue_message=None, now_message=None):
        """
        Validates the existence of the music state, queue, or 'now playing' song.
        Raises CommandCheckError if requirements are not met.
        Returns the music state object.
        """
        q_msg = queue_message or t(interaction, "QUEUE_EMPTY")
        n_msg = now_message or t(interaction, "NOTHING_PLAYING")
        state = self.guild_states.get(interaction.guild_id)
        if not state:
            raise CommandCheckError(n_msg if require_now else q_msg)
        if require_queue and not state.queue:
            raise CommandCheckError(q_msg)
        if require_now and not state.now:
            raise CommandCheckError(n_msg)
        return state

    def build_idle_embed(self, interaction: discord.Interaction):
        """Constructs a 'waiting for a track' embed shown when nothing is playing."""
        embed = SafeEmbed(title=t(interaction, "MP_IDLE_TITLE"), description="\U0001F997", color=self.get_embed_color(interaction.guild_id))
        return embed, None

    def build_mp_embed(self, interaction: discord.Interaction, state: GuildMusicState, *, attach_placeholder: bool = False):
        """Constructs the embed object for the music player message."""
        file: discord.File | None = None
        entry = state.now or {}
        loop_mode = state.loop_mode
        queue_list = state.queue
        guild_id = interaction.guild_id

        unknown = t(interaction, "UNKNOWN")
        title = entry.get("title") or unknown

        fields_cfg = self.get_mp_fields(guild_id)
        enabled_fields = sorted(
            ((k, v) for k, v in fields_cfg.items() if v.get("enabled", True) and k not in ("thumbnail", "queue", "vote_info")),
            key=lambda x: x[1].get("order", 99),
        )

        field_values = {
            "duration":  (t(interaction, "DURATION"),  _fmt_dur(entry.get("duration")) or unknown, True),
            "requester": (t(interaction, "REQUESTER"), f"<@{entry.get('requester')}>" if entry.get("requester") else unknown, True),
            "loop":      (t(interaction, "LOOP"),      t(interaction, "LOOP_SONG") if loop_mode == "song" else t(interaction, "LOOP_QUEUE") if loop_mode == "queue" else t(interaction, "LOOP_OFF"), True),
            "url":       (t(interaction, "URL_LABEL"), f"[{t(interaction, 'URL_TEXT')}]({entry.get('webpage_url')})" if entry.get("webpage_url") else unknown, True),
            "views":     (t(interaction, "VIEWS"),     f"{entry.get('view_count'):,}".replace(",", ".") if isinstance(entry.get("view_count"), int) else unknown, True),
            "uploader":  (t(interaction, "UPLOADER"),  entry.get("uploader") or unknown, True),
        }

        mp_title = t(interaction, "MP_TITLE_RADIO") if self.is_radio_active(guild_id) else t(interaction, "MP_TITLE")
        if entry.get("is_live"):
            max_live_h = self.guild_live_max_hours.get(guild_id, 1)
            if max_live_h > 0 and state.playing_since:
                tz_offset = self.guild_timezones.get(guild_id, 0)
                end_ts = state.playing_since + max_live_h * 3600 - state._total_paused
                end_local = time.gmtime(end_ts + tz_offset * 3600)
                end_str = f"{end_local.tm_hour:02d}:{end_local.tm_min:02d}"
                mp_title += t(interaction, "MP_LIVE_END_SUFFIX", time=end_str)
            else:
                mp_title += t(interaction, "MP_LIVE_SUFFIX")
        if state.paused_at is not None:
            pause_timeout = self.guild_pause_timeout.get(guild_id, 900)
            behavior = self.guild_pause_timeout_behavior.get(guild_id, "leave")
            action_key = {"leave": "MP_PAUSE_ACTION_LEAVE", "continue": "MP_PAUSE_ACTION_CONTINUE", "skip": "MP_PAUSE_ACTION_SKIP"}.get(behavior, "MP_PAUSE_ACTION_LEAVE")
            tz_offset = self.guild_timezones.get(guild_id, 0)
            expiry_local = time.gmtime(state.paused_at + pause_timeout + tz_offset * 3600)
            time_str = f"{expiry_local.tm_hour:02d}:{expiry_local.tm_min:02d}"
            mp_title += t(interaction, "MP_PAUSED_SUFFIX", behavior=t(interaction, action_key), time=time_str)
        embed = SafeEmbed(title=mp_title, description=f"```fix\n{title}\n```", color=self.get_embed_color(guild_id))

        inline_count = 0
        for key, _ in enabled_fields:
            if key in field_values:
                name, value, inline = field_values[key]
                embed.add_field(name=name, value=value, inline=inline)
                if inline:
                    inline_count += 1
        remainder = inline_count % 3
        if remainder:
            for _ in range(3 - remainder):
                embed.add_field(name="\u200b", value="\u200b", inline=True)

        queue_cfg = fields_cfg.get("queue", {})
        if queue_cfg.get("enabled", True):
            if queue_list:
                _up_lines = []
                for i, item in enumerate(queue_list[:3]):
                    _t = item.get('title', unknown)
                    _u = item.get('uploader', unknown)
                    _d = _fmt_dur(item.get('spotify_duration') if item.get('source') == 'spotify' else item.get('duration'))
                    _dp = f" - {_d}" if _d else ""
                    _up_lines.append(f"{i+1}. {_t} - {_u}{_dp}")
                upcoming_titles = "\n".join(_up_lines)
            else:
                upcoming_titles = t(interaction, "QUEUE_EMPTY")
            embed.add_field(name=t(interaction, "QUEUE_LABEL"), value=upcoming_titles, inline=False)

        thumb_cfg = fields_cfg.get("thumbnail", {})
        if thumb_cfg.get("enabled", True):
            thumbnail = entry.get("thumbnail")
            if thumbnail:
                embed.set_image(url=thumbnail)
            elif attach_placeholder and _PLACEHOLDER_EXISTS:
                embed.set_image(url="attachment://no_thumbnail.png")
                file = discord.File(io.BytesIO(_PLACEHOLDER_BYTES), filename="no_thumbnail.png")

        vote_cfg = fields_cfg.get("vote_info", {})
        footer_parts = []
        if vote_cfg.get("enabled", True):
            vc = interaction.guild.voice_client if interaction.guild else None
            vote_footer = self._vote_footer_text(guild_id, vc)
            if vote_footer:
                footer_parts.append(vote_footer)

        session = self.get_radio_session(guild_id)
        if session and session.active:
            seed_name = session.seed_track.get("title", "?")
            seed_artist = session.seed_track.get("artist") or session.seed_track.get("uploader") or ""
            if seed_artist:
                seed_name = f"{seed_name} - {seed_artist}"
            if len(seed_name) > 50:
                seed_name = seed_name[:47] + "..."
            related_played = session.tracks_played
            if session.track_limit > 0:
                radio_text = t(interaction, "RADIO_FOOTER_TRACK_LIMIT",
                               played=related_played, limit=session.track_limit, seed=seed_name)
            else:
                radio_text = t(interaction, "RADIO_FOOTER",
                               played=related_played, seed=seed_name)
            if session.timeout_minutes > 0:
                remaining = max(0, int(session.timeout_minutes * 60 - (time.time() - session.started_at)))
                h, rem = divmod(remaining, 3600)
                m, s = divmod(rem, 60)
                time_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
                radio_text += f" · {t(interaction, 'RADIO_FOOTER_TIME_LIMIT', remaining=time_str)}"
            if session.refill_failed:
                radio_text += f" · {t(interaction, 'RADIO_FOOTER_REFILL_FAILED')}"
            footer_parts.append(radio_text)

        if footer_parts:
            embed.set_footer(text="\n".join(footer_parts))
        return embed, file

    async def handle_skip(self, interaction: discord.Interaction) -> bool:
        """Returns True if the skip was executed, False if only a vote was registered."""
        session = self.get_radio_session(interaction.guild_id)
        if session and session.active and session._initial_fetch and not session._initial_fetch.done():
            await self.send_reply(interaction, t(interaction, "RADIO_STILL_LOADING"), ephemeral=True)
            return False

        vc = interaction.guild.voice_client if interaction.guild else None
        state = self.guild_states.get(interaction.guild_id)
        current = state.now if state else None

        if not vc or not state or not current or (not vc.is_playing() and not vc.is_paused()):
            await self.send_reply(interaction, t(interaction, "SKIP_NOTHING"), ephemeral=True)
            return False

        is_owner = current.get("requester") == interaction.user.id
        current_song_id = current.get("id")
        if not current_song_id:
            await self.send_reply(interaction, t(interaction, "SKIP_NO_INFO"), ephemeral=True)
            return False

        vote_key = (interaction.guild_id, current_song_id)

        vote_passed = await self.handle_vote(
            interaction, "skip", vote_key,
            success_message=t(interaction, "SKIP_SUCCESS"),
            vote_message=t(interaction, "SKIP_VOTE"),
            is_owner=is_owner
        )

        if vote_passed:
            if state.now is not current:
                return True
            if state.loop_mode == "song":
                state.skip_current_song_once = True
            vc.stop()
        return vote_passed

    async def handle_previous(self, interaction: discord.Interaction) -> bool:
        vc = interaction.guild.voice_client if interaction.guild else None
        state = self.guild_states.get(interaction.guild_id)
        current = state.now if state else None

        if not vc or not state or not current or (not vc.is_playing() and not vc.is_paused()):
            await self.send_reply(interaction, t(interaction, "NOTHING_PLAYING"), ephemeral=True)
            return False

        guild_id = interaction.guild_id
        max_h = self.get_max_history(guild_id)
        if max_h <= 0:
            await self.send_reply(interaction, t(interaction, "HISTORY_DISABLED"), ephemeral=True)
            return False

        history = await db.get_history(guild_id, limit=2)
        if len(history) < 2:
            await self.send_reply(interaction, t(interaction, "NO_PREVIOUS"), ephemeral=True)
            return False

        is_owner = current.get("requester") == interaction.user.id
        vote_key = (guild_id, "previous")

        vote_passed = await self.handle_vote(
            interaction, "previous", vote_key,
            success_message=t(interaction, "PREVIOUS_SUCCESS"),
            vote_message=t(interaction, "PREVIOUS_VOTE"),
            is_owner=is_owner,
        )

        if vote_passed:
            if state.now is not current:
                return True
            prev = history[1]
            entry = {
                "title": prev.get("title") or None,
                "uploader": prev.get("uploader") or None,
                "duration": prev.get("duration"),
                "requester": interaction.user.id,
                "suppress_announce": True,
            }
            url = prev.get("url", "")
            if url and is_spotify_url(url):
                entry["source"] = "spotify"
                entry["spotify_url"] = url
                entry["spotify_duration"] = prev.get("duration")
            else:
                entry["webpage_url"] = url
                entry["url"] = url
            state.queue.insert(0, entry)
            if state.loop_mode == "song":
                state.skip_current_song_once = True
            vc.stop()
        return vote_passed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild and self.is_excluded(interaction.guild_id, interaction.user):
            await self.send_reply(
                interaction, t(interaction, "EXCLUDED_USER"), ephemeral=True,
                delete_after=self._resolve_delete_after(interaction.guild_id),
            )
            return False
        if interaction.guild:
            _to_expires = self.is_timed_out(interaction.guild_id, interaction.user.id)
            if _to_expires:
                await self.send_reply(
                    interaction,
                    t(interaction, "USER_TIMED_OUT", remaining=f"<t:{int(_to_expires)}:R>"),
                    ephemeral=True,
                    delete_after=self._resolve_delete_after(interaction.guild_id),
                )
                return False
        return True

    async def check_view_interaction(self, interaction: discord.Interaction) -> bool:
        """Shared check for View.interaction_check, blocks excluded and timed-out users."""
        if not interaction.guild:
            return True
        if self.is_excluded(interaction.guild_id, interaction.user):
            await self.send_reply(
                interaction, t(interaction, "EXCLUDED_USER"), ephemeral=True,
                delete_after=self._resolve_delete_after(interaction.guild_id),
            )
            return False
        _to_expires = self.is_timed_out(interaction.guild_id, interaction.user.id)
        if _to_expires:
            await self.send_reply(
                interaction, t(interaction, "USER_TIMED_OUT", remaining=f"<t:{int(_to_expires)}:R>"),
                ephemeral=True,
                delete_after=self._resolve_delete_after(interaction.guild_id),
            )
            return False
        return True

    async def cog_app_command_error(self, interaction: discord.Interaction, error: discord.app_commands.AppCommandError):
        """
        Global error handler for application commands within this Cog.
        Handles custom CommandCheckErrors and generic check failures.
        """
        original = getattr(error, 'original', error)
        if isinstance(original, CommandCheckError):
            return await respond_with_error(interaction, original)
        if isinstance(error, discord.app_commands.CheckFailure):
            await self.send_reply(interaction, str(error), ephemeral=True)
        else:
            traceback.print_exception(type(error), error, error.__traceback__, file=sys.stderr)
            await self.send_reply(interaction, t(interaction, "ERROR_GENERIC_TITLE"), ephemeral=True)

    async def cog_load(self):
        await init_locales_cache()
        for name, loader, target in self._settings_loaders():
            try:
                fresh = await loader()
                target.clear()
                target.update(fresh)
            except Exception as e:
                print(f"[warn] failed to load {name}: {e}")
        try:
            act = await db.get_bot_activity()
            self.bot_activity_type = act["type"]
            self.bot_activity_text = act["text"]
            self.bot_activity_mode = act["mode"]
            self.bot_activity_interval = act["interval"]
            self.bot_activity_selected = act["selected"]
            self.bot_activity_list = await db.get_bot_activity_list()
        except Exception as e:
            print(f"[warn] failed to load bot activity: {e}")
        try:
            from core.media import set_max_workers
            from core.radio import set_radio_concurrency
            self._silent_log = await db.get_silent_log()
            self._prefetch = await db.get_prefetch()
            self._safe_prefetch = await db.get_safe_prefetch()
            mw = await db.get_max_workers()
            self._max_workers = mw
            set_max_workers(mw)
            set_radio_concurrency(mw)
        except Exception as e:
            print(f"[warn] failed to load performance settings: {e}")
        try:
            rows = await db.get_all_timeouts()
            now = time.time()
            for gid, uid, expires in rows:
                if expires > now:
                    self._timeouts[(gid, uid)] = expires
            expired = await db.cleanup_expired_timeouts(now)
            if expired:
                print(f"[startup] Cleaned up {expired} expired timeout(s).")
        except Exception as e:
            print(f"[warn] failed to load timeouts: {e}")
        self._update_discord_loggers()
        log_youtube_status()

    @commands.Cog.listener()
    async def on_ready(self):
        for guild_id, (msg, _ctx, view) in list(self.active_mp.items()):
            if view is None:
                continue
            try:
                self.bot.add_view(view, message_id=msg.id)
            except Exception:
                pass
        for guild_id, entry in list(self.active_queues.items()):
            if entry[1] is None:
                continue
            try:
                self.bot.add_view(entry[1], message_id=entry[0].id)
            except Exception:
                pass
        print(t(None, "READY_LOG", user=self.bot.user))
        await self.update_presence()
        self.start_activity_cycle()
