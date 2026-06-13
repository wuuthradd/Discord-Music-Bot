import random
import asyncio
import gc
import io
import json as _json
import logging
import re
import time

_log = logging.getLogger(__name__)

try:
    import psutil
    _PSUTIL_PROCESS = psutil.Process()
except Exception:
    _PSUTIL_PROCESS = None

import discord
from discord.ext import commands, tasks
from discord import app_commands
from db.db import db

from locales.localization import t, SUPPORTED_LOCALES, DEFAULT_LOCALE, set_locale, guild_locales, load_locales, refresh_supported_locales, init_locales_cache
from core.media import (
    extract_entries,
    extract_youtube_start_time,
    is_youtube_url,
    looks_like_url,
    StaleCookieError,
    JSRuntimeError,
    has_valid_cookies,
    has_js_runtime,
    is_playable_entry,
    unavailable_reason,
    _clean_ytdlp_error,
    _YT_PLAYLIST_BATCH,
    yt_playlist_start_id,
    slice_from_video,
)
from core.spotify import get_spotify_first_batch, is_spotify_url, has_spotify_api, SpotifyError, fetch_remaining_tracks, fetch_all_via_spotapi
from core.playback import CommandCheckError
from core.music_handlers import (
    MusicHandlers, respond_with_error, EMBED_COLOR, README_URL, MAX_QUEUE,
    _fmt_dur, _fmt_track_lines, _fmt_uptime, RadioSession,
)
from core.radio import fetch_radio_pool
from core.safe_embed import SafeEmbed

MAX_PLAYLIST_TRACKS = 5000              # tracks, max items per saved playlist
TRACK_LIMIT_RANGE = (0, 10000)          # tracks, per-user queue add cap (0 = off)
PAUSE_TIMEOUT_RANGE = (1, 60)           # minutes, auto-disconnect after pause
IDLE_TIMEOUT_RANGE = (0, 10080)         # minutes, auto-disconnect when idle (0 = off)
RADIO_COOLDOWN_RANGE = (1, 15)          # minutes, cooldown between radio starts
QUEUE_PER_PAGE_RANGE = (5, 15)          # count, tracks shown per queue page
ACTIVITY_INTERVAL_RANGE = (1, 10080)    # minutes, bot activity status refresh
RADIO_TRACK_LIMIT_RANGE = (15, 10000)    # tracks, auto-stop radio after N tracks
RADIO_TIME_LIMIT_RANGE = (1, 10080)     # minutes, auto-stop radio after N minutes


def _entry_to_track_dict(e: dict) -> dict:
    d = {
        "title": e.get("title") or None,
        "uploader": e.get("uploader") or None,
        "duration": e.get("spotify_duration") or e.get("duration"),
        "url": e.get("webpage_url") or e.get("url") or e.get("spotify_url", ""),
    }
    if e.get("is_live"):
        d["is_live"] = True
    return d


def _format_queue_add_msg(ctx, entries: list[dict], total: int | None = None) -> str:
    if total is None:
        total = len(entries)
    show = min(3, total) if total < len(entries) else 3
    display_list = []
    for e in entries[:show]:
        title = (e.get("title") or t(ctx, "UNKNOWN")).replace("`", "'")
        uploader = (e.get("uploader") or t(ctx, "UNKNOWN")).replace("`", "'")
        display_list.append(f"`{title}` - `{uploader}`")
    if len(display_list) == 1:
        return t(ctx, "ADDED_TO_QUEUE_SINGLE", title=display_list[0])
    if len(display_list) == 2:
        return t(ctx, "ADDED_TO_QUEUE_DOUBLE", first=display_list[0], second=display_list[1])
    if total <= 3:
        return t(ctx, "ADDED_TO_QUEUE_MULTI", items=", ".join(display_list[:-1]), last=display_list[-1])
    remaining = total - 3
    return t(ctx, "ADDED_TO_QUEUE_REMAINING", items=", ".join(display_list), remaining=remaining)


def _playlist_track_to_entry(track: dict) -> dict:
    url = track.get("url", "")
    entry = {
        "title": track.get("title") or None,
        "uploader": track.get("uploader") or None,
        "duration": track.get("duration"),
    }
    if track.get("is_live"):
        entry["is_live"] = True
    if url and is_spotify_url(url):
        entry["source"] = "spotify"
        entry["spotify_url"] = url
        entry["spotify_duration"] = track.get("duration")
    else:
        entry["webpage_url"] = url
        entry["url"] = url
    return entry


class _GuildCtx:
    """Minimal guild context carrier used when no real interaction is available."""
    def __init__(self, guild):
        self.guild = guild
        self.guild_id = guild.id


async def _persistent_view_prechecks(cog, interaction, view_type):
    """Shared pre-checks for persistent view recovery buttons.

    view_type is "mp" or "queue".
    Returns True if the caller should proceed to rebuild, False otherwise.
    Handles interaction response (defer / error messages) internally.
    """
    guild_id = interaction.guild.id
    msg_id = interaction.message.id
    active_dict = cog.active_mp if view_type == "mp" else cog.active_queues
    view_index = 2 if view_type == "mp" else 1
    cooldown_key = "mp:recover" if view_type == "mp" else "q:recover"

    tracked = active_dict.get(guild_id)
    if tracked and tracked[0].id == msg_id and tracked[view_index] is not None:
        return False

    is_tracked = tracked and tracked[0].id == msg_id
    if not is_tracked:
        row = await db.get_active_view(guild_id, view_type)
        is_tracked = row and row[1] == msg_id
    if not is_tracked:
        await interaction.response.defer()
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass
        return False

    result = cog.check_view_restriction(guild_id, interaction.channel_id, interaction.user)
    if result:
        reason, view_ch = result
        if reason in ("wrong_channel", "owner_wrong_channel"):
            await interaction.response.defer()
            try:
                await interaction.message.delete()
            except discord.HTTPException:
                pass
            active_dict.pop(guild_id, None)
            await db.delete_active_view(guild_id, view_type)
            return False
        if reason == "user_blocked":
            await interaction.response.send_message(
                t(interaction, "VIEW_USER_BLOCKED"), ephemeral=True,
                delete_after=cog._resolve_delete_after(guild_id))
            return False

    await interaction.response.defer()

    if not await cog._check_cooldown(interaction, cooldown_key, 5, per_guild=True):
        return False
    if interaction.guild and cog.is_excluded(interaction.guild_id, interaction.user):
        _da = cog._resolve_delete_after(interaction.guild_id)
        _msg = await interaction.followup.send(
            t(interaction, "EXCLUDED_USER"), ephemeral=True, wait=True)
        if _da and _msg:
            await _msg.delete(delay=_da)
        return False
    _to_expires = interaction.guild and cog.is_timed_out(interaction.guild_id, interaction.user.id)
    if _to_expires:
        _da = cog._resolve_delete_after(interaction.guild_id)
        _msg = await interaction.followup.send(
            t(interaction, "USER_TIMED_OUT", remaining=f"<t:{int(_to_expires)}:R>"), ephemeral=True, wait=True)
        if _da and _msg:
            await _msg.delete(delay=_da)
        return False
    return True


class _PersistentMPButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r'mp:(?P<action>\w+)',
):
    """DynamicItem that catches any mp:* button press after a bot restart and rebuilds the full view."""

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(item)

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("MusicCog")
        if not cog:
            return await interaction.response.defer()
        if not await _persistent_view_prechecks(cog, interaction, "mp"):
            return

        guild_id = interaction.guild.id
        state = cog.guild_states.get(guild_id)
        if state and state.now:
            embed, file = cog.build_mp_embed(interaction, state, attach_placeholder=True)
            view = cog.MusicPlayerView(interaction, cog)
        else:
            embed, file = cog.build_idle_embed(interaction)
            view = cog.MusicPlayerView(interaction, cog, idle=True)
        kwargs = {"embed": embed, "view": view, "attachments": []}
        if file:
            kwargs["attachments"] = [file]
        await interaction.edit_original_response(**kwargs)
        msg = interaction.channel.get_partial_message(interaction.message.id)
        cog.active_mp[guild_id] = (msg, interaction, view)
        await cog._save_view(guild_id, "mp", interaction.channel_id, interaction.message.id)


class _PersistentQueueButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r'q:(?P<action>\w+)',
):
    """DynamicItem that catches any q:* button press after a bot restart and rebuilds the full view."""

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str], /):
        return cls(item)

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("MusicCog")
        if not cog:
            return await interaction.response.defer()
        if not await _persistent_view_prechecks(cog, interaction, "queue"):
            return

        guild_id = interaction.guild.id
        view, QueueViewCls = cog._build_queue(interaction)
        await interaction.edit_original_response(embed=view.get_embed(), view=view)
        msg = interaction.channel.get_partial_message(interaction.message.id)
        cog.active_queues[guild_id] = (msg, view, QueueViewCls)
        await cog._save_view(guild_id, "queue", interaction.channel_id, interaction.message.id)


class _SearchResultsView(discord.ui.View):
    """Shared paginated search results view for queue, playlist, and shared-playlist."""
    def __init__(self, matches, ctx, embed_color=EMBED_COLOR, per_page=10, show_details=False, cog=None):
        super().__init__(timeout=180)
        self.matches = matches
        self.ctx = ctx
        self.cog = cog
        self.per_page = per_page
        self.embed_color = embed_color
        self.show_details = show_details
        self.page = 0
        self.total_pages = max(1, (len(matches) - 1) // per_page + 1)
        self._build()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.cog:
            return await self.cog.check_view_interaction(interaction)
        return True

    @staticmethod
    def _entry_url(entry):
        return entry.get("url") or entry.get("webpage_url") or entry.get("spotify_url") or ""

    def get_embed(self):
        start = self.page * self.per_page
        end = start + self.per_page
        lines = []
        for i, tr in self.matches[start:end]:
            title_str = tr.get("title") or t(self.ctx, "UNKNOWN")
            uploader = tr.get("uploader") or t(self.ctx, "UNKNOWN")
            url = self._entry_url(tr)
            safe_title = title_str.replace("[", "⌜").replace("]", "⌝")
            linked = f"[{safe_title}]({url})" if url else title_str
            line = f"`{i}.` {linked} - *{uploader}*"
            if self.show_details:
                dur = _fmt_dur(tr.get("spotify_duration") if tr.get("source") == "spotify" else tr.get("duration"))
                if dur:
                    line += f" - `{dur}`"
                req = tr.get("requester")
                if req:
                    line += f"\n> {t(self.ctx, 'REQUESTER')}: <@{req}>"
            lines.append(line)
        return SafeEmbed(
            title=t(self.ctx, "QUEUE_SEARCH_RESULTS", count=len(self.matches)),
            description="\n".join(lines),
            color=self.embed_color,
        ).set_footer(text=f"{self.page + 1}/{self.total_pages}")

    def _build(self):
        self.clear_items()
        single = self.total_pages <= 1
        self.add_item(_PageButton(self, "⏪", None, "first", disabled=single or self.page == 0))
        self.add_item(_PageButton(self, "⬅️", None, "prev", disabled=single or self.page == 0))
        self.add_item(_PageButton(self, "➡️", None, "next", disabled=single or self.page >= self.total_pages - 1))
        self.add_item(_PageButton(self, "⏩", None, "last", disabled=single or self.page >= self.total_pages - 1))
        self.add_item(_GoToPageButton(self, disabled=single))


def _pl_add_msg(ctx, track_dicts, added, playlist_name):
    """Build a descriptive playlist-add reply matching the /play response style."""
    display = []
    _unk = t(ctx, "UNKNOWN")
    for td in track_dicts[:3]:
        display.append(f"`{td['title'] or _unk}` - `{td['uploader'] or _unk}`")
    if len(display) == 1:
        return t(ctx, "PL_TRACK_ADDED", title=display[0], name=playlist_name)
    if len(display) == 2:
        return t(ctx, "PL_TRACKS_ADDED_DOUBLE", first=display[0], second=display[1], name=playlist_name)
    if added <= 3:
        return t(ctx, "PL_TRACKS_ADDED_MULTI", items=", ".join(display[:-1]), last=display[-1], name=playlist_name)
    return t(ctx, "PL_TRACKS_ADDED_REMAINING", items=", ".join(display), remaining=added - 3, name=playlist_name)


def _match_tracks(tracks: list[dict], term: str) -> list[int]:
    """Return indices (0-based) of tracks whose title or uploader contain term."""
    term = term.strip().lower()
    if not term:
        return []
    return [i for i, e in enumerate(tracks) if term in (e.get("title") or "").lower() or term in (e.get("uploader") or "").lower()]


def _remove_by_search(queue: list, term: str) -> int:
    """Remove all queue entries matching term in title or uploader. Returns count removed."""
    indices = _match_tracks(queue, term)
    for offset, i in enumerate(indices):
        queue.pop(i - offset)
    return len(indices)


async def _search_submit(tracks, search_value, modal_interaction, cog, *, show_details=False):
    """Shared search logic called by search modal on_submit handlers (queue, playlist, shared)."""
    term = search_value.strip().lower()
    if not term:
        return await modal_interaction.response.defer()
    guild = modal_interaction.guild
    matches = []
    for i, tr in enumerate(tracks, 1):
        title = tr.get("title") or ""
        uploader = tr.get("uploader") or ""
        if term in title.lower() or term in uploader.lower():
            matches.append((i, tr))
        elif show_details and guild:
            req_id = tr.get("requester")
            if req_id:
                member = guild.get_member(req_id)
                if member and (term in member.display_name.lower() or term in member.name.lower()):
                    matches.append((i, tr))
    if not matches:
        return await cog.send_reply(modal_interaction,
            t(modal_interaction, "QUEUE_SEARCH_NO_RESULTS"), ephemeral=True)
    embed_color = cog.get_embed_color(modal_interaction.guild_id)
    view = _SearchResultsView(matches, modal_interaction, embed_color=embed_color, show_details=show_details, cog=cog)
    await modal_interaction.response.send_message(embed=view.get_embed(), view=view, ephemeral=True)


async def _copy_submit(playlist_id, name_value, modal_interaction, cog):
    """Shared on_submit handler for copy modals (playlist and shared)."""
    name = name_value.strip()[:50]
    if not name:
        return await cog.send_reply(modal_interaction, t(modal_interaction, "PL_NAME_EMPTY"), ephemeral=True)
    copier_id = modal_interaction.user.id
    copier_guild = modal_interaction.guild_id
    max_pl = cog.get_max_playlists(copier_guild)
    count = await db.get_user_playlist_count(copier_guild, copier_id)
    if count >= max_pl:
        return await cog.send_reply(modal_interaction, t(modal_interaction, "PL_MAX_REACHED", max=max_pl), ephemeral=True)
    track_limit = cog.guild_playlist_track_limit.get(copier_guild, 5000)
    new_id = await db.copy_playlist(playlist_id, copier_guild, copier_id, name, max_tracks=track_limit)
    if new_id is None:
        return await cog.send_reply(modal_interaction, t(modal_interaction, "PL_DUPLICATE_NAME", name=name), ephemeral=True)
    await cog.send_reply(modal_interaction, t(modal_interaction, "PL_COPIED", name=name), ephemeral=True)


# --- Shared playlist-operation helpers ---
# Each does pure validation + DB work and returns a result.
# Callers (slash cmd / modal on_submit) handle interaction responses and UI refresh.

async def _pl_create(ctx, guild_id, user_id, name, max_pl):
    """Returns (message, new_playlist_id or None)."""
    name = name.strip()[:50]
    if not name:
        return t(ctx, "PL_NAME_EMPTY"), None
    count = await db.get_user_playlist_count(guild_id, user_id)
    if count >= max_pl:
        return t(ctx, "PL_MAX_REACHED", max=max_pl), None
    pid = await db.create_playlist(guild_id, user_id, name)
    if pid is None:
        return t(ctx, "PL_DUPLICATE_NAME", name=name), None
    return t(ctx, "PL_CREATED", name=name), pid


async def _pl_delete_one(ctx, guild_id, user_id, playlist_id, name, was_fav):
    """Delete one playlist, reassign favourite if needed. Returns message."""
    await db.delete_playlist(playlist_id)
    if was_fav:
        remaining = await db.get_user_playlists(guild_id, user_id)
        if remaining:
            await db.set_favourite(guild_id, user_id, remaining[0]["id"])
    return t(ctx, "PL_DELETED", name=name)


async def _pl_set_favourite(ctx, guild_id, user_id, playlist_id, name, is_favourite):
    """Set favourite. Returns message."""
    if is_favourite:
        return t(ctx, "PL_ALREADY_FAVOURITE", name=name)
    await db.set_favourite(guild_id, user_id, playlist_id)
    return t(ctx, "PL_FAVOURITE_SET", name=name)


async def _pl_rename(ctx, guild_id, user_id, playlist_id, old_name, new_name):
    """Returns (message, success_bool)."""
    new_name = new_name.strip()[:50]
    if not new_name:
        return t(ctx, "PL_NAME_EMPTY"), False
    if new_name == old_name:
        return t(ctx, "PL_RENAME_SAME"), False
    existing = await db.get_playlist_by_name(guild_id, user_id, new_name)
    if existing and existing["id"] != playlist_id:
        return t(ctx, "PL_DUPLICATE_NAME", name=new_name), False
    ok = await db.rename_playlist(playlist_id, new_name)
    if not ok:
        return t(ctx, "PL_DUPLICATE_NAME", name=new_name), False
    return t(ctx, "PL_RENAMED", old=old_name, new=new_name), True


async def _pl_share(cog, ctx, playlist_id, name, mention):
    """Send shared playlist view to channel. Returns error message or None."""
    tracks = await db.get_playlist_tracks(playlist_id)
    view = cog._make_shared_playlist_view(ctx, playlist_id, name, tracks, mention, cog)
    try:
        view._msg = await ctx.channel.send(embed=view.get_embed(), view=view)
    except discord.Forbidden:
        return t(ctx, "PL_SHARE_NO_PERMS")
    return None


async def _pl_add_tracks(ctx, user_id, playlist_id, playlist_name, query, track_limit, silent, *, shuffle=False):
    """Extract tracks and add to playlist. Returns (message, track_dicts)."""
    try:
        if is_spotify_url(query):
            result = await get_spotify_first_batch(query)
            raw = list(result.tracks)
            if result.use_free_fallback:
                remaining = await fetch_all_via_spotapi(result.entity_type, result.entity_id)
                seen = {e.get("spotify_url") for e in raw if e.get("spotify_url")}
                raw.extend(e for e in remaining if e.get("spotify_url") not in seen)
            elif has_spotify_api() and result.total and result.total > len(raw):
                async for batch in fetch_remaining_tracks(result.entity_type, result.entity_id, len(raw)):
                    raw.extend(batch)
        else:
            raw = await extract_entries(query, silent=silent)
            if is_youtube_url(query):
                raw = slice_from_video(raw, yt_playlist_start_id(query))
    except Exception as e:
        return t(ctx, "VIDEO_CANNOT_PLAY", reason=str(e)[:200]), []
    entries = [e for e in raw if e.get("webpage_url") or e.get("url") or e.get("spotify_url")]
    if not entries:
        return t(ctx, "NO_PLAYABLE"), []
    track_dicts = [_entry_to_track_dict(e) for e in entries]
    if shuffle and len(track_dicts) > 1:
        random.shuffle(track_dicts)
    added = await db.add_playlist_tracks(playlist_id, track_dicts, track_limit, user_id=user_id)
    if added < 0:
        return t(ctx, "PL_ADD_BUSY"), []
    total = len(track_dicts)
    if added < total:
        return t(ctx, "PL_PARTIAL_ADD", added=added, total=total, limit=track_limit), track_dicts
    return _pl_add_msg(ctx, track_dicts, added, playlist_name), track_dicts


async def _do_resume(cog, interaction, state, vc):
    """Execute resume: update vc, adjust pause tracking, notify."""
    vc.resume()
    if state.paused_at is not None:
        state._total_paused += time.time() - state.paused_at
        state.paused_at = None
    if state.pause_disconnect_task and not state.pause_disconnect_task.done():
        state.pause_disconnect_task.cancel()
    state.pause_disconnect_task = None
    if state.idle_disconnect_task and not state.idle_disconnect_task.done():
        state.idle_disconnect_task.cancel()
    state.idle_disconnect_task = None
    await cog.send_reply(interaction, t(interaction, "RESUMED"))
    cog._schedule_refresh(interaction.guild.id)


def _deactivate_radio_session(session):
    """Deactivate a radio session and cancel its background tasks."""
    session.active = False
    for task in (session._initial_fetch, session._timeout_task, session._refill_task):
        if task and not task.done():
            task.cancel()


async def _queue_loop(cog, interaction, guild_id, mode):
    """Shared loop logic for slash and modal. Returns True if applied."""
    state = cog.guild_states.get(guild_id)
    if not state or not state.now:
        await cog.send_reply(interaction, t(interaction, "NOTHING_PLAYING"), ephemeral=True)
        return False
    mode_label = {
        "off": t(interaction, "LOOP_OFF"),
        "song": t(interaction, "LOOP_SONG"),
        "queue": t(interaction, "LOOP_QUEUE"),
    }.get(mode, mode)
    if state.loop_mode == mode:
        await cog.send_reply(interaction, t(interaction, "LOOP_ALREADY", mode=mode_label))
        return False
    loop_letter = {"off": "O", "song": "S", "queue": "Q"}.get(mode, "?")
    vote_passed = await cog.handle_vote(
        interaction, "loop", (guild_id, loop_letter),
        success_message=t(interaction, "LOOP_MODE_SET", mode=mode_label),
        vote_message=t(interaction, "LOOP_VOTE", mode=mode_label),
        already_voted_message=t(interaction, "ALREADY_VOTED"),
        is_owner=False, requires_same_channel=True,
    )
    if not vote_passed:
        return False
    state.loop_mode = mode
    if mode == "off":
        state.skip_current_song_once = False
    cog._schedule_refresh(guild_id)
    return True


async def _queue_select(cog, interaction, guild_id, index, snapshot):
    """Shared select logic. Returns True if applied."""
    state = cog.guild_states.get(guild_id)
    queue = state.queue if state else []
    if not (1 <= index <= len(queue)):
        await cog.send_reply(interaction, t(interaction, "SELECT_INVALID"), ephemeral=True)
        return False
    song = queue[index - 1]
    title = song.get("title") or t(interaction, "UNKNOWN")
    uploader = song.get("uploader") or t(interaction, "UNKNOWN")
    vote_passed = await cog.handle_vote(
        interaction, "select", (guild_id, index - 1),
        success_message=t(interaction, "SELECT_SUCCESS", index=index, title=title, uploader=uploader),
        vote_message=t(interaction, "SELECT_VOTE", index=index, title=title, uploader=uploader),
        send_success=False,
    )
    if not vote_passed:
        return False
    queue = state.queue if state else []
    if tuple(id(e) for e in queue) != snapshot:
        await cog.send_reply(interaction, t(interaction, "QUEUE_STATE_CHANGED"), ephemeral=True)
        return False
    if index - 1 < len(queue):
        if state.loop_mode == "song":
            state.skip_current_song_once = True
        selected = queue.pop(index - 1)
        queue.insert(0, selected)
        vc = interaction.guild.voice_client
        if vc:
            vc.stop()
    await cog.send_reply(interaction, t(interaction, "SELECT_SUCCESS", index=index, title=title, uploader=uploader))
    return True


async def _queue_shuffle(cog, interaction, guild_id, from_idx, to_idx, snapshot):
    """Shared shuffle logic. from_idx is 1-based or None for full shuffle.
    Returns True if applied."""
    state = cog.guild_states.get(guild_id)
    queue = state.queue if state else []
    if from_idx is not None:
        vote_key = (guild_id, from_idx, to_idx)
        vote_passed = await cog.handle_vote(
            interaction, "shuffle", vote_key,
            success_message=t(interaction, "SHUFFLE_RANGE_DONE", from_pos=from_idx, to_pos=to_idx),
            vote_message=t(interaction, "SHUFFLE_RANGE_VOTE", from_pos=from_idx, to_pos=to_idx),
        )
        if not vote_passed:
            return False
        queue = state.queue if state else []
        if tuple(id(e) for e in queue) != snapshot:
            await cog.send_reply(interaction, t(interaction, "QUEUE_STATE_CHANGED"), ephemeral=True)
            return False
        _qh = id(queue[0]) if queue else None
        sub = queue[from_idx - 1:to_idx]
        random.shuffle(sub)
        queue[from_idx - 1:to_idx] = sub
    else:
        vote_passed = await cog.handle_vote(
            interaction, "shuffle", guild_id,
            success_message=t(interaction, "SHUFFLE_DONE"),
            vote_message=t(interaction, "SHUFFLE_VOTE"),
        )
        if not vote_passed:
            return False
        queue = state.queue if state else []
        if snapshot and tuple(id(e) for e in queue) != snapshot:
            await cog.send_reply(interaction, t(interaction, "QUEUE_STATE_CHANGED"), ephemeral=True)
            return False
        _qh = id(queue[0]) if queue else None
        if len(queue) >= 2:
            random.shuffle(queue)
    cog._maybe_restart_prefetch(guild_id, _qh)
    cog._schedule_refresh(guild_id)
    return True


def _check_seek_permission(cog, interaction, guild_id, current):
    """Check seek permission. Returns (allowed, is_dj, is_admin)."""
    is_dj = cog.has_control_privilege(guild_id, interaction.user)
    is_admin = cog.has_admin_privilege(guild_id, interaction.user)
    is_effective_owner = cog._is_effective_owner(guild_id, interaction.user)
    is_requester = current.get("requester") == interaction.user.id
    seek_perm = cog.guild_seek_permission.get(guild_id, "requester_dj")
    allowed = True
    if seek_perm == "owner":
        allowed = is_effective_owner
    elif seek_perm == "admin":
        allowed = is_admin
    elif seek_perm == "dj":
        allowed = is_dj or is_admin
    elif seek_perm == "everyone":
        pass
    else:  # requester_dj
        allowed = is_requester or is_dj or is_admin
    return allowed, is_dj, is_admin


async def _do_stop_full(cog, interaction, guild_id, vc):
    """Shared stop logic for slash and button. Handles vote, radio, cleanup."""
    session = cog.get_radio_session(guild_id)
    vote_passed = await cog.handle_vote(
        interaction, "stop", guild_id,
        success_message=t(interaction, "STOPPED"),
        vote_message=t(interaction, "STOP_VOTED"),
    )
    if not vote_passed:
        return False
    if session and session.active:
        starter_id = session.starter_id
        is_self_stop = interaction.user.id == starter_id
        reason = "stopped_self" if is_self_stop else "stopped"
        await cog._end_radio(guild_id, reason)
    cog._do_stop(guild_id, vc, interaction)
    return True


async def _queue_move_single(cog, interaction, guild_id, from_index, dest, queue, snapshot):
    """Move a single track. Returns True if applied."""
    state = cog.guild_states.get(guild_id)
    qlen = len(queue)
    if from_index == dest:
        await cog.send_reply(interaction, t(interaction, "MOVE_SAME"), ephemeral=True)
        return False
    if not (1 <= from_index <= qlen):
        await cog.send_reply(interaction, t(interaction, "MOVE_INVALID"), ephemeral=True)
        return False
    target_index = max(1, min(dest, qlen))
    song = queue[from_index - 1]
    title = song.get("title") or t(interaction, "UNKNOWN")
    uploader = song.get("uploader") or t(interaction, "UNKNOWN")
    uid = interaction.user.id
    lo, hi = min(from_index, target_index), max(from_index, target_index)
    all_owned = all(queue[i - 1].get("requester") == uid for i in range(lo, hi + 1))
    vote_passed = await cog.handle_vote(
        interaction, "move", (guild_id, from_index, target_index),
        success_message=t(interaction, "MOVE_SUCCESS", title=title, uploader=uploader, from_pos=from_index, to_pos=target_index),
        vote_message=t(interaction, "MOVE_VOTE", title=title, uploader=uploader, from_pos=from_index, to_pos=target_index),
        already_voted_message=t(interaction, "ALREADY_VOTED"),
        is_owner=all_owned, requires_same_channel=True,
    )
    if not vote_passed:
        return False
    queue = state.queue if state else []
    if tuple(id(e) for e in queue) != snapshot:
        await cog.send_reply(interaction, t(interaction, "QUEUE_STATE_CHANGED"), ephemeral=True)
        return False
    if from_index - 1 < len(queue):
        _qh = id(queue[0]) if queue else None
        moved = queue.pop(from_index - 1)
        target_index = max(1, min(dest, len(queue) + 1))
        queue.insert(target_index - 1, moved)
        cog._maybe_restart_prefetch(guild_id, _qh)
        cog._schedule_refresh(guild_id)
    return True


async def _queue_move_range(cog, interaction, guild_id, range_start, range_end, dest, queue, snapshot):
    """Move a range of tracks. Returns True if applied."""
    state = cog.guild_states.get(guild_id)
    qlen = len(queue)
    if range_end < range_start:
        await cog.send_reply(interaction, t(interaction, "MOVE_INVALID_RANGE",
            to_index=t(interaction, "OPTNAME_MOVE_TO"), from_index=t(interaction, "OPTNAME_MOVE_FROM")), ephemeral=True)
        return False
    if not (1 <= range_start <= qlen) or not (1 <= range_end <= qlen):
        await cog.send_reply(interaction, t(interaction, "MOVE_INVALID"), ephemeral=True)
        return False
    dest_clamped = max(1, min(dest, qlen))
    if range_start <= dest_clamped <= range_end:
        await cog.send_reply(interaction, t(interaction, "MOVE_INVALID"), ephemeral=True)
        return False
    uid = interaction.user.id
    if dest_clamped < range_start:
        _affected = range(dest_clamped, range_end + 1)
    else:
        _affected = range(range_start, dest_clamped + 1)
    _all_owned = all(queue[i - 1].get("requester") == uid for i in _affected)
    vote_passed = await cog.handle_vote(
        interaction, "move", (guild_id, range_start, range_end, dest_clamped),
        success_message=t(interaction, "MOVE_RANGE_SUCCESS", **{"from": range_start, "to": range_end, "dest": dest_clamped}),
        vote_message=t(interaction, "MOVE_RANGE_VOTE", **{"from": range_start, "to": range_end, "dest": dest_clamped}),
        already_voted_message=t(interaction, "ALREADY_VOTED"),
        is_owner=_all_owned, requires_same_channel=True,
    )
    if not vote_passed:
        return False
    queue = state.queue if state else []
    if tuple(id(e) for e in queue) != snapshot:
        await cog.send_reply(interaction, t(interaction, "QUEUE_STATE_CHANGED"), ephemeral=True)
        return False
    _qh = id(queue[0]) if queue else None
    chunk = queue[range_start - 1:range_end]
    del queue[range_start - 1:range_end]
    di = dest_clamped - 1
    if dest_clamped > range_end:
        di -= len(chunk)
    ins = max(0, min(di, len(queue)))
    queue[ins:ins] = chunk
    cog._maybe_restart_prefetch(guild_id, _qh)
    cog._schedule_refresh(guild_id)
    return True


async def _queue_remove_index(cog, interaction, guild_id, index, index_to, search, queue, snapshot):
    """Handle index-based removal (single or range) with vote. Returns (parts_list, mutated_bool)."""
    state = cog.guild_states.get(guild_id)
    parts = []
    if index_to is not None:
        is_privileged = cog.has_control_privilege(guild_id, interaction.user) or cog.has_admin_privilege(guild_id, interaction.user)
        user_id = interaction.user.id
        slice_indices = list(range(index, index_to + 1))
        owned = [i for i in slice_indices if queue[i - 1].get("requester") == user_id]
        not_owned = [i for i in slice_indices if i not in owned]
        if is_privileged or not owned:
            msg = t(interaction, "REMOVE_SLICE_SUCCESS", start=index, end=index_to)
            vote_passed = await cog.handle_vote(
                interaction, "remove", (guild_id, index, index_to),
                success_message=msg,
                vote_message=t(interaction, "REMOVE_VOTE", index=f"{index}-{index_to}", title=msg, uploader=""),
                already_voted_message=t(interaction, "ALREADY_VOTED"),
                is_owner=is_privileged or (not not_owned),
                requires_same_channel=True,
                send_success=not search,
            )
            if not vote_passed:
                return parts, False
            queue = state.queue if state else []
            if tuple(id(e) for e in queue) != snapshot:
                await cog.send_reply(interaction, t(interaction, "QUEUE_STATE_CHANGED"), ephemeral=True)
                return parts, False
            _qh = id(queue[0]) if queue else None
            _del_end = min(index_to, len(queue))
            del queue[index - 1:_del_end]
            cog._maybe_restart_prefetch(guild_id, _qh)
            if search:
                parts.append(msg)
            else:
                cog._schedule_refresh(guild_id)
        else:
            _qh = id(queue[0]) if queue else None
            for i in sorted(owned, reverse=True):
                queue.pop(i - 1)
            cog._maybe_restart_prefetch(guild_id, _qh)
            if not not_owned:
                parts.append(t(interaction, "REMOVE_SLICE_SUCCESS", start=index, end=index_to))
                cog._schedule_refresh(guild_id)
            else:
                owned_msg = ""
                if owned:
                    owned_msg = t(interaction, "REMOVE_OWNED_REMOVED", count=len(owned), start=index, end=index_to)
                new_indices = []
                for orig_idx in not_owned:
                    shift = sum(1 for o in owned if o < orig_idx)
                    new_indices.append(orig_idx - shift)
                snapshot_after = tuple(id(e) for e in queue)
                idx_str = f"#{new_indices[0]}" if len(new_indices) == 1 else f"#{new_indices[0]}-#{new_indices[-1]}"
                vote_msg = t(interaction, "REMOVE_VOTE_REMAINING", indices=idx_str)
                if owned_msg:
                    vote_msg = owned_msg + "\n" + vote_msg
                vote_passed = await cog.handle_vote(
                    interaction, "remove", (guild_id, *tuple(new_indices)),
                    success_message=t(interaction, "REMOVE_SLICE_SUCCESS", start=new_indices[0], end=new_indices[-1]),
                    vote_message=vote_msg,
                    already_voted_message=t(interaction, "ALREADY_VOTED"),
                    is_owner=False, requires_same_channel=True,
                    send_success=False,
                )
                if vote_passed:
                    queue = state.queue if state else []
                    if tuple(id(e) for e in queue) != snapshot_after:
                        await cog.send_reply(interaction, t(interaction, "QUEUE_STATE_CHANGED"), ephemeral=True)
                        return parts, False
                    _qh = id(queue[0]) if queue else None
                    for i in sorted(new_indices, reverse=True):
                        if i - 1 < len(queue):
                            queue.pop(i - 1)
                    cog._maybe_restart_prefetch(guild_id, _qh)
                    if owned_msg:
                        parts.append(owned_msg)
                    parts.append(t(interaction, "REMOVE_SLICE_SUCCESS", start=new_indices[0], end=new_indices[-1]))
                else:
                    cog._schedule_refresh(guild_id)
                    if owned_msg:
                        parts.append(owned_msg)
                    if not search:
                        return parts, False
    else:
        entry = queue[index - 1]
        title = entry.get("title") or t(interaction, "UNKNOWN")
        uploader = entry.get("uploader") or t(interaction, "UNKNOWN")
        msg = t(interaction, "REMOVE_SUCCESS", index=index, title=title, uploader=uploader)
        vote_passed = await cog.handle_vote(
            interaction, "remove", (guild_id, index),
            success_message=msg,
            vote_message=t(interaction, "REMOVE_VOTE", index=index, title=title, uploader=uploader),
            already_voted_message=t(interaction, "ALREADY_VOTED"),
            is_owner=entry.get("requester") == interaction.user.id,
            requires_same_channel=True,
            send_success=not search,
        )
        if not vote_passed:
            return parts, False
        queue = state.queue if state else []
        if tuple(id(e) for e in queue) != snapshot:
            await cog.send_reply(interaction, t(interaction, "QUEUE_STATE_CHANGED"), ephemeral=True)
            return parts, False
        _qh = id(queue[0]) if queue else None
        if index - 1 < len(queue):
            queue.pop(index - 1)
        cog._maybe_restart_prefetch(guild_id, _qh)
        if search:
            parts.append(msg)
        else:
            cog._schedule_refresh(guild_id)
    return parts, True


async def _queue_remove_search(cog, interaction, guild_id, search, queue, state, parts):
    """Handle search-based removal. Appends to parts list."""
    if not cog.has_control_privilege(guild_id, interaction.user) and not cog.has_admin_privilege(guild_id, interaction.user):
        await cog.send_reply(interaction, t(interaction, "NOT_DJ_OR_ADMIN"), ephemeral=True)
        return
    _qh = id(queue[0]) if queue else None
    count = _remove_by_search(queue, search)
    if count:
        cog._maybe_restart_prefetch(guild_id, _qh)
        parts.append(t(interaction, "REMOVE_SEARCH_SUCCESS", count=count, search=search))
    elif not parts:
        parts.append(t(interaction, "REMOVE_SEARCH_NONE", search=search))


async def _queue_clear_by_user(cog, interaction, guild_id, state, target_id, target_name=None):
    """Clear a user's tracks from queue. target_name=None means clearing own tracks. Returns message or None."""
    count = sum(1 for e in state.queue if e.get("requester") == target_id)
    if count == 0:
        key = "CLEAR_MINE_EMPTY" if target_name is None else "CLEAR_USER_EMPTY"
        await cog.send_reply(interaction, t(interaction, key), ephemeral=True)
        return None
    _qh = id(state.queue[0]) if state.queue else None
    state.queue[:] = [e for e in state.queue if e.get("requester") != target_id]
    cog._maybe_restart_prefetch(guild_id, _qh)
    cog._schedule_refresh(guild_id)
    if target_name is None:
        return t(interaction, "CLEAR_MINE_SUCCESS", count=count)
    return t(interaction, "CLEAR_USER_SUCCESS", count=count, user=target_name)


async def _queue_clear_all(cog, interaction, guild_id, state):
    """Clear entire queue via vote. Returns True if cleared."""
    vote_passed = await cog.handle_vote(
        interaction, "clear", guild_id,
        success_message=t(interaction, "QUEUE_CLEARED"),
        vote_message=t(interaction, "CLEAR_VOTE"),
    )
    if not vote_passed:
        return False
    cog._cancel_bg_fetch(guild_id)
    state.queue.clear()
    cog.playback.cleanup_guild_votes(guild_id)
    cog._schedule_refresh(guild_id)
    return True


class PLRenameModalStandalone(discord.ui.Modal):
    """Rename modal used from the /playlist slash command (not tied to a PlaylistView)."""

    def __init__(self, cog, interaction, guild_id, user_id, playlist):
        super().__init__(title=t(interaction, "PL_RENAME_TITLE"))
        self.cog = cog
        self.guild_id = guild_id
        self.user_id = user_id
        self.playlist = playlist
        self.name_input = discord.ui.TextInput(
            label=t(interaction, "PL_RENAME_LABEL"),
            default=playlist["name"],
            max_length=50,
            required=True,
        )
        self.add_item(self.name_input)

    async def on_submit(self, modal_interaction: discord.Interaction):
        msg, ok = await _pl_rename(modal_interaction, self.guild_id, self.user_id,
            self.playlist["id"], self.playlist["name"], self.name_input.value)
        await self.cog.send_reply(modal_interaction, msg, ephemeral=True)
        if ok:
            await self.cog._refresh_active_playlist(self.guild_id, self.user_id)


async def _dispatch_single_play(cog, interaction, entry, played):
    title, uploader = entry.get("title") or t(interaction, "UNKNOWN"), entry.get("uploader") or t(interaction, "UNKNOWN")
    if played == "restricted":
        await cog.send_reply(interaction, t(interaction, "JOIN_RESTRICTED_CHANNEL"), ephemeral=True)
    elif played == "user_limit":
        await cog.send_reply(interaction, t(interaction, "USER_TRACK_LIMIT"), ephemeral=True)
    elif played == "connect_failed":
        await cog.send_reply(interaction, t(interaction, "VOICE_CONNECT_FAILED"), ephemeral=True)
    elif played == "live_blocked":
        await cog.send_reply(interaction, t(interaction, "LIVE_BLOCKED"), ephemeral=True)
    elif played is None:
        _q_lim = cog.guild_queue_limit.get(interaction.guild_id, MAX_QUEUE)
        await cog.send_reply(interaction, t(interaction, "QUEUE_FULL", limit=_q_lim), ephemeral=True)
    elif played:
        await cog.send_reply(interaction, t(interaction, "ANNOUNCE_NOW", title=title, uploader=uploader))
    else:
        await cog.send_reply(interaction, t(interaction, "ADDED_TO_QUEUE_SINGLE", title=f"`{title}` - `{uploader}`"))
        cog._schedule_refresh(interaction.guild_id)


def l_cmd(name_key: str, desc_key: str) -> dict:
    """
    Returns a dict containing name and description for use in an app_command decorator.
    """
    default_name = t(DEFAULT_LOCALE, name_key).lower()
    default_desc = t(DEFAULT_LOCALE, desc_key)
    return {
        "name": discord.app_commands.locale_str(default_name, message=name_key),
        "description": discord.app_commands.locale_str(default_desc, message=desc_key),
    }


def l_opt(key: str) -> discord.app_commands.locale_str:
    """Returns a locale_str for localizing option names and descriptions."""
    return discord.app_commands.locale_str(t(DEFAULT_LOCALE, key), message=key)


def localized_choice(value: str, key: str) -> discord.app_commands.Choice:
    """Creates a Discord app_commands.Choice with localized mapping."""
    default_name = t(DEFAULT_LOCALE, key)
    return discord.app_commands.Choice(name=discord.app_commands.locale_str(default_name, message=key), value=value)


class _OwnerChannelConfirmView(discord.ui.View):
    def __init__(self, cog, interaction, command_name):
        super().__init__(timeout=30)
        self.cog = cog
        self.ctx = interaction
        self.command_name = command_name
        self.result = None
        confirm_label = t(interaction, "OWNER_CHANNEL_CONFIRM")
        cancel_label = t(interaction, "BUTTON_CANCEL")
        self.add_item(_OwnerConfirmButton(self, confirm_label))
        self.add_item(_OwnerCancelButton(self, cancel_label))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.cog.check_view_interaction(interaction)


class _OwnerConfirmButton(discord.ui.Button):
    def __init__(self, view_ref, label):
        super().__init__(style=discord.ButtonStyle.primary, label=label)
        self.view_ref = view_ref

    async def callback(self, btn_interaction: discord.Interaction):
        self.view_ref.result = True
        await btn_interaction.response.defer()
        self.view_ref.stop()


class _OwnerCancelButton(discord.ui.Button):
    def __init__(self, view_ref, label):
        super().__init__(style=discord.ButtonStyle.secondary, emoji="✖️", label=label)
        self.view_ref = view_ref

    async def callback(self, btn_interaction: discord.Interaction):
        self.view_ref.result = False
        await btn_interaction.response.defer()
        self.view_ref.stop()


_BUTTON_EMOJIS = {
    "BUTTON_FIRST": "⏪",
    "BUTTON_PREV": "⬅️",
    "BUTTON_NEXT": "➡️",
    "BUTTON_LAST": "⏩",
    "BUTTON_REFRESH": "🔄",
    "BUTTON_TOGGLE": "⏯️",
    "BUTTON_SKIP": "⏭️",
    "BUTTON_STOP": "⏹️",
    "BUTTON_SELECT": "▶️",
    "BUTTON_SHUFFLE": "🔀",
    "BUTTON_REMOVE": "🗑️",
    "BUTTON_MOVE": "↕️",
    "BUTTON_LOOP": "🔁",
    "BUTTON_SEEK": "⏩",
    "BUTTON_ADD_TRACK": "🎶",
    "BUTTON_PLAY_ALL": "▶️",
    "BUTTON_PLAY_SHUFFLE": "🔀",
    "BUTTON_COPY": "📋",
    "BUTTON_SEARCH": "🔍",
    "BUTTON_PREV_TRACK": "⏮️",
    "BUTTON_CLEAR_QUEUE": "🧹",
}


async def _apply_seek(cog, interaction, state, vc, current, h, m, s):
    if state.now is None:
        await cog.send_reply(interaction, t(interaction, "SEEK_NO_VALID"), ephemeral=True)
        return False
    if current.get("is_live"):
        await cog.send_reply(interaction, t(interaction, "LIVE_NO_SEEK"), ephemeral=True)
        return False
    new_entry = current.copy()
    total_seconds = max(0, s + m * 60 + h * 3600)
    new_entry["seek_time"] = total_seconds
    new_entry["suppress_announce"] = True

    if state.loop_mode == "song":
        state.now["seek_time"] = total_seconds
        state.now["suppress_announce"] = True
    else:
        state.queue.insert(0, new_entry)

    state._seeking = True
    state.playing_since = None
    state.paused_at = None
    state._total_paused = 0.0
    state.cancel_tasks()
    vc.stop()

    _hu = t(interaction, 'ABBR_HOURS')
    _mu = t(interaction, 'ABBR_MINUTES')
    _su = t(interaction, 'ABBR_SECONDS')
    parts = []
    if h:
        parts.append(f"{h}{_hu}")
    if m:
        parts.append(f"{m}{_mu}")
    if s:
        parts.append(f"{s}{_su}")
    if not parts:
        parts.append(t(interaction, "SEEK_START"))

    await cog.send_reply(interaction, t(interaction, "SEEKING", position=' '.join(parts)))
    return True


class _PageButton(discord.ui.Button):
    def __init__(self, view, emoji, label, target, *, disabled=False, custom_id=None, row=0,
                 page_attr="page", total_attr="total_pages", wrap=False):
        super().__init__(style=discord.ButtonStyle.secondary, emoji=emoji, label=label, disabled=disabled, row=row, custom_id=custom_id)
        self.view_ref = view
        self.target = target
        self._pa = page_attr
        self._ta = total_attr
        self._wrap = wrap

    async def callback(self, btn_interaction: discord.Interaction):
        v = self.view_ref
        if hasattr(v, '_busy'):
            v._busy = True
        try:
            old = getattr(v, self._pa)
            total = getattr(v, self._ta)
            cur = old
            if self.target == "first":
                cur = 0
            elif self.target == "last":
                cur = total - 1
            elif self.target == "prev":
                cur = (cur - 1) % total if self._wrap else max(0, cur - 1)
            else:
                cur = (cur + 1) % total if self._wrap else min(total - 1, cur + 1)
            setattr(v, self._pa, cur)
            if hasattr(v, 'validate_page'):
                v.validate_page()
            else:
                v._build()
            try:
                await btn_interaction.response.edit_message(embed=v.get_embed(), view=v)
            except discord.HTTPException:
                setattr(v, self._pa, old)
        finally:
            if hasattr(v, '_busy'):
                v._busy = False


class _GoToPageModal(discord.ui.Modal):
    def __init__(self, view_ref, ctx, *, page_attr="page", total_attr="total_pages"):
        total = getattr(view_ref, total_attr)
        super().__init__(title=t(ctx, "QUEUE_GOTO_TITLE"))
        self._view = view_ref
        self._pa = page_attr
        self._ta = total_attr
        self.page_input = discord.ui.TextInput(
            label=t(ctx, "QUEUE_GOTO_LABEL"),
            placeholder=f"1-{total}",
            max_length=5,
            required=True,
        )
        self.add_item(self.page_input)

    async def on_submit(self, modal_interaction: discord.Interaction):
        try:
            page = int(self.page_input.value)
        except ValueError:
            return await modal_interaction.response.send_message(
                t(modal_interaction, "INVALID_INPUT"), ephemeral=True, delete_after=self._view.cog._resolve_delete_after(modal_interaction.guild_id) if hasattr(self._view, 'cog') and self._view.cog else None)
        v = self._view
        total = getattr(v, self._ta)
        if not (1 <= page <= total):
            return await modal_interaction.response.send_message(
                t(modal_interaction, "QUEUE_GOTO_INVALID"), ephemeral=True, delete_after=self._view.cog._resolve_delete_after(modal_interaction.guild_id) if hasattr(self._view, 'cog') and self._view.cog else None)
        setattr(v, self._pa, page - 1)
        if hasattr(v, 'validate_page'):
            v.validate_page()
        elif hasattr(v, '_build'):
            v._build()
        await modal_interaction.response.edit_message(embed=v.get_embed(), view=v)


class _GoToPageButton(discord.ui.Button):
    def __init__(self, view, *, disabled=False, custom_id=None, label=None, row=0,
                 page_attr="page", total_attr="total_pages"):
        super().__init__(style=discord.ButtonStyle.secondary, emoji="#️⃣", label=label, disabled=disabled, row=row, custom_id=custom_id)
        self.view_ref = view
        self._pa = page_attr
        self._ta = total_attr

    async def callback(self, btn_interaction: discord.Interaction):
        await btn_interaction.response.send_modal(
            _GoToPageModal(self.view_ref, btn_interaction, page_attr=self._pa, total_attr=self._ta))


class _SearchButton(discord.ui.Button):
    """Shared search button. The view must have ``_get_search_tracks()`` and ``cog``."""
    def __init__(self, view, *, label=None, emoji="🔍", disabled=False, custom_id=None, row=0, show_details=False):
        super().__init__(style=discord.ButtonStyle.secondary, emoji=emoji, label=label, disabled=disabled, row=row, custom_id=custom_id)
        self.view_ref = view
        self._show_details = show_details

    async def callback(self, btn_interaction: discord.Interaction):
        v = self.view_ref
        tracks = v._get_search_tracks()
        if not tracks:
            return await v.cog.send_reply(btn_interaction, t(btn_interaction, "QUEUE_SEARCH_NO_RESULTS"), ephemeral=True)
        await btn_interaction.response.send_modal(_SearchModal(v, btn_interaction, show_details=self._show_details))


class _SearchModal(discord.ui.Modal):
    """Shared search modal. The view must have ``_get_search_tracks()`` and ``cog``."""
    def __init__(self, view_ref, ctx, *, show_details=False):
        super().__init__(title=t(ctx, "QUEUE_SEARCH_TITLE"))
        self._view = view_ref
        self._show_details = show_details
        self.search_input = discord.ui.TextInput(
            label=t(ctx, "QUEUE_SEARCH_LABEL"),
            placeholder=t(ctx, "QUEUE_SEARCH_PLACEHOLDER"),
            max_length=100,
            required=True,
        )
        self.add_item(self.search_input)

    async def on_submit(self, modal_interaction: discord.Interaction):
        tracks = self._view._get_search_tracks()
        await _search_submit(tracks, self.search_input.value, modal_interaction, self._view.cog, show_details=self._show_details)


# --- Shared playlist buttons (used by both PlaylistView and SharedPlaylistView) ---
# Views must implement: cog, ctx, tracks, get_embed(), set_busy(bool), _edit_view(),
#                        reload_tracks(), get_playlist_id(), _build()

class _PLRefreshBtn(discord.ui.Button):
    def __init__(self, view, emoji, label, *, cooldown_key="pl:refresh", row=2):
        super().__init__(style=discord.ButtonStyle.success, emoji=emoji, label=label, row=row)
        self.view_ref = view
        self._ck = cooldown_key
    async def callback(self, btn_interaction: discord.Interaction):
        if not await self.view_ref.cog._check_cooldown(btn_interaction, self._ck, 2): return
        v = self.view_ref
        v._busy = True
        try:
            await v.reload_tracks()
            v.total_pages = max(1, (len(v.tracks) - 1) // v.per_page + 1)
            v.page = min(v.page, v.total_pages - 1)
            v._build()
            await btn_interaction.response.edit_message(embed=v.get_embed(), view=v)
        finally:
            v._busy = False


class _PLPlayBtn(discord.ui.Button):
    def __init__(self, view, emoji, label, *, disabled=False, cooldown_key="pl:play", row=3):
        super().__init__(style=discord.ButtonStyle.secondary, emoji=emoji, label=label, disabled=disabled, row=row)
        self.view_ref = view
        self._ck = cooldown_key
    async def callback(self, btn_interaction: discord.Interaction):
        if not await self.view_ref.cog._check_cooldown(btn_interaction, self._ck, 5): return
        v = self.view_ref
        if not btn_interaction.user.voice or not btn_interaction.user.voice.channel:
            return await v.cog.send_reply(btn_interaction, t(btn_interaction, "JOIN_VOICE_FIRST"), ephemeral=True)
        if not v.tracks:
            return await v.cog.send_reply(btn_interaction, t(btn_interaction, "PL_EMPTY"), ephemeral=True)
        await btn_interaction.response.send_modal(_PLPlayModal(v, btn_interaction))


class _PLPlayModal(discord.ui.Modal):
    def __init__(self, view_ref, ctx):
        super().__init__(title=t(ctx, "BUTTON_PLAY_ALL"))
        self._view = view_ref
        tcount = len(view_ref.tracks)
        self.index_input = discord.ui.TextInput(
            label=t(ctx, "QUEUE_SELECT_LABEL"),
            placeholder=f"1-{tcount}",
            max_length=5,
            required=False,
        )
        self.indexto_input = discord.ui.TextInput(
            label=t(ctx, "OPT_SELECT_INDEXTO"),
            placeholder=f"1-{tcount}",
            max_length=5,
            required=False,
        )
        self.shuffle_lbl = discord.ui.Label(
            text=t(ctx, "SHUFFLE_LABEL"),
            description=t(ctx, "SHUFFLE_PLAY_DESC"),
            component=discord.ui.Checkbox(custom_id="shuffle"),
        )
        self.add_item(self.index_input)
        self.add_item(self.indexto_input)
        self.add_item(self.shuffle_lbl)

    async def on_submit(self, modal_interaction: discord.Interaction):
        v = self._view
        tcount = len(v.tracks)
        do_shuffle = self.shuffle_lbl.component.value
        raw_from = self.index_input.value.strip() if self.index_input.value else ""
        raw_to = self.indexto_input.value.strip() if self.indexto_input.value else ""
        if raw_from:
            try:
                idx = int(raw_from)
            except ValueError:
                return await v.cog.send_reply(modal_interaction, t(modal_interaction, "INVALID_INPUT"), ephemeral=True)
            if not (1 <= idx <= tcount):
                return await v.cog.send_reply(modal_interaction, t(modal_interaction, "SELECT_INVALID"), ephemeral=True)
            if raw_to:
                try:
                    idx_to = int(raw_to)
                except ValueError:
                    return await v.cog.send_reply(modal_interaction, t(modal_interaction, "INVALID_INPUT"), ephemeral=True)
                if idx_to < idx or idx_to > tcount:
                    return await v.cog.send_reply(modal_interaction, t(modal_interaction, "PL_PLAY_INVALID_RANGE"), ephemeral=True)
                selected = v.tracks[idx - 1:idx_to]
            else:
                selected = v.tracks[idx - 1:]
        else:
            selected = v.tracks
        v._busy = True
        _gid = getattr(v, "guild_id", None)
        _vid = getattr(v, "viewer_id", None)
        if _gid is not None and _vid is not None:
            v.cog.playlist_busy.add((_gid, _vid))
        for item in v.children:
            item.disabled = True
        try:
            await modal_interaction.response.edit_message(embed=v.get_embed(), view=v)
        except discord.HTTPException:
            if not modal_interaction.response.is_done():
                await modal_interaction.response.defer()
        try:
            await v.cog._play_playlist(modal_interaction, selected, shuffle=do_shuffle)
        finally:
            await v.set_busy(False)


class _PLCopyBtn(discord.ui.Button):
    def __init__(self, view, emoji, label, *, disabled=False, cooldown_key="pl:copy", row=3):
        super().__init__(style=discord.ButtonStyle.secondary, emoji=emoji, label=label, disabled=disabled, row=row)
        self.view_ref = view
        self._ck = cooldown_key
    async def callback(self, btn_interaction: discord.Interaction):
        if not await self.view_ref.cog._check_cooldown(btn_interaction, self._ck, 5): return
        v = self.view_ref
        if not v.get_playlist_id():
            return await v.cog.send_reply(btn_interaction, t(btn_interaction, "PL_NO_PLAYLISTS"), ephemeral=True)
        key = (btn_interaction.guild_id, btn_interaction.user.id)
        prev = v.cog._active_copy_pickers.pop(key, None)
        if prev:
            try:
                await prev.delete_original_response()
            except discord.HTTPException:
                pass
        user_playlists = await db.get_user_playlists(btn_interaction.guild_id, btn_interaction.user.id)
        source_id = v.get_playlist_id()
        targets = [p for p in user_playlists if p["id"] != source_id]
        if targets:
            src_name = getattr(v, "playlist_name", None) or (v.playlist["name"] if getattr(v, "playlist", None) else "?")
            dest_view = _PLCopyDestView(v, btn_interaction, targets, btn_interaction)
            await v.cog.send_reply(btn_interaction, t(btn_interaction, "PL_COPY_DEST_PROMPT", name=src_name), view=dest_view, ephemeral=True, delete_after=None)
            v.cog._active_copy_pickers[key] = btn_interaction
        else:
            await btn_interaction.response.send_modal(_PLCopyNewModal(v, btn_interaction))


class _PLCopyDestView(discord.ui.View):
    def __init__(self, parent_view, ctx, playlists: list[dict], origin):
        super().__init__(timeout=120)
        self._parent = parent_view
        self._cog = getattr(parent_view, "cog", None)
        self._origin = origin
        options = []
        for p in playlists[:25]:
            fav = "\u2605 " if p.get("is_favourite") else ""
            options.append(discord.SelectOption(
                label=f"{fav}{p['name']}"[:100], value=str(p["id"]),
                description=t(ctx, "PL_TRACK_COUNT", count=p.get("track_count", 0))[:100]))
        sel = discord.ui.Select(placeholder=t(ctx, "PL_COPY_DEST_PLACEHOLDER"), options=options, row=0)
        sel.callback = self._select_cb
        self.add_item(sel)
        btn = discord.ui.Button(style=discord.ButtonStyle.primary, label=t(ctx, "PL_COPY_NEW"), emoji="\u2795", row=1)
        btn.callback = self._new_cb
        self.add_item(btn)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self._cog:
            return await self._cog.check_view_interaction(interaction)
        return True

    def _untrack(self, guild_id: int, user_id: int):
        self._parent.cog._active_copy_pickers.pop((guild_id, user_id), None)

    async def on_timeout(self):
        self._untrack(self._origin.guild_id, self._origin.user.id)

    async def _select_cb(self, interaction: discord.Interaction):
        v = self._parent
        self._untrack(interaction.guild_id, interaction.user.id)
        dest_id = int(self.children[0].values[0])
        is_shared = not hasattr(v, "viewer_id")
        if not is_shared:
            v._busy = True
        try:
            limit = v.cog.guild_playlist_track_limit.get(interaction.guild_id, 5000)
            count = await db.append_tracks_to_playlist(v.get_playlist_id(), dest_id, max_tracks=limit)
            dest_pl = await db.get_playlist_by_id(dest_id)
            dest_name = dest_pl["name"] if dest_pl else "?"
            await interaction.response.edit_message(
                content=t(interaction, "PL_COPY_APPENDED", count=count, name=dest_name), view=None)
        finally:
            if not is_shared:
                v._busy = False
        self.stop()

    async def _new_cb(self, interaction: discord.Interaction):
        self._untrack(interaction.guild_id, interaction.user.id)
        await interaction.response.send_modal(_PLCopyNewModal(self._parent, interaction))
        try:
            await self._origin.delete_original_response()
        except discord.HTTPException:
            pass
        self.stop()


class _PLCopyNewModal(discord.ui.Modal):
    def __init__(self, view_ref, ctx):
        super().__init__(title=t(ctx, "PL_COPY_TITLE"))
        self._view = view_ref
        self.name_input = discord.ui.TextInput(
            label=t(ctx, "PL_COPY_LABEL"), max_length=50, required=True,
        )
        self.add_item(self.name_input)

    async def on_submit(self, modal_interaction: discord.Interaction):
        v = self._view
        name = self.name_input.value.strip()[:50]
        v._busy = True
        try:
            await _copy_submit(v.get_playlist_id(), name, modal_interaction, v.cog)
        finally:
            v._busy = False


class MusicCog(MusicHandlers):

    def _do_stop(self, guild_id: int, vc, interaction):
        self._cancel_bg_fetch(guild_id)
        session = self.radio_sessions.pop(guild_id, None)
        if session:
            _deactivate_radio_session(session)
        state = self.guild_states.get(guild_id)
        if not state:
            return
        if vc.is_playing() or vc.is_paused():
            state.suppress_after_callback = True
            vc.stop()
        state.queue.clear()
        state.now = None
        state.playing = False
        state.playing_since = None
        state.paused_at = None
        state._total_paused = 0.0
        state.loop_mode = "off"
        state.skip_current_song_once = False
        self.playback.cleanup_guild_votes(guild_id)
        self.playback._cancel_live_timer(guild_id)
        state.vc = vc
        state.text_channel = state.text_channel or interaction.channel
        state.cancel_tasks()
        idle_timeout = self.guild_idle_disconnect.get(guild_id, 180)
        if idle_timeout > 0:
            state.idle_disconnect_task = self._create_task(self.playback.auto_disconnect_after(guild_id, idle_timeout), name=f"idle-dc-{guild_id}")
        self._schedule_refresh(guild_id)

    # region Commands

    _CLEAR_MINE_CHECK = [app_commands.Choice(name="✅", value="on")]

    @app_commands.command(**l_cmd("CMD_NAME_CLEAR", "CMD_DESC_CLEAR"))
    @app_commands.rename(mine=l_opt("OPTNAME_CLEAR_MINE"), user=l_opt("OPTNAME_CLEAR_USER"))
    @app_commands.describe(mine=l_opt("OPT_CLEAR_MINE"), user=l_opt("OPT_CLEAR_USER"))
    @app_commands.choices(mine=_CLEAR_MINE_CHECK)
    async def clear_cmd(self, interaction: discord.Interaction, mine: str | None = None, user: discord.Member | None = None):
        if not await self._check_cooldown(interaction, "mp:clear", 1, per_guild=True): return
        if not await self._check_cooldown(interaction, "mp:clear", 5): return
        await interaction.response.defer()
        if self.is_radio_active(interaction.guild_id):
            return await self.send_reply(interaction, t(interaction, "RADIO_CMD_RESTRICTED", cmd_stop=t(interaction, "CMD_NAME_STOP")), ephemeral=True)
        try:
            state = self.ensure_queue_data(interaction, require_queue=True, queue_message=t(interaction, "CLEAR_EMPTY"))
        except CommandCheckError as e:
            return await respond_with_error(interaction, e)
        guild_id = interaction.guild.id
        if user:
            if not self.has_control_privilege(guild_id, interaction.user) and not self.has_admin_privilege(guild_id, interaction.user):
                return await self.send_reply(interaction, t(interaction, "NOT_DJ_OR_ADMIN"), ephemeral=True)
            msg = await _queue_clear_by_user(self, interaction, guild_id, state, user.id, user.display_name)
            if msg:
                await self.send_reply(interaction, msg)
        elif mine:
            msg = await _queue_clear_by_user(self, interaction, guild_id, state, interaction.user.id)
            if msg:
                await self.send_reply(interaction, msg)
        else:
            await _queue_clear_all(self, interaction, guild_id, state)

    @app_commands.command(**l_cmd("CMD_NAME_JOIN", "CMD_DESC_JOIN"))
    async def join(self, interaction: discord.Interaction):
        if not await self._check_cooldown(interaction, "join", 5): return
        await interaction.response.defer()

        if not interaction.user.voice or not interaction.user.voice.channel:
            return await self.send_reply(interaction, t(interaction, "JOIN_VOICE_FIRST"), ephemeral=True)

        voice_channel = interaction.user.voice.channel

        if not self.check_join_restriction(interaction.guild.id, voice_channel.id, interaction.user):
            return await self.send_reply(interaction, t(interaction, "JOIN_RESTRICTED_CHANNEL"), ephemeral=True)

        vc = interaction.guild.voice_client
        message = None
        ephemeral = False

        if vc:
            if not vc.is_connected():
                self._intentional_disconnect.add(interaction.guild.id)
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
                vc = None

        if vc:
            current_channel = vc.channel

            if current_channel != voice_channel:
                if len(current_channel.members) <= 1 or self.has_control_privilege(interaction.guild.id, interaction.user) or self.has_admin_privilege(interaction.guild.id, interaction.user):
                    try:
                        await vc.move_to(voice_channel)
                    except Exception:
                        return await self.send_reply(interaction, t(interaction, "VOICE_CONNECT_FAILED"), ephemeral=True)
                    message = t(interaction, "MOVED_CHANNEL")
                else:
                    return await self.send_reply(interaction, t(interaction, "MOVE_RESTRICTED"), ephemeral=True)
            else:
                message = t(interaction, "ALREADY_HERE")
                ephemeral = True
        else:
            try:
                vc = await voice_channel.connect()
            except TimeoutError:
                self._intentional_disconnect.add(interaction.guild.id)
                zombie = interaction.guild.voice_client
                if zombie:
                    try:
                        await zombie.disconnect(force=True)
                    except Exception:
                        pass
                try:
                    vc = await voice_channel.connect()
                except Exception:
                    return await self.send_reply(interaction, t(interaction, "VOICE_CONNECT_FAILED"), ephemeral=True)
            message = t(interaction, "HELLO")

        state = self.get_state(interaction.guild.id)
        state.vc = vc
        state.text_channel = state.text_channel or interaction.channel
        if not state.playing:
            state.cancel_tasks()
            idle_timeout = self.guild_idle_disconnect.get(interaction.guild.id, 180)
            if idle_timeout > 0:
                state.idle_disconnect_task = self._create_task(self.playback.auto_disconnect_after(interaction.guild.id, idle_timeout), name=f"idle-dc-{interaction.guild.id}")

        return await self.send_reply(interaction, message, ephemeral=ephemeral)

    @app_commands.command(**l_cmd("CMD_NAME_LEAVE", "CMD_DESC_LEAVE"))
    async def leave(self, interaction: discord.Interaction):
        if not await self._check_cooldown(interaction, "leave", 5): return
        await interaction.response.defer()
        if not self.has_control_privilege(interaction.guild.id, interaction.user) and not self.has_admin_privilege(interaction.guild.id, interaction.user):
            return await self.send_reply(interaction, t(interaction, "NOT_DJ_OR_ADMIN"), ephemeral=True)
        if not interaction.guild.voice_client:
            return await self.send_reply(interaction, t(interaction, "NOT_CONNECTED"), ephemeral=True)

        guild_id = interaction.guild.id
        self._cancel_bg_fetch(guild_id)
        session = self.radio_sessions.pop(guild_id, None)
        if session:
            _deactivate_radio_session(session)
        state = self.guild_states.get(guild_id)
        if state:
            vc = interaction.guild.voice_client
            if vc and (vc.is_playing() or vc.is_paused()):
                state.suppress_after_callback = True
            state.cancel_tasks()
        self._intentional_disconnect.add(guild_id)
        try:
            await interaction.guild.voice_client.disconnect(force=True)
        except Exception:
            pass
        self.guild_states.pop(guild_id, None)
        self.playback.cleanup_guild_votes(guild_id)
        self.playback._play_locks.pop(guild_id, None)
        self._schedule_refresh(guild_id)
        await self.send_reply(interaction, t(interaction, "GOODBYE"))

    @app_commands.command(**l_cmd("CMD_NAME_LOOP", "CMD_DESC_LOOP"))
    @app_commands.rename(mode=l_opt("OPTNAME_LOOP_MODE"))
    @app_commands.describe(mode=l_opt("OPT_LOOP_MODE"))
    @app_commands.choices(mode=[
        localized_choice("off", "LOOP_OFF"),
        localized_choice("song", "LOOP_SONG"),
        localized_choice("queue", "LOOP_QUEUE"),
    ])
    async def loop_cmd(self, interaction: discord.Interaction, mode: str):
        if not await self._check_cooldown(interaction, "mp:loop", 1, per_guild=True): return
        if not await self._check_cooldown(interaction, "mp:loop", 5): return
        await interaction.response.defer()
        if self.is_radio_active(interaction.guild_id):
            return await self.send_reply(interaction, t(interaction, "RADIO_CMD_RESTRICTED", cmd_stop=t(interaction, "CMD_NAME_STOP")), ephemeral=True)
        try:
            vc, state = self.ensure_voice_and_state(interaction, require_now=True, now_message=t(interaction, "NOTHING_PLAYING"))
        except CommandCheckError as e:
            return await respond_with_error(interaction, e)
        state.vc = state.vc or vc
        await _queue_loop(self, interaction, interaction.guild.id, mode)

    @app_commands.command(**l_cmd("CMD_NAME_MOVE", "CMD_DESC_MOVE"))
    @app_commands.rename(
        from_index=l_opt("OPTNAME_MOVE_FROM"),
        move_to=l_opt("OPTNAME_MOVE_DEST"),
        to_index=l_opt("OPTNAME_MOVE_TO"),
    )
    @app_commands.describe(
        from_index=l_opt("OPT_MOVE_FROM"),
        move_to=l_opt("OPT_MOVE_DEST"),
        to_index=l_opt("OPT_MOVE_TO"),
    )
    async def move_cmd(self, interaction: discord.Interaction, from_index: int, move_to: int, to_index: int | None = None):
        if not await self._check_cooldown(interaction, "mp:move", 1, per_guild=True): return
        if not await self._check_cooldown(interaction, "mp:move", 5): return
        if self.is_radio_active(interaction.guild_id):
            return await self.send_reply(interaction, t(interaction, "RADIO_CMD_RESTRICTED", cmd_stop=t(interaction, "CMD_NAME_STOP")), ephemeral=True)
        await interaction.response.defer()
        try:
            state = self.ensure_queue_data(interaction, require_queue=True, queue_message=t(interaction, "QUEUE_EMPTY"))
        except CommandCheckError as e:
            return await respond_with_error(interaction, e)
        queue = state.queue
        snapshot = tuple(id(e) for e in queue)
        guild_id = interaction.guild.id
        if to_index is not None:
            await _queue_move_range(self, interaction, guild_id, from_index, to_index, move_to, queue, snapshot)
        else:
            await _queue_move_single(self, interaction, guild_id, from_index, move_to, queue, snapshot)

    async def _check_view_access(self, interaction, view_type):
        """Check view restriction for /nowplaying or /queue.

        view_type: "mp" or "queue".
        Returns (proceed, owner_confirmed).
        If proceed is False, the caller should return immediately.
        Handles deferring the interaction response internally.
        """
        guild_id = interaction.guild.id
        active_dict = self.active_mp if view_type == "mp" else self.active_queues
        confirm_label = "musicplayer" if view_type == "mp" else "queue"

        result = self.check_view_restriction(guild_id, interaction.channel_id, interaction.user)
        if result is not None:
            reason, ch_id = result
            if reason == "user_blocked":
                await interaction.response.defer(ephemeral=True)
                msg_text = t(interaction, "VIEW_USER_BLOCKED")
                active = active_dict.get(guild_id)
                if active:
                    msg_text += f"\n{active[0].jump_url}"
                await self.send_reply(interaction, msg_text, ephemeral=True)
                return False, False
            elif reason == "owner_wrong_channel":
                await interaction.response.defer(ephemeral=True)
                confirm_view = _OwnerChannelConfirmView(self, interaction, confirm_label)
                msg_text = t(interaction, "OWNER_CHANNEL_WARN", channel=f"<#{ch_id}>")
                msg = await interaction.followup.send(msg_text, view=confirm_view, ephemeral=True, wait=True)
                await confirm_view.wait()
                try:
                    await msg.delete()
                except Exception:
                    pass
                if not confirm_view.result:
                    return False, False
                return True, True
            else:
                await interaction.response.defer(ephemeral=True)
                msg_text = t(interaction, "VIEW_CHANNEL_RESTRICTED", channel=f"<#{ch_id}>")
                active = active_dict.get(guild_id)
                if active:
                    msg_text += f"\n{active[0].jump_url}"
                await self.send_reply(interaction, msg_text, ephemeral=True)
                return False, False
        await interaction.response.defer(ephemeral=True)
        self._owner_override_views.discard((guild_id, view_type))
        return True, False

    @app_commands.command(**l_cmd("CMD_NAME_NOWPLAYING", "CMD_DESC_NOW"))
    async def musicplayer_cmd(self, interaction: discord.Interaction):
        if not await self._check_cooldown(interaction, "mp", 10, per_guild=True): return
        guild_id = interaction.guild.id
        proceed, owner_confirmed = await self._check_view_access(interaction, "mp")
        if not proceed:
            return

        old = self.active_mp.pop(interaction.guild.id, None)
        if old:
            try:
                await old[0].delete()
            except discord.HTTPException:
                pass
        await self._delete_stale_view(guild_id, "mp", self.bot)

        state = self.guild_states.get(interaction.guild.id)
        if state and state.now:
            embed, file = self.build_mp_embed(interaction, state, attach_placeholder=True)
            view = self.MusicPlayerView(interaction, self)
        else:
            embed, file = self.build_idle_embed(interaction)
            view = self.MusicPlayerView(interaction, self, idle=True)

        kwargs = {"embed": embed, "view": view}
        if file:
            kwargs["file"] = file
        msg = await interaction.channel.send(**kwargs)
        if owner_confirmed:
            self._owner_override_views.add((guild_id, "mp"))
        if msg:
            msg = interaction.channel.get_partial_message(msg.id)
            self.active_mp[interaction.guild.id] = (msg, interaction, view)
            await self._save_view(guild_id, "mp", interaction.channel_id, msg.id)
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass

    class MusicPlayerView(discord.ui.View):
        def __init__(self, interaction, cog, *, idle=False):
            super().__init__(timeout=None)
            self.ctx = interaction
            self.cog = cog
            guild_id = interaction.guild.id
            compact = cog.is_compact(guild_id)
            layout = cog.get_mp_layout(guild_id)

            def _cl(key):
                e = _BUTTON_EMOJIS.get(key)
                l = None if compact else t(interaction, key)
                return e, l

            has_queue = bool(cog.guild_states.get(guild_id) and cog.guild_states[guild_id].queue)

            btn_map = {
                "toggle":    self.TogglePauseButton(interaction, cog, *_cl("BUTTON_TOGGLE")),
                "prev":      self.PreviousButton(interaction, cog, *_cl("BUTTON_PREV_TRACK")),
                "skip":      self.SkipButton(interaction, cog, *_cl("BUTTON_SKIP")),
                "stop":      self.StopButton(interaction, cog, *_cl("BUTTON_STOP")),
                "add_track": self.AddTrackButton(interaction, cog, *_cl("BUTTON_ADD_TRACK")),
                "remove":    self.RemoveButton(interaction, cog, *_cl("BUTTON_REMOVE"), disabled=idle or not has_queue),
                "move":      self.MoveButton(interaction, cog, *_cl("BUTTON_MOVE"), disabled=idle or not has_queue),
                "select":    self.SelectButton(interaction, cog, *_cl("BUTTON_SELECT"), disabled=idle or not has_queue),
                "loop":      self.LoopButton(interaction, cog, *_cl("BUTTON_LOOP"), disabled=idle),
                "shuffle":   self.ShuffleButton(interaction, cog, *_cl("BUTTON_SHUFFLE"), disabled=idle or not has_queue),
                "clear":     self.ClearQueueButton(interaction, cog, *_cl("BUTTON_CLEAR_QUEUE"), disabled=idle or not has_queue),
                "seek":      self.SeekButton(interaction, cog, *_cl("BUTTON_SEEK"), disabled=idle),
                "refresh":   self.RefreshMPButton(interaction, self, cog, *_cl("BUTTON_REFRESH")),
            }
            if idle:
                for k in ("toggle", "prev", "skip", "stop"):
                    btn_map[k].disabled = True

            if not idle and cog.is_radio_active(guild_id):
                for k in ("add_track", "remove", "move", "select", "loop", "shuffle", "clear", "prev"):
                    if k in btn_map:
                        btn_map[k].disabled = True

            items = []
            for key, cfg in layout.items():
                if not cfg.get("enabled", True) or key not in btn_map:
                    continue
                btn = btn_map[key]
                btn.row = cfg["row"]
                items.append((cfg["row"], cfg["col"], btn))
            items.sort(key=lambda x: (x[0], x[1]))
            for _, _, btn in items:
                self.add_item(btn)

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            return await self.cog.check_view_interaction(interaction)

        class RefreshMPButton(discord.ui.Button):
            def __init__(self, interaction, view_ref, cog, emoji, label):
                super().__init__(style=discord.ButtonStyle.success, emoji=emoji, label=label, row=2, custom_id="mp:refresh")
                self.ctx = interaction
                self.view_ref = view_ref
                self.cog = cog

            async def callback(self, interaction: discord.Interaction):
                if not await self.cog._check_cooldown(interaction, "mp:refresh", 1, per_guild=True): return
                if not await self.cog._check_cooldown(interaction, "mp:refresh", 5): return
                guild_id = interaction.guild.id
                state = self.cog.guild_states.get(guild_id)
                await interaction.response.defer()

                if not state or not state.now:
                    embed, file = self.cog.build_idle_embed(interaction)
                    view = self.cog.MusicPlayerView(self.ctx, self.cog, idle=True)
                else:
                    embed, file = self.cog.build_mp_embed(interaction, state, attach_placeholder=True)
                    view = self.cog.MusicPlayerView(self.ctx, self.cog)

                msg = await interaction.edit_original_response(
                    embed=embed,
                    view=view,
                    attachments=[file] if file else []
                )
                old_entry = self.cog.active_mp.get(guild_id)
                if old_entry:
                    old_view = old_entry[2]
                    if old_view is not self.view_ref:
                        old_view.stop()
                partial = interaction.channel.get_partial_message(msg.id)
                self.cog.active_mp[guild_id] = (partial, self.ctx, view)

        class TogglePauseButton(discord.ui.Button):
            def __init__(self, interaction, cog, emoji, label):
                super().__init__(style=discord.ButtonStyle.secondary, emoji=emoji, label=label, row=0, custom_id="mp:toggle")
                self.ctx = interaction
                self.cog = cog

            async def callback(self, interaction: discord.Interaction):
                if not await self.cog._check_cooldown(interaction, "mp:toggle", 1, per_guild=True): return
                if not await self.cog._check_cooldown(interaction, "mp:toggle", 5): return
                vc = interaction.guild.voice_client
                state = self.cog.guild_states.get(interaction.guild.id)
                current = state.now if state else None

                if vc and vc.channel:
                    _uc = interaction.user.voice.channel if interaction.user.voice else None
                    if _uc != vc.channel and not self.cog.has_control_privilege(interaction.guild.id, interaction.user) and not self.cog.has_admin_privilege(interaction.guild.id, interaction.user):
                        return await self.cog.send_reply(interaction, t(interaction, "SAME_CHANNEL_REQUIRED"), ephemeral=True)
                if not vc or not current:
                    await interaction.response.defer()
                    embed, file = self.cog.build_idle_embed(interaction)
                    view = self.cog.MusicPlayerView(self.ctx, self.cog, idle=True)
                    try:
                        await interaction.message.edit(embed=embed, view=view, attachments=[file] if file else [])
                    except discord.HTTPException:
                        pass
                    return

                if not self.cog.has_pause_permission(interaction.guild.id, interaction.user, current.get("requester")):
                    return await self.cog.send_reply(interaction, t(interaction, "PAUSE_NO_PERMISSION"), ephemeral=True)

                perm = self.cog.guild_pause_permission.get(interaction.guild.id, "requester_dj")
                is_dj = self.cog.has_control_privilege(interaction.guild.id, interaction.user)
                is_admin = self.cog.has_admin_privilege(interaction.guild.id, interaction.user)
                is_owner = interaction.user.id == current.get("requester")

                if current and current.get("is_live"):
                    return await self.cog.send_reply(interaction, t(interaction, "LIVE_NO_PAUSE"), ephemeral=True)

                if vc.is_paused():
                    await _do_resume(self.cog, interaction, state, vc)
                elif vc.is_playing():
                    if perm == "everyone" and not (is_dj or is_admin or is_owner):
                        vote_passed = await self.cog.handle_vote(
                            interaction, "pause", interaction.guild.id,
                            success_message=t(interaction, "PAUSED"),
                            vote_message=t(interaction, "PAUSE_VOTE"),
                        )
                        if not vote_passed:
                            self.cog._schedule_refresh(interaction.guild.id)
                            return
                        vc.pause()
                    else:
                        vc.pause()
                        await self.cog.send_reply(interaction, t(interaction, "PAUSED"))
                    state.paused_at = time.time()
                    state.cancel_tasks()
                    pause_timeout = self.cog.guild_pause_timeout.get(interaction.guild.id, 900)
                    state.pause_disconnect_task = self.cog._create_task(self.cog.playback.handle_pause_timeout(interaction.guild.id, pause_timeout), name=f"pause-dc-{interaction.guild.id}")
                    self.cog._schedule_refresh(interaction.guild.id)
                else:
                    await self.cog.send_reply(interaction, t(interaction, "NOTHING_PLAYING_LABEL"), ephemeral=True)

        class SkipButton(discord.ui.Button):
            def __init__(self, interaction, cog, emoji, label):
                super().__init__(style=discord.ButtonStyle.secondary, emoji=emoji, label=label, row=0, custom_id="mp:skip")
                self.ctx = interaction
                self.cog = cog

            async def callback(self, interaction: discord.Interaction):
                if not await self.cog._check_cooldown(interaction, "mp:skip", 1, per_guild=True): return
                if not await self.cog._check_cooldown(interaction, "mp:skip", 5): return
                vc = interaction.guild.voice_client
                if vc and vc.channel:
                    _uc = interaction.user.voice.channel if interaction.user.voice else None
                    if _uc != vc.channel and not self.cog.has_control_privilege(interaction.guild.id, interaction.user) and not self.cog.has_admin_privilege(interaction.guild.id, interaction.user):
                        return await self.cog.send_reply(interaction, t(interaction, "SAME_CHANNEL_REQUIRED"), ephemeral=True)
                await interaction.response.defer()
                await self.cog.handle_skip(interaction)

        class StopButton(discord.ui.Button):
            def __init__(self, interaction, cog, emoji, label):
                super().__init__(style=discord.ButtonStyle.secondary, emoji=emoji, label=label, row=0, custom_id="mp:stop")
                self.ctx = interaction
                self.cog = cog

            async def callback(self, interaction: discord.Interaction):
                if not await self.cog._check_cooldown(interaction, "mp:stop", 1, per_guild=True): return
                if not await self.cog._check_cooldown(interaction, "mp:stop", 5): return
                vc = interaction.guild.voice_client
                if not vc:
                    return await self.cog.send_reply(interaction, t(interaction, "BOT_NOT_IN_VOICE"), ephemeral=True)
                _uc = interaction.user.voice.channel if interaction.user.voice else None
                if _uc != vc.channel and not self.cog.has_control_privilege(interaction.guild.id, interaction.user) and not self.cog.has_admin_privilege(interaction.guild.id, interaction.user):
                    return await self.cog.send_reply(interaction, t(interaction, "SAME_CHANNEL_REQUIRED"), ephemeral=True)
                await _do_stop_full(self.cog, interaction, interaction.guild.id, vc)

        class PreviousButton(discord.ui.Button):
            def __init__(self, interaction, cog, emoji, label):
                super().__init__(style=discord.ButtonStyle.secondary, emoji=emoji, label=label, row=0, custom_id="mp:prev")
                self.ctx = interaction
                self.cog = cog

            async def callback(self, interaction: discord.Interaction):
                if not await self.cog._check_cooldown(interaction, "mp:prev", 1, per_guild=True): return
                if not await self.cog._check_cooldown(interaction, "mp:prev", 5): return
                vc = interaction.guild.voice_client
                if vc and vc.channel:
                    _uc = interaction.user.voice.channel if interaction.user.voice else None
                    if _uc != vc.channel and not self.cog.has_control_privilege(interaction.guild.id, interaction.user) and not self.cog.has_admin_privilege(interaction.guild.id, interaction.user):
                        return await self.cog.send_reply(interaction, t(interaction, "SAME_CHANNEL_REQUIRED"), ephemeral=True)
                if self.cog.is_radio_active(interaction.guild.id):
                    return await self.cog.send_reply(interaction, t(interaction, "RADIO_CMD_RESTRICTED", cmd_stop=t(interaction, "CMD_NAME_STOP")), ephemeral=True)
                await interaction.response.defer()
                await self.cog.handle_previous(interaction)

        class AddTrackButton(discord.ui.Button):
            def __init__(self, interaction, cog, emoji, label, *, disabled=False):
                super().__init__(style=discord.ButtonStyle.secondary, emoji=emoji, label=label, row=1, disabled=disabled, custom_id="mp:addtrack")
                self.ctx = interaction
                self.cog = cog

            async def callback(self, interaction: discord.Interaction):
                if not await self.cog._check_cooldown(interaction, "mp:addtrack", 1, per_guild=True): return
                if not await self.cog._check_cooldown(interaction, "mp:addtrack", 5): return
                cog = self.cog
                guild_id = interaction.guild.id
                vc = interaction.guild.voice_client
                if vc and vc.channel:
                    _uc = interaction.user.voice.channel if interaction.user.voice else None
                    if _uc != vc.channel and not cog.has_control_privilege(guild_id, interaction.user) and not cog.has_admin_privilege(guild_id, interaction.user):
                        return await cog.send_reply(interaction, t(interaction, "SAME_CHANNEL_REQUIRED"), ephemeral=True)
                elif not interaction.user.voice or not interaction.user.voice.channel:
                    return await cog.send_reply(interaction, t(interaction, "JOIN_VOICE_FIRST"), ephemeral=True)
                await interaction.response.send_modal(self._AddTrackModal(interaction, cog, guild_id))

            class _AddTrackModal(discord.ui.Modal):
                def __init__(self, ctx, cog, guild_id):
                    super().__init__(title=t(ctx, "MP_ADD_TRACK_TITLE"))
                    self.cog = cog
                    self.guild_id = guild_id
                    self.query_input = discord.ui.TextInput(
                        label=t(ctx, "PL_ADD_TRACK_LABEL"),
                        placeholder=t(ctx, "OPT_PLAY_QUERY"),
                        max_length=4000,
                        required=True,
                    )
                    self.shuffle_lbl = discord.ui.Label(
                        text=t(ctx, "SHUFFLE_LABEL"),
                        component=discord.ui.Checkbox(custom_id="shuffle"),
                    )
                    self.add_item(self.query_input)
                    self.add_item(self.shuffle_lbl)
                    self._show_forced = False
                    can_force = cog.has_force_play_privilege(guild_id, ctx.user)
                    radio_active = cog.is_radio_active(guild_id)
                    if can_force and (not radio_active or cog.guild_force_radio.get(guild_id, "disabled") == "enabled"):
                        self._show_forced = True
                        self.forced_lbl = discord.ui.Label(
                            text=t(ctx, "FORCE_PLAY_LABEL"),
                            component=discord.ui.Checkbox(custom_id="forced"),
                        )
                        self.add_item(self.forced_lbl)

                async def on_submit(self, modal_interaction: discord.Interaction):
                    cog = self.cog
                    query = self.query_input.value.strip()
                    if not query:
                        return await cog.send_reply(modal_interaction, t(modal_interaction, "NO_PLAYABLE"), ephemeral=True)
                    await modal_interaction.response.defer()

                    _gid = self.guild_id
                    _is_forced = self._show_forced and bool(self.forced_lbl.component.value)
                    if _is_forced:
                        cog._cancel_bg_fetch(_gid, forced_only=True)
                        await cog._execute_play(
                            modal_interaction, query,
                            shuffle=bool(self.shuffle_lbl.component.value),
                            forced=True,
                            respond_fn=lambda content, **kw: cog.send_reply(modal_interaction, content, **kw),
                        )
                    else:
                        play_lock = cog._command_locks.setdefault(_gid, asyncio.Lock())
                        _wm = None
                        if play_lock.locked():
                            _wm = await cog.send_reply(modal_interaction, t(modal_interaction, "PLAY_WAIT"), delete_after=None)

                        async with play_lock:
                            async def _respond(content, **kw):
                                nonlocal _wm
                                if _wm:
                                    m = _wm
                                    _wm = None
                                    return await cog._edit_or_reply(modal_interaction, m, content, **kw)
                                return await cog.send_reply(modal_interaction, content, **kw)

                            await cog._execute_play(
                                modal_interaction, query,
                                shuffle=bool(self.shuffle_lbl.component.value),
                                forced=False,
                                respond_fn=_respond,
                            )

        class RemoveButton(discord.ui.Button):
            def __init__(self, interaction, cog, emoji, label, *, disabled=False):
                super().__init__(style=discord.ButtonStyle.secondary, emoji=emoji, label=label, row=1, disabled=disabled, custom_id="mp:remove")
                self.ctx = interaction
                self.cog = cog

            async def callback(self, interaction: discord.Interaction):
                if not await self.cog._check_cooldown(interaction, "mp:remove", 1, per_guild=True): return
                if not await self.cog._check_cooldown(interaction, "mp:remove", 5): return
                cog = self.cog
                guild_id = interaction.guild.id
                state = cog.guild_states.get(guild_id)
                queue = state.queue if state else []
                if not queue:
                    return await cog.send_reply(interaction, t(interaction, "QUEUE_EMPTY"), ephemeral=True)

                vc = interaction.guild.voice_client
                if vc and vc.channel:
                    _uc = interaction.user.voice.channel if interaction.user.voice else None
                    if _uc != vc.channel and not cog.has_control_privilege(guild_id, interaction.user) and not cog.has_admin_privilege(guild_id, interaction.user):
                        return await cog.send_reply(interaction, t(interaction, "SAME_CHANNEL_REQUIRED"), ephemeral=True)
                if cog.is_radio_active(guild_id):
                    return await cog.send_reply(interaction, t(interaction, "RADIO_CMD_RESTRICTED", cmd_stop=t(interaction, "CMD_NAME_STOP")), ephemeral=True)
                await interaction.response.send_modal(self._RemoveModal(interaction, cog, guild_id, len(queue)))

            class _RemoveModal(discord.ui.Modal):
                def __init__(self, ctx, cog, guild_id, queue_len):
                    super().__init__(title=t(ctx, "BUTTON_REMOVE"))
                    self.cog = cog
                    self.guild_id = guild_id
                    self.index_input = discord.ui.TextInput(
                        label=t(ctx, "OPT_REMOVE_INDEX"),
                        placeholder=f"1-{queue_len}",
                        max_length=5,
                        required=False,
                    )
                    self.index_to_input = discord.ui.TextInput(
                        label=t(ctx, "OPT_REMOVE_INDEXTO"),
                        placeholder=f"1-{queue_len}",
                        max_length=5,
                        required=False,
                    )
                    self.add_item(self.index_input)
                    self.add_item(self.index_to_input)
                    self.search_input = None
                    if cog.has_control_privilege(guild_id, ctx.user) or cog.has_admin_privilege(guild_id, ctx.user):
                        self.search_input = discord.ui.TextInput(
                            label=t(ctx, "REMOVE_SEARCH_LABEL"),
                            placeholder=t(ctx, "REMOVE_SEARCH_PLACEHOLDER"),
                            max_length=100,
                            required=False,
                        )
                        self.add_item(self.search_input)

                async def on_submit(self, interaction: discord.Interaction):
                    cog = self.cog
                    guild_id = self.guild_id
                    raw_index = self.index_input.value.strip()
                    raw_search = self.search_input.value.strip() if self.search_input and self.search_input.value else ""
                    if not raw_index and not raw_search:
                        return await interaction.response.send_message(t(interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                    await interaction.response.defer()
                    state = cog.guild_states.get(guild_id)
                    queue = state.queue if state else []
                    snapshot = tuple(id(e) for e in queue)
                    parts = []
                    if raw_index:
                        try:
                            index = int(raw_index)
                        except ValueError:
                            return await cog.send_reply(interaction, t(interaction, "INVALID_INPUT"), ephemeral=True)
                        if not (1 <= index <= len(queue)):
                            return await cog.send_reply(interaction, t(interaction, "SELECT_INVALID"), ephemeral=True)
                        index_to = None
                        if self.index_to_input:
                            raw_to = self.index_to_input.value.strip()
                            if raw_to:
                                try:
                                    index_to = int(raw_to)
                                except ValueError:
                                    return await cog.send_reply(interaction, t(interaction, "INVALID_INPUT"), ephemeral=True)
                                if index_to <= index:
                                    return await cog.send_reply(interaction, t(interaction, "REMOVE_ORDER",
                                        indexto=t(interaction, "OPTNAME_REMOVE_INDEXTO"), index=t(interaction, "OPTNAME_REMOVE_INDEX")), ephemeral=True)
                                if index_to > len(queue):
                                    return await cog.send_reply(interaction, t(interaction, "REMOVE_INVALID_RANGE",
                                        indexto=t(interaction, "OPTNAME_REMOVE_INDEXTO")), ephemeral=True)
                        idx_parts, ok = await _queue_remove_index(cog, interaction, guild_id, index, index_to, raw_search, queue, snapshot)
                        parts.extend(idx_parts)
                        if not ok and not raw_search:
                            return
                    if raw_search:
                        await _queue_remove_search(cog, interaction, guild_id, raw_search, queue, state, parts)
                    if parts:
                        await cog.send_reply(interaction, "\n".join(parts))
                        cog._schedule_refresh(guild_id)

        class MoveButton(discord.ui.Button):
            def __init__(self, interaction, cog, emoji, label, *, disabled=False):
                super().__init__(style=discord.ButtonStyle.secondary, emoji=emoji, label=label, row=1, disabled=disabled, custom_id="mp:move")
                self.ctx = interaction
                self.cog = cog

            async def callback(self, interaction: discord.Interaction):
                if not await self.cog._check_cooldown(interaction, "mp:move", 1, per_guild=True): return
                if not await self.cog._check_cooldown(interaction, "mp:move", 5): return
                cog = self.cog
                guild_id = interaction.guild.id
                state = cog.guild_states.get(guild_id)
                queue = state.queue if state else []
                if not queue:
                    return await cog.send_reply(interaction, t(interaction, "QUEUE_EMPTY"), ephemeral=True)

                vc = interaction.guild.voice_client
                if vc and vc.channel:
                    _uc = interaction.user.voice.channel if interaction.user.voice else None
                    if _uc != vc.channel and not cog.has_control_privilege(guild_id, interaction.user) and not cog.has_admin_privilege(guild_id, interaction.user):
                        return await cog.send_reply(interaction, t(interaction, "SAME_CHANNEL_REQUIRED"), ephemeral=True)
                if cog.is_radio_active(guild_id):
                    return await cog.send_reply(interaction, t(interaction, "RADIO_CMD_RESTRICTED", cmd_stop=t(interaction, "CMD_NAME_STOP")), ephemeral=True)
                await interaction.response.send_modal(self._MoveModal(interaction, cog, guild_id, len(queue)))

            class _MoveModal(discord.ui.Modal):
                def __init__(self, ctx, cog, guild_id, queue_len):
                    super().__init__(title=t(ctx, "BUTTON_MOVE"))
                    self.cog = cog
                    self.guild_id = guild_id
                    state = cog.guild_states.get(guild_id)
                    q = state.queue if state else []
                    self._queue_snapshot = tuple(id(e) for e in q)
                    self.from_input = discord.ui.TextInput(
                        label=t(ctx, "OPT_MOVE_FROM"),
                        placeholder=f"1-{queue_len}",
                        max_length=5,
                        required=True,
                    )
                    self.dest_input = discord.ui.TextInput(
                        label=t(ctx, "OPT_MOVE_DEST"),
                        placeholder=f"1-{queue_len}",
                        max_length=5,
                        required=True,
                    )
                    self.to_input = discord.ui.TextInput(
                        label=t(ctx, "OPT_MOVE_TO"),
                        placeholder=f"1-{queue_len}",
                        max_length=5,
                        required=False,
                    )
                    self.add_item(self.from_input)
                    self.add_item(self.dest_input)
                    self.add_item(self.to_input)

                async def on_submit(self, interaction: discord.Interaction):
                    cog = self.cog
                    state = cog.guild_states.get(self.guild_id)
                    queue = state.queue if state else []
                    if tuple(id(e) for e in queue) != self._queue_snapshot:
                        return await interaction.response.send_message(t(interaction, "QUEUE_STATE_CHANGED"), ephemeral=True, delete_after=cog._resolve_delete_after(self.guild_id))
                    try:
                        from_index = int(self.from_input.value)
                        dest = int(self.dest_input.value)
                    except ValueError:
                        return await interaction.response.send_message(t(interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(self.guild_id))
                    to_raw = self.to_input.value.strip() if self.to_input.value else ""
                    to_index = None
                    if to_raw:
                        try:
                            to_index = int(to_raw)
                        except ValueError:
                            return await interaction.response.send_message(t(interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(self.guild_id))
                    await interaction.response.defer()
                    if to_index is not None:
                        await _queue_move_range(cog, interaction, self.guild_id, from_index, to_index, dest, queue, self._queue_snapshot)
                    else:
                        await _queue_move_single(cog, interaction, self.guild_id, from_index, dest, queue, self._queue_snapshot)

        class SelectButton(discord.ui.Button):
            def __init__(self, interaction, cog, emoji, label, *, disabled=False):
                super().__init__(style=discord.ButtonStyle.secondary, emoji=emoji, label=label, row=1, disabled=disabled, custom_id="mp:select")
                self.ctx = interaction
                self.cog = cog

            async def callback(self, interaction: discord.Interaction):
                if not await self.cog._check_cooldown(interaction, "mp:select", 1, per_guild=True): return
                if not await self.cog._check_cooldown(interaction, "mp:select", 5): return
                cog = self.cog
                guild_id = interaction.guild.id
                state = cog.guild_states.get(guild_id)
                queue = state.queue if state else []
                if not queue:
                    return await cog.send_reply(interaction, t(interaction, "QUEUE_EMPTY"), ephemeral=True)

                vc = interaction.guild.voice_client
                if vc and vc.channel:
                    _uc = interaction.user.voice.channel if interaction.user.voice else None
                    if _uc != vc.channel and not cog.has_control_privilege(guild_id, interaction.user) and not cog.has_admin_privilege(guild_id, interaction.user):
                        return await cog.send_reply(interaction, t(interaction, "SAME_CHANNEL_REQUIRED"), ephemeral=True)
                if cog.is_radio_active(guild_id):
                    return await cog.send_reply(interaction, t(interaction, "RADIO_CMD_RESTRICTED", cmd_stop=t(interaction, "CMD_NAME_STOP")), ephemeral=True)
                await interaction.response.send_modal(self._SelectModal(interaction, cog, guild_id, len(queue)))

            class _SelectModal(discord.ui.Modal):
                def __init__(self, ctx, cog, guild_id, queue_len):
                    super().__init__(title=t(ctx, "QUEUE_SELECT_TITLE"))
                    self.cog = cog
                    self.guild_id = guild_id
                    state = cog.guild_states.get(guild_id)
                    q = state.queue if state else []
                    self._queue_snapshot = tuple(id(e) for e in q)
                    self.track_input = discord.ui.TextInput(
                        label=t(ctx, "QUEUE_SELECT_LABEL"),
                        placeholder=f"1-{queue_len}",
                        max_length=5,
                        required=True,
                    )
                    self.add_item(self.track_input)

                async def on_submit(self, interaction: discord.Interaction):
                    try:
                        index = int(self.track_input.value)
                    except ValueError:
                        return await interaction.response.send_message(t(interaction, "INVALID_INPUT"), ephemeral=True, delete_after=self.cog._resolve_delete_after(self.guild_id))
                    await interaction.response.defer()
                    await _queue_select(self.cog, interaction, self.guild_id, index, self._queue_snapshot)

        class LoopButton(discord.ui.Button):
            def __init__(self, interaction, cog, emoji, label, *, disabled=False):
                super().__init__(style=discord.ButtonStyle.secondary, emoji=emoji, label=label, row=2, disabled=disabled, custom_id="mp:loop")
                self.ctx = interaction
                self.cog = cog

            async def callback(self, interaction: discord.Interaction):
                if not await self.cog._check_cooldown(interaction, "mp:loop", 1, per_guild=True): return
                if not await self.cog._check_cooldown(interaction, "mp:loop", 5): return
                cog = self.cog
                guild_id = interaction.guild.id
                state = cog.guild_states.get(guild_id)
                if not state or not state.now:
                    return await cog.send_reply(interaction, t(interaction, "NOTHING_PLAYING"), ephemeral=True)
                vc = interaction.guild.voice_client
                if vc and vc.channel:
                    _uc = interaction.user.voice.channel if interaction.user.voice else None
                    if _uc != vc.channel and not cog.has_control_privilege(guild_id, interaction.user) and not cog.has_admin_privilege(guild_id, interaction.user):
                        return await cog.send_reply(interaction, t(interaction, "SAME_CHANNEL_REQUIRED"), ephemeral=True)
                if cog.is_radio_active(guild_id):
                    return await cog.send_reply(interaction, t(interaction, "RADIO_CMD_RESTRICTED", cmd_stop=t(interaction, "CMD_NAME_STOP")), ephemeral=True)
                await interaction.response.send_modal(self._LoopModal(interaction, cog, guild_id, state.loop_mode))

            class _LoopModal(discord.ui.Modal):
                def __init__(self, ctx, cog, guild_id, current_mode):
                    super().__init__(title=t(ctx, "LOOP_MODAL_TITLE"))
                    self.cog = cog
                    self.guild_id = guild_id
                    modes = [
                        ("off", t(ctx, "LOOP_OFF")),
                        ("song", t(ctx, "LOOP_SONG")),
                        ("queue", t(ctx, "LOOP_QUEUE")),
                    ]
                    self._mode_labels = dict(modes)
                    options = [
                        discord.SelectOption(label=label, value=value, default=(value == current_mode))
                        for value, label in modes
                    ]
                    self.mode_select = discord.ui.Label(
                        text=t(ctx, "LOOP_MODE_LABEL"),
                        component=discord.ui.Select(
                            custom_id="loop_mode",
                            options=options,
                        ),
                    )
                    self.add_item(self.mode_select)

                async def on_submit(self, modal_interaction: discord.Interaction):
                    mode = self.mode_select.component.values[0]
                    await _queue_loop(self.cog, modal_interaction, self.guild_id, mode)

        class ShuffleButton(discord.ui.Button):
            def __init__(self, interaction, cog, emoji, label, *, disabled=False):
                super().__init__(style=discord.ButtonStyle.secondary, emoji=emoji, label=label, row=2, disabled=disabled, custom_id="mp:shuffle")
                self.ctx = interaction
                self.cog = cog

            async def callback(self, interaction: discord.Interaction):
                if not await self.cog._check_cooldown(interaction, "mp:shuffle", 1, per_guild=True): return
                if not await self.cog._check_cooldown(interaction, "mp:shuffle", 5): return
                cog = self.cog
                guild_id = interaction.guild.id
                state = cog.guild_states.get(guild_id)
                if not state or len(state.queue) < 2:
                    return await cog.send_reply(interaction, t(interaction, "SHUFFLE_NEED_TWO"), ephemeral=True)
                vc = interaction.guild.voice_client
                if vc and vc.channel:
                    _uc = interaction.user.voice.channel if interaction.user.voice else None
                    if _uc != vc.channel and not cog.has_control_privilege(guild_id, interaction.user) and not cog.has_admin_privilege(guild_id, interaction.user):
                        return await cog.send_reply(interaction, t(interaction, "SAME_CHANNEL_REQUIRED"), ephemeral=True)
                if cog.is_radio_active(guild_id):
                    return await cog.send_reply(interaction, t(interaction, "RADIO_CMD_RESTRICTED", cmd_stop=t(interaction, "CMD_NAME_STOP")), ephemeral=True)
                await interaction.response.send_modal(self._ShuffleModal(interaction, cog, guild_id, len(state.queue)))

            class _ShuffleModal(discord.ui.Modal):
                def __init__(self, ctx, cog, guild_id, queue_len):
                    super().__init__(title=t(ctx, "BUTTON_SHUFFLE"))
                    self.cog = cog
                    self.guild_id = guild_id
                    state = cog.guild_states.get(guild_id)
                    q = state.queue if state else []
                    self._queue_snapshot = tuple(id(e) for e in q)
                    self.from_input = discord.ui.TextInput(
                        label=t(ctx, "OPT_SHUFFLE_FROM"),
                        placeholder=f"1-{queue_len}",
                        max_length=5,
                        required=False,
                    )
                    self.to_input = discord.ui.TextInput(
                        label=t(ctx, "OPT_SHUFFLE_TO"),
                        placeholder=f"1-{queue_len}",
                        max_length=5,
                        required=False,
                    )
                    self.add_item(self.from_input)
                    self.add_item(self.to_input)

                async def on_submit(self, interaction: discord.Interaction):
                    cog = self.cog
                    guild_id = self.guild_id
                    state = cog.guild_states.get(guild_id)
                    queue = state.queue if state else []
                    if len(queue) < 2:
                        return await cog.send_reply(interaction, t(interaction, "SHUFFLE_NEED_TWO"), ephemeral=True)
                    raw_from = self.from_input.value.strip() if self.from_input.value else ""
                    raw_to = self.to_input.value.strip() if self.to_input.value else ""
                    if raw_from or raw_to:
                        try:
                            from_idx = int(raw_from) if raw_from else 1
                        except ValueError:
                            return await cog.send_reply(interaction, t(interaction, "INVALID_INPUT"), ephemeral=True)
                        try:
                            to_idx = int(raw_to) if raw_to else len(queue)
                        except ValueError:
                            return await cog.send_reply(interaction, t(interaction, "INVALID_INPUT"), ephemeral=True)
                        if from_idx < 1 or to_idx > len(queue) or to_idx <= from_idx:
                            return await cog.send_reply(interaction, t(interaction, "SHUFFLE_RANGE_INVALID"), ephemeral=True)
                        await _queue_shuffle(cog, interaction, guild_id, from_idx, to_idx, self._queue_snapshot)
                    else:
                        await _queue_shuffle(cog, interaction, guild_id, None, None, self._queue_snapshot)

        class ClearQueueButton(discord.ui.Button):
            def __init__(self, interaction, cog, emoji, label, *, disabled=False):
                super().__init__(style=discord.ButtonStyle.secondary, emoji=emoji, label=label, row=2, disabled=disabled, custom_id="mp:clear")
                self.ctx = interaction
                self.cog = cog

            async def callback(self, interaction: discord.Interaction):
                if not await self.cog._check_cooldown(interaction, "mp:clear", 1, per_guild=True): return
                if not await self.cog._check_cooldown(interaction, "mp:clear", 5): return
                cog = self.cog
                guild_id = interaction.guild.id
                state = cog.guild_states.get(guild_id)
                if not state or not state.queue:
                    return await cog.send_reply(interaction, t(interaction, "QUEUE_EMPTY"), ephemeral=True)
                vc = interaction.guild.voice_client
                if vc and vc.channel:
                    _uc = interaction.user.voice.channel if interaction.user.voice else None
                    if _uc != vc.channel and not cog.has_control_privilege(guild_id, interaction.user) and not cog.has_admin_privilege(guild_id, interaction.user):
                        return await cog.send_reply(interaction, t(interaction, "SAME_CHANNEL_REQUIRED"), ephemeral=True)
                if cog.is_radio_active(guild_id):
                    return await cog.send_reply(interaction, t(interaction, "RADIO_CMD_RESTRICTED", cmd_stop=t(interaction, "CMD_NAME_STOP")), ephemeral=True)
                await interaction.response.send_modal(self._ClearQueueModal(interaction, cog, guild_id))

            class _ClearQueueModal(discord.ui.Modal):
                def __init__(self, ctx, cog, guild_id):
                    super().__init__(title=t(ctx, "CLEAR_MODAL_TITLE"))
                    self.cog = cog
                    self.guild_id = guild_id
                    is_privileged = cog.has_control_privilege(guild_id, ctx.user) or cog.has_admin_privilege(guild_id, ctx.user)
                    radio_options = [
                        discord.RadioGroupOption(label=t(ctx, "CLEAR_ALL_OPTION"), value="all", default=True),
                        discord.RadioGroupOption(label=t(ctx, "CLEAR_MINE_OPTION"), value="mine"),
                    ]
                    if is_privileged:
                        radio_options.append(
                            discord.RadioGroupOption(label=t(ctx, "CLEAR_USER_OPTION"), value="user"),
                        )
                    self.scope_radio = discord.ui.Label(
                        text=t(ctx, "CLEAR_SCOPE_LABEL"),
                        component=discord.ui.RadioGroup(
                            custom_id="clear_scope",
                            options=radio_options,
                        ),
                    )
                    self.add_item(self.scope_radio)
                    self.user_select = None
                    if is_privileged:
                        self.user_select = discord.ui.Label(
                            text=t(ctx, "CLEAR_USER_LABEL"),
                            component=discord.ui.UserSelect(
                                custom_id="clear_user",
                                min_values=1, max_values=1,
                            ),
                        )
                        self.add_item(self.user_select)

                async def on_submit(self, modal_interaction: discord.Interaction):
                    cog = self.cog
                    guild_id = self.guild_id
                    state = cog.guild_states.get(guild_id)
                    if not state or not state.queue:
                        return await cog.send_reply(modal_interaction, t(modal_interaction, "QUEUE_EMPTY"), ephemeral=True)
                    scope = self.scope_radio.component.value or "all"
                    if scope == "mine":
                        msg = await _queue_clear_by_user(cog, modal_interaction, guild_id, state, modal_interaction.user.id)
                        if msg:
                            await cog.send_reply(modal_interaction, msg)
                    elif scope == "user" and self.user_select:
                        selected = self.user_select.component.values
                        if not selected:
                            return await cog.send_reply(modal_interaction, t(modal_interaction, "INVALID_INPUT"), ephemeral=True)
                        target = selected[0]
                        target_id = target.id if hasattr(target, 'id') else int(target)
                        target_name = target.display_name if hasattr(target, 'display_name') else str(target)
                        msg = await _queue_clear_by_user(cog, modal_interaction, guild_id, state, target_id, target_name)
                        if msg:
                            await cog.send_reply(modal_interaction, msg)
                    else:
                        await _queue_clear_all(cog, modal_interaction, guild_id, state)

        class SeekButton(discord.ui.Button):
            def __init__(self, interaction, cog, emoji, label, *, disabled=False):
                super().__init__(style=discord.ButtonStyle.secondary, emoji=emoji, label=label, row=2, disabled=disabled, custom_id="mp:seek")
                self.ctx = interaction
                self.cog = cog

            async def callback(self, interaction: discord.Interaction):
                if not await self.cog._check_cooldown(interaction, "mp:seek", 1, per_guild=True): return
                if not await self.cog._check_cooldown(interaction, "mp:seek", 5): return
                cog = self.cog
                guild_id = interaction.guild.id
                state = cog.guild_states.get(guild_id)
                current = state.now if state else None
                vc = interaction.guild.voice_client

                if not vc or not current or (not vc.is_playing() and not vc.is_paused()):
                    return await cog.send_reply(interaction, t(interaction, "NOTHING_PLAYING"), ephemeral=True)

                allowed, _is_dj, _is_admin = _check_seek_permission(cog, interaction, guild_id, current)
                if not allowed:
                    return await cog.send_reply(interaction, t(interaction, "SEEK_NO_PERMISSION"), ephemeral=True)

                _uc = interaction.user.voice.channel if interaction.user.voice else None
                if _uc != vc.channel and not _is_dj and not _is_admin:
                    return await cog.send_reply(interaction, t(interaction, "SAME_CHANNEL_REQUIRED"), ephemeral=True)
                await interaction.response.send_modal(self._SeekModal(interaction, cog, guild_id, _is_dj, _is_admin))

            class _SeekModal(discord.ui.Modal):
                def __init__(self, ctx, cog, guild_id, is_dj, is_admin):
                    super().__init__(title=f"{t(ctx, 'BUTTON_SEEK')} ({t(ctx, 'SEEK_EMPTY_HINT')})"[:45])
                    self.cog = cog
                    self.guild_id = guild_id
                    self.is_dj = is_dj
                    self.is_admin = is_admin
                    self.hours_input = discord.ui.TextInput(
                        label=t(ctx, "OPT_SEEK_HOURS"),
                        placeholder="0",
                        required=False,
                        max_length=3,
                    )
                    self.minutes_input = discord.ui.TextInput(
                        label=t(ctx, "OPT_SEEK_MINUTES"),
                        placeholder="0",
                        required=False,
                        max_length=3,
                    )
                    self.seconds_input = discord.ui.TextInput(
                        label=t(ctx, "OPT_SEEK_SECONDS"),
                        placeholder="0",
                        required=False,
                        max_length=5,
                    )
                    self.add_item(self.hours_input)
                    self.add_item(self.minutes_input)
                    self.add_item(self.seconds_input)

                async def on_submit(self, interaction: discord.Interaction):
                    cog = self.cog
                    state = cog.guild_states.get(self.guild_id)
                    current = state.now if state else None
                    vc = interaction.guild.voice_client

                    if not vc or not current or (not vc.is_playing() and not vc.is_paused()):
                        return await interaction.response.send_message(
                            t(interaction, "NOTHING_PLAYING"), ephemeral=True, delete_after=self.cog._resolve_delete_after(self.guild_id))

                    if not current.get("url"):
                        return await interaction.response.send_message(
                            t(interaction, "SEEK_NO_VALID"), ephemeral=True, delete_after=self.cog._resolve_delete_after(self.guild_id))

                    try:
                        h = int(self.hours_input.value or 0)
                        m = int(self.minutes_input.value or 0)
                        s = int(self.seconds_input.value or 0)
                    except ValueError:
                        return await interaction.response.send_message(
                            t(interaction, "SEEK_NO_VALID"), ephemeral=True, delete_after=self.cog._resolve_delete_after(self.guild_id))

                    if self.is_admin:
                        max_seeks = 0
                    elif self.is_dj:
                        max_seeks = cog.guild_max_seeks_dj.get(self.guild_id, 0)
                    else:
                        max_seeks = cog.guild_max_seeks_per_track.get(self.guild_id, 3)
                    if max_seeks > 0:
                        counts = current.setdefault("_seek_counts", {})
                        user_count = counts.get(interaction.user.id, 0)
                        if user_count >= max_seeks:
                            return await interaction.response.send_message(
                                t(interaction, "SEEK_LIMIT_REACHED"), ephemeral=True, delete_after=self.cog._resolve_delete_after(self.guild_id))

                    success = await _apply_seek(cog, interaction, state, vc, current, h, m, s)

                    if success and max_seeks > 0:
                        counts = current.setdefault("_seek_counts", {})
                        counts[interaction.user.id] = counts.get(interaction.user.id, 0) + 1

    @app_commands.command(**l_cmd("CMD_NAME_PAUSE", "CMD_DESC_PAUSE"))
    async def pause_cmd(self, interaction: discord.Interaction):
        if not await self._check_cooldown(interaction, "mp:toggle", 1, per_guild=True): return
        if not await self._check_cooldown(interaction, "mp:toggle", 5): return
        await interaction.response.defer()
        try:
            vc, state, _ = self.check_playback_permissions(interaction)
        except CommandCheckError as e:
            return await respond_with_error(interaction, e)

        if vc.is_paused():
            return await self.send_reply(interaction, t(interaction, "PAUSE_ALREADY"), ephemeral=True)
        if not vc.is_playing():
            return await self.send_reply(interaction, t(interaction, "PAUSE_NOTHING"), ephemeral=True)

        current = state.now if state else None
        if current and current.get("is_live"):
            return await self.send_reply(interaction, t(interaction, "LIVE_NO_PAUSE"), ephemeral=True)

        perm = self.guild_pause_permission.get(interaction.guild.id, "requester_dj")
        if perm == "everyone":
            vote_passed = await self.handle_vote(
                interaction, "pause", interaction.guild.id,
                success_message=t(interaction, "PAUSED"),
                vote_message=t(interaction, "PAUSE_VOTE"),
                is_owner=(current.get("requester") == interaction.user.id),
            )
            if not vote_passed:
                self._schedule_refresh(interaction.guild.id)
                return
        else:
            await self.send_reply(interaction, t(interaction, "PAUSED"))

        vc.pause()
        state.paused_at = time.time()
        state.cancel_tasks()
        pause_timeout = self.guild_pause_timeout.get(interaction.guild.id, 900)
        state.pause_disconnect_task = self._create_task(self.playback.handle_pause_timeout(interaction.guild.id, pause_timeout), name=f"pause-dc-{interaction.guild.id}")
        self._schedule_refresh(interaction.guild.id)

    @app_commands.command(**l_cmd("CMD_NAME_PLAY", "CMD_DESC_PLAY"))
    @app_commands.rename(
        query=l_opt("OPTNAME_PLAY_QUERY"),
        shuffle=l_opt("OPTNAME_PLAY_SHUFFLE"),
        forced=l_opt("OPTNAME_PLAY_FORCED"),
        seconds=l_opt("OPTNAME_SEEK_SECONDS"),
        minutes=l_opt("OPTNAME_SEEK_MINUTES"),
        hours=l_opt("OPTNAME_SEEK_HOURS"),
    )
    @app_commands.describe(
        query=l_opt("OPT_PLAY_QUERY"),
        shuffle=l_opt("OPT_PLAY_SHUFFLE"),
        forced=l_opt("OPT_PLAY_FORCED"),
        seconds=l_opt("OPT_SEEK_SECONDS"),
        minutes=l_opt("OPT_SEEK_MINUTES"),
        hours=l_opt("OPT_SEEK_HOURS"),
    )
    @app_commands.choices(
        shuffle=[discord.app_commands.Choice(name="🔀", value="on")],
        forced=[discord.app_commands.Choice(name="⏭️", value="on")],
    )
    async def play_cmd(self, interaction: discord.Interaction, query: str, shuffle: str | None = None, forced: str | None = None, seconds: int = 0, minutes: int = 0, hours: int = 0):
        if not await self._check_cooldown(interaction, "play", 1, per_guild=True): return
        if not await self._check_cooldown(interaction, "play", 5): return
        await interaction.response.defer()

        is_forced = forced is not None
        radio_active = self.is_radio_active(interaction.guild_id)

        if is_forced:
            if not self.has_force_play_privilege(interaction.guild_id, interaction.user):
                return await self.send_reply(interaction, t(interaction, "FORCE_PLAY_NO_PERM"), ephemeral=True)
            if radio_active and self.guild_force_radio.get(interaction.guild_id, "disabled") == "disabled":
                return await self.send_reply(interaction, t(interaction, "FORCE_RADIO_DISABLED"), ephemeral=True)
        elif radio_active:
            return await self.send_reply(interaction, t(interaction, "RADIO_CMD_RESTRICTED", cmd_stop=t(interaction, "CMD_NAME_STOP")), ephemeral=True)

        if not interaction.user.voice or not interaction.user.voice.channel:
            return await self.send_reply(interaction, t(interaction, "JOIN_VOICE_FIRST"), ephemeral=True)

        if not interaction.guild.voice_client and not self.check_join_restriction(
                interaction.guild.id, interaction.user.voice.channel.id, interaction.user):
            return await self.send_reply(interaction, t(interaction, "JOIN_RESTRICTED_CHANNEL"), ephemeral=True)

        guild_id = interaction.guild_id
        if is_forced:
            self._cancel_bg_fetch(guild_id, forced_only=True)
            return await self._execute_play(
                interaction, query,
                shuffle=shuffle is not None,
                forced=True,
                seek_time=seconds + minutes * 60 + hours * 3600,
                respond_fn=lambda content, **kw: self.send_reply(interaction, content, **kw),
            )

        play_lock = self._command_locks.setdefault(guild_id, asyncio.Lock())
        _wm = None
        if play_lock.locked():
            _wm = await self.send_reply(interaction, t(interaction, "PLAY_WAIT"), delete_after=None)

        async with play_lock:
            async def _respond(content, **kw):
                nonlocal _wm
                if _wm:
                    m = _wm
                    _wm = None
                    return await self._edit_or_reply(interaction, m, content, **kw)
                return await self.send_reply(interaction, content, **kw)

            return await self._execute_play(
                interaction, query,
                shuffle=shuffle is not None,
                forced=False,
                seek_time=seconds + minutes * 60 + hours * 3600,
                respond_fn=_respond,
            )

    async def _execute_play(self, interaction: discord.Interaction, query: str, *,
                            shuffle: bool = False, forced: bool = False,
                            seek_time: int = 0, respond_fn):
        guild_id = interaction.guild_id
        _silent = self.is_silent_log(guild_id)
        self._acquire_extract()
        try:
            entries, spotify_result, yt_has_more, skipped = await self._resolve_play_entries(
                interaction, query, silent=_silent)
        except SpotifyError as e:
            print(f"[Spotify error] {e}")
            return await respond_fn(t(interaction, "SPOTIFY_ERROR"))
        except StaleCookieError:
            key = "YTDLP_COOKIE_STALE" if has_valid_cookies() else "YTDLP_AGE_RESTRICTED"
            return await respond_fn(
                t(interaction, "VIDEO_AGE_RESTRICTED") + "\n\n" + t(interaction, key))
        except JSRuntimeError:
            return await respond_fn(t(interaction, "YTDLP_NO_JS_RUNTIME"))
        except Exception as e:
            return await respond_fn(
                t(interaction, "VIDEO_CANNOT_PLAY", reason=_clean_ytdlp_error(e)))
        finally:
            self._release_extract()

        skip_parts = []
        if skipped.get("deleted"):
            skip_parts.append(t(interaction, "SKIPPED_DELETED", count=skipped["deleted"]))
        if skipped.get("private"):
            skip_parts.append(t(interaction, "SKIPPED_PRIVATE", count=skipped["private"]))
        if skipped.get("members_only"):
            skip_parts.append(t(interaction, "SKIPPED_MEMBERS_ONLY", count=skipped["members_only"]))

        if not entries:
            if spotify_result is not None:
                return await respond_fn(t(interaction, "SPOTIFY_NO_MATCH"))
            if not yt_has_more:
                if skip_parts:
                    return await respond_fn("\n".join(skip_parts))
                return await respond_fn(t(interaction, "NO_PLAYABLE"))
            result = await self.handle_play(interaction, [])
            if result[0] == "connect_failed":
                return await respond_fn(t(interaction, "VOICE_CONNECT_FAILED"))
            if result[0] == "restricted":
                return await respond_fn(t(interaction, "JOIN_RESTRICTED_CHANNEL"))
            if result[0] == "live_blocked":
                return await respond_fn(t(interaction, "LIVE_BLOCKED"))
            if result[0] is None:
                if skip_parts:
                    return await respond_fn("\n".join(skip_parts))
                return await respond_fn(t(interaction, "NO_PLAYABLE"))
            state = self.get_state(guild_id)
            state.cancel_tasks()
            _loading_suffix = "\n" + t(interaction, "PLAYLIST_LOADING_REMAINING")
            skip_msg = "\n".join(skip_parts) + _loading_suffix if skip_parts else _loading_suffix
            reply = await respond_fn(skip_msg, delete_after=None)
            await self._start_remaining_fetch(interaction, query, None, yt_has_more,
                silent=_silent, reply_msg=reply, forced=forced, insert_at=0 if forced else None)
            if not forced:
                await self._await_bg_fetch(guild_id)
            return reply

        if shuffle and len(entries) > 1:
            random.shuffle(entries)

        radio_active = self.is_radio_active(guild_id)

        if forced and radio_active and len(entries) > 1:
            entries = entries[:1]
            await respond_fn(t(interaction, "FORCE_RADIO_SINGLE"))

        if forced and radio_active:
            entries[0]["_radio_forced"] = True

        if not seek_time and is_youtube_url(query):
            seek_time = extract_youtube_start_time(query) or 0
        if seek_time and not entries[0].get("is_live"):
            entries[0]["seek_time"] = seek_time

        skip_suffix = "\n" + "\n".join(skip_parts) if skip_parts else ""

        played, added = await self.handle_play(interaction, entries, forced=forced)
        if played == "restricted":
            return await respond_fn(t(interaction, "JOIN_RESTRICTED_CHANNEL"))
        if played == "user_limit":
            return await respond_fn(t(interaction, "USER_TRACK_LIMIT"))
        if played == "connect_failed":
            return await respond_fn(t(interaction, "VOICE_CONNECT_FAILED"))
        if played == "live_blocked":
            return await respond_fn(t(interaction, "LIVE_BLOCKED"))
        if played is None:
            _q_lim = self.guild_queue_limit.get(guild_id, MAX_QUEUE)
            return await respond_fn(t(interaction, "QUEUE_FULL", limit=_q_lim))
        if not played:
            self._schedule_refresh(guild_id)

        if spotify_result:
            _sp_has_more = (spotify_result.total is not None and spotify_result.total > len(spotify_result.tracks)) or \
                           (spotify_result.total is None and len(spotify_result.tracks) >= 100)
            has_remaining = bool(spotify_result.use_free_fallback or _sp_has_more)
        else:
            has_remaining = bool(yt_has_more)

        _loading_suffix = "\n" + t(interaction, "PLAYLIST_LOADING_REMAINING") if has_remaining else ""

        if played:
            state = self.get_state(guild_id)
            entry = state.now or entries[0]
            await respond_fn(t(interaction, "ANNOUNCE_NOW", title=entry.get("title") or t(interaction, "UNKNOWN"), uploader=entry.get("uploader") or t(interaction, "UNKNOWN")) + skip_suffix)
            if added > 1:
                reply = await interaction.channel.send(_format_queue_add_msg(interaction, entries[1:added], total=added - 1) + _loading_suffix, delete_after=self._resolve_delete_after(guild_id) if not has_remaining else None)
            elif has_remaining:
                reply = await interaction.channel.send(t(interaction, "PLAYLIST_LOADING_REMAINING"))
            else:
                reply = None
        else:
            if has_remaining:
                reply = await respond_fn(_format_queue_add_msg(interaction, entries, total=added) + skip_suffix + _loading_suffix, delete_after=None)
            else:
                reply = await respond_fn(_format_queue_add_msg(interaction, entries, total=added) + skip_suffix)

        if has_remaining:
            await self._start_remaining_fetch(interaction, query, spotify_result, yt_has_more,
                silent=_silent, reply_msg=reply, forced=forced, insert_at=added if forced else None)
            if not forced:
                await self._await_bg_fetch(guild_id)
        return reply

    async def _resolve_play_entries(self, ctx, query, *, silent=False):
        spotify_result = None
        yt_has_more = False
        skipped = {}

        if is_spotify_url(query):
            if not has_js_runtime():
                raise JSRuntimeError("No JS runtime available for Spotify playback")
            spotify_result = await get_spotify_first_batch(query)
            entries = list(spotify_result.tracks)
        else:
            raw, playlist_count = await extract_entries(
                query, silent=silent, playlistend=_YT_PLAYLIST_BATCH, return_count=True)
            if playlist_count is not None:
                yt_has_more = playlist_count > len(raw)
            else:
                yt_has_more = len(raw) >= _YT_PLAYLIST_BATCH
            raw = slice_from_video(raw, yt_playlist_start_id(query))
            for e in raw:
                if e and not e.get("title"):
                    fallback_url = e.get("url") or e.get("webpage_url") or ""
                    path = fallback_url.rstrip("/").rsplit("/", 1)[-1] if fallback_url else ""
                    e["title"] = path.replace("-", " ").replace("_", " ").strip() or fallback_url or t(ctx, "UNKNOWN")
            entries = []
            for e in raw:
                reason = unavailable_reason(e)
                if reason:
                    skipped[reason] = skipped.get(reason, 0) + 1
                elif is_playable_entry(e):
                    entries.append(e)
            for entry in entries:
                if not entry.get("uploader"):
                    entry["uploader"] = t(ctx, "UNKNOWN")

        return entries, spotify_result, yt_has_more, skipped

    async def _start_remaining_fetch(self, interaction, query, spotify_result, yt_has_more, *, silent=False, reply_msg=None, first_batch=None, forced=False, insert_at: int | None = None):
        guild_id = interaction.guild_id
        if spotify_result:
            await self._handle_spotify_remaining(interaction, spotify_result, reply_msg=reply_msg, first_batch=first_batch, forced=forced, insert_at=insert_at)
        elif yt_has_more:
            self._start_bg_fetch(guild_id, self._fetch_remaining_youtube(
                guild_id, query, _YT_PLAYLIST_BATCH + 1,
                interaction.user.id, interaction.channel,
                silent=silent, reply_msg=reply_msg,
                first_batch=first_batch, forced=forced, insert_at=insert_at,
            ), forced=forced)

    async def _handle_spotify_remaining(self, interaction: discord.Interaction, result, *, reply_msg=None, first_batch=None, forced=False, insert_at: int | None = None):
        guild_id = interaction.guild_id
        has_more = (result.total is not None and result.total > len(result.tracks)) or \
                   (result.total is None and len(result.tracks) >= 100)

        if result.use_free_fallback:
            initial_urls = {tr.get("spotify_url") for tr in result.tracks} - {None}
            self._start_bg_fetch(guild_id, self._fetch_remaining_spotapi(
                guild_id, result.entity_type, result.entity_id,
                interaction.user.id, interaction.channel, reply_msg=reply_msg,
                initial_urls=initial_urls,
                first_batch=first_batch, forced=forced, insert_at=insert_at,
            ), forced=forced)
        elif has_more and has_spotify_api():
            self._start_bg_fetch(guild_id, self._fetch_remaining_spotify(
                guild_id, result.entity_type, result.entity_id,
                len(result.tracks), interaction.user.id, interaction.channel, reply_msg=reply_msg,
                first_batch=first_batch, forced=forced, insert_at=insert_at,
            ), forced=forced)

    async def _send_views_to_channel(self, guild: discord.Guild, channel):
        """Send MP + queue views to a channel without a user interaction (for restrict_all auto-send)."""
        ctx = _GuildCtx(guild)

        # Clean up existing views
        for vtype, store in [("queue", self.active_queues), ("mp", self.active_mp)]:
            old = store.pop(guild.id, None)
            if old:
                try:
                    await old[0].delete()
                except discord.HTTPException:
                    pass
            await self._delete_stale_view(guild.id, vtype, self.bot)

        # Send queue view
        q_view, QueueViewCls = self._build_queue(ctx)
        q_msg = await channel.send(embed=q_view.get_embed(), view=q_view)
        if q_msg:
            partial = channel.get_partial_message(q_msg.id)
            self.active_queues[guild.id] = (partial, q_view, QueueViewCls)
            await self._save_view(guild.id, "queue", channel.id, q_msg.id)

        # Send MP view
        state = self.guild_states.get(guild.id)
        if state and state.now:
            embed, file = self.build_mp_embed(ctx, state, attach_placeholder=True)
            mp_view = self.MusicPlayerView(ctx, self)
        else:
            embed, file = self.build_idle_embed(ctx)
            mp_view = self.MusicPlayerView(ctx, self, idle=True)

        if file:
            mp_msg = await channel.send(embed=embed, view=mp_view, file=file)
        else:
            mp_msg = await channel.send(embed=embed, view=mp_view)
        if mp_msg:
            partial = channel.get_partial_message(mp_msg.id)
            self.active_mp[guild.id] = (partial, ctx, mp_view)
            await self._save_view(guild.id, "mp", channel.id, mp_msg.id)

    def _build_queue(self, interaction):
        """Build a QueueView for the given context (Interaction or _GuildCtx)."""
        guild_id = interaction.guild.id if hasattr(interaction, 'guild') and interaction.guild else interaction.guild_id
        state = self.guild_states.get(guild_id)
        queue_list = state.queue if state else []

        class QueueView(discord.ui.View):
            def __init__(self, queue, guild_states, cog):
                super().__init__(timeout=None)
                self.guild_id = interaction.guild.id
                self.guild_states = guild_states
                self.cog = cog
                self.compact = cog.is_queue_button_compact(self.guild_id)
                self.per_page = cog.get_queue_per_page(self.guild_id)
                self.queue_compact = cog.is_queue_compact(self.guild_id)
                self.page = 0
                self._busy = False
                self.queue = queue
                self.total_pages = (len(queue) - 1) // self.per_page + 1 if queue else 1
                self.update_buttons()

            async def interaction_check(self, interaction_: discord.Interaction) -> bool:
                return await self.cog.check_view_interaction(interaction_)

            def _cl(self, key):
                e = _BUTTON_EMOJIS.get(key)
                l = None if self.compact else t(interaction, key)
                return e, l

            def _get_search_tracks(self):
                state = self.guild_states.get(self.guild_id)
                return state.queue if state else []

            def update_buttons(self):
                empty = not self.queue
                # First call: create buttons and add to view once
                if not hasattr(self, '_btn_refs'):
                    self._btn_refs = {}
                    q_layout = self.cog.get_queue_layout(self.guild_id)
                    goto_label = None if self.compact else t(interaction, "BUTTON_GOTO_PAGE")
                    btn_map = {
                        "first":     _PageButton(self, *self._cl("BUTTON_FIRST"), "first", disabled=True, custom_id="q:first"),
                        "prev_page": _PageButton(self, *self._cl("BUTTON_PREV"), "prev", disabled=True, custom_id="q:prev"),
                        "next_page": _PageButton(self, *self._cl("BUTTON_NEXT"), "next", disabled=True, custom_id="q:next"),
                        "last":      _PageButton(self, *self._cl("BUTTON_LAST"), "last", disabled=True, custom_id="q:last"),
                        "goto":      _GoToPageButton(self, disabled=True, custom_id="q:goto", label=goto_label),
                        "search":    _SearchButton(self, label=None if self.compact else t(interaction, "BUTTON_SEARCH"), disabled=True, custom_id="q:search", show_details=True),
                        "refresh":   RefreshButton(self),
                    }
                    items = []
                    for key, cfg in q_layout.items():
                        if not cfg.get("enabled", True) or key not in btn_map:
                            continue
                        btn = btn_map[key]
                        btn.row = cfg["row"]
                        self._btn_refs[key] = btn
                        items.append((cfg["row"], cfg["col"], btn))
                    items.sort(key=lambda x: (x[0], x[1]))
                    for _, _, btn in items:
                        self.add_item(btn)
                # Update disabled state on existing button instances
                for key, btn in self._btn_refs.items():
                    if key in ("first", "prev_page"):
                        btn.disabled = empty or self.page == 0
                    elif key in ("next_page", "last"):
                        btn.disabled = empty or self.page >= self.total_pages - 1
                    elif key == "goto":
                        btn.disabled = empty or self.total_pages <= 1
                    elif key == "search":
                        btn.disabled = empty

            def get_embed(self):
                _color = self.cog.get_embed_color(self.guild_id)
                if not self.queue:
                    embed = SafeEmbed(title=t(interaction, "QUEUE_TITLE", page=1, total=1), color=_color)
                    embed.description = t(interaction, "QUEUE_EMPTY")
                else:
                    start = self.page * self.per_page
                    end = start + self.per_page
                    embed = SafeEmbed(title=t(interaction, "QUEUE_TITLE", page=self.page + 1, total=self.total_pages), color=_color)
                    description = ""

                    for i, entry in enumerate(self.queue[start:end], start=start + 1):
                        title = entry.get("title") or t(interaction, "UNKNOWN")
                        if len(title) > 60:
                            title = title[:57] + "..."
                        uploader = entry.get("uploader") or t(interaction, "UNKNOWN")
                        if len(uploader) > 30:
                            uploader = uploader[:27] + "..."

                        ie_key = entry.get("ie_key", "").lower()
                        video_id = entry.get("id")
                        if "youtube" in ie_key and not entry.get("webpage_url"):
                            url = f"https://www.youtube.com/watch?v={video_id}" if video_id else ""
                        else:
                            url = entry.get("webpage_url", "") or entry.get("spotify_url", "")
                        if url:
                            safe_title = title.replace("[", "⌜").replace("]", "⌝")
                            linked_title = f"[{safe_title}]({url})"
                        else:
                            linked_title = title
                        dur_str = _fmt_dur(entry.get("spotify_duration") if entry.get("source") == "spotify" else entry.get("duration"))
                        dur_part = f" - `{dur_str}`" if dur_str else ""
                        req_id = entry.get("requester")
                        req_part = f"\n> {t(interaction, 'REQUESTER')}: <@{req_id}>" if req_id else ""
                        sep = "\n" if self.queue_compact else "\n\n"
                        line = f"`{i}.` {linked_title} - *{uploader}*{dur_part}{req_part}{sep}"
                        description += line

                    embed.description = description

                qf = self.cog.get_queue_fields(self.guild_id)
                footer_parts = []
                if qf.get("total_duration", {}).get("enabled", True):
                    total_secs = 0
                    for e in self.queue:
                        d = e.get("spotify_duration") if e.get("source") == "spotify" else e.get("duration")
                        if isinstance(d, (int, float)):
                            total_secs += int(d)
                    if total_secs > 0:
                        _d = t(interaction, 'ABBR_DAYS')
                        _h = t(interaction, 'ABBR_HOURS')
                        _m = t(interaction, 'ABBR_MINUTES')
                        _s = t(interaction, 'ABBR_SECONDS')
                        td, rem = divmod(total_secs, 86400)
                        th, rem = divmod(rem, 3600)
                        tm, ts = divmod(rem, 60)
                        if td:
                            dur_fmt = f"{td}{_d} {th}{_h} {tm}{_m}"
                        elif th:
                            dur_fmt = f"{th}{_h} {tm}{_m} {ts}{_s}"
                        else:
                            dur_fmt = f"{tm}{_m} {ts}{_s}"
                        footer_parts.append(f"{t(interaction, 'QUEUE_FOOTER_TOTAL_DURATION')}: {dur_fmt}")

                if qf.get("playing_since", {}).get("enabled", True):
                    state = self.guild_states.get(self.guild_id)
                    if state and state.playing_since:
                        paused_total = state._total_paused
                        if state.paused_at is not None:
                            paused_total += time.time() - state.paused_at
                        elapsed = int(time.time() - state.playing_since - paused_total)
                        if elapsed > 0:
                            _d = t(interaction, 'ABBR_DAYS')
                            _h = t(interaction, 'ABBR_HOURS')
                            _m = t(interaction, 'ABBR_MINUTES')
                            _s = t(interaction, 'ABBR_SECONDS')
                            pd, rem = divmod(elapsed, 86400)
                            ph, rem = divmod(rem, 3600)
                            pm, ps = divmod(rem, 60)
                            if pd:
                                el_fmt = f"{pd}{_d} {ph}{_h} {pm}{_m}"
                            elif ph:
                                el_fmt = f"{ph}{_h} {pm}{_m}"
                            else:
                                el_fmt = f"{pm}{_m} {ps}{_s}"
                            footer_parts.append(f"{t(interaction, 'QUEUE_FOOTER_PLAYING_SINCE')}: {el_fmt}")

                if footer_parts:
                    embed.set_footer(text="\n".join(footer_parts))

                return embed

            def validate_page(self):
                state = self.guild_states.get(self.guild_id)
                new_queue = state.queue if state else []
                self.queue = new_queue
                self.compact = self.cog.is_queue_button_compact(self.guild_id)
                self.per_page = self.cog.get_queue_per_page(self.guild_id)
                self.queue_compact = self.cog.is_queue_compact(self.guild_id)
                self.total_pages = (len(self.queue) - 1) // self.per_page + 1 if self.queue else 1
                self.page = min(self.page, self.total_pages - 1)
                self.update_buttons()

        class RefreshButton(discord.ui.Button):
            def __init__(self, view):
                _lbl = None if view.compact else t(interaction, "BUTTON_REFRESH")
                super().__init__(style=discord.ButtonStyle.success, emoji=_BUTTON_EMOJIS["BUTTON_REFRESH"], label=_lbl, custom_id="q:refresh")
                self.view_ref = view

            async def callback(self, interaction: discord.Interaction):
                if not await self.view_ref.cog._check_cooldown(interaction, "q:refresh", 5, per_guild=True): return
                cog = self.view_ref.cog
                guild_id = self.view_ref.guild_id
                await interaction.response.defer()
                cog._schedule_refresh(guild_id, immediate=True)

        view = QueueView(queue_list, self.guild_states, self)
        return view, QueueView

    def _build_radio_queue_view(self, interaction):
        """Build a RadioQueueView for the given context (Interaction or _GuildCtx)."""
        guild_id = interaction.guild.id if hasattr(interaction, 'guild') and interaction.guild else interaction.guild_id

        class RadioQueueView(discord.ui.View):
            is_radio = True

            def __init__(rqv, queue, guild_states, cog):
                super().__init__(timeout=None)
                rqv.guild_id = guild_id
                rqv.guild_states = guild_states
                rqv.cog = cog
                rqv.queue = queue
                rqv.compact = cog.is_queue_button_compact(guild_id)
                rqv.page = 0
                rqv.total_pages = 1
                rqv.update_buttons()

            async def interaction_check(rqv, interaction_: discord.Interaction) -> bool:
                return await rqv.cog.check_view_interaction(interaction_)

            def update_buttons(rqv):
                if not hasattr(rqv, '_btn_refs'):
                    rqv._btn_refs = {}
                    btn = RadioRefreshButton(rqv)
                    rqv._btn_refs["refresh"] = btn
                    rqv.add_item(btn)

            def get_embed(rqv, ctx=None):
                _ctx = ctx or interaction
                session = rqv.cog.get_radio_session(rqv.guild_id)
                if session and session.active:
                    return rqv.cog._build_radio_queue_embed(_ctx, session)
                _color = rqv.cog.get_embed_color(rqv.guild_id)
                embed = SafeEmbed(title=t(_ctx, "QUEUE_TITLE", page=1, total=1), color=_color)
                embed.description = t(_ctx, "QUEUE_EMPTY")
                return embed

            def validate_page(rqv):
                rqv.compact = rqv.cog.is_queue_button_compact(rqv.guild_id)
                rqv.update_buttons()

        class RadioRefreshButton(discord.ui.Button):
            def __init__(self, view):
                _lbl = None if view.compact else t(interaction, "BUTTON_REFRESH")
                super().__init__(style=discord.ButtonStyle.success, emoji=_BUTTON_EMOJIS["BUTTON_REFRESH"], label=_lbl, custom_id="q:refresh")
                self.view_ref = view

            async def callback(self, interaction: discord.Interaction):
                if not await self.view_ref.cog._check_cooldown(interaction, "q:refresh", 5, per_guild=True): return
                cog = self.view_ref.cog
                guild_id = self.view_ref.guild_id
                await interaction.response.defer()
                cog._schedule_refresh(guild_id, immediate=True)

        state = self.guild_states.get(guild_id)
        queue_list = state.queue if state else []
        view = RadioQueueView(queue_list, self.guild_states, self)
        return view, RadioQueueView

    @app_commands.command(**l_cmd("CMD_NAME_QUEUE", "CMD_DESC_QUEUE"))
    async def queue_cmd(self, interaction: discord.Interaction):
        if not await self._check_cooldown(interaction, "queue", 10, per_guild=True): return
        guild_id = interaction.guild.id
        proceed, owner_confirmed = await self._check_view_access(interaction, "queue")
        if not proceed:
            return

        old = self.active_queues.pop(interaction.guild.id, None)
        if old:
            try:
                await old[0].delete()
            except discord.HTTPException:
                pass
        await self._delete_stale_view(guild_id, "queue", self.bot)

        # If radio is active, show radio view with refresh + auto-refresh
        session = self.get_radio_session(guild_id)
        if session and session.active:
            view, RadioViewCls = self._build_radio_queue_view(interaction)
            msg = await interaction.channel.send(embed=view.get_embed(), view=view)
            if owner_confirmed:
                self._owner_override_views.add((guild_id, "queue"))
            if msg:
                msg = interaction.channel.get_partial_message(msg.id)
                self.active_queues[interaction.guild.id] = (msg, view, RadioViewCls)
                await self._save_view(guild_id, "queue", interaction.channel_id, msg.id)
            try:
                await interaction.delete_original_response()
            except discord.HTTPException:
                pass
            return

        view, QueueViewCls = self._build_queue(interaction)
        msg = await interaction.channel.send(embed=view.get_embed(), view=view)
        if owner_confirmed:
            self._owner_override_views.add((guild_id, "queue"))
        if msg:
            msg = interaction.channel.get_partial_message(msg.id)
            self.active_queues[interaction.guild.id] = (msg, view, QueueViewCls)
            await self._save_view(guild_id, "queue", interaction.channel_id, msg.id)
        try:
            await interaction.delete_original_response()
        except discord.HTTPException:
            pass

    # --- History ---

    def _build_history_view(self, entries, interaction):
        per_page = self.get_queue_per_page(interaction.guild_id)

        class HistorySelectModal(discord.ui.Modal):
            def __init__(modal_self, hist_view, ctx):
                super().__init__(title=t(ctx, "QUEUE_SELECT_TITLE"))
                modal_self._hist_view = hist_view
                tcount = len(hist_view.entries)
                modal_self.index_input = discord.ui.TextInput(
                    label=t(ctx, "QUEUE_SELECT_LABEL"),
                    placeholder=f"1-{tcount}",
                    max_length=5,
                    required=True,
                )
                modal_self.indexto_input = discord.ui.TextInput(
                    label=t(ctx, "OPT_SELECT_INDEXTO"),
                    placeholder=f"1-{tcount}",
                    max_length=5,
                    required=False,
                )
                modal_self.shuffle_lbl = discord.ui.Label(
                    text=t(ctx, "SHUFFLE_LABEL"),
                    component=discord.ui.Checkbox(custom_id="shuffle"),
                )
                modal_self.add_item(modal_self.index_input)
                modal_self.add_item(modal_self.indexto_input)
                modal_self.add_item(modal_self.shuffle_lbl)

            async def on_submit(modal_self, modal_interaction: discord.Interaction):
                try:
                    idx = int(modal_self.index_input.value)
                except ValueError:
                    return await self.send_reply(modal_interaction, t(modal_interaction, "INVALID_INPUT"), ephemeral=True)
                v = modal_self._hist_view
                tcount = len(v.entries)
                if not (1 <= idx <= tcount):
                    return await self.send_reply(modal_interaction, t(modal_interaction, "SELECT_INVALID"), ephemeral=True)
                raw_to = modal_self.indexto_input.value.strip() if modal_self.indexto_input.value else ""
                do_shuffle = modal_self.shuffle_lbl.component.value
                if raw_to:
                    try:
                        idx_to = int(raw_to)
                    except ValueError:
                        return await self.send_reply(modal_interaction, t(modal_interaction, "INVALID_INPUT"), ephemeral=True)
                    if idx_to < idx or idx_to > tcount:
                        return await self.send_reply(modal_interaction, t(modal_interaction, "PL_PLAY_INVALID_RANGE"), ephemeral=True)
                    selected = v.entries[idx - 1:idx_to]
                    await modal_interaction.response.defer()
                    await self._play_playlist(modal_interaction, selected, shuffle=do_shuffle)
                else:
                    if do_shuffle:
                        await modal_interaction.response.defer()
                        await self._play_playlist(modal_interaction, [v.entries[idx - 1]], shuffle=False)
                    else:
                        track = v.entries[idx - 1]
                        entry = _playlist_track_to_entry(track)
                        await modal_interaction.response.defer()
                        _pl = self._command_locks.setdefault(modal_interaction.guild_id, asyncio.Lock())
                        async with _pl:
                            played, _ = await self.handle_play(modal_interaction, [entry])
                        await _dispatch_single_play(self, modal_interaction, entry, played)

        class HistorySelectButton(discord.ui.Button):
            def __init__(btn_self, view, *, disabled=False):
                _lbl = None if view.compact else t(interaction, "BUTTON_SELECT")
                super().__init__(style=discord.ButtonStyle.secondary, emoji=_BUTTON_EMOJIS["BUTTON_SELECT"], label=_lbl, disabled=disabled, row=1)
                btn_self.view_ref = view

            async def callback(btn_self, btn_interaction: discord.Interaction):
                if not await self._check_cooldown(btn_interaction, "hist:select", 5): return
                v = btn_self.view_ref
                if not v.entries:
                    return await self.send_reply(btn_interaction, t(btn_interaction, "HISTORY_EMPTY"), ephemeral=True)
                await btn_interaction.response.send_modal(HistorySelectModal(v, btn_interaction))

        class HistoryRefreshButton(discord.ui.Button):
            def __init__(btn_self, view, *, row=1):
                _lbl = None if view.compact else t(interaction, "BUTTON_REFRESH")
                super().__init__(style=discord.ButtonStyle.success, emoji=_BUTTON_EMOJIS["BUTTON_REFRESH"], label=_lbl, row=row)
                btn_self.view_ref = view

            async def callback(btn_self, btn_interaction: discord.Interaction):
                if not await self._check_cooldown(btn_interaction, "hist:refresh", 2): return
                v = btn_self.view_ref
                guild_id = btn_interaction.guild.id
                max_h = self.guild_max_history.get(guild_id, 50)
                if max_h <= 0:
                    return await self.send_reply(btn_interaction, t(btn_interaction, "HISTORY_DISABLED"), ephemeral=True)
                entries = await db.get_history(guild_id, max_h)
                v.entries = entries
                v.total_pages = max(1, (len(entries) - 1) // v.per_page + 1)
                if v.page >= v.total_pages:
                    v.page = v.total_pages - 1
                v._build()
                await btn_interaction.response.edit_message(embed=v.get_embed(), view=v)

        class HistoryClearButton(discord.ui.Button):
            def __init__(btn_self, view, *, disabled=False, row=1):
                _lbl = None if view.compact else t(interaction, "BUTTON_CLEAR_QUEUE")
                super().__init__(style=discord.ButtonStyle.danger, emoji=_BUTTON_EMOJIS["BUTTON_CLEAR_QUEUE"], label=_lbl, disabled=disabled, row=row)
                btn_self.view_ref = view

            async def callback(btn_self, btn_interaction: discord.Interaction):
                if not await self._check_cooldown(btn_interaction, "hist:clear", 5): return
                if not self.has_admin_privilege(btn_interaction.guild.id, btn_interaction.user):
                    return await self.send_reply(btn_interaction, t(btn_interaction, "NOT_DJ_OR_ADMIN"), ephemeral=True)
                guild_id = btn_interaction.guild.id
                await db.clear_history(guild_id)
                v = btn_self.view_ref
                v.entries = []
                v.page = 0
                v.total_pages = 1
                v._build()
                await btn_interaction.response.edit_message(embed=v.get_embed(), view=v)

        is_privileged = self.has_admin_privilege(interaction.guild_id, interaction.user)

        class HistoryView(discord.ui.View):
            def __init__(hv_self, hist_entries):
                super().__init__(timeout=900)
                hv_self.entries = hist_entries
                hv_self.cog = self
                hv_self.page = 0
                hv_self.per_page = per_page
                hv_self.total_pages = max(1, (len(hist_entries) - 1) // per_page + 1)
                hv_self.compact = self.is_queue_button_compact(interaction.guild_id)
                hv_self.is_privileged = is_privileged
                hv_self.message = None
                hv_self._build()

            async def interaction_check(hv_self, interaction_: discord.Interaction) -> bool:
                return await hv_self.cog.check_view_interaction(interaction_)

            async def on_timeout(hv_self):
                hv_self.cog.active_history.pop((interaction.guild_id, interaction.user.id), None)

            def _get_search_tracks(hv_self):
                return hv_self.entries

            def _build(hv_self):
                hv_self.clear_items()
                empty = not hv_self.entries
                single = hv_self.total_pages <= 1
                hv_self.add_item(_PageButton(hv_self, "⏪", None, "first", disabled=empty or hv_self.page == 0))
                hv_self.add_item(_PageButton(hv_self, "⬅️", None, "prev", disabled=empty or hv_self.page == 0))
                hv_self.add_item(_PageButton(hv_self, "➡️", None, "next", disabled=empty or hv_self.page >= hv_self.total_pages - 1))
                hv_self.add_item(_PageButton(hv_self, "⏩", None, "last", disabled=empty or hv_self.page >= hv_self.total_pages - 1))
                hv_self.add_item(_GoToPageButton(hv_self, disabled=single))
                hv_self.add_item(HistorySelectButton(hv_self, disabled=empty))
                search_label = None if hv_self.compact else t(interaction, "BUTTON_SEARCH")
                hv_self.add_item(_SearchButton(hv_self, label=search_label, disabled=empty, row=1, show_details=True))
                hv_self.add_item(HistoryRefreshButton(hv_self, row=1))
                if hv_self.is_privileged:
                    hv_self.add_item(HistoryClearButton(hv_self, disabled=empty, row=1))

            def get_embed(hv_self):
                color = self.get_embed_color(interaction.guild_id)
                total = len(hv_self.entries)
                embed = SafeEmbed(
                    title=t(interaction, "HISTORY_TITLE", page=hv_self.page + 1, total=hv_self.total_pages),
                    color=color,
                )
                if not hv_self.entries:
                    embed.description = t(interaction, "HISTORY_EMPTY")
                    return embed
                start = hv_self.page * hv_self.per_page
                end = start + hv_self.per_page
                lines = []
                for i, entry in enumerate(hv_self.entries[start:end], start=start + 1):
                    title_str = entry.get("title") or t(interaction, "UNKNOWN")
                    uploader = entry.get("uploader") or t(interaction, "UNKNOWN")
                    dur = _fmt_dur(entry.get("duration"))
                    dur_str = f" `{dur}`" if dur else ""
                    url = entry.get("url") or ""
                    if url:
                        safe_title = title_str.replace("[", "⌜").replace("]", "⌝")
                        line = f"**{i}.** [{safe_title}]({url}) - *{uploader}*{dur_str}"
                    else:
                        line = f"**{i}.** {title_str} - *{uploader}*{dur_str}"
                    req = entry.get("requester")
                    if req:
                        line += f"\n> {t(interaction, 'REQUESTER')}: <@{req}>"
                    lines.append(line)
                embed.description = "\n".join(lines)
                embed.set_footer(text=f"{t(interaction, 'PL_TRACK_COUNT', count=total)}")
                return embed

        return HistoryView(entries)

    @app_commands.command(**l_cmd("CMD_NAME_HISTORY", "CMD_DESC_HISTORY"))
    async def history_cmd(self, interaction: discord.Interaction):
        if not await self._check_cooldown(interaction, "history", 5): return
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild.id
        max_h = self.get_max_history(guild_id)
        if max_h <= 0:
            return await self.send_reply(interaction, t(interaction, "HISTORY_DISABLED"), ephemeral=True)
        entries = await db.get_history(guild_id, limit=max_h)
        _hist_key = (guild_id, interaction.user.id)
        old = self.active_history.pop(_hist_key, None)
        if old:
            try:
                await old.delete()
            except Exception:
                pass
        view = self._build_history_view(entries, interaction)
        msg = await interaction.followup.send(embed=view.get_embed(), view=view, ephemeral=True, wait=True)
        view.message = msg
        self.active_history[_hist_key] = msg

    @app_commands.command(**l_cmd("CMD_NAME_PREVIOUS", "CMD_DESC_PREVIOUS"))
    async def previous_cmd(self, interaction: discord.Interaction):
        if not await self._check_cooldown(interaction, "mp:prev", 1, per_guild=True): return
        if not await self._check_cooldown(interaction, "mp:prev", 5): return
        if self.is_radio_active(interaction.guild_id):
            return await self.send_reply(interaction, t(interaction, "RADIO_CMD_RESTRICTED", cmd_stop=t(interaction, "CMD_NAME_STOP")), ephemeral=True)
        await interaction.response.defer()
        await self.handle_previous(interaction)

    @app_commands.command(**l_cmd("CMD_NAME_REMOVE", "CMD_DESC_REMOVE"))
    @app_commands.rename(
        index=l_opt("OPTNAME_REMOVE_INDEX"),
        index_to=l_opt("OPTNAME_REMOVE_INDEXTO"),
        search=l_opt("OPTNAME_REMOVE_SEARCH"),
    )
    @app_commands.describe(
        index=l_opt("OPT_REMOVE_INDEX"),
        index_to=l_opt("OPT_REMOVE_INDEXTO"),
        search=l_opt("OPT_REMOVE_SEARCH"),
    )
    async def remove_cmd(self, interaction: discord.Interaction, index: int | None = None, index_to: int | None = None, search: str | None = None):
        if not await self._check_cooldown(interaction, "mp:remove", 1, per_guild=True): return
        if not await self._check_cooldown(interaction, "mp:remove", 5): return
        if self.is_radio_active(interaction.guild_id):
            return await self.send_reply(interaction, t(interaction, "RADIO_CMD_RESTRICTED", cmd_stop=t(interaction, "CMD_NAME_STOP")), ephemeral=True)
        if index is None and search is None:
            return await self.send_reply(interaction, t(interaction, "INVALID_INPUT"), ephemeral=True)
        await interaction.response.defer()
        try:
            state = self.ensure_queue_data(interaction, require_queue=True, queue_message=t(interaction, "QUEUE_EMPTY"))
        except CommandCheckError as e:
            return await respond_with_error(interaction, e)
        queue = state.queue
        guild_id = interaction.guild.id
        snapshot = tuple(id(e) for e in queue)
        parts = []
        if index is not None:
            if index < 1 or index > len(queue):
                return await self.send_reply(interaction, t(interaction, "SELECT_INVALID"), ephemeral=True)
            if index_to is not None:
                if index_to <= index:
                    return await self.send_reply(interaction, t(interaction, "REMOVE_ORDER",
                        indexto=t(interaction, "OPTNAME_REMOVE_INDEXTO"), index=t(interaction, "OPTNAME_REMOVE_INDEX")), ephemeral=True)
                if index_to > len(queue):
                    return await self.send_reply(interaction, t(interaction, "REMOVE_INVALID_RANGE",
                        indexto=t(interaction, "OPTNAME_REMOVE_INDEXTO")), ephemeral=True)
            idx_parts, ok = await _queue_remove_index(self, interaction, guild_id, index, index_to, search, queue, snapshot)
            parts.extend(idx_parts)
            if not ok and not search:
                return
        if search:
            await _queue_remove_search(self, interaction, guild_id, search, queue, state, parts)
        if parts:
            await self.send_reply(interaction, "\n".join(parts))
            self._schedule_refresh(guild_id)

    @app_commands.command(**l_cmd("CMD_NAME_RESUME", "CMD_DESC_RESUME"))
    async def resume_cmd(self, interaction: discord.Interaction):
        if not await self._check_cooldown(interaction, "mp:toggle", 1, per_guild=True): return
        if not await self._check_cooldown(interaction, "mp:toggle", 5): return
        await interaction.response.defer()
        try:
            vc, state, _ = self.check_playback_permissions(interaction)
        except CommandCheckError as e:
            return await respond_with_error(interaction, e)

        if not vc.is_paused():
            return await self.send_reply(interaction, t(interaction, "NOTHING_PLAYING"), ephemeral=True)

        await _do_resume(self, interaction, state, vc)

    @app_commands.command(**l_cmd("CMD_NAME_SEARCH", "CMD_DESC_SEARCH"))
    @app_commands.rename(query=l_opt("OPTNAME_SEARCH_QUERY"))
    @app_commands.describe(query=l_opt("OPT_SEARCH_QUERY"))
    async def search_cmd(self, interaction: discord.Interaction, query: str):
        if not await self._check_cooldown(interaction, "search", 5): return
        await interaction.response.defer()

        if self.is_radio_active(interaction.guild_id):
            return await self.send_reply(interaction, t(interaction, "RADIO_CMD_RESTRICTED", cmd_stop=t(interaction, "CMD_NAME_STOP")), ephemeral=True)

        if not interaction.user.voice or not interaction.user.voice.channel:
            return await self.send_reply(interaction, t(interaction, "JOIN_VOICE_FIRST"), ephemeral=True)

        try:
            entries = await extract_entries(f"ytsearch10:{query}", silent=self.is_silent_log(interaction.guild.id))
        except StaleCookieError:
            key = "YTDLP_COOKIE_STALE" if has_valid_cookies() else "YTDLP_AGE_RESTRICTED"
            return await self.send_reply(interaction,
                t(interaction, key), ephemeral=True)
        except JSRuntimeError:
            return await self.send_reply(interaction,
                t(interaction, "YTDLP_NO_JS_RUNTIME"), ephemeral=True)
        except Exception as e:
            return await self.send_reply(interaction,
                t(interaction, "VIDEO_CANNOT_PLAY", reason=_clean_ytdlp_error(e)), ephemeral=True)

        if not entries:
            return await self.send_reply(interaction, t(interaction, "NO_RESULTS"), ephemeral=True)

        _search_key = (interaction.guild_id, interaction.user.id)
        old_message = self.active_searches.get(_search_key)
        if old_message:
            try:
                await old_message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            self.active_searches.pop(_search_key, None)

        entries = entries[:10]
        view = self.SearchView(interaction, entries, self)
        message = await self.send_reply(interaction, embed=view.get_embed(), view=view, delete_after=None)
        view.message = message

        self.active_searches[_search_key] = view.message

    class SearchView(discord.ui.View):
        def __init__(self, interaction, entries, cog, timeout=180):
            super().__init__(timeout=timeout)
            self.ctx = interaction
            self.entries = entries
            self.page = 0
            self.max_page = (len(entries) - 1) // 5
            self.message = None
            self.cog = cog
            self.update_items()

        def update_items(self):
            self.clear_items()
            if self.page > 0:
                prev_btn = discord.ui.Button(emoji="⬅️", label=t(self.ctx, "BUTTON_PREV_PAGE"), style=discord.ButtonStyle.secondary)

                async def prev_cb(interaction: discord.Interaction):
                    self.page -= 1
                    self.update_items()
                    await interaction.response.edit_message(embed=self.get_embed(), view=self)

                prev_btn.callback = prev_cb
                self.add_item(prev_btn)
            if self.page < self.max_page:
                next_btn = discord.ui.Button(emoji="➡️", label=t(self.ctx, "BUTTON_NEXT_PAGE"), style=discord.ButtonStyle.secondary)

                async def next_cb(interaction: discord.Interaction):
                    self.page += 1
                    self.update_items()
                    await interaction.response.edit_message(embed=self.get_embed(), view=self)

                next_btn.callback = next_cb
                self.add_item(next_btn)
            self.add_item(self.cog.SearchSelect(self.page, self.entries, self.ctx, self, self.cog))
            self.add_item(self.cog.CancelButton(self.ctx, self.cog))

        def get_embed(self):
            """Generates the search results list embed."""
            embed = SafeEmbed(
                title=t(self.ctx, "SEARCH_TITLE", page=self.page + 1, total=self.max_page + 1),
                color=self.cog.get_embed_color(self.ctx.guild_id)
            )

            start = self.page * 5
            end = start + 5
            description = ""

            for i, entry in enumerate(self.entries[start:end], start=start + 1):
                title = entry.get("title") or t(self.ctx, "UNKNOWN")
                uploader = entry.get("uploader") or t(self.ctx, "UNKNOWN")
                video_id = entry.get("id")
                url = entry.get("webpage_url") or (f"https://www.youtube.com/watch?v={video_id}" if video_id else "")
                if url:
                    safe_title = title.replace("[", "⌜").replace("]", "⌝")
                    linked_title = f"[{safe_title}]({url})"
                else:
                    linked_title = title
                dur_str = _fmt_dur(entry.get("duration")) or "?"
                views = entry.get("view_count")
                views_str = f"{views:,}" if isinstance(views, int) else "?"
                description += f"`{i}.` {linked_title} - *{uploader}* | {t(self.ctx, 'DURATION').lower()}: `{dur_str}` | {t(self.ctx, 'VIEWS').lower()}: `{views_str}`\n\n"

            embed.description = description
            embed.set_footer(
                text=t(self.ctx, "SEARCH_FOOTER", user=self.ctx.user.display_name),
                icon_url=self.ctx.user.display_avatar.url
            )
            return embed

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if not await self.cog.check_view_interaction(interaction):
                return False
            if interaction.user.id != self.ctx.user.id:
                await self.cog.send_reply(interaction, 
                    t(interaction, "SEARCH_ONLY_OWNER"),
                    ephemeral=True
                )
                return False
            return True

        async def on_timeout(self):
            for item in self.children:
                item.disabled = True

            if self.message:
                try:
                    await self.message.delete()
                except discord.HTTPException:
                    pass

            try:
                guild = self.ctx.guild
                da = self.cog._resolve_delete_after(guild.id) if guild else None
                if da == 0:
                    da = None
                await self.ctx.channel.send(
                    t(self.ctx, "SEARCH_TIMEOUT_MSG", user=self.ctx.user.mention),
                    delete_after=da
                )
            except (discord.HTTPException, AttributeError):
                pass

            self.cog.active_searches.pop((self.ctx.guild_id, self.ctx.user.id), None)

    class SearchSelect(discord.ui.Select):
        def __init__(self, page, entries, interaction, view_ref, cog):
            self.entries = entries
            self.ctx = interaction
            self.view_ref = view_ref
            self.cog = cog

            start = page * 5
            end = min(start + 5, len(entries))
            options = [
                discord.SelectOption(
                    label=t(interaction, "SEARCH_OPTION", index=i + 1),
                    description=(entries[i].get("title") or t(interaction, "UNKNOWN"))[:100],
                    value=str(i)
                )
                for i in range(start, end)
            ]
            super().__init__(placeholder=t(interaction, "SELECT_OPTION_PLACEHOLDER"), options=options)

        async def callback(self, interaction: discord.Interaction):
            if not await self.cog._check_cooldown(interaction, "search:select", 3): return
            index = int(self.values[0])
            entry = self.entries[index]
            title = entry.get("title") or t(self.ctx, "UNKNOWN")
            uploader = entry.get("uploader") or t(self.ctx, "UNKNOWN")

            for item in self.view_ref.children:
                item.disabled = True
            try:
                await interaction.response.edit_message(view=self.view_ref)
            except discord.HTTPException:
                if not interaction.response.is_done():
                    await interaction.response.defer()

            _pl = self.cog._command_locks.setdefault(interaction.guild_id, asyncio.Lock())
            async with _pl:
                played_now, _ = await self.cog.handle_play(interaction, [entry])
            if played_now == "restricted":
                await self.cog.send_reply(interaction, t(self.ctx, "JOIN_RESTRICTED_CHANNEL"), ephemeral=True)
                return
            if played_now == "user_limit":
                await self.cog.send_reply(interaction, t(self.ctx, "USER_TRACK_LIMIT"), ephemeral=True)
                return
            if played_now == "connect_failed":
                await self.cog.send_reply(interaction, t(self.ctx, "VOICE_CONNECT_FAILED"), ephemeral=True)
                return
            if played_now == "live_blocked":
                await self.cog.send_reply(interaction, t(self.ctx, "LIVE_BLOCKED"), ephemeral=True)
                return
            if played_now is None:
                _q_lim = self.cog.guild_queue_limit.get(interaction.guild_id, MAX_QUEUE)
                await self.cog.send_reply(interaction, t(self.ctx, "QUEUE_FULL", limit=_q_lim), ephemeral=True)
                return

            if not played_now:
                self.cog._schedule_refresh(interaction.guild.id)

            try:
                if self.view_ref.message:
                    await self.view_ref.message.delete()
            except discord.HTTPException:
                pass

            if played_now:
                message = t(self.ctx, "ANNOUNCE_NOW", title=title, uploader=uploader)
            else:
                message = t(self.ctx, "ADDED_TO_QUEUE_SINGLE", title=f"`{title}` - `{uploader}`")

            await self.cog.send_reply(interaction, message)

            self.cog.active_searches.pop((self.ctx.guild_id, self.ctx.user.id), None)

            self.view_ref.stop()

    class CancelButton(discord.ui.Button):
        def __init__(self, interaction, cog):
            super().__init__(emoji="✖️", label=t(interaction, "BUTTON_CANCEL"), style=discord.ButtonStyle.danger)
            self.ctx = interaction
            self.cog = cog

        async def callback(self, interaction: discord.Interaction):
            if interaction.user.id != self.ctx.user.id:
                await self.cog.send_reply(interaction, 
                    t(interaction, "SEARCH_CANCEL_ONLY_OWNER"),
                    ephemeral=True
                )
                return

            await interaction.response.defer()
            try:
                await interaction.message.delete()
            except discord.HTTPException:
                pass

            self.cog.active_searches.pop((self.ctx.guild_id, self.ctx.user.id), None)
            self.view.stop()

    @app_commands.command(**l_cmd("CMD_NAME_SEEK", "CMD_DESC_SEEK"))
    @app_commands.rename(
        seconds=l_opt("OPTNAME_SEEK_SECONDS"),
        minutes=l_opt("OPTNAME_SEEK_MINUTES"),
        hours=l_opt("OPTNAME_SEEK_HOURS"),
    )
    @app_commands.describe(
        seconds=l_opt("OPT_SEEK_SECONDS"),
        minutes=l_opt("OPT_SEEK_MINUTES"),
        hours=l_opt("OPT_SEEK_HOURS"),
    )
    async def seek_cmd(self, interaction: discord.Interaction, seconds: int = 0, minutes: int = 0, hours: int = 0):
        if not await self._check_cooldown(interaction, "mp:seek", 1, per_guild=True): return
        if not await self._check_cooldown(interaction, "mp:seek", 5): return
        await interaction.response.defer()
        try:
            vc, state = self.ensure_voice_and_state(interaction, require_now=True)
        except CommandCheckError as e:
            return await respond_with_error(interaction, e)

        if not vc.is_playing() and not vc.is_paused():
            return await self.send_reply(interaction, t(interaction, "NOTHING_PLAYING"), ephemeral=True)

        current = state.now
        if not current or "url" not in current:
            return await self.send_reply(interaction, t(interaction, "SEEK_NO_VALID"), ephemeral=True)

        guild_id = interaction.guild_id
        allowed, _is_dj, _is_admin = _check_seek_permission(self, interaction, guild_id, current)
        if not allowed:
            return await self.send_reply(interaction, t(interaction, "SEEK_NO_PERMISSION"), ephemeral=True)

        if _is_admin:
            max_seeks = 0
        elif _is_dj:
            max_seeks = self.guild_max_seeks_dj.get(guild_id, 0)
        else:
            max_seeks = self.guild_max_seeks_per_track.get(guild_id, 3)
        if max_seeks > 0:
            counts = current.setdefault("_seek_counts", {})
            user_count = counts.get(interaction.user.id, 0)
            if user_count >= max_seeks:
                return await self.send_reply(interaction, t(interaction, "SEEK_LIMIT_REACHED"), ephemeral=True)

        success = await _apply_seek(self, interaction, state, vc, current, hours, minutes, seconds)

        if success and max_seeks > 0:
            counts = current.setdefault("_seek_counts", {})
            counts[interaction.user.id] = counts.get(interaction.user.id, 0) + 1

    @app_commands.command(**l_cmd("CMD_NAME_SELECT", "CMD_DESC_SELECT"))
    @app_commands.rename(index=l_opt("OPTNAME_SELECT_INDEX"))
    @app_commands.describe(index=l_opt("OPT_SELECT_INDEX"))
    async def select_cmd(self, interaction: discord.Interaction, index: int):
        if not await self._check_cooldown(interaction, "mp:select", 1, per_guild=True): return
        if not await self._check_cooldown(interaction, "mp:select", 5): return
        if self.is_radio_active(interaction.guild_id):
            return await self.send_reply(interaction, t(interaction, "RADIO_CMD_RESTRICTED", cmd_stop=t(interaction, "CMD_NAME_STOP")), ephemeral=True)
        await interaction.response.defer()
        try:
            state = self.ensure_queue_data(interaction, require_queue=True)
        except CommandCheckError as e:
            return await respond_with_error(interaction, e)
        snapshot = tuple(id(e) for e in state.queue)
        await _queue_select(self, interaction, interaction.guild.id, index, snapshot)

    _PRESET_COLORS = [
        (0xFF003C, "COLOR_NEON_RED"),
        (0xFF6B35, "COLOR_FLAME"),
        (0xFFAA00, "COLOR_AMBER"),
        (0xFFF01F, "COLOR_ELECTRIC_YELLOW"),
        (0xADFF2F, "COLOR_LIME_PUNCH"),
        (0x39FF14, "COLOR_NEON_GREEN"),
        (0x00E676, "COLOR_MINT"),
        (0x00BFA5, "COLOR_TEAL"),
        (0x00E5FF, "COLOR_CYAN"),
        (0x00B0FF, "COLOR_SKY"),
        (0x2979FF, "COLOR_ELECTRIC_BLUE"),
        (0x5865F2, "COLOR_BLURPLE"),
        (0x651FFF, "COLOR_ULTRAVIOLET"),
        (0xAA00FF, "COLOR_VIVID_PURPLE"),
        (0xD500F9, "COLOR_NEON_PURPLE"),
        (0xFF1493, "COLOR_HOT_PINK"),
        (0xFF006E, "COLOR_NEON_ROSE"),
        (0xF50057, "COLOR_CRIMSON_GLOW"),
        (0xFF9100, "COLOR_TANGERINE"),
        (0xFFD600, "COLOR_SUNBEAM"),
        (0x76FF03, "COLOR_ACID_GREEN"),
        (0x18FFFF, "COLOR_ICE_BLUE"),
        (0xE0E0E0, "COLOR_SILVER"),
        (0x546E7A, "COLOR_STEEL"),
        (0x212121, "COLOR_OBSIDIAN"),
    ]

    _OWNER_ONLY_SETTINGS = frozenset({
        "admin_users", "admin_roles", "admin_priv",
        "excluded_users", "excluded_roles",
        "join_restrict_level", "join_restrict_channels",
        "track_limit_admin",
    })
    _APP_OWNER_ONLY_SETTINGS = frozenset({
        "bot_activity", "bot_activity_list",
        "prefetch", "safe_prefetch", "max_workers",
        "silent_log",
    })

    # --- Setting definitions (key, label_locale, desc_locale, type) ---
    _SETTING_DEFS = [
        ("language",       "SETTINGS_SHOW_LANGUAGE",             "SETTINGS_DESC_LANGUAGE",             "language"),
        ("vote_mode",      "SETTINGS_SHOW_VOTE_MODE",            "SETTINGS_DESC_VOTE_MODE",            "choice"),
        ("dj",             "SETTINGS_SHOW_DJ",                   "SETTINGS_DESC_DJ",                   "dj"),
        ("delete_after",   "SETTINGS_SHOW_DELETE_AFTER",         "SETTINGS_DESC_DELETE_AFTER",         "number"),
        ("silent_log",     "SETTINGS_SHOW_SILENT_LOG",           "SETTINGS_DESC_SILENT_LOG",           "boolean"),
        ("embed_views",    "SETTINGS_SHOW_EMBED_VIEW_SETTINGS",  "SETTINGS_DESC_EMBED_VIEW_SETTINGS",  "embed_views"),
        ("limits",         "SETTINGS_SHOW_LIMITS",               "SETTINGS_DESC_LIMITS",               "limits"),
        ("timezone",       "SETTINGS_SHOW_TIMEZONE",             "SETTINGS_DESC_TIMEZONE",             "timezone"),
        ("manage_perms",   "SETTINGS_SHOW_MANAGE_PERMS",         "SETTINGS_DESC_MANAGE_PERMS",         "manage_perms"),
        ("bot_activity",   "SETTINGS_SHOW_BOT_ACTIVITY",         "SETTINGS_DESC_BOT_ACTIVITY",         "bot_activity"),
    ]

    _EMBED_VIEWS_SUB_DEFS = {
        "embed_color":   ("SETTINGS_SHOW_EMBED_COLOR",   "SETTINGS_DESC_EMBED_COLOR"),
        "mp_display":    ("SETTINGS_SHOW_MP_DISPLAY",    "SETTINGS_DESC_MP_DISPLAY"),
        "queue_display": ("SETTINGS_SHOW_QUEUE_DISPLAY", "SETTINGS_DESC_QUEUE_DISPLAY"),
        "view_restrict": ("SETTINGS_SHOW_VIEW_RESTRICT", "SETTINGS_DESC_VIEW_RESTRICT"),
    }

    _LIMITS_SUB_DEFS = {
        "max_playlists":    ("SETTINGS_SHOW_MAX_PLAYLISTS",    "SETTINGS_DESC_MAX_PLAYLISTS"),
        "max_history":      ("SETTINGS_SHOW_MAX_HISTORY",      "SETTINGS_DESC_MAX_HISTORY"),
        "max_user_tracks":  ("SETTINGS_SHOW_TRACK_LIMIT",     "SETTINGS_DESC_TRACK_LIMIT"),
        "limit_usage":      ("SETTINGS_SHOW_LIMIT_USAGE",      "SETTINGS_DESC_LIMIT_USAGE"),
        "pause_control":    ("SETTINGS_SHOW_PAUSE_CONTROL",    "SETTINGS_DESC_PAUSE_CONTROL"),
        "bot_connection":   ("SETTINGS_SHOW_BOT_CONNECTION",   "SETTINGS_DESC_BOT_CONNECTION"),
    }

    def _settings_value_label(self, ctx, guild_id: int, key: str) -> str:
        if key == "language":
            lc = guild_locales.get(guild_id)
            return SUPPORTED_LOCALES.get(lc, lc) if lc else t(ctx, "SETTINGS_SHOW_AUTO")
        if key == "vote_mode":
            vm = self.vote_modes.get(guild_id, "half_plus_one")
            mode = t(ctx, "VOTE_MODE_HALF") if vm == "half" else t(ctx, "VOTE_MODE_HALF_PLUS_ONE")
            deaf = self.guild_vote_exclude_deafened.get(guild_id, 1)
            deaf_label = t(ctx, "VOTE_DEAFENED_EXCLUDE") if deaf else t(ctx, "VOTE_DEAFENED_INCLUDE")
            return f"{mode}, {deaf_label}"
        if key == "dj":
            dj = self.dj_roles.get(guild_id)
            dj_count = len(self.dj_users.get(guild_id, set()))
            parts = []
            if dj:
                parts.append(f"<@&{dj}>")
            if dj_count:
                parts.append(f"{dj_count} {t(ctx, 'ABBR_USERS')}")
            return ", ".join(parts) if parts else t(ctx, "SETTINGS_SHOW_NONE")
        if key == "delete_after":
            da = self.guild_delete_after.get(guild_id, 10)
            return f"{da}{t(ctx, 'ABBR_SECONDS')}" if da > 0 else t(ctx, "DISABLED")
        if key == "silent_log":
            sl = self._silent_log
            return t(ctx, "SETTINGS_ACTIVE") if sl else t(ctx, "SETTINGS_INACTIVE")
        if key == "timezone":
            tz = self.guild_timezones.get(guild_id, 0)
            return f"UTC{tz:+d}" if tz != 0 else "UTC"
        if key == "embed_color":
            ec = self.guild_embed_colors.get(guild_id)
            return f"#{ec:06X}" if ec is not None else t(ctx, "COLOR_DEFAULT_LABEL")
        if key == "embed_views":
            ec = self.guild_embed_colors.get(guild_id)
            ec_str = f"#{ec:06X}" if ec is not None else t(ctx, "COLOR_DEFAULT_LABEL")
            mp_l = self.get_mp_layout(guild_id)
            q_l = self.get_queue_layout(guild_id)
            mp_on = sum(1 for v in mp_l.values() if v.get("enabled", True))
            q_on = sum(1 for v in q_l.values() if v.get("enabled", True))
            pp = self.guild_queue_per_page.get(guild_id, 10)
            qc = self.guild_queue_compact.get(guild_id, True)
            mode = t(ctx, "SETTINGS_QD_NORMAL") if qc else t(ctx, "SETTINGS_QD_SPACIOUS")
            mp_f = self.get_mp_fields(guild_id)
            mf_on = sum(1 for v in mp_f.values() if v.get("enabled", True))
            return f"{t(ctx, 'ABBR_MP')} {mp_on}/{len(mp_l)}, {t(ctx, 'ABBR_FIELDS')} {mf_on}/{len(mp_f)}, {t(ctx, 'ABBR_Q')} {q_on}/{len(q_l)}, {mode}, {pp}{t(ctx, 'ABBR_PER_PAGE')}, {t(ctx, 'ABBR_COLOR')}: {ec_str}"
        if key == "queue_display":
            pp = self.guild_queue_per_page.get(guild_id, 10)
            qc = self.guild_queue_compact.get(guild_id, True)
            mode = t(ctx, "SETTINGS_QD_NORMAL") if qc else t(ctx, "SETTINGS_QD_SPACIOUS")
            qf = self.get_queue_fields(guild_id)
            qf_on = sum(1 for v in qf.values() if v.get("enabled", True))
            return f"{pp} / {mode}, {qf_on}/{len(qf)}"
        if key == "limits":
            mp = self.guild_max_playlists.get(guild_id, 15)
            mp_str = str(mp) if mp > 0 else t(ctx, "DISABLED")
            mh = self.guild_max_history.get(guild_id, 50)
            mh_str = str(mh) if mh > 0 else t(ctx, "DISABLED")
            _u = self.guild_track_limit_users.get(guild_id, 0)
            _d = self.guild_track_limit_dj.get(guild_id, 0)
            _a = self.guild_track_limit_admin.get(guild_id, 0)
            if _u or _d or _a:
                _tl_parts = []
                if _u: _tl_parts.append(f"{t(ctx, 'TRACK_LIMIT_GROUP_USERS')}: {_u}")
                if _d: _tl_parts.append(f"{t(ctx, 'TRACK_LIMIT_GROUP_DJ')}: {_d}")
                if _a: _tl_parts.append(f"{t(ctx, 'TRACK_LIMIT_GROUP_ADMIN')}: {_a}")
                mu_str = " / ".join(_tl_parts)
            else:
                mu_str = t(ctx, "DISABLED")
            eu = len(self.excluded_users.get(guild_id, set()))
            er = len(self.excluded_roles.get(guild_id, set()))
            lu_str = t(ctx, "DISABLED") if eu == 0 and er == 0 else f"{eu + er}"
            pt = self.guild_pause_timeout.get(guild_id, 900) // 60
            radio = self._radio_settings_desc(ctx, guild_id)
            return f"{t(ctx, 'ABBR_PL')}: {mp_str}, {t(ctx, 'ABBR_HISTORY')}: {mh_str}, {t(ctx, 'ABBR_TRACKS')}: {mu_str}, {t(ctx, 'ABBR_USAGE')}: {lu_str}, {t(ctx, 'ABBR_PAUSE')}: {pt}{t(ctx, 'ABBR_MINUTES')}, {t(ctx, 'ABBR_RADIO')}: {radio}"
        if key == "max_playlists":
            mp = self.guild_max_playlists.get(guild_id, 15)
            return str(mp) if mp > 0 else t(ctx, "DISABLED")
        if key == "view_restrict":
            _VR_LABELS = {0: "VIEW_RESTRICT_DISABLED", 1: "VIEW_RESTRICT_NON_DJ", 2: "VIEW_RESTRICT_DJ_USER", 3: "VIEW_RESTRICT_ALL"}
            ch = self.guild_view_channels.get(guild_id)
            lvl = self.guild_view_restricts.get(guild_id, 0)
            ch_str = f"<#{ch}>" if ch else t(ctx, "SETTINGS_SHOW_NONE")
            lvl_str = t(ctx, _VR_LABELS.get(lvl, "VIEW_RESTRICT_DISABLED"))
            return f"{ch_str} / {lvl_str}"
        if key == "max_history":
            mh = self.guild_max_history.get(guild_id, 50)
            return str(mh) if mh > 0 else t(ctx, "DISABLED")
        if key == "max_user_tracks":
            u_lim = self.guild_track_limit_users.get(guild_id, 0)
            d_lim = self.guild_track_limit_dj.get(guild_id, 0)
            a_lim = self.guild_track_limit_admin.get(guild_id, 0)
            q_lim = self.guild_queue_limit.get(guild_id, 5000)
            pl_lim = self.guild_playlist_track_limit.get(guild_id, 5000)
            parts = []
            if u_lim: parts.append(f"{t(ctx, 'ABBR_U')}:{u_lim}")
            if d_lim: parts.append(f"{t(ctx, 'ABBR_DJ')}:{d_lim}")
            if a_lim: parts.append(f"{t(ctx, 'ABBR_A')}:{a_lim}")
            parts.append(f"{t(ctx, 'ABBR_Q')}:{q_lim}")
            parts.append(f"{t(ctx, 'ABBR_PL')}:{pl_lim}")
            return " / ".join(parts)
        if key == "limit_usage":
            eu = len(self.excluded_users.get(guild_id, set()))
            er = len(self.excluded_roles.get(guild_id, set()))
            if eu == 0 and er == 0:
                return t(ctx, "DISABLED")
            parts = []
            if eu:
                parts.append(f"{eu} {t(ctx, 'ABBR_USERS')}")
            if er:
                parts.append(f"{er} {t(ctx, 'ABBR_ROLES')}")
            return ", ".join(parts)
        if key == "manage_perms":
            enabled = self.guild_admin_priv.get(guild_id, 1)
            parts = [f"{t(ctx, 'MANAGE_PERMS_ADMIN_PRIV')}: {t(ctx, 'STATE_ON' if enabled else 'STATE_OFF')}"]
            au = len(self.admin_users.get(guild_id, set()))
            ar = len(self.admin_roles.get(guild_id, set()))
            if au:
                parts.append(f"{au} {t(ctx, 'ABBR_USERS')}")
            if ar:
                parts.append(f"{ar} {t(ctx, 'ABBR_ROLES')}")
            return ", ".join(parts)
        if key == "bot_activity":
            _MK = {"static": "ACTIVITY_MODE_STATIC", "random": "ACTIVITY_MODE_RANDOM", "ordered": "ACTIVITY_MODE_ORDERED"}
            mode_label = t(ctx, _MK.get(self.bot_activity_mode, "ACTIVITY_MODE_STATIC"))
            lst = self.bot_activity_list
            if lst:
                return f"{mode_label} ({len(lst)})"
            return f"{self._activity_type_name(ctx, self.bot_activity_type)}: {self.bot_activity_text}"
        return "?"

    def _build_overview_embed(self, ctx, guild_id: int, *, is_app_owner: bool = False, is_guild_admin: bool = True, _effective_owner: bool = False) -> discord.Embed:
        lines = []
        for key, label_key, _, stype in self._SETTING_DEFS:
            if stype in ("action",):
                continue
            if key == "manage_perms" and not _effective_owner:
                continue
            if key in ("bot_activity", "silent_log") and not is_app_owner:
                continue
            if not is_guild_admin and is_app_owner and key not in ("bot_activity", "silent_log"):
                continue
            value = self._settings_value_label(ctx, guild_id, key)
            label = t(ctx, label_key)
            lines.append(f"**{label}:** {value}")
        embed = SafeEmbed(
            title=t(ctx, "SETTINGS_TITLE"),
            description="\n".join(lines),
            color=self.get_embed_color(guild_id),
        )
        return embed

    def _build_detail_embed(self, ctx, guild_id: int, key: str) -> discord.Embed:
        label_key = desc_key = None
        for skey, lk, dk, _ in self._SETTING_DEFS:
            if skey == key:
                label_key, desc_key = lk, dk
                break
        if not label_key and key in self._EMBED_VIEWS_SUB_DEFS:
            label_key, desc_key = self._EMBED_VIEWS_SUB_DEFS[key]
        if not label_key and key in self._LIMITS_SUB_DEFS:
            label_key, desc_key = self._LIMITS_SUB_DEFS[key]
        if not label_key:
            return SafeEmbed(title="?", color=self.get_embed_color(guild_id))
        title = f"⚙️ {t(ctx, label_key)}"
        desc = t(ctx, desc_key)
        val = self._settings_value_label(ctx, guild_id, key)
        body = desc
        if key == "max_user_tracks":
            u_lim = self.guild_track_limit_users.get(guild_id, 0)
            d_lim = self.guild_track_limit_dj.get(guild_id, 0)
            a_lim = self.guild_track_limit_admin.get(guild_id, 0)
            q_lim = self.guild_queue_limit.get(guild_id, 5000)
            pl_lim = self.guild_playlist_track_limit.get(guild_id, 5000)
            _dis = t(ctx, "DISABLED")
            body += f"\n\n> **{t(ctx, 'TRACK_LIMIT_GROUP_USERS')}:** {u_lim if u_lim > 0 else _dis}"
            body += f"\n> **{t(ctx, 'TRACK_LIMIT_GROUP_DJ')}:** {d_lim if d_lim > 0 else _dis}"
            body += f"\n> **{t(ctx, 'TRACK_LIMIT_GROUP_ADMIN')}:** {a_lim if a_lim > 0 else _dis}"
            body += f"\n\n> **{t(ctx, 'QUEUE_LIMIT_BUTTON')}:** {q_lim}"
            body += f"\n> **{t(ctx, 'PL_TRACK_LIMIT_BUTTON')}:** {pl_lim}"
        elif key == "limit_usage":
            eu = self.excluded_users.get(guild_id, set())
            er = self.excluded_roles.get(guild_id, set())
            if eu or er:
                if eu:
                    body += f"\n\n**{t(ctx, 'LIMIT_USAGE_USERS_LABEL')}:** " + ", ".join(f"<@{uid}>" for uid in eu)
                if er:
                    body += f"\n**{t(ctx, 'LIMIT_USAGE_ROLES_LABEL')}:** " + ", ".join(f"<@&{rid}>" for rid in er)
        elif key == "manage_perms":
            au = self.admin_users.get(guild_id, set())
            ar = self.admin_roles.get(guild_id, set())
            enabled = self.guild_admin_priv.get(guild_id, 1)
            status = t(ctx, "STATE_ON") if enabled else t(ctx, "STATE_OFF")
            body += f"\n\n**{t(ctx, 'MANAGE_PERMS_ADMIN_PRIV')}:** {status}"
            if au:
                body += f"\n**{t(ctx, 'MANAGE_PERMS_USERS_LABEL')}:** " + ", ".join(f"<@{uid}>" for uid in au)
            if ar:
                body += f"\n**{t(ctx, 'MANAGE_PERMS_ROLES_LABEL')}:** " + ", ".join(f"<@&{rid}>" for rid in ar)
        elif key == "queue_display":
            pp = self.guild_queue_per_page.get(guild_id, 10)
            qc = self.guild_queue_compact.get(guild_id, True)
            mode = t(ctx, "SETTINGS_QD_NORMAL") if qc else t(ctx, "SETTINGS_QD_SPACIOUS")
            body += f"\n\n**{t(ctx, 'SETTINGS_CURRENT')}:** {pp}{t(ctx, 'ABBR_PER_PAGE')}, {mode}"
            from core.music_handlers import _QUEUE_FIELD_LABELS
            qf = self.get_queue_fields(guild_id)
            for fkey, label_key in _QUEUE_FIELD_LABELS.items():
                enabled = qf.get(fkey, {}).get("enabled", True)
                icon = "✅" if enabled else "❌"
                body += f"\n{icon} **{t(ctx, label_key)}**"
        elif val:
            if key == "dj":
                body += f"\n\n{val}"
            else:
                body += f"\n\n**{t(ctx, 'SETTINGS_CURRENT')}:** {val}"
        return SafeEmbed(
            title=title,
            description=body,
            color=self.get_embed_color(guild_id),
        )

    _ACT_PER_PAGE = 5
    _ACT_TYPE_KEYS = {0: "ACTIVITY_PLAYING", 2: "ACTIVITY_LISTENING", 3: "ACTIVITY_WATCHING", 5: "ACTIVITY_COMPETING"}

    def _activity_type_name(self, ctx, act_type: int) -> str:
        return t(ctx, self._ACT_TYPE_KEYS.get(act_type, "ACTIVITY_TYPE_LISTENING"))

    def _build_activity_embed(self, ctx, *, page: int = 0, guild_id: int | None = None) -> discord.Embed:
        _MODE_KEYS = {"static": "ACTIVITY_MODE_STATIC", "random": "ACTIVITY_MODE_RANDOM", "ordered": "ACTIVITY_MODE_ORDERED"}
        color = self.get_embed_color(guild_id)
        lst = self.bot_activity_list
        embed = SafeEmbed(
            title=f"⚙️ {t(ctx, 'SETTINGS_SHOW_BOT_ACTIVITY')}",
            description=t(ctx, "SETTINGS_DESC_BOT_ACTIVITY"),
            color=color,
        )
        if lst:
            pp = self._ACT_PER_PAGE
            total_pages = (len(lst) + pp - 1) // pp
            page = max(0, min(page, total_pages - 1))
            start = page * pp
            end = min(start + pp, len(lst))
            lines = []
            for i in range(start, end):
                item = lst[i]
                tname = self._activity_type_name(ctx, item["type"])
                marker = " ◀" if self.bot_activity_mode == "static" and i == self.bot_activity_selected else ""
                lines.append(f"`{i + 1}.` **{tname}:** {item['text']}{marker}")
            value = "\n".join(lines)
            name = t(ctx, "ACTIVITY_LIST_LABEL")
            if total_pages > 1:
                name += f" ({page + 1}/{total_pages})"
            embed.add_field(name=name, value=value, inline=False)
            mode_label = t(ctx, _MODE_KEYS.get(self.bot_activity_mode, "ACTIVITY_MODE_STATIC"))
            info = f"**{t(ctx, 'ACTIVITY_CURRENT_MODE')}:** {mode_label}"
            if self.bot_activity_mode == "static":
                sel = min(self.bot_activity_selected, len(lst) - 1) + 1
                info += f"  •  **{t(ctx, 'ACTIVITY_CURRENT_SELECTED')}:** #{sel}"
            else:
                interval = self.bot_activity_interval
                _h = t(ctx, 'ABBR_HOURS')
                _m = t(ctx, 'ABBR_MINUTES')
                if interval >= 60:
                    time_str = f"{interval // 60}{_h} {interval % 60}{_m}" if interval % 60 else f"{interval // 60}{_h}"
                else:
                    time_str = f"{interval}{_m}"
                info += f"  •  **{t(ctx, 'ACTIVITY_CURRENT_INTERVAL')}:** {time_str}"
            embed.add_field(name="\u200b", value=info, inline=False)
        else:
            def_type = self._activity_type_name(ctx, self.bot_activity_type)
            embed.add_field(
                name=t(ctx, "ACTIVITY_DEFAULT_LABEL"),
                value=f"`{def_type}: {self.bot_activity_text}`",
                inline=False,
            )
        return embed

    def _build_activity_add_embed(self, ctx, act_type: int, *, last_added: dict | None = None) -> discord.Embed:
        color = self.get_embed_color(None)
        total = len(self.bot_activity_list)
        embed = SafeEmbed(
            title=f"⚙️ {t(ctx, 'ACTIVITY_ADD_TITLE')} ({total}/10)",
            description=t(ctx, "ACTIVITY_ADD_DESC"),
            color=color,
        )
        type_name = self._activity_type_name(ctx, act_type)
        embed.add_field(name=t(ctx, "ACTIVITY_ADD_CURRENT_TYPE"), value=f"`{type_name}`", inline=False)
        if last_added:
            tname = self._activity_type_name(ctx, last_added.get("type", 2))
            embed.add_field(name="✅", value=f"`{tname}: {last_added['text']}`", inline=False)
        return embed

    def _build_activity_edit_embed(self, ctx, item: dict, act_type: int) -> discord.Embed:
        color = self.get_embed_color(None)
        embed = SafeEmbed(
            title=f"⚙️ {t(ctx, 'ACTIVITY_EDIT_TEXT_TITLE')}",
            description=t(ctx, "ACTIVITY_EDIT_DESC"),
            color=color,
        )
        type_name = self._activity_type_name(ctx, act_type)
        embed.add_field(name=t(ctx, "ACTIVITY_EDIT_CURRENT"), value=f"`{type_name}: {item['text']}`", inline=False)
        return embed

    def _build_layout_detail_embed(self, ctx, guild_id: int, view_type: str) -> discord.Embed:
        from core.music_handlers import _MP_BUTTON_LABELS, _QUEUE_BUTTON_LABELS
        labels = _MP_BUTTON_LABELS if view_type == "mp" else _QUEUE_BUTTON_LABELS
        layout = self.get_mp_layout(guild_id) if view_type == "mp" else self.get_queue_layout(guild_id)
        title_key = "EMBED_LAYOUT_MP" if view_type == "mp" else "EMBED_LAYOUT_QUEUE"
        lines = []
        refresh_disabled = False
        for idx, key in enumerate(labels, 1):
            cfg = layout.get(key, {})
            enabled = cfg.get("enabled", True)
            icon = "✅" if enabled else "❌"
            r, c = cfg.get("row", 0) + 1, cfg.get("col", 0) + 1
            lbl = t(ctx, labels[key])
            lines.append(f"{icon} `{idx}.` **{lbl}** - {t(ctx, 'ABBR_ROW')}: {r} {t(ctx, 'ABBR_COL')}: {c}")
            if key == "refresh" and not enabled:
                refresh_disabled = True
        cm = self.is_compact(guild_id) if view_type == "mp" else self.is_queue_button_compact(guild_id)
        compact = t(ctx, "STATE_ON") if cm else t(ctx, "STATE_OFF")
        lines.append(f"\n**{t(ctx, 'ABBR_COMPACT')}:** {compact}")
        if refresh_disabled:
            lines.append(f"\n⚠️ *{t(ctx, 'EMBED_LAYOUT_REFRESH_WARNING')}*")
        embed = SafeEmbed(
            title=f"⚙️ {t(ctx, title_key)}",
            description="\n".join(lines),
            color=self.get_embed_color(guild_id),
        )
        embed.set_footer(text=f"{t(ctx, 'ABBR_ROW')}: {t(ctx, 'ABBR_ROW_FULL')} {t(ctx, 'ABBR_COL')}: {t(ctx, 'ABBR_COL_FULL')}")
        return embed

    def _build_mp_display_detail_embed(self, ctx, guild_id: int) -> discord.Embed:
        from core.music_handlers import _MP_FIELD_LABELS, _MP_REORDERABLE_FIELDS
        fields = self.get_mp_fields(guild_id)
        reorderable = {k: v for k, v in fields.items() if k in _MP_REORDERABLE_FIELDS}
        sorted_reorderable = sorted(reorderable.items(), key=lambda x: x[1].get("order", 99))
        lines = []
        for idx, (key, cfg) in enumerate(sorted_reorderable, 1):
            enabled = cfg.get("enabled", True)
            icon = "✅" if enabled else "❌"
            lbl = t(ctx, _MP_FIELD_LABELS.get(key, key))
            lines.append(f"{icon} `{idx}.` **{lbl}**")
        extra_keys = [k for k in fields if k not in _MP_REORDERABLE_FIELDS]
        for key in extra_keys:
            cfg = fields[key]
            enabled = cfg.get("enabled", True)
            icon = "✅" if enabled else "❌"
            lbl = t(ctx, _MP_FIELD_LABELS.get(key, key))
            lines.append(f"{icon} **{lbl}**")
        desc = t(ctx, "SETTINGS_DESC_MP_DISPLAY")
        desc += "\n\n" + "\n".join(lines)
        return SafeEmbed(
            title=f"⚙️ {t(ctx, 'SETTINGS_SHOW_MP_DISPLAY')}",
            description=desc,
            color=self.get_embed_color(guild_id),
        )

    def _build_embed_views_main_embed(self, ctx, guild_id: int) -> discord.Embed:
        mp_l = self.get_mp_layout(guild_id)
        q_l = self.get_queue_layout(guild_id)
        mp_on = sum(1 for v in mp_l.values() if v.get("enabled", True))
        q_on = sum(1 for v in q_l.values() if v.get("enabled", True))
        mp_cm = t(ctx, "STATE_ON") if self.is_compact(guild_id) else t(ctx, "STATE_OFF")
        q_cm = t(ctx, "STATE_ON") if self.is_queue_button_compact(guild_id) else t(ctx, "STATE_OFF")
        pp = self.guild_queue_per_page.get(guild_id, 10)
        qc = self.guild_queue_compact.get(guild_id, True)
        qd_mode = t(ctx, "SETTINGS_QD_NORMAL") if qc else t(ctx, "SETTINGS_QD_SPACIOUS")
        _VR_LABELS = {0: "VIEW_RESTRICT_DISABLED", 1: "VIEW_RESTRICT_NON_DJ", 2: "VIEW_RESTRICT_DJ_USER", 3: "VIEW_RESTRICT_ALL"}
        ch = self.guild_view_channels.get(guild_id)
        lvl = self.guild_view_restricts.get(guild_id, 0)
        ch_str = f"<#{ch}>" if ch else t(ctx, "SETTINGS_SHOW_NONE")
        lvl_str = t(ctx, _VR_LABELS.get(lvl, "VIEW_RESTRICT_DISABLED"))
        ec = self.guild_embed_colors.get(guild_id)
        ec_str = f"#{ec:06X}" if ec is not None else t(ctx, "COLOR_DEFAULT_LABEL")
        desc = t(ctx, "SETTINGS_DESC_EMBED_VIEW_SETTINGS")
        mp_f = self.get_mp_fields(guild_id)
        mf_on = sum(1 for v in mp_f.values() if v.get("enabled", True))
        desc += f"\n\n**{t(ctx, 'SETTINGS_SHOW_EMBED_COLOR')}:** > {ec_str}"
        desc += f"\n**{t(ctx, 'EMBED_VIEWS_MP_BUTTONS')}:** > {mp_on}/{len(mp_l)}, {t(ctx, 'ABBR_COMPACT')}: {mp_cm}"
        desc += f"\n**{t(ctx, 'SETTINGS_SHOW_MP_DISPLAY')}:** > {mf_on}/{len(mp_f)}"
        desc += f"\n**{t(ctx, 'EMBED_VIEWS_QUEUE_BUTTONS')}:** > {q_on}/{len(q_l)}, {t(ctx, 'ABBR_COMPACT')}: {q_cm}"
        desc += f"\n**{t(ctx, 'SETTINGS_SHOW_QUEUE_DISPLAY')}:** > {qd_mode}, {pp}{t(ctx, 'ABBR_PER_PAGE')}"
        desc += f"\n**{t(ctx, 'SETTINGS_SHOW_VIEW_RESTRICT')}:** > {ch_str} / {lvl_str}"
        return SafeEmbed(
            title=f"⚙️ {t(ctx, 'SETTINGS_SHOW_EMBED_VIEW_SETTINGS')}",
            description=desc,
            color=self.get_embed_color(guild_id),
        )

    _PAUSE_PERM_LABELS = {
        "everyone": "PAUSE_PERM_EVERYONE",
        "requester_dj": "PERM_REQUESTER_DJ",
        "dj": "PERM_DJ_ADMIN",
        "admin": "PERM_ADMIN_ONLY",
        "owner": "PERM_OWNER_ONLY",
    }

    def _pause_control_desc(self, ctx, guild_id: int) -> str:
        perm = self.guild_pause_permission.get(guild_id, "requester_dj")
        timeout = self.guild_pause_timeout.get(guild_id, 900) // 60
        behavior = self.guild_pause_timeout_behavior.get(guild_id, "leave")
        lbl = t(ctx, self._PAUSE_PERM_LABELS.get(perm, "PERM_REQUESTER_DJ"))
        b_lbl = t(ctx, self._PAUSE_BEHAVIOR_LABELS.get(behavior, "PAUSE_BEHAVIOR_LEAVE"))
        return f"{lbl}, {timeout}{t(ctx, 'ABBR_MINUTES')}, {b_lbl}"

    _RADIO_PERM_LABELS_MAP = {
        "everyone": "PERM_EVERYONE",
        "dj": "PERM_DJ_ADMIN",
        "admin": "PERM_ADMIN_ONLY",
        "owner": "PERM_OWNER_ONLY",
    }

    _RADIO_EDIT_PERM_LABELS_MAP = {
        "dj": "PERM_DJ_ADMIN",
        "admin": "PERM_ADMIN_ONLY",
        "owner": "PERM_OWNER_ONLY",
    }

    def _radio_settings_desc(self, ctx, guild_id: int) -> str:
        perm = self.guild_radio_permissions.get(guild_id, "dj")
        cd = self.guild_radio_cooldowns.get(guild_id, 3)
        lbl = t(ctx, self._RADIO_PERM_LABELS_MAP.get(perm, "PERM_DJ_ADMIN"))
        return f"{lbl}, {cd}{t(ctx, 'ABBR_MINUTES')}"

    def _build_radio_settings_embed(self, ctx, guild_id: int) -> discord.Embed:
        perm = self.guild_radio_permissions.get(guild_id, "dj")
        edit_perm = self.guild_radio_edit_permissions.get(guild_id, "dj")
        cd = self.guild_radio_cooldowns.get(guild_id, 3)
        perm_label = t(ctx, self._RADIO_PERM_LABELS_MAP.get(perm, "PERM_DJ_ADMIN"))
        edit_perm_label = t(ctx, self._RADIO_EDIT_PERM_LABELS_MAP.get(edit_perm, "PERM_DJ_ADMIN"))
        desc = t(ctx, "SETTINGS_DESC_RADIO")
        desc += f"\n\n**{t(ctx, 'RADIO_PERM_PLACEHOLDER')}:** {perm_label}"
        desc += f"\n**{t(ctx, 'RADIO_EDIT_PERM_PLACEHOLDER')}:** {edit_perm_label}"
        desc += f"\n**{t(ctx, 'RADIO_COOLDOWN_BUTTON')}:** {cd}{t(ctx, 'ABBR_MINUTES')}"
        desc += f"\n> {t(ctx, 'RADIO_COOLDOWN_DESC')}"
        return SafeEmbed(
            title=f"⚙️ {t(ctx, 'SETTINGS_SHOW_RADIO')}",
            description=desc,
            color=self.get_embed_color(guild_id),
        )

    _FORCE_PERM_LABELS = {
        "dj": "PERM_DJ_ADMIN",
        "admin": "PERM_ADMIN_ONLY",
        "owner": "PERM_OWNER_ONLY",
    }

    def _force_play_desc(self, ctx, guild_id: int) -> str:
        perm = self.guild_force_play_permission.get(guild_id, "dj")
        perm_lbl = t(ctx, self._FORCE_PERM_LABELS.get(perm, "PERM_DJ_ADMIN"))
        radio = self.guild_force_radio.get(guild_id, "disabled")
        radio_lbl = t(ctx, "FORCE_RADIO_ENABLED" if radio == "enabled" else "FORCE_RADIO_DISABLED_LABEL")
        return f"{perm_lbl}, {t(ctx, 'ABBR_RADIO')}: {radio_lbl}"

    def _build_force_play_embed(self, ctx, guild_id: int) -> discord.Embed:
        perm = self.guild_force_play_permission.get(guild_id, "dj")
        perm_label = t(ctx, self._FORCE_PERM_LABELS.get(perm, "PERM_DJ_ADMIN"))
        radio = self.guild_force_radio.get(guild_id, "disabled")
        radio_label = t(ctx, "FORCE_RADIO_ENABLED" if radio == "enabled" else "FORCE_RADIO_DISABLED_LABEL")
        desc = t(ctx, "SETTINGS_DESC_FORCE_PLAY")
        desc += f"\n\n**{t(ctx, 'FORCE_PERM_PLACEHOLDER')}:** {perm_label}"
        desc += f"\n**{t(ctx, 'FORCE_RADIO_BUTTON')}:** {radio_label}"
        return SafeEmbed(
            title=f"⚙️ {t(ctx, 'SETTINGS_SHOW_FORCE_PLAY')}",
            description=desc,
            color=self.get_embed_color(guild_id),
        )

    _PAUSE_BEHAVIOR_LABELS = {
        "leave": "PAUSE_BEHAVIOR_LEAVE",
        "continue": "PAUSE_BEHAVIOR_CONTINUE",
        "skip": "PAUSE_BEHAVIOR_SKIP",
    }

    _SEEK_PERM_LABELS = {
        "everyone": "PERM_EVERYONE",
        "requester_dj": "PERM_REQUESTER_DJ",
        "dj": "PERM_DJ_ADMIN",
        "admin": "PERM_ADMIN_ONLY",
        "owner": "PERM_OWNER_ONLY",
    }

    def _build_pause_control_embed(self, ctx, guild_id: int) -> discord.Embed:
        perm = self.guild_pause_permission.get(guild_id, "requester_dj")
        timeout = self.guild_pause_timeout.get(guild_id, 900) // 60
        behavior = self.guild_pause_timeout_behavior.get(guild_id, "leave")
        perm_label = t(ctx, self._PAUSE_PERM_LABELS.get(perm, "PERM_REQUESTER_DJ"))
        behavior_label = t(ctx, self._PAUSE_BEHAVIOR_LABELS.get(behavior, "PAUSE_BEHAVIOR_LEAVE"))
        desc = t(ctx, "SETTINGS_DESC_PAUSE_CONTROL")
        desc += f"\n\n**{t(ctx, 'PAUSE_PERM_PLACEHOLDER')}:** {perm_label}"
        desc += f"\n**{t(ctx, 'PAUSE_TIMEOUT_BUTTON')}:** {timeout}{t(ctx, 'ABBR_MINUTES')}"
        desc += f"\n**{t(ctx, 'PAUSE_BEHAVIOR_PLACEHOLDER')}:** {behavior_label}"
        # Seek settings
        seek_perm = self.guild_seek_permission.get(guild_id, "requester_dj")
        seek_perm_label = t(ctx, self._SEEK_PERM_LABELS.get(seek_perm, "PERM_REQUESTER_DJ"))
        max_seeks = self.guild_max_seeks_per_track.get(guild_id, 3)
        max_seeks_dj = self.guild_max_seeks_dj.get(guild_id, 0)
        _dis = t(ctx, "DISABLED")
        desc += f"\n**{t(ctx, 'SEEK_PERM_PLACEHOLDER')}:** {seek_perm_label}"
        desc += f"\n**{t(ctx, 'SEEK_LIMIT_BUTTON')}:** {max_seeks if max_seeks > 0 else _dis}"
        desc += f"\n**{t(ctx, 'SEEK_LIMIT_DJ_BUTTON')}:** {max_seeks_dj if max_seeks_dj > 0 else _dis}"
        return SafeEmbed(
            title=f"⚙️ {t(ctx, 'SETTINGS_SHOW_PAUSE_CONTROL')}",
            description=desc,
            color=self.get_embed_color(guild_id),
        )

    _JOIN_RESTRICT_LABELS = {
        "none": "JOIN_RESTRICT_NONE",
        "users": "JOIN_RESTRICT_USERS",
        "dj": "JOIN_RESTRICT_DJ",
        "admin": "JOIN_RESTRICT_ADMIN",
    }

    def _bot_connection_desc(self, ctx, guild_id: int) -> str:
        idle = self.guild_idle_disconnect.get(guild_id, 180)
        idle_str = f"{idle // 60}{t(ctx, 'ABBR_MINUTES')}" if idle > 0 else t(ctx, "DISABLED")
        level = self.guild_join_restrict_level.get(guild_id, "none")
        lvl_str = t(ctx, self._JOIN_RESTRICT_LABELS.get(level, "JOIN_RESTRICT_NONE"))
        ch_count = len(self.guild_join_restrict_channels.get(guild_id, set()))
        if ch_count:
            return f"{idle_str}, {lvl_str} ({ch_count}{t(ctx, 'ABBR_CH')})"
        return f"{idle_str}, {lvl_str}"

    def _build_bot_connection_embed(self, ctx, guild_id: int) -> discord.Embed:
        idle = self.guild_idle_disconnect.get(guild_id, 180)
        idle_str = f"{idle // 60}{t(ctx, 'ABBR_MINUTES')}" if idle > 0 else t(ctx, "DISABLED")
        level = self.guild_join_restrict_level.get(guild_id, "none")
        lvl_str = t(ctx, self._JOIN_RESTRICT_LABELS.get(level, "JOIN_RESTRICT_NONE"))
        channels = self.guild_join_restrict_channels.get(guild_id, set())
        desc = t(ctx, "SETTINGS_DESC_BOT_CONNECTION")
        desc += f"\n\n> **{t(ctx, 'BOT_CONN_IDLE_TIMEOUT')}:** {idle_str}"
        desc += f"\n> **{t(ctx, 'BOT_CONN_JOIN_RESTRICT')}:** {lvl_str}"
        if channels:
            ch_list = ", ".join(f"<#{cid}>" for cid in channels)
            desc += f"\n> **{t(ctx, 'BOT_CONN_CHANNELS')}:** {ch_list}"
        return SafeEmbed(
            title=f"⚙️ {t(ctx, 'SETTINGS_SHOW_BOT_CONNECTION')}",
            description=desc,
            color=self.get_embed_color(guild_id),
        )

    _LIVE_PERM_LABELS = {
        "owner": "PERM_OWNER_ONLY",
        "admin": "PERM_ADMIN_ONLY",
        "dj": "PERM_DJ_ADMIN",
        "everyone": "PERM_EVERYONE",
    }

    def _live_playback_desc(self, ctx, guild_id: int) -> str:
        enabled = self.guild_live_enabled.get(guild_id, 0)
        return t(ctx, "ENABLED") if enabled else t(ctx, "DISABLED")

    def _build_live_playback_embed(self, ctx, guild_id: int) -> discord.Embed:
        enabled = self.guild_live_enabled.get(guild_id, 0)
        status = t(ctx, "ENABLED") if enabled else t(ctx, "DISABLED")
        perm = self.guild_live_permission.get(guild_id, "admin")
        perm_str = t(ctx, self._LIVE_PERM_LABELS.get(perm, "PERM_ADMIN_ONLY"))
        max_h = self.guild_live_max_hours.get(guild_id, 1)
        max_str = f"{max_h}{t(ctx, 'ABBR_HOURS')}" if max_h > 0 else t(ctx, "DISABLED")
        desc = t(ctx, "SETTINGS_DESC_LIVE_PLAYBACK")
        desc += f"\n\n> **{t(ctx, 'LIVE_STATUS')}:** {status}"
        desc += f"\n> **{t(ctx, 'LIVE_PERMISSION')}:** {perm_str}"
        desc += f"\n> **{t(ctx, 'LIVE_MAX_HOURS')}:** {max_str}"
        return SafeEmbed(
            title=f"⚙️ {t(ctx, 'SETTINGS_SHOW_LIVE_PLAYBACK')}",
            description=desc,
            color=self.get_embed_color(guild_id),
        )

    def _performance_desc(self, ctx, guild_id: int) -> str:
        pf = t(ctx, "ENABLED") if self._prefetch else t(ctx, "DISABLED")
        sp = t(ctx, "ENABLED") if self._safe_prefetch else t(ctx, "DISABLED")
        return f"{t(ctx, 'SETTINGS_SHOW_PREFETCH')}: {pf} / {t(ctx, 'SETTINGS_SHOW_SAFE_PREFETCH')}: {sp} / {t(ctx, 'SETTINGS_SHOW_MAX_WORKERS')}: {self._max_workers}"

    def _build_performance_embed(self, ctx, guild_id: int) -> discord.Embed:
        enabled = self._prefetch
        pf_status = t(ctx, "ENABLED") if enabled else t(ctx, "DISABLED")
        sp_status = t(ctx, "ENABLED") if self._safe_prefetch else t(ctx, "DISABLED")
        desc = t(ctx, "SETTINGS_DESC_PERFORMANCE")
        desc += f"\n\n**{t(ctx, 'SETTINGS_SHOW_PREFETCH')}:** {pf_status}"
        desc += f"\n**{t(ctx, 'SETTINGS_SHOW_SAFE_PREFETCH')}:** {sp_status}"
        desc += f"\n**{t(ctx, 'SETTINGS_SHOW_MAX_WORKERS')}:** {self._max_workers}"
        return SafeEmbed(
            title=f"⚙️ {t(ctx, 'SETTINGS_SHOW_PERFORMANCE')}",
            description=desc,
            color=self.get_embed_color(guild_id),
        )

    def _build_limits_main_embed(self, ctx, guild_id: int, *, is_app_owner: bool = False) -> discord.Embed:
        mp = self.guild_max_playlists.get(guild_id, 15)
        mp_str = str(mp) if mp > 0 else t(ctx, "DISABLED")
        mh = self.guild_max_history.get(guild_id, 50)
        mh_str = str(mh) if mh > 0 else t(ctx, "DISABLED")
        u_lim = self.guild_track_limit_users.get(guild_id, 0)
        d_lim = self.guild_track_limit_dj.get(guild_id, 0)
        a_lim = self.guild_track_limit_admin.get(guild_id, 0)
        any_track_lim = u_lim or d_lim or a_lim
        eu = len(self.excluded_users.get(guild_id, set()))
        er = len(self.excluded_roles.get(guild_id, set()))
        if eu == 0 and er == 0:
            lu_str = t(ctx, "DISABLED")
        else:
            parts = []
            if eu:
                parts.append(f"{eu} {t(ctx, 'ABBR_USERS')}")
            if er:
                parts.append(f"{er} {t(ctx, 'ABBR_ROLES')}")
            lu_str = ", ".join(parts)
        desc = t(ctx, "SETTINGS_DESC_LIMITS")
        _dis = t(ctx, "DISABLED")
        if any_track_lim:
            tl_parts = []
            if u_lim: tl_parts.append(f"{t(ctx, 'TRACK_LIMIT_GROUP_USERS')}: {u_lim}")
            if d_lim: tl_parts.append(f"{t(ctx, 'TRACK_LIMIT_GROUP_DJ')}: {d_lim}")
            if a_lim: tl_parts.append(f"{t(ctx, 'TRACK_LIMIT_GROUP_ADMIN')}: {a_lim}")
            mu_str = " / ".join(tl_parts)
        else:
            mu_str = _dis
        desc += f"\n\n**{t(ctx, 'SETTINGS_SHOW_MAX_PLAYLISTS')}:** > {mp_str}"
        desc += f"\n**{t(ctx, 'SETTINGS_SHOW_MAX_HISTORY')}:** > {mh_str}"
        desc += f"\n**{t(ctx, 'SETTINGS_SHOW_TRACK_LIMIT')}:** > {mu_str}"
        desc += f"\n**{t(ctx, 'SETTINGS_SHOW_LIMIT_USAGE')}:** > {lu_str}"
        desc += f"\n**{t(ctx, 'SETTINGS_SHOW_PAUSE_CONTROL')}:** > {self._pause_control_desc(ctx, guild_id)}"
        desc += f"\n**{t(ctx, 'SETTINGS_SHOW_RADIO')}:** > {self._radio_settings_desc(ctx, guild_id)}"
        desc += f"\n**{t(ctx, 'SETTINGS_SHOW_FORCE_PLAY')}:** > {self._force_play_desc(ctx, guild_id)}"
        desc += f"\n**{t(ctx, 'SETTINGS_SHOW_BOT_CONNECTION')}:** > {self._bot_connection_desc(ctx, guild_id)}"
        desc += f"\n**{t(ctx, 'SETTINGS_SHOW_LIVE_PLAYBACK')}:** > {self._live_playback_desc(ctx, guild_id)}"
        if is_app_owner:
            desc += f"\n**{t(ctx, 'SETTINGS_SHOW_PERFORMANCE')}:** > {self._performance_desc(ctx, guild_id)}"
        return SafeEmbed(
            title=f"⚙️ {t(ctx, 'SETTINGS_SHOW_LIMITS')}",
            description=desc,
            color=self.get_embed_color(guild_id),
        )

    @app_commands.command(**l_cmd("CMD_NAME_SETTINGS", "CMD_DESC_SETTINGS"))
    async def settings_cmd(self, interaction: discord.Interaction):
        if not await self._check_cooldown(interaction, "settings", 5): return
        _is_app_owner_early = await self.bot.is_owner(interaction.user)
        if not _is_app_owner_early and not self.has_admin_privilege(interaction.guild_id, interaction.user):
            return await self.send_reply(
                interaction, t(interaction, "NOT_MANAGE_ADMIN"), ephemeral=True,
                delete_after=self._resolve_delete_after(interaction.guild_id))
        await interaction.response.defer(ephemeral=True)
        cog = self
        guild_id = interaction.guild.id
        user_id = interaction.user.id
        ctx = interaction

        # Kill old settings view for this user in this guild
        old_entry = self.active_settings.pop((guild_id, user_id), None)
        if old_entry:
            old_msg, old_view = old_entry if isinstance(old_entry, tuple) else (old_entry, None)
            if old_view:
                old_view.stop()
            if old_msg:
                try:
                    await old_msg.delete()
                except discord.HTTPException:
                    pass

        per_page = 25
        is_app_owner = _is_app_owner_early
        is_guild_owner = interaction.user.id == interaction.guild.owner_id
        is_guild_admin = self.has_admin_privilege(interaction.guild_id, interaction.user)
        _effective_owner = is_guild_owner or (is_app_owner and is_guild_admin)

        # --- Inner components ---

        class EmbedViewsBackButton(discord.ui.Button):
            def __init__(self, view_ref, *, row=2):
                super().__init__(style=discord.ButtonStyle.secondary, emoji="◀️", label=t(ctx, "SETTINGS_BACK"), row=row)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                view = self.view_ref
                view._rebuild_embed_views_main()
                await btn_interaction.response.edit_message(
                    embed=cog._build_embed_views_main_embed(btn_interaction, guild_id), view=view)

        class LimitsBackButton(discord.ui.Button):
            def __init__(self, view_ref, *, row=2):
                super().__init__(style=discord.ButtonStyle.secondary, emoji="◀️", label=t(ctx, "SETTINGS_BACK"), row=row)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                view = self.view_ref
                view._rebuild_limits_main()
                await btn_interaction.response.edit_message(
                    embed=cog._build_limits_main_embed(btn_interaction, guild_id, is_app_owner=is_app_owner), view=view)

        class LimitsSelect(discord.ui.Select):
            def __init__(self, view_ref):
                mp = cog.guild_max_playlists.get(guild_id, 15)
                mp_desc = str(mp) if mp > 0 else t(ctx, "DISABLED")
                mh = cog.guild_max_history.get(guild_id, 50)
                mh_desc = str(mh) if mh > 0 else t(ctx, "DISABLED")
                u_lim = cog.guild_track_limit_users.get(guild_id, 0)
                d_lim = cog.guild_track_limit_dj.get(guild_id, 0)
                a_lim = cog.guild_track_limit_admin.get(guild_id, 0)
                if u_lim or d_lim or a_lim:
                    _parts = []
                    if u_lim: _parts.append(f"{t(ctx, 'ABBR_U')}:{u_lim}")
                    if d_lim: _parts.append(f"{t(ctx, 'ABBR_DJ')}:{d_lim}")
                    if a_lim: _parts.append(f"{t(ctx, 'ABBR_A')}:{a_lim}")
                    mu_desc = " / ".join(_parts)
                else:
                    mu_desc = t(ctx, "DISABLED")
                eu = len(cog.excluded_users.get(guild_id, set()))
                er = len(cog.excluded_roles.get(guild_id, set()))
                lu_desc = t(ctx, "DISABLED") if eu == 0 and er == 0 else f"{eu} {t(ctx, 'ABBR_USERS')}, {er} {t(ctx, 'ABBR_ROLES')}"
                options = [
                    discord.SelectOption(label=t(ctx, "SETTINGS_SHOW_MAX_PLAYLISTS"), value="max_playlists",
                                         description=mp_desc[:100]),
                    discord.SelectOption(label=t(ctx, "SETTINGS_SHOW_MAX_HISTORY"), value="max_history",
                                         description=mh_desc[:100]),
                    discord.SelectOption(label=t(ctx, "SETTINGS_SHOW_TRACK_LIMIT"), value="max_user_tracks",
                                         description=mu_desc[:100]),
                ]
                options.append(discord.SelectOption(label=t(ctx, "SETTINGS_SHOW_LIMIT_USAGE"), value="limit_usage",
                                         description=lu_desc[:100]))
                options += [
                    discord.SelectOption(label=t(ctx, "SETTINGS_SHOW_PAUSE_CONTROL"), value="pause_control",
                                         description=cog._pause_control_desc(ctx, guild_id)[:100]),
                    discord.SelectOption(label=t(ctx, "SETTINGS_SHOW_RADIO"), value="radio_settings",
                                         description=cog._radio_settings_desc(ctx, guild_id)[:100]),
                    discord.SelectOption(label=t(ctx, "SETTINGS_SHOW_FORCE_PLAY"), value="force_play",
                                         description=cog._force_play_desc(ctx, guild_id)[:100]),
                    discord.SelectOption(label=t(ctx, "SETTINGS_SHOW_BOT_CONNECTION"), value="bot_connection",
                                         description=cog._bot_connection_desc(ctx, guild_id)[:100]),
                ]
                options.append(discord.SelectOption(
                    label=t(ctx, "SETTINGS_SHOW_LIVE_PLAYBACK"), value="live_playback",
                    description=cog._live_playback_desc(ctx, guild_id)[:100]))
                if is_app_owner:
                    options.append(discord.SelectOption(
                        label=t(ctx, "SETTINGS_SHOW_PERFORMANCE"), value="performance",
                        description=cog._performance_desc(ctx, guild_id)[:100]))

                super().__init__(placeholder=t(ctx, "LIMITS_SELECT_PLACEHOLDER"), options=options, row=0)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                chosen = self.values[0]
                if chosen == "pause_control":
                    self.view_ref._rebuild_pause_control()
                    embed = cog._build_pause_control_embed(btn_interaction, guild_id)
                elif chosen == "radio_settings":
                    self.view_ref._rebuild_radio_settings()
                    embed = cog._build_radio_settings_embed(btn_interaction, guild_id)
                elif chosen == "force_play":
                    self.view_ref._rebuild_force_play()
                    embed = cog._build_force_play_embed(btn_interaction, guild_id)
                elif chosen == "bot_connection":
                    self.view_ref._rebuild_bot_connection()
                    embed = cog._build_bot_connection_embed(btn_interaction, guild_id)
                elif chosen == "live_playback":
                    self.view_ref._rebuild_live_playback()
                    embed = cog._build_live_playback_embed(btn_interaction, guild_id)
                elif chosen == "performance":
                    self.view_ref._rebuild_performance()
                    embed = cog._build_performance_embed(btn_interaction, guild_id)
                else:
                    self.view_ref._rebuild_limits_sub(chosen)
                    embed = cog._build_detail_embed(btn_interaction, guild_id, chosen)
                await btn_interaction.response.edit_message(embed=embed, view=self.view_ref)

        class TrackLimitGroupButton(discord.ui.Button):
            def __init__(self, view_ref, group, *, row=0):
                _GROUP_LABELS = {"users": "TRACK_LIMIT_GROUP_USERS", "dj": "TRACK_LIMIT_GROUP_DJ", "admin": "TRACK_LIMIT_GROUP_ADMIN"}
                _GROUP_DICTS = {"users": cog.guild_track_limit_users, "dj": cog.guild_track_limit_dj, "admin": cog.guild_track_limit_admin}
                val = _GROUP_DICTS[group].get(guild_id, 0)
                label_text = f"{t(ctx, _GROUP_LABELS[group])}: {val}" if val > 0 else f"{t(ctx, _GROUP_LABELS[group])}: {t(ctx, 'DISABLED')}"
                super().__init__(style=discord.ButtonStyle.secondary, label=label_text, row=row)
                self.view_ref = view_ref
                self._group = group

            async def callback(self, btn_interaction: discord.Interaction):
                if self._group == "admin" and not _effective_owner:
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "ONLY_OWNER_CAN_SET"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                await btn_interaction.response.send_modal(TrackLimitGroupModal(self.view_ref, self._group))

        class TrackLimitGroupModal(discord.ui.Modal):
            def __init__(self, view_ref, group):
                _GROUP_LABELS = {"users": "TRACK_LIMIT_GROUP_USERS", "dj": "TRACK_LIMIT_GROUP_DJ", "admin": "TRACK_LIMIT_GROUP_ADMIN"}
                _GROUP_DICTS = {"users": cog.guild_track_limit_users, "dj": cog.guild_track_limit_dj, "admin": cog.guild_track_limit_admin}
                super().__init__(title=t(ctx, _GROUP_LABELS[group]))
                self.view_ref = view_ref
                self._group = group
                current = _GROUP_DICTS[group].get(guild_id, 0)
                self.num_input = discord.ui.TextInput(
                    label=t(ctx, "SETTINGS_MAX_HISTORY_LABEL", min=TRACK_LIMIT_RANGE[0], max=TRACK_LIMIT_RANGE[1]),
                    default=str(current), max_length=5, required=True,
                )
                self.add_item(self.num_input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                try:
                    val = int(self.num_input.value.strip())
                except ValueError:
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                if not (TRACK_LIMIT_RANGE[0] <= val <= TRACK_LIMIT_RANGE[1]):
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT_RANGE", min=TRACK_LIMIT_RANGE[0], max=TRACK_LIMIT_RANGE[1]), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                _GROUP_DICTS = {"users": (cog.guild_track_limit_users, db.set_track_limit_users),
                                "dj": (cog.guild_track_limit_dj, db.set_track_limit_dj),
                                "admin": (cog.guild_track_limit_admin, db.set_track_limit_admin)}
                d, setter = _GROUP_DICTS[self._group]
                d[guild_id] = val
                await setter(guild_id, val)
                self.view_ref._rebuild_limits_sub("max_user_tracks")
                await modal_interaction.response.edit_message(
                    embed=cog._build_detail_embed(modal_interaction, guild_id, "max_user_tracks"),
                    view=self.view_ref)

        class TrackLimitClearAllButton(discord.ui.Button):
            def __init__(self, view_ref, *, row=1):
                super().__init__(style=discord.ButtonStyle.danger, label=t(ctx, "MANAGE_PERMS_CLEAR_ALL"), row=row)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                is_owner = _effective_owner
                admin_val = cog.guild_track_limit_admin.get(guild_id, 0)
                cog.guild_track_limit_users[guild_id] = 0
                await db.set_track_limit_users(guild_id, 0)
                cog.guild_track_limit_dj[guild_id] = 0
                await db.set_track_limit_dj(guild_id, 0)
                if is_owner:
                    cog.guild_track_limit_admin[guild_id] = 0
                    await db.set_track_limit_admin(guild_id, 0)
                self.view_ref._rebuild_limits_sub("max_user_tracks")
                embed = cog._build_detail_embed(btn_interaction, guild_id, "max_user_tracks")
                if not is_owner and admin_val > 0:
                    await btn_interaction.response.edit_message(embed=embed, view=self.view_ref)
                    _da = cog._resolve_delete_after(guild_id)
                    _msg = await btn_interaction.followup.send(
                        t(btn_interaction, "TRACK_LIMIT_CLEAR_ADMIN_SKIPPED"), ephemeral=True, wait=True)
                    if _da and _msg:
                        await _msg.delete(delay=_da)
                else:
                    await btn_interaction.response.edit_message(embed=embed, view=self.view_ref)

        QUEUE_PL_LIMIT_RANGE = (100, 10000)

        class QueueLimitButton(discord.ui.Button):
            def __init__(self, view_ref, *, row=2):
                val = cog.guild_queue_limit.get(guild_id, MAX_QUEUE)
                super().__init__(style=discord.ButtonStyle.secondary,
                                 label=f"{t(ctx, 'QUEUE_LIMIT_BUTTON')}: {val}", row=row)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                await btn_interaction.response.send_modal(QueueLimitModal(self.view_ref))

        class QueueLimitModal(discord.ui.Modal):
            def __init__(self, view_ref):
                super().__init__(title=t(ctx, "QUEUE_LIMIT_TITLE"))
                self.view_ref = view_ref
                current = cog.guild_queue_limit.get(guild_id, MAX_QUEUE)
                self.input = discord.ui.TextInput(
                    label=t(ctx, "QUEUE_LIMIT_LABEL", min=QUEUE_PL_LIMIT_RANGE[0], max=QUEUE_PL_LIMIT_RANGE[1]),
                    default=str(current), max_length=5, required=True,
                )
                self.add_item(self.input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                try:
                    val = int(self.input.value.strip())
                except ValueError:
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT"), ephemeral=True)
                if not (QUEUE_PL_LIMIT_RANGE[0] <= val <= QUEUE_PL_LIMIT_RANGE[1]):
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT_RANGE", min=QUEUE_PL_LIMIT_RANGE[0], max=QUEUE_PL_LIMIT_RANGE[1]),
                        ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                cog.guild_queue_limit[guild_id] = val
                await db.set_queue_limit(guild_id, val)
                self.view_ref._rebuild_limits_sub("max_user_tracks")
                await modal_interaction.response.edit_message(
                    embed=cog._build_detail_embed(modal_interaction, guild_id, "max_user_tracks"),
                    view=self.view_ref)

        class PlaylistTrackLimitButton(discord.ui.Button):
            def __init__(self, view_ref, *, row=2):
                val = cog.guild_playlist_track_limit.get(guild_id, MAX_PLAYLIST_TRACKS)
                super().__init__(style=discord.ButtonStyle.secondary,
                                 label=f"{t(ctx, 'PL_TRACK_LIMIT_BUTTON')}: {val}", row=row)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                await btn_interaction.response.send_modal(PlaylistTrackLimitModal(self.view_ref))

        class PlaylistTrackLimitModal(discord.ui.Modal):
            def __init__(self, view_ref):
                super().__init__(title=t(ctx, "PL_TRACK_LIMIT_BUTTON"))
                self.view_ref = view_ref
                current = cog.guild_playlist_track_limit.get(guild_id, MAX_PLAYLIST_TRACKS)
                self.input = discord.ui.TextInput(
                    label=t(ctx, "PL_TRACK_LIMIT_LABEL", min=QUEUE_PL_LIMIT_RANGE[0], max=QUEUE_PL_LIMIT_RANGE[1]),
                    default=str(current), max_length=5, required=True,
                )
                self.add_item(self.input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                try:
                    val = int(self.input.value.strip())
                except ValueError:
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT"), ephemeral=True)
                if not (QUEUE_PL_LIMIT_RANGE[0] <= val <= QUEUE_PL_LIMIT_RANGE[1]):
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT_RANGE", min=QUEUE_PL_LIMIT_RANGE[0], max=QUEUE_PL_LIMIT_RANGE[1]),
                        ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                cog.guild_playlist_track_limit[guild_id] = val
                await db.set_playlist_track_limit(guild_id, val)
                self.view_ref._rebuild_limits_sub("max_user_tracks")
                await modal_interaction.response.edit_message(
                    embed=cog._build_detail_embed(modal_interaction, guild_id, "max_user_tracks"),
                    view=self.view_ref)

        class PausePermSelect(discord.ui.Select):
            def __init__(self, view_ref):
                current = cog.guild_pause_permission.get(guild_id, "requester_dj")
                options = [
                    discord.SelectOption(label=t(ctx, "PAUSE_PERM_EVERYONE"), value="everyone",
                                         description=t(ctx, "PAUSE_PERM_EVERYONE_DESC"), default=current == "everyone"),
                    discord.SelectOption(label=t(ctx, "PERM_REQUESTER_DJ"), value="requester_dj",
                                         description=t(ctx, "PAUSE_PERM_REQUESTER_DJ_DESC"), default=current == "requester_dj"),
                    discord.SelectOption(label=t(ctx, "PERM_DJ_ADMIN"), value="dj",
                                         description=t(ctx, "PAUSE_PERM_DJ_DESC"), default=current == "dj"),
                    discord.SelectOption(label=t(ctx, "PERM_ADMIN_ONLY"), value="admin",
                                         description=t(ctx, "PAUSE_PERM_ADMIN_DESC"), default=current == "admin"),
                    discord.SelectOption(label=t(ctx, "PERM_OWNER_ONLY"), value="owner",
                                         description=t(ctx, "PAUSE_PERM_OWNER_DESC"), default=current == "owner"),
                ]
                super().__init__(placeholder=t(ctx, "PAUSE_PERM_PLACEHOLDER"), options=options, row=0)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                chosen = self.values[0]
                current = cog.guild_pause_permission.get(guild_id, "requester_dj")
                if (chosen in ("owner", "admin") or current in ("owner", "admin")) and not _effective_owner:
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "ONLY_OWNER_CAN_SET"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                cog.guild_pause_permission[guild_id] = chosen
                await db.set_pause_permission(guild_id, chosen)
                self.view_ref._rebuild_pause_control()
                await btn_interaction.response.edit_message(
                    embed=cog._build_pause_control_embed(btn_interaction, guild_id),
                    view=self.view_ref)

        class PauseTimeoutButton(discord.ui.Button):
            def __init__(self, view_ref):
                timeout = cog.guild_pause_timeout.get(guild_id, 900) // 60
                super().__init__(style=discord.ButtonStyle.secondary,
                                 label=f"{t(ctx, 'PAUSE_TIMEOUT_BUTTON')}: {timeout}{t(ctx, 'ABBR_MINUTES')}", row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                await btn_interaction.response.send_modal(PauseTimeoutModal(self.view_ref))

        class PauseTimeoutModal(discord.ui.Modal):
            def __init__(self, view_ref):
                super().__init__(title=t(ctx, "PAUSE_TIMEOUT_TITLE"))
                self.view_ref = view_ref
                current = cog.guild_pause_timeout.get(guild_id, 900) // 60
                self.timeout_input = discord.ui.TextInput(
                    label=t(ctx, "PAUSE_TIMEOUT_LABEL", min=PAUSE_TIMEOUT_RANGE[0], max=PAUSE_TIMEOUT_RANGE[1]),
                    default=str(current),
                    max_length=2, required=True,
                )
                self.add_item(self.timeout_input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                try:
                    val = int(self.timeout_input.value.strip())
                except ValueError:
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                if not (PAUSE_TIMEOUT_RANGE[0] <= val <= PAUSE_TIMEOUT_RANGE[1]):
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT_RANGE", min=PAUSE_TIMEOUT_RANGE[0], max=PAUSE_TIMEOUT_RANGE[1]), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                cog.guild_pause_timeout[guild_id] = val * 60
                await db.set_pause_timeout(guild_id, val * 60)
                self.view_ref._rebuild_pause_control()
                await modal_interaction.response.edit_message(
                    embed=cog._build_pause_control_embed(modal_interaction, guild_id),
                    view=self.view_ref)

        class IdleTimeoutButton(discord.ui.Button):
            def __init__(self, view_ref):
                idle = cog.guild_idle_disconnect.get(guild_id, 180)
                idle_str = f"{idle // 60}{t(ctx, 'ABBR_MINUTES')}" if idle > 0 else t(ctx, "DISABLED")
                super().__init__(style=discord.ButtonStyle.secondary,
                                 label=f"{t(ctx, 'BOT_CONN_IDLE_TIMEOUT')}: {idle_str}", row=0)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                if cog.guild_join_restrict_level.get(guild_id, "none") == "admin" and not _effective_owner:
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "ONLY_OWNER_CAN_SET"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                await btn_interaction.response.send_modal(IdleTimeoutModal(self.view_ref))

        class IdleTimeoutModal(discord.ui.Modal):
            def __init__(self, view_ref):
                super().__init__(title=t(ctx, "BOT_CONN_IDLE_TIMEOUT"))
                self.view_ref = view_ref
                current = cog.guild_idle_disconnect.get(guild_id, 180) // 60
                self.num_input = discord.ui.TextInput(
                    label=t(ctx, "BOT_CONN_IDLE_LABEL", min=IDLE_TIMEOUT_RANGE[0], max=IDLE_TIMEOUT_RANGE[1]),
                    default=str(current), max_length=5, required=True,
                )
                self.add_item(self.num_input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                try:
                    val = int(self.num_input.value.strip())
                except ValueError:
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                if not (IDLE_TIMEOUT_RANGE[0] <= val <= IDLE_TIMEOUT_RANGE[1]):
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT_RANGE", min=IDLE_TIMEOUT_RANGE[0], max=IDLE_TIMEOUT_RANGE[1]), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                seconds = val * 60
                cog.guild_idle_disconnect[guild_id] = seconds
                await db.set_idle_disconnect_timeout(guild_id, seconds)
                state = cog.guild_states.get(guild_id)
                if state:
                    if state.idle_disconnect_task and not state.idle_disconnect_task.done():
                        state.idle_disconnect_task.cancel()
                    if seconds > 0:
                        state.idle_disconnect_task = cog._create_task(
                            cog.playback.auto_disconnect_after(guild_id, seconds),
                            name=f"idle-dc-{guild_id}")
                    else:
                        state.idle_disconnect_task = None
                self.view_ref._rebuild_bot_connection()
                await modal_interaction.response.edit_message(
                    embed=cog._build_bot_connection_embed(modal_interaction, guild_id),
                    view=self.view_ref)

        class JoinRestrictLevelSelect(discord.ui.Select):
            def __init__(self, view_ref):
                current = cog.guild_join_restrict_level.get(guild_id, "none")
                _LEVELS = [
                    ("none", "JOIN_RESTRICT_NONE", "JOIN_RESTRICT_NONE_DESC"),
                    ("users", "JOIN_RESTRICT_USERS", "JOIN_RESTRICT_USERS_DESC"),
                    ("dj", "JOIN_RESTRICT_DJ", "JOIN_RESTRICT_DJ_DESC"),
                    ("admin", "JOIN_RESTRICT_ADMIN", "JOIN_RESTRICT_ADMIN_DESC"),
                ]
                options = [
                    discord.SelectOption(label=t(ctx, lk), value=v, description=t(ctx, dk), default=(v == current))
                    for v, lk, dk in _LEVELS
                ]
                super().__init__(placeholder=t(ctx, "JOIN_RESTRICT_PLACEHOLDER"), options=options, row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                chosen = self.values[0]
                is_owner = _effective_owner
                current_level = cog.guild_join_restrict_level.get(guild_id, "none")
                if chosen == "admin" and not is_owner:
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "ONLY_OWNER_CAN_SET"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                if current_level == "admin" and not is_owner:
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "ONLY_OWNER_CAN_SET"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                cog.guild_join_restrict_level[guild_id] = chosen
                await db.set_join_restrict_level(guild_id, chosen)
                self.view_ref._rebuild_bot_connection()
                await btn_interaction.response.edit_message(
                    embed=cog._build_bot_connection_embed(btn_interaction, guild_id),
                    view=self.view_ref)

        class JoinRestrictChannelSelect(discord.ui.ChannelSelect):
            def __init__(self, view_ref):
                super().__init__(
                    channel_types=[discord.ChannelType.voice, discord.ChannelType.stage_voice],
                    placeholder=t(ctx, "JOIN_RESTRICT_CHANNEL_PLACEHOLDER"),
                    min_values=1, max_values=1, row=2,
                )
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                if cog.guild_join_restrict_level.get(guild_id, "none") == "admin" and not _effective_owner:
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "ONLY_OWNER_CAN_SET"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                ch = self.values[0]
                ch_set = cog.guild_join_restrict_channels.setdefault(guild_id, set())
                if ch.id in ch_set:
                    ch_set.discard(ch.id)
                    await db.remove_join_restrict_channel(guild_id, ch.id)
                else:
                    ch_set.add(ch.id)
                    await db.add_join_restrict_channel(guild_id, ch.id)
                if not ch_set:
                    cog.guild_join_restrict_channels.pop(guild_id, None)
                self.view_ref._rebuild_bot_connection()
                await btn_interaction.response.edit_message(
                    embed=cog._build_bot_connection_embed(btn_interaction, guild_id),
                    view=self.view_ref)

        class JoinRestrictClearButton(discord.ui.Button):
            def __init__(self, view_ref, *, disabled=False):
                super().__init__(style=discord.ButtonStyle.danger, emoji="✖", row=3, disabled=disabled)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                if cog.guild_join_restrict_level.get(guild_id, "none") == "admin" and not _effective_owner:
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "ONLY_OWNER_CAN_SET"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                cog.guild_join_restrict_channels.pop(guild_id, None)
                await db.clear_join_restrict_channels(guild_id)
                self.view_ref._rebuild_bot_connection()
                await btn_interaction.response.edit_message(
                    embed=cog._build_bot_connection_embed(btn_interaction, guild_id),
                    view=self.view_ref)

        class LiveEnabledToggle(discord.ui.Button):
            def __init__(self, view_ref):
                enabled = cog.guild_live_enabled.get(guild_id, 0)
                label = f"{t(ctx, 'LIVE_STATUS')}: {t(ctx, 'ENABLED') if enabled else t(ctx, 'DISABLED')}"
                super().__init__(style=discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary,
                                 label=label, row=0)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                current_perm = cog.guild_live_permission.get(guild_id, "admin")
                if current_perm in ("owner", "admin") and not _effective_owner:
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "ONLY_OWNER_CAN_SET"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                current = cog.guild_live_enabled.get(guild_id, 0)
                new_val = 0 if current else 1
                cog.guild_live_enabled[guild_id] = new_val
                await db.set_live_enabled(guild_id, new_val)
                self.view_ref._rebuild_live_playback()
                await btn_interaction.response.edit_message(
                    embed=cog._build_live_playback_embed(btn_interaction, guild_id),
                    view=self.view_ref)

        class LivePermSelect(discord.ui.Select):
            def __init__(self, view_ref):
                current = cog.guild_live_permission.get(guild_id, "admin")
                _PERMS = [
                    ("everyone", "PERM_EVERYONE"),
                    ("dj", "PERM_DJ_ADMIN"),
                    ("admin", "PERM_ADMIN_ONLY"),
                    ("owner", "PERM_OWNER_ONLY"),
                ]
                options = [
                    discord.SelectOption(label=t(ctx, lk), value=v, default=(current == v))
                    for v, lk in _PERMS
                ]
                super().__init__(placeholder=t(ctx, "LIVE_PERMISSION"), options=options, row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                value = self.values[0]
                is_owner = _effective_owner
                current_perm = cog.guild_live_permission.get(guild_id, "admin")
                if (value in ("owner", "admin") or current_perm in ("owner", "admin")) and not is_owner:
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "ONLY_OWNER_CAN_SET"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                cog.guild_live_permission[guild_id] = value
                await db.set_live_permission(guild_id, value)
                self.view_ref._rebuild_live_playback()
                await btn_interaction.response.edit_message(
                    embed=cog._build_live_playback_embed(btn_interaction, guild_id),
                    view=self.view_ref)

        class LiveMaxHoursButton(discord.ui.Button):
            def __init__(self, view_ref):
                max_h = cog.guild_live_max_hours.get(guild_id, 1)
                max_str = f"{max_h}{t(ctx, 'ABBR_HOURS')}" if max_h > 0 else t(ctx, "DISABLED")
                super().__init__(style=discord.ButtonStyle.secondary,
                                 label=f"{t(ctx, 'LIVE_MAX_HOURS')}: {max_str}", row=2)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                current_perm = cog.guild_live_permission.get(guild_id, "admin")
                if current_perm in ("owner", "admin") and not _effective_owner:
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "ONLY_OWNER_CAN_SET"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                await btn_interaction.response.send_modal(LiveMaxHoursModal(self.view_ref))

        class LiveMaxHoursModal(discord.ui.Modal):
            def __init__(self, view_ref):
                super().__init__(title=t(ctx, "LIVE_MAX_HOURS"))
                self.view_ref = view_ref
                current = cog.guild_live_max_hours.get(guild_id, 1)
                self.num_input = discord.ui.TextInput(
                    label=t(ctx, "LIVE_MAX_HOURS_LABEL"),
                    default=str(current), max_length=2, required=True,
                )
                self.add_item(self.num_input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                try:
                    val = int(self.num_input.value.strip())
                except ValueError:
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                if not (0 <= val <= 24):
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT_RANGE", min=0, max=24), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                cog.guild_live_max_hours[guild_id] = val
                await db.set_live_max_hours(guild_id, val)
                self.view_ref._rebuild_live_playback()
                await modal_interaction.response.edit_message(
                    embed=cog._build_live_playback_embed(modal_interaction, guild_id),
                    view=self.view_ref)

        class PauseTimeoutBehaviorSelect(discord.ui.Select):
            def __init__(self, view_ref):
                current = cog.guild_pause_timeout_behavior.get(guild_id, "leave")
                options = [
                    discord.SelectOption(label=t(ctx, "PAUSE_BEHAVIOR_LEAVE"), value="leave",
                                         description=t(ctx, "PAUSE_BEHAVIOR_LEAVE_DESC"), default=current == "leave"),
                    discord.SelectOption(label=t(ctx, "PAUSE_BEHAVIOR_CONTINUE"), value="continue",
                                         description=t(ctx, "PAUSE_BEHAVIOR_CONTINUE_DESC"), default=current == "continue"),
                    discord.SelectOption(label=t(ctx, "PAUSE_BEHAVIOR_SKIP"), value="skip",
                                         description=t(ctx, "PAUSE_BEHAVIOR_SKIP_DESC"), default=current == "skip"),
                ]
                super().__init__(placeholder=t(ctx, "PAUSE_BEHAVIOR_PLACEHOLDER"), options=options, row=2)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                chosen = self.values[0]
                cog.guild_pause_timeout_behavior[guild_id] = chosen
                await db.set_pause_timeout_behavior(guild_id, chosen)
                self.view_ref._rebuild_pause_control()
                await btn_interaction.response.edit_message(
                    embed=cog._build_pause_control_embed(btn_interaction, guild_id),
                    view=self.view_ref)

        class SeekPermSelect(discord.ui.Select):
            def __init__(self, view_ref):
                current = cog.guild_seek_permission.get(guild_id, "requester_dj")
                options = [
                    discord.SelectOption(label=t(ctx, "PERM_EVERYONE"), value="everyone",
                                         description=t(ctx, "SEEK_PERM_EVERYONE_DESC"), default=current == "everyone"),
                    discord.SelectOption(label=t(ctx, "PERM_REQUESTER_DJ"), value="requester_dj",
                                         description=t(ctx, "SEEK_PERM_REQUESTER_DJ_DESC"), default=current == "requester_dj"),
                    discord.SelectOption(label=t(ctx, "PERM_DJ_ADMIN"), value="dj",
                                         description=t(ctx, "SEEK_PERM_DJ_DESC"), default=current == "dj"),
                    discord.SelectOption(label=t(ctx, "PERM_ADMIN_ONLY"), value="admin",
                                         description=t(ctx, "SEEK_PERM_ADMIN_DESC"), default=current == "admin"),
                    discord.SelectOption(label=t(ctx, "PERM_OWNER_ONLY"), value="owner",
                                         description=t(ctx, "SEEK_PERM_OWNER_DESC"), default=current == "owner"),
                ]
                super().__init__(placeholder=t(ctx, "SEEK_PERM_PLACEHOLDER"), options=options, row=3)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                chosen = self.values[0]
                current = cog.guild_seek_permission.get(guild_id, "requester_dj")
                if (chosen in ("owner", "admin") or current in ("owner", "admin")) and not _effective_owner:
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "ONLY_OWNER_CAN_SET"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                cog.guild_seek_permission[guild_id] = chosen
                await db.set_seek_permission(guild_id, chosen)
                self.view_ref._rebuild_pause_control()
                await btn_interaction.response.edit_message(
                    embed=cog._build_pause_control_embed(btn_interaction, guild_id),
                    view=self.view_ref)

        SEEK_LIMIT_RANGE = (0, 60)

        class SeekLimitButton(discord.ui.Button):
            def __init__(self, view_ref):
                val = cog.guild_max_seeks_per_track.get(guild_id, 3)
                _dis = t(ctx, "DISABLED")
                lbl = str(val) if val > 0 else _dis
                super().__init__(style=discord.ButtonStyle.secondary,
                                 label=f"{t(ctx, 'SEEK_LIMIT_BUTTON')}: {lbl}", row=4)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                await btn_interaction.response.send_modal(SeekLimitModal(self.view_ref))

        class SeekLimitModal(discord.ui.Modal):
            def __init__(self, view_ref):
                super().__init__(title=t(ctx, "SEEK_LIMIT_TITLE"))
                self.view_ref = view_ref
                current = cog.guild_max_seeks_per_track.get(guild_id, 3)
                self.input = discord.ui.TextInput(
                    label=t(ctx, "SEEK_LIMIT_LABEL", min=SEEK_LIMIT_RANGE[0], max=SEEK_LIMIT_RANGE[1]),
                    default=str(current), max_length=2, required=True, placeholder=f"{SEEK_LIMIT_RANGE[0]}-{SEEK_LIMIT_RANGE[1]}",
                )
                self.add_item(self.input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                try:
                    val = int(self.input.value.strip())
                except ValueError:
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT"), ephemeral=True)
                if not (SEEK_LIMIT_RANGE[0] <= val <= SEEK_LIMIT_RANGE[1]):
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT_RANGE", min=SEEK_LIMIT_RANGE[0], max=SEEK_LIMIT_RANGE[1]),
                        ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                cog.guild_max_seeks_per_track[guild_id] = val
                await db.set_max_seeks_per_track(guild_id, val)
                self.view_ref._rebuild_pause_control()
                await modal_interaction.response.edit_message(
                    embed=cog._build_pause_control_embed(modal_interaction, guild_id),
                    view=self.view_ref)

        class SeekLimitDJButton(discord.ui.Button):
            def __init__(self, view_ref):
                val = cog.guild_max_seeks_dj.get(guild_id, 0)
                _dis = t(ctx, "DISABLED")
                lbl = str(val) if val > 0 else _dis
                super().__init__(style=discord.ButtonStyle.secondary,
                                 label=f"{t(ctx, 'SEEK_LIMIT_DJ_BUTTON')}: {lbl}", row=4)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                await btn_interaction.response.send_modal(SeekLimitDJModal(self.view_ref))

        class SeekLimitDJModal(discord.ui.Modal):
            def __init__(self, view_ref):
                super().__init__(title=t(ctx, "SEEK_LIMIT_DJ_TITLE"))
                self.view_ref = view_ref
                current = cog.guild_max_seeks_dj.get(guild_id, 0)
                self.input = discord.ui.TextInput(
                    label=t(ctx, "SEEK_LIMIT_DJ_LABEL", min=SEEK_LIMIT_RANGE[0], max=SEEK_LIMIT_RANGE[1]),
                    default=str(current), max_length=2, required=True, placeholder=f"{SEEK_LIMIT_RANGE[0]}-{SEEK_LIMIT_RANGE[1]}",
                )
                self.add_item(self.input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                try:
                    val = int(self.input.value.strip())
                except ValueError:
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT"), ephemeral=True)
                if not (0 <= val <= 60):
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT_RANGE", min=0, max=60),
                        ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                cog.guild_max_seeks_dj[guild_id] = val
                await db.set_max_seeks_dj(guild_id, val)
                self.view_ref._rebuild_pause_control()
                await modal_interaction.response.edit_message(
                    embed=cog._build_pause_control_embed(modal_interaction, guild_id),
                    view=self.view_ref)


        class RadioPermSelect(discord.ui.Select):
            def __init__(self, view_ref):
                current = cog.guild_radio_permissions.get(guild_id, "dj")
                options = [
                    discord.SelectOption(label=t(ctx, "PERM_EVERYONE"), value="everyone",
                                         description=t(ctx, "RADIO_PERM_EVERYONE_DESC"), default=current == "everyone"),
                    discord.SelectOption(label=t(ctx, "PERM_DJ_ADMIN"), value="dj",
                                         description=t(ctx, "RADIO_PERM_DJ_DESC"), default=current == "dj"),
                    discord.SelectOption(label=t(ctx, "PERM_ADMIN_ONLY"), value="admin",
                                         description=t(ctx, "RADIO_PERM_ADMIN_DESC"), default=current == "admin"),
                    discord.SelectOption(label=t(ctx, "PERM_OWNER_ONLY"), value="owner",
                                         description=t(ctx, "RADIO_PERM_OWNER_DESC"), default=current == "owner"),
                ]
                super().__init__(placeholder=t(ctx, "RADIO_PERM_PLACEHOLDER"), options=options, row=0)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                chosen = self.values[0]
                current = cog.guild_radio_permissions.get(guild_id, "dj")
                if (chosen in ("owner", "admin") or current in ("owner", "admin")) and not _effective_owner:
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "ONLY_OWNER_CAN_SET"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                cog.guild_radio_permissions[guild_id] = chosen
                await db.set_radio_permission(guild_id, chosen)
                self.view_ref._rebuild_radio_settings()
                await btn_interaction.response.edit_message(
                    embed=cog._build_radio_settings_embed(btn_interaction, guild_id),
                    view=self.view_ref)

        class RadioEditPermSelect(discord.ui.Select):
            def __init__(self, view_ref):
                current = cog.guild_radio_edit_permissions.get(guild_id, "dj")
                options = [
                    discord.SelectOption(label=t(ctx, "PERM_DJ_ADMIN"), value="dj",
                                         description=t(ctx, "RADIO_EDIT_PERM_DJ_DESC"), default=current == "dj"),
                    discord.SelectOption(label=t(ctx, "PERM_ADMIN_ONLY"), value="admin",
                                         description=t(ctx, "RADIO_EDIT_PERM_ADMIN_DESC"), default=current == "admin"),
                    discord.SelectOption(label=t(ctx, "PERM_OWNER_ONLY"), value="owner",
                                         description=t(ctx, "RADIO_EDIT_PERM_OWNER_DESC"), default=current == "owner"),
                ]
                super().__init__(placeholder=t(ctx, "RADIO_EDIT_PERM_PLACEHOLDER"), options=options, row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                chosen = self.values[0]
                current = cog.guild_radio_edit_permissions.get(guild_id, "dj")
                radio_locked = cog.guild_radio_permissions.get(guild_id, "dj") in ("owner", "admin")
                if (chosen in ("owner", "admin") or current in ("owner", "admin") or radio_locked) and not _effective_owner:
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "ONLY_OWNER_CAN_SET"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                cog.guild_radio_edit_permissions[guild_id] = chosen
                await db.set_radio_edit_permission(guild_id, chosen)
                self.view_ref._rebuild_radio_settings()
                await btn_interaction.response.edit_message(
                    embed=cog._build_radio_settings_embed(btn_interaction, guild_id),
                    view=self.view_ref)

        class RadioCooldownButton(discord.ui.Button):
            def __init__(self, view_ref):
                cd_min = cog.guild_radio_cooldowns.get(guild_id, 3)
                super().__init__(style=discord.ButtonStyle.secondary,
                                 label=f"{t(ctx, 'RADIO_COOLDOWN_BUTTON')}: {cd_min}{t(ctx, 'ABBR_MINUTES')}", row=2)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                if cog.guild_radio_permissions.get(guild_id, "dj") in ("owner", "admin") and not _effective_owner:
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "ONLY_OWNER_CAN_SET"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                await btn_interaction.response.send_modal(RadioCooldownModal(self.view_ref))

        class RadioCooldownModal(discord.ui.Modal):
            def __init__(self, view_ref):
                super().__init__(title=t(ctx, "RADIO_COOLDOWN_TITLE"))
                self.view_ref = view_ref
                current = cog.guild_radio_cooldowns.get(guild_id, 3)
                self.cd_input = discord.ui.TextInput(
                    label=t(ctx, "RADIO_COOLDOWN_LABEL", min=RADIO_COOLDOWN_RANGE[0], max=RADIO_COOLDOWN_RANGE[1]),
                    default=str(current),
                    max_length=2, required=True,
                )
                self.add_item(self.cd_input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                try:
                    val = int(self.cd_input.value.strip())
                except ValueError:
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                if not (RADIO_COOLDOWN_RANGE[0] <= val <= RADIO_COOLDOWN_RANGE[1]):
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT_RANGE", min=RADIO_COOLDOWN_RANGE[0], max=RADIO_COOLDOWN_RANGE[1]), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                cog.guild_radio_cooldowns[guild_id] = val
                await db.set_radio_cooldown(guild_id, val)
                self.view_ref._rebuild_radio_settings()
                await modal_interaction.response.edit_message(
                    embed=cog._build_radio_settings_embed(modal_interaction, guild_id),
                    view=self.view_ref)

        class ForcePlayPermSelect(discord.ui.Select):
            def __init__(self, view_ref):
                current = cog.guild_force_play_permission.get(guild_id, "dj")
                options = [
                    discord.SelectOption(label=t(ctx, "PERM_DJ_ADMIN"), value="dj",
                                         description=t(ctx, "FORCE_PERM_DJ_DESC"), default=current == "dj"),
                    discord.SelectOption(label=t(ctx, "PERM_ADMIN_ONLY"), value="admin",
                                         description=t(ctx, "FORCE_PERM_ADMIN_DESC"), default=current == "admin"),
                    discord.SelectOption(label=t(ctx, "PERM_OWNER_ONLY"), value="owner",
                                         description=t(ctx, "FORCE_PERM_OWNER_DESC"), default=current == "owner"),
                ]
                super().__init__(placeholder=t(ctx, "FORCE_PERM_PLACEHOLDER"), options=options, row=0)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                chosen = self.values[0]
                current = cog.guild_force_play_permission.get(guild_id, "dj")
                if (chosen in ("owner", "admin") or current in ("owner", "admin")) and not _effective_owner:
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "ONLY_OWNER_CAN_SET"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                cog.guild_force_play_permission[guild_id] = chosen
                await db.set_force_play_permission(guild_id, chosen)
                self.view_ref._rebuild_force_play()
                await btn_interaction.response.edit_message(
                    embed=cog._build_force_play_embed(btn_interaction, guild_id),
                    view=self.view_ref)

        class ForceRadioToggle(discord.ui.Button):
            def __init__(self, view_ref):
                current = cog.guild_force_radio.get(guild_id, "disabled")
                label = t(ctx, "FORCE_RADIO_ENABLED") if current == "enabled" else t(ctx, "FORCE_RADIO_DISABLED_LABEL")
                style = discord.ButtonStyle.success if current == "enabled" else discord.ButtonStyle.secondary
                super().__init__(style=style, label=f"{t(ctx, 'FORCE_RADIO_BUTTON')}: {label}", row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                fp_perm = cog.guild_force_play_permission.get(guild_id, "dj")
                if fp_perm in ("owner", "admin") and not _effective_owner:
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "ONLY_OWNER_CAN_SET"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                current = cog.guild_force_radio.get(guild_id, "disabled")
                new_val = "disabled" if current == "enabled" else "enabled"
                cog.guild_force_radio[guild_id] = new_val
                await db.set_force_radio(guild_id, new_val)
                self.view_ref._rebuild_force_play()
                await btn_interaction.response.edit_message(
                    embed=cog._build_force_play_embed(btn_interaction, guild_id),
                    view=self.view_ref)

        class PrefetchToggle(discord.ui.Button):
            def __init__(self, view_ref):
                current = cog._prefetch
                label = t(ctx, "ENABLED") if current else t(ctx, "DISABLED")
                style = discord.ButtonStyle.success if current else discord.ButtonStyle.secondary
                super().__init__(style=style, label=f"{t(ctx, 'SETTINGS_SHOW_PREFETCH')}: {label}", row=0)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                if not await cog.bot.is_owner(btn_interaction.user):
                    return
                new_val = not cog._prefetch
                cog._prefetch = new_val
                await db.set_prefetch(new_val)
                self.view_ref._rebuild_performance()
                await btn_interaction.response.edit_message(
                    embed=cog._build_performance_embed(btn_interaction, guild_id),
                    view=self.view_ref)

        class SafePrefetchToggle(discord.ui.Button):
            def __init__(self, view_ref):
                current = cog._safe_prefetch
                label = t(ctx, "ENABLED") if current else t(ctx, "DISABLED")
                style = discord.ButtonStyle.success if current else discord.ButtonStyle.secondary
                super().__init__(style=style, label=f"{t(ctx, 'SETTINGS_SHOW_SAFE_PREFETCH')}: {label}", row=0)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                if not await cog.bot.is_owner(btn_interaction.user):
                    return
                new_val = not cog._safe_prefetch
                cog._safe_prefetch = new_val
                await db.set_safe_prefetch(new_val)
                self.view_ref._rebuild_performance()
                await btn_interaction.response.edit_message(
                    embed=cog._build_performance_embed(btn_interaction, guild_id),
                    view=self.view_ref)

        class MaxWorkersButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.secondary,
                                 label=f"{t(ctx, 'SETTINGS_SHOW_MAX_WORKERS')}: {cog._max_workers}", row=0)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                if not await cog.bot.is_owner(btn_interaction.user):
                    return
                modal = MaxWorkersModal(self.view_ref)
                await btn_interaction.response.send_modal(modal)

        class MaxWorkersModal(discord.ui.Modal):
            def __init__(self, view_ref):
                super().__init__(title=t(ctx, "SETTINGS_SHOW_MAX_WORKERS"))
                self.value = discord.ui.TextInput(
                    label=t(ctx, "SETTINGS_MAX_HISTORY_LABEL", min=1, max=32),
                    placeholder="16", default=str(cog._max_workers), max_length=2)
                self.add_item(self.value)
                self.view_ref = view_ref

            async def on_submit(self, modal_interaction: discord.Interaction):
                try:
                    val = int(self.value.value.strip())
                except ValueError:
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT_RANGE", min=1, max=32),
                        ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                if not (1 <= val <= 32):
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT_RANGE", min=1, max=32),
                        ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                from core.media import set_max_workers
                from core.radio import set_radio_concurrency
                cog._max_workers = val
                set_max_workers(val)
                set_radio_concurrency(val)
                await db.set_max_workers(val)
                self.view_ref._rebuild_performance()
                await modal_interaction.response.edit_message(
                    embed=cog._build_performance_embed(modal_interaction, guild_id),
                    view=self.view_ref)

        class SettingsSelect(discord.ui.Select):
            def __init__(self, view_ref, options):
                super().__init__(
                    placeholder=t(ctx, "SETTINGS_SELECT_PLACEHOLDER"),
                    options=options,
                    row=0,
                )
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                key = self.values[0]
                view = self.view_ref
                setting_type = None
                for skey, _, _, stype in cog._SETTING_DEFS:
                    if skey == key:
                        setting_type = stype
                        break
                await view._show_setting(btn_interaction, key, setting_type)

        class SettingsPageButton(discord.ui.Button):
            def __init__(self, view_ref, delta, emoji, label):
                super().__init__(style=discord.ButtonStyle.secondary, emoji=emoji, label=label, row=2)
                self.view_ref = view_ref
                self.delta = delta

            async def callback(self, btn_interaction: discord.Interaction):
                view = self.view_ref
                view.page = (view.page + self.delta) % view.total_pages
                view._rebuild_overview()
                await btn_interaction.response.edit_message(embed=cog._build_overview_embed(btn_interaction, guild_id, is_app_owner=is_app_owner, is_guild_admin=is_guild_admin, _effective_owner=_effective_owner), view=view)

        class BackButton(discord.ui.Button):
            def __init__(self, view_ref, *, row=2):
                super().__init__(style=discord.ButtonStyle.secondary, emoji="◀️", label=t(ctx, "SETTINGS_BACK"), row=row)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                view = self.view_ref
                view._rebuild_overview()
                await btn_interaction.response.edit_message(embed=cog._build_overview_embed(btn_interaction, guild_id, is_app_owner=is_app_owner, is_guild_admin=is_guild_admin, _effective_owner=_effective_owner), view=view)

        class ExportButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.secondary, label=t(ctx, "SETTINGS_EXPORT_GUILD"), row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                if not await cog._check_cooldown(btn_interaction, "settings:export", 5): return
                if not _effective_owner:
                    return
                data = await db.export_guild_settings(guild_id)
                raw = _json.dumps(data, indent=2, ensure_ascii=False)
                buf = io.BytesIO(raw.encode("utf-8"))
                file = discord.File(buf, filename="guild_settings.json")
                await btn_interaction.response.send_message(file=file, ephemeral=True)

        class ImportButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.secondary, label=t(ctx, "SETTINGS_IMPORT_GUILD"), row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                if not await cog._check_cooldown(btn_interaction, "settings:import", 10, per_guild=True): return
                await btn_interaction.response.send_modal(ImportModal(self.view_ref))

        class ImportModal(discord.ui.Modal):
            def __init__(self, view_ref):
                super().__init__(title=t(ctx, "SETTINGS_IMPORT_GUILD"))
                self.view_ref = view_ref

            upload = discord.ui.Label(
                text=t(ctx, "SETTINGS_IMPORT_FILE_LABEL"),
                description=t(ctx, "SETTINGS_IMPORT_FILE_DESC"),
                component=discord.ui.FileUpload(
                    custom_id="settings_import_file",
                    required=True,
                    max_values=1,
                ),
            )

            async def on_submit(self, modal_interaction: discord.Interaction):
                if not _effective_owner:
                    return
                attachments = self.upload.component.values
                if not attachments:
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "PL_IMPORT_NO_FILE"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                att = attachments[0]
                if att.size > 1_000_000:
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "PL_IMPORT_TOO_LARGE"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                try:
                    raw = await att.read()
                    data = _json.loads(raw)
                    if not isinstance(data, dict):
                        raise ValueError
                except (ValueError, _json.JSONDecodeError, UnicodeDecodeError, TypeError):
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "ERROR_GENERIC_TITLE"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                await modal_interaction.response.defer()
                await db.import_guild_settings(guild_id, data)
                await cog._reload_guild_from_db(guild_id)
                view = self.view_ref
                view._rebuild_overview()
                await modal_interaction.edit_original_response(
                    embed=cog._build_overview_embed(modal_interaction, guild_id, is_app_owner=is_app_owner, is_guild_admin=is_guild_admin, _effective_owner=_effective_owner), view=view)

        class ResetDefaultsButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.danger, label=t(ctx, "SETTINGS_RESET_GUILD"), row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                if not await cog._check_cooldown(btn_interaction, "settings:reset", 10, per_guild=True): return
                view = self.view_ref
                view._rebuild_reset_confirm()
                embed = SafeEmbed(
                    title=t(btn_interaction, "SETTINGS_RESET_CONFIRM_TITLE"),
                    description=t(btn_interaction, "SETTINGS_RESET_CONFIRM_DESC"),
                    color=cog.get_embed_color(guild_id),
                )
                await btn_interaction.response.edit_message(embed=embed, view=view)

        class ResetConfirmYesButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.danger, label=t(ctx, "SETTINGS_RESET_YES"), row=2)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                if not _effective_owner:
                    return
                await btn_interaction.response.defer()
                await db.reset_guild_settings(guild_id)
                await cog._reload_guild_from_db(guild_id)
                view = self.view_ref
                view._rebuild_overview()
                await btn_interaction.edit_original_response(
                    embed=cog._build_overview_embed(btn_interaction, guild_id, is_app_owner=is_app_owner, is_guild_admin=is_guild_admin, _effective_owner=_effective_owner), view=view)

        class ResetConfirmCancelButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.secondary, emoji="◀️", label=t(ctx, "SETTINGS_BACK"), row=2)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                view = self.view_ref
                view._rebuild_overview()
                await btn_interaction.response.edit_message(
                    embed=cog._build_overview_embed(btn_interaction, guild_id, is_app_owner=is_app_owner, is_guild_admin=is_guild_admin, _effective_owner=_effective_owner), view=view)

        # --- Bot settings buttons (app owner only) ---

        class BotExportButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.secondary, label=t(ctx, "SETTINGS_EXPORT_BOT"), row=2)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                if not await cog._check_cooldown(btn_interaction, "settings:bot_export", 5): return
                data = await db.export_bot_settings()
                raw = _json.dumps(data, indent=2, ensure_ascii=False)
                buf = io.BytesIO(raw.encode("utf-8"))
                file = discord.File(buf, filename="bot_settings.json")
                await btn_interaction.response.send_message(file=file, ephemeral=True)

        class BotImportButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.secondary, label=t(ctx, "SETTINGS_IMPORT_BOT"), row=2)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                if not await cog._check_cooldown(btn_interaction, "settings:bot_import", 10): return
                await btn_interaction.response.send_modal(BotImportModal(self.view_ref))

        class BotImportModal(discord.ui.Modal):
            def __init__(self, view_ref):
                super().__init__(title=t(ctx, "SETTINGS_IMPORT_BOT"))
                self.view_ref = view_ref

            upload = discord.ui.Label(
                text=t(ctx, "SETTINGS_IMPORT_FILE_LABEL"),
                description=t(ctx, "SETTINGS_IMPORT_FILE_DESC"),
                component=discord.ui.FileUpload(
                    custom_id="bot_settings_import_file",
                    required=True,
                    max_values=1,
                ),
            )

            async def on_submit(self, modal_interaction: discord.Interaction):
                attachments = self.upload.component.values
                if not attachments:
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "PL_IMPORT_NO_FILE"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                att = attachments[0]
                if att.size > 1_000_000:
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "PL_IMPORT_TOO_LARGE"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                try:
                    raw = await att.read()
                    data = _json.loads(raw)
                    if not isinstance(data, dict):
                        raise ValueError
                except (ValueError, _json.JSONDecodeError, UnicodeDecodeError, TypeError):
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "ERROR_GENERIC_TITLE"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                await modal_interaction.response.defer()
                await db.import_bot_settings(data)
                cog._silent_log = await db.get_silent_log()
                cog._update_discord_loggers()
                cog._prefetch = await db.get_prefetch()
                cog._safe_prefetch = await db.get_safe_prefetch()
                mw = await db.get_max_workers()
                cog._max_workers = mw
                from core.media import set_max_workers as _smw
                from core.radio import set_radio_concurrency as _src
                _smw(mw)
                _src(mw)
                act = await db.get_bot_activity()
                cog.bot_activity_type = act["type"]
                cog.bot_activity_text = act["text"]
                cog.bot_activity_mode = act["mode"]
                cog.bot_activity_interval = act["interval"]
                cog.bot_activity_selected = act["selected"]
                cog.bot_activity_list = await db.get_bot_activity_list()
                await cog.update_presence()
                cog.start_activity_cycle()
                view = self.view_ref
                view._rebuild_overview()
                await modal_interaction.edit_original_response(
                    embed=cog._build_overview_embed(modal_interaction, guild_id, is_app_owner=is_app_owner, is_guild_admin=is_guild_admin, _effective_owner=_effective_owner), view=view)

        class BotResetButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.danger, label=t(ctx, "SETTINGS_RESET_BOT"), row=2)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                if not await cog._check_cooldown(btn_interaction, "settings:bot_reset", 10): return
                view = self.view_ref
                view._rebuild_bot_reset_confirm()
                embed = SafeEmbed(
                    title=t(btn_interaction, "SETTINGS_BOT_RESET_CONFIRM_TITLE"),
                    description=t(btn_interaction, "SETTINGS_BOT_RESET_CONFIRM_DESC"),
                    color=cog.get_embed_color(guild_id),
                )
                await btn_interaction.response.edit_message(embed=embed, view=view)

        class BotResetConfirmYesButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.danger, label=t(ctx, "SETTINGS_RESET_YES"), row=2)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                await btn_interaction.response.defer()
                await db.reset_bot_settings()
                cog._silent_log = False
                cog._update_discord_loggers()
                cog._prefetch = True
                cog._safe_prefetch = True
                cog._max_workers = 16
                from core.media import set_max_workers as _smw
                from core.radio import set_radio_concurrency as _src
                _smw(16)
                _src(16)
                act = await db.get_bot_activity()
                cog.bot_activity_type = act["type"]
                cog.bot_activity_text = act["text"]
                cog.bot_activity_mode = act["mode"]
                cog.bot_activity_interval = act["interval"]
                cog.bot_activity_selected = act["selected"]
                cog.bot_activity_list = await db.get_bot_activity_list()
                await cog.update_presence()
                cog.start_activity_cycle()
                view = self.view_ref
                view._rebuild_overview()
                await btn_interaction.edit_original_response(
                    embed=cog._build_overview_embed(btn_interaction, guild_id, is_app_owner=is_app_owner, is_guild_admin=is_guild_admin, _effective_owner=_effective_owner), view=view)

        class BotResetConfirmCancelButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.secondary, emoji="◀️", label=t(ctx, "SETTINGS_BACK"), row=2)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                view = self.view_ref
                view._rebuild_overview()
                await btn_interaction.response.edit_message(
                    embed=cog._build_overview_embed(btn_interaction, guild_id, is_app_owner=is_app_owner, is_guild_admin=is_guild_admin, _effective_owner=_effective_owner), view=view)

        class BoolSelect(discord.ui.Select):
            def __init__(self, view_ref, key):
                self._key = key
                current = cog._settings_value_label(ctx, guild_id, key)
                active_label = t(ctx, "SETTINGS_ACTIVE")
                inactive_label = t(ctx, "SETTINGS_INACTIVE")
                options = [
                    discord.SelectOption(label=active_label, value="true", default=(current == active_label)),
                    discord.SelectOption(label=inactive_label, value="false", default=(current == inactive_label)),
                ]
                super().__init__(options=options, row=0)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                value = self.values[0] == "true"
                if self._key == "silent_log":
                    if not await cog.bot.is_owner(btn_interaction.user):
                        return
                    cog._silent_log = value
                    await db.set_silent_log(value)
                    cog._update_discord_loggers()
                chosen = self.values[0]
                for opt in self.options:
                    opt.default = (opt.value == chosen)
                await btn_interaction.response.edit_message(embed=cog._build_detail_embed(btn_interaction, guild_id, self._key), view=self.view_ref)

        class ChoiceSelect(discord.ui.Select):
            def __init__(self, view_ref, key):
                self._key = key
                current_vm = cog.vote_modes.get(guild_id, "half_plus_one")
                options = [
                    discord.SelectOption(label=t(ctx, "VOTE_MODE_HALF"), value="half", default=(current_vm == "half")),
                    discord.SelectOption(label=t(ctx, "VOTE_MODE_HALF_PLUS_ONE"), value="half_plus_one", default=(current_vm == "half_plus_one")),
                ]
                super().__init__(options=options, row=0)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                value = self.values[0]
                cog.vote_modes[guild_id] = value
                await db.set_vote_mode(guild_id, value)
                for opt in self.options:
                    opt.default = (opt.value == value)
                await btn_interaction.response.edit_message(embed=cog._build_detail_embed(btn_interaction, guild_id, self._key), view=self.view_ref)

        class VoteDeafenedButton(discord.ui.Button):
            def __init__(self, view_ref):
                current = cog.guild_vote_exclude_deafened.get(guild_id, 1)
                label = t(ctx, "VOTE_DEAFENED_EXCLUDE") if current else t(ctx, "VOTE_DEAFENED_INCLUDE")
                super().__init__(style=discord.ButtonStyle.secondary, label=label, row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                current = cog.guild_vote_exclude_deafened.get(guild_id, 1)
                new_val = 0 if current else 1
                cog.guild_vote_exclude_deafened[guild_id] = new_val
                await db.set_vote_exclude_deafened(guild_id, new_val)
                self.label = t(btn_interaction, "VOTE_DEAFENED_EXCLUDE") if new_val else t(btn_interaction, "VOTE_DEAFENED_INCLUDE")
                await btn_interaction.response.edit_message(embed=cog._build_detail_embed(btn_interaction, guild_id, "vote_mode"), view=self.view_ref)

        class LangSelect(discord.ui.Select):
            def __init__(self, view_ref, options):
                super().__init__(placeholder=t(ctx, "LANG_SELECT_PLACEHOLDER"), options=options, row=0)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                value = self.values[0]
                await set_locale(guild_id, value)
                view = self.view_ref
                view._rebuild_lang()
                await btn_interaction.response.edit_message(embed=cog._build_detail_embed(btn_interaction, guild_id, "language"), view=view)

        class LangPageButton(discord.ui.Button):
            def __init__(self, view_ref, delta, label):
                super().__init__(style=discord.ButtonStyle.secondary, label=label, row=1)
                self.view_ref = view_ref
                self.delta = delta

            async def callback(self, btn_interaction: discord.Interaction):
                view = self.view_ref
                view.lang_page = (view.lang_page + self.delta) % view.lang_total_pages
                view._rebuild_lang()
                await btn_interaction.response.edit_message(embed=cog._build_detail_embed(btn_interaction, guild_id, "language"), view=view)

        class ColorSelect(discord.ui.Select):
            def __init__(self, view_ref):
                options = [
                    discord.SelectOption(label=f"{t(ctx, name_key)} | #{value:06X}", value=str(value))
                    for value, name_key in cog._PRESET_COLORS
                ]
                super().__init__(placeholder=t(ctx, "COLOR_SELECT_PLACEHOLDER"), options=options, row=0)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                color_int = int(self.values[0])
                cog.guild_embed_colors[guild_id] = color_int
                await db.set_embed_color(guild_id, color_int)
                await btn_interaction.response.edit_message(embed=cog._build_detail_embed(btn_interaction, guild_id, "embed_color"), view=self.view_ref)

        class CustomColorButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.secondary, emoji="🎨", label=t(ctx, "COLOR_CUSTOM_BUTTON"), row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                await btn_interaction.response.send_modal(CustomColorModal(self.view_ref))

        class RandomColorButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.secondary, emoji="🎲", row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                color_int = random.randint(0, 0xFFFFFF)
                cog.guild_embed_colors[guild_id] = color_int
                await db.set_embed_color(guild_id, color_int)
                await btn_interaction.response.edit_message(embed=cog._build_detail_embed(btn_interaction, guild_id, "embed_color"), view=self.view_ref)

        class CustomColorModal(discord.ui.Modal):
            def __init__(self, view_ref):
                super().__init__(title=t(ctx, "COLOR_CUSTOM_TITLE"))
                self.view_ref = view_ref
                self.hex_input = discord.ui.TextInput(
                    label=t(ctx, "COLOR_CUSTOM_LABEL"),
                    placeholder=t(ctx, "COLOR_CUSTOM_PLACEHOLDER"),
                    max_length=7, min_length=3, required=True,
                )
                self.add_item(self.hex_input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                raw = self.hex_input.value.strip().lstrip("#")
                try:
                    color_int = int(raw, 16)
                    if not (0 <= color_int <= 0xFFFFFF):
                        raise ValueError
                except ValueError:
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "COLOR_CUSTOM_INVALID"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                cog.guild_embed_colors[guild_id] = color_int
                await db.set_embed_color(guild_id, color_int)
                await modal_interaction.response.edit_message(embed=cog._build_detail_embed(modal_interaction, guild_id, "embed_color"), view=self.view_ref)

        class TimezoneSelect(discord.ui.Select):
            _TZ_OFFSETS_P0 = list(range(-12, 2))   # -12 to +1 (14 items)
            _TZ_OFFSETS_P1 = list(range(2, 15))     # +2 to +14 (13 items)

            def __init__(self, view_ref, *, page=0):
                current = cog.guild_timezones.get(guild_id, 0)
                offsets = self._TZ_OFFSETS_P0 if page == 0 else self._TZ_OFFSETS_P1
                options = []
                for o in offsets:
                    label = f"UTC{o:+d}" if o != 0 else "UTC"
                    options.append(discord.SelectOption(label=label, value=str(o), default=o == current))
                super().__init__(placeholder=t(ctx, "TIMEZONE_PLACEHOLDER"), options=options, row=page)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                val = int(self.values[0])
                cog.guild_timezones[guild_id] = val
                await db.set_timezone(guild_id, val)
                self.view_ref._rebuild_timezone()
                await btn_interaction.response.edit_message(
                    embed=cog._build_detail_embed(btn_interaction, guild_id, "timezone"),
                    view=self.view_ref)

        class ConfigureButton(discord.ui.Button):
            def __init__(self, view_ref, key):
                super().__init__(style=discord.ButtonStyle.primary, emoji="✏️", row=0)
                self.view_ref = view_ref
                self._key = key

            async def callback(self, btn_interaction: discord.Interaction):
                await btn_interaction.response.send_modal(NumberModal(self.view_ref, self._key))

        class DJRoleSelect(discord.ui.RoleSelect):
            def __init__(self, view_ref):
                super().__init__(placeholder=t(ctx, "SETTINGS_DJ_ROLE_TITLE"), row=0)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                role = self.values[0]
                current = cog.dj_roles.get(guild_id)
                if current == role.id:
                    cog.dj_roles.pop(guild_id, None)
                    await db.set_dj_role(guild_id, None)
                else:
                    cog.dj_roles[guild_id] = role.id
                    await db.set_dj_role(guild_id, role.id)
                self.view_ref._rebuild_dj()
                await btn_interaction.response.edit_message(
                    embed=cog._build_detail_embed(btn_interaction, guild_id, "dj"),
                    view=self.view_ref)

        class DJUserSelect(discord.ui.UserSelect):
            def __init__(self, view_ref):
                super().__init__(placeholder=t(ctx, "SETTINGS_SHOW_DJ_USERS"), row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                user = self.values[0]
                dj_set = cog.dj_users.setdefault(guild_id, set())
                if user.id in dj_set:
                    dj_set.discard(user.id)
                    if not dj_set:
                        cog.dj_users.pop(guild_id, None)
                    await db.remove_dj_user(guild_id, user.id)
                else:
                    dj_set.add(user.id)
                    await db.add_dj_user(guild_id, user.id)
                self.view_ref._rebuild_dj()
                await btn_interaction.response.edit_message(
                    embed=cog._build_detail_embed(btn_interaction, guild_id, "dj"),
                    view=self.view_ref)

        class ClearDJRoleButton(discord.ui.Button):
            def __init__(self, view_ref, *, row=2):
                super().__init__(style=discord.ButtonStyle.danger, label=t(ctx, "SETTINGS_CLEAR_DJ_ROLE"), row=row)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                await db.set_dj_role(guild_id, None)
                cog.dj_roles.pop(guild_id, None)
                self.view_ref._rebuild_dj()
                await btn_interaction.response.edit_message(
                    embed=cog._build_detail_embed(btn_interaction, guild_id, "dj"),
                    view=self.view_ref)

        class ClearDJUsersButton(discord.ui.Button):
            def __init__(self, view_ref, *, row=2):
                super().__init__(style=discord.ButtonStyle.danger, label=t(ctx, "SETTINGS_CLEAR_DJ_USERS"), row=row)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                await db.clear_dj_users(guild_id)
                cog.dj_users.pop(guild_id, None)
                self.view_ref._rebuild_dj()
                await btn_interaction.response.edit_message(
                    embed=cog._build_detail_embed(btn_interaction, guild_id, "dj"),
                    view=self.view_ref)

        class ShowAllDJsButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.secondary, label=t(ctx, "SETTINGS_DJ_SHOW_ALL"), row=2)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                view = self.view_ref
                view.dj_list_page = 0
                view._rebuild_dj_list()
                await btn_interaction.response.edit_message(embed=view._build_dj_list_embed(), view=view)

        class DJListPageButton(discord.ui.Button):
            def __init__(self, view_ref, delta, emoji, label, *, disabled=False):
                super().__init__(style=discord.ButtonStyle.secondary, emoji=emoji, label=label, disabled=disabled, row=1)
                self.view_ref = view_ref
                self.delta = delta

            async def callback(self, btn_interaction: discord.Interaction):
                view = self.view_ref
                view.dj_list_page = max(0, min(view.dj_list_page + self.delta, view.dj_list_total_pages - 1))
                view._rebuild_dj_list()
                await btn_interaction.response.edit_message(embed=view._build_dj_list_embed(), view=view)

        class DJListFirstButton(discord.ui.Button):
            def __init__(self, view_ref, *, disabled=False):
                super().__init__(style=discord.ButtonStyle.secondary, emoji=_BUTTON_EMOJIS["BUTTON_FIRST"], label=t(ctx, "BUTTON_FIRST"), disabled=disabled, row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                view = self.view_ref
                view.dj_list_page = 0
                view._rebuild_dj_list()
                await btn_interaction.response.edit_message(embed=view._build_dj_list_embed(), view=view)

        class DJListLastButton(discord.ui.Button):
            def __init__(self, view_ref, *, disabled=False):
                super().__init__(style=discord.ButtonStyle.secondary, emoji=_BUTTON_EMOJIS["BUTTON_LAST"], label=t(ctx, "BUTTON_LAST"), disabled=disabled, row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                view = self.view_ref
                view.dj_list_page = view.dj_list_total_pages - 1
                view._rebuild_dj_list()
                await btn_interaction.response.edit_message(embed=view._build_dj_list_embed(), view=view)

        class DJListGoToButton(discord.ui.Button):
            def __init__(self, view_ref, *, disabled=False):
                super().__init__(style=discord.ButtonStyle.secondary, emoji="#️⃣", disabled=disabled, row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                await btn_interaction.response.send_modal(DJListGoToModal(self.view_ref))

        class DJListGoToModal(discord.ui.Modal):
            def __init__(self, view_ref):
                super().__init__(title=t(ctx, "QUEUE_GOTO_TITLE"))
                self._view = view_ref
                self.page_input = discord.ui.TextInput(
                    label=t(ctx, "QUEUE_GOTO_LABEL"),
                    placeholder=f"1-{view_ref.dj_list_total_pages}",
                    max_length=5, required=True,
                )
                self.add_item(self.page_input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                try:
                    page = int(self.page_input.value)
                except ValueError:
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                v = self._view
                if not (1 <= page <= v.dj_list_total_pages):
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "QUEUE_GOTO_INVALID"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                v.dj_list_page = page - 1
                v._rebuild_dj_list()
                await modal_interaction.response.edit_message(embed=v._build_dj_list_embed(), view=v)

        class DJListRemoveButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.danger, emoji=_BUTTON_EMOJIS["BUTTON_REMOVE"], label=t(ctx, "BUTTON_REMOVE"), row=2)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                await btn_interaction.response.send_modal(DJListRemoveModal(self.view_ref))

        class DJListRemoveModal(discord.ui.Modal):
            def __init__(self, view_ref):
                super().__init__(title=t(ctx, "BUTTON_REMOVE"))
                self._view = view_ref
                total = len(view_ref._dj_list)
                self.index_input = discord.ui.TextInput(
                    label=t(ctx, "SETTINGS_DJ_REMOVE_INDEX"),
                    placeholder=f"1-{total}",
                    max_length=5, required=True,
                )
                self.indexto_input = discord.ui.TextInput(
                    label=t(ctx, "SETTINGS_DJ_REMOVE_INDEXTO"),
                    placeholder=f"1-{total}",
                    max_length=5, required=False,
                )
                self.add_item(self.index_input)
                self.add_item(self.indexto_input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                try:
                    idx = int(self.index_input.value.strip())
                except ValueError:
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                v = self._view
                dj_list = v._dj_list
                total = len(dj_list)
                if not (1 <= idx <= total):
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                to_val = (self.indexto_input.value or "").strip()
                if to_val:
                    try:
                        idx_to = int(to_val)
                    except ValueError:
                        return await modal_interaction.response.send_message(
                            t(modal_interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                    if idx_to < idx or idx_to > total:
                        return await modal_interaction.response.send_message(
                            t(modal_interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                else:
                    idx_to = idx
                to_remove = dj_list[idx - 1:idx_to]
                await modal_interaction.response.defer()
                dj_set = cog.dj_users.get(guild_id, set())
                for uid in to_remove:
                    dj_set.discard(uid)
                    await db.remove_dj_user(guild_id, uid)
                if not dj_set:
                    cog.dj_users.pop(guild_id, None)
                dj_set_after = cog.dj_users.get(guild_id, set())
                if dj_set_after:
                    v.dj_list_page = 0
                    v._rebuild_dj_list()
                    await modal_interaction.edit_original_response(embed=v._build_dj_list_embed(), view=v)
                else:
                    v._rebuild_dj()
                    await modal_interaction.edit_original_response(
                        embed=cog._build_detail_embed(modal_interaction, guild_id, "dj"),
                        view=v)

        class DJListBackButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.secondary, emoji="◀️", label=t(ctx, "SETTINGS_BACK"), row=3)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                view = self.view_ref
                view._rebuild_dj()
                await btn_interaction.response.edit_message(
                    embed=cog._build_detail_embed(btn_interaction, guild_id, "dj"),
                    view=view)

        class QueuePerPageButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.primary, emoji="✏️", row=0)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                await btn_interaction.response.send_modal(QueuePerPageModal(self.view_ref))

        class QueuePerPageModal(discord.ui.Modal):
            def __init__(self, view_ref):
                super().__init__(title=t(ctx, "SETTINGS_SHOW_QUEUE_DISPLAY"))
                self.view_ref = view_ref
                current = str(cog.guild_queue_per_page.get(guild_id, 10))
                self.num_input = discord.ui.TextInput(
                    label=t(ctx, "SETTINGS_QD_PER_PAGE_LABEL", min=QUEUE_PER_PAGE_RANGE[0], max=QUEUE_PER_PAGE_RANGE[1]),
                    placeholder="5-15",
                    default=current,
                    max_length=2, required=True,
                )
                self.add_item(self.num_input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                try:
                    val = int(self.num_input.value.strip())
                except ValueError:
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                if not (QUEUE_PER_PAGE_RANGE[0] <= val <= QUEUE_PER_PAGE_RANGE[1]):
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT_RANGE", min=QUEUE_PER_PAGE_RANGE[0], max=QUEUE_PER_PAGE_RANGE[1]), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                cog.guild_queue_per_page[guild_id] = val
                await db.set_queue_per_page(guild_id, val)
                self.view_ref._rebuild_queue_display()
                await modal_interaction.response.edit_message(embed=cog._build_detail_embed(modal_interaction, guild_id, "queue_display"), view=self.view_ref)

        class QueueCompactSelect(discord.ui.Select):
            def __init__(self, view_ref):
                current = cog.guild_queue_compact.get(guild_id, True)
                normal_label = t(ctx, "SETTINGS_QD_NORMAL")
                spacious_label = t(ctx, "SETTINGS_QD_SPACIOUS")
                options = [
                    discord.SelectOption(label=normal_label, value="true", default=current),
                    discord.SelectOption(label=spacious_label, value="false", default=not current),
                ]
                super().__init__(options=options, row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                value = self.values[0] == "true"
                cog.guild_queue_compact[guild_id] = value
                await db.set_queue_compact(guild_id, value)
                self.view_ref._rebuild_queue_display()
                await btn_interaction.response.edit_message(embed=cog._build_detail_embed(btn_interaction, guild_id, "queue_display"), view=self.view_ref)

        class QueueFieldToggleSelect(discord.ui.Select):
            def __init__(self, view_ref):
                from core.music_handlers import _QUEUE_FIELD_LABELS
                qf = cog.get_queue_fields(guild_id)
                options = []
                for key, label_key in _QUEUE_FIELD_LABELS.items():
                    enabled = qf.get(key, {}).get("enabled", True)
                    icon = "✅" if enabled else "❌"
                    options.append(discord.SelectOption(
                        label=f"{icon} {t(ctx, label_key)}",
                        value=key,
                    ))
                super().__init__(placeholder=t(ctx, "QUEUE_FOOTER_TOGGLE_PLACEHOLDER"), options=options, row=2)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                from core.music_handlers import DEFAULT_QUEUE_FIELDS
                key = self.values[0]
                full = cog.guild_embed_layouts.get(guild_id, {})
                sub = full.setdefault("queue_fields", {})
                if key not in sub:
                    sub[key] = dict(DEFAULT_QUEUE_FIELDS.get(key, {"enabled": True}))
                sub[key]["enabled"] = not sub[key].get("enabled", True)
                cog.guild_embed_layouts[guild_id] = full
                await db.set_embed_layout(guild_id, full)
                self.view_ref._rebuild_queue_display()
                await btn_interaction.response.edit_message(
                    embed=cog._build_detail_embed(btn_interaction, guild_id, "queue_display"),
                    view=self.view_ref)

        class ViewChannelSelect(discord.ui.ChannelSelect):
            def __init__(self, view_ref):
                super().__init__(
                    channel_types=[discord.ChannelType.text, discord.ChannelType.voice],
                    placeholder=t(ctx, "SETTINGS_SHOW_VIEW_RESTRICT"),
                    min_values=0, max_values=1, row=0,
                )
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                if cog.guild_view_restricts.get(guild_id, 0) == 3 and not _effective_owner:
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "ONLY_OWNER_CAN_SET"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                if self.values:
                    ch_id = self.values[0].id
                    cog.guild_view_channels[guild_id] = ch_id
                    await db.set_view_channel(guild_id, ch_id)
                else:
                    cog.guild_view_channels.pop(guild_id, None)
                    await db.set_view_channel(guild_id, None)
                self.view_ref._rebuild_view_restrict()
                embed = cog._build_detail_embed(btn_interaction, guild_id, "view_restrict")
                # Auto-send if restrict_all + channel
                if cog.guild_view_restricts.get(guild_id, 0) == 3 and self.values:
                    real_ch = btn_interaction.guild.get_channel(ch_id)
                    if real_ch:
                        await btn_interaction.response.defer()
                        await cog._send_views_to_channel(btn_interaction.guild, real_ch)
                        await btn_interaction.edit_original_response(embed=embed, view=self.view_ref)
                        return
                await btn_interaction.response.edit_message(embed=embed, view=self.view_ref)

        class ViewRestrictSelect(discord.ui.Select):
            _LEVELS = [
                ("0", "VIEW_RESTRICT_DISABLED"),
                ("1", "VIEW_RESTRICT_NON_DJ"),
                ("2", "VIEW_RESTRICT_DJ_USER"),
                ("3", "VIEW_RESTRICT_ALL"),
            ]

            def __init__(self, view_ref):
                current = str(cog.guild_view_restricts.get(guild_id, 0))
                options = [
                    discord.SelectOption(label=t(ctx, lk), value=v, default=(v == current))
                    for v, lk in self._LEVELS
                ]
                super().__init__(options=options, row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                level = int(self.values[0])
                is_owner = _effective_owner
                current_level = cog.guild_view_restricts.get(guild_id, 0)
                if level == 3 and not is_owner:
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "ONLY_OWNER_CAN_SET"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                if current_level == 3 and not is_owner:
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "ONLY_OWNER_CAN_SET"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                cog.guild_view_restricts[guild_id] = level
                await db.set_view_restrict(guild_id, level)
                self.view_ref._rebuild_view_restrict()
                embed = cog._build_detail_embed(btn_interaction, guild_id, "view_restrict")
                ch_id = cog.guild_view_channels.get(guild_id)
                if level == 3 and ch_id:
                    ch = btn_interaction.guild.get_channel(ch_id)
                    if ch:
                        await btn_interaction.response.defer()
                        await cog._send_views_to_channel(btn_interaction.guild, ch)
                        await btn_interaction.edit_original_response(embed=embed, view=self.view_ref)
                        return
                await btn_interaction.response.edit_message(embed=embed, view=self.view_ref)

        class SendViewsButton(discord.ui.Button):
            def __init__(self, view_ref, *, disabled=False):
                super().__init__(
                    style=discord.ButtonStyle.primary,
                    label=t(ctx, "VIEW_RESTRICT_SEND_VIEWS"),
                    row=2, disabled=disabled,
                )
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                if cog.guild_view_restricts.get(guild_id, 0) == 3 and not _effective_owner:
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "ONLY_OWNER_CAN_SET"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                ch_id = cog.guild_view_channels.get(guild_id)
                if not ch_id:
                    return await btn_interaction.response.defer()
                ch = btn_interaction.guild.get_channel(ch_id)
                if not ch:
                    return await btn_interaction.response.defer()
                await btn_interaction.response.defer()
                await cog._send_views_to_channel(btn_interaction.guild, ch)
                await btn_interaction.edit_original_response(
                    embed=cog._build_detail_embed(btn_interaction, guild_id, "view_restrict"),
                    view=self.view_ref,
                )

        class ClearViewChannelButton(discord.ui.Button):
            def __init__(self, view_ref, *, disabled=False):
                super().__init__(
                    style=discord.ButtonStyle.danger,
                    emoji="✖",
                    row=2, disabled=disabled,
                )
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                is_owner = _effective_owner
                current_level = cog.guild_view_restricts.get(guild_id, 0)
                if current_level == 3 and not is_owner:
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "ONLY_OWNER_CAN_SET"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                cog.guild_view_channels.pop(guild_id, None)
                await db.set_view_channel(guild_id, None)
                self.view_ref._rebuild_view_restrict()
                await btn_interaction.response.edit_message(
                    embed=cog._build_detail_embed(btn_interaction, guild_id, "view_restrict"),
                    view=self.view_ref,
                )

        class ExcludeUserSelect(discord.ui.UserSelect):
            def __init__(self, view_ref):
                super().__init__(placeholder=t(ctx, "LIMIT_USAGE_USER_PLACEHOLDER"), row=0)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                if not cog.has_admin_privilege(guild_id, btn_interaction.user):
                    return
                user = self.values[0]
                if user.id == btn_interaction.user.id:
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "EXCLUDED_CANNOT_SELF"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                if user.id == btn_interaction.guild.owner_id:
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "EXCLUDED_CANNOT_OWNER"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                if cog.has_admin_privilege(guild_id, user):
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "EXCLUDED_CANNOT_ADMIN"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                exc_set = cog.excluded_users.setdefault(guild_id, set())
                if user.id in exc_set:
                    exc_set.discard(user.id)
                    if not exc_set:
                        cog.excluded_users.pop(guild_id, None)
                    await db.remove_excluded_user(guild_id, user.id)
                else:
                    exc_set.add(user.id)
                    await db.add_excluded_user(guild_id, user.id)
                self.view_ref._rebuild_limit_usage()
                await btn_interaction.response.edit_message(embed=cog._build_detail_embed(btn_interaction, guild_id, "limit_usage"), view=self.view_ref)

        class ExcludeRoleSelect(discord.ui.RoleSelect):
            def __init__(self, view_ref):
                super().__init__(placeholder=t(ctx, "LIMIT_USAGE_ROLE_PLACEHOLDER"), row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                if not cog.has_admin_privilege(guild_id, btn_interaction.user):
                    return
                role = self.values[0]
                dj_role_id = cog.dj_roles.get(guild_id)
                if role.id == dj_role_id:
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "EXCLUDED_ROLE_IS_DJ"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                exc_set = cog.excluded_roles.setdefault(guild_id, set())
                if role.id in exc_set:
                    exc_set.discard(role.id)
                    if not exc_set:
                        cog.excluded_roles.pop(guild_id, None)
                    await db.remove_excluded_role(guild_id, role.id)
                else:
                    exc_set.add(role.id)
                    await db.add_excluded_role(guild_id, role.id)
                self.view_ref._rebuild_limit_usage()
                await btn_interaction.response.edit_message(embed=cog._build_detail_embed(btn_interaction, guild_id, "limit_usage"), view=self.view_ref)

        class ClearExcludedUsersButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.danger, label=t(ctx, "EXCLUDED_CLEAR_USERS"), row=2)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                if not cog.has_admin_privilege(guild_id, btn_interaction.user):
                    return
                cog.excluded_users.pop(guild_id, None)
                await db.clear_excluded_users(guild_id)
                self.view_ref._rebuild_limit_usage()
                await btn_interaction.response.edit_message(embed=cog._build_detail_embed(btn_interaction, guild_id, "limit_usage"), view=self.view_ref)

        class ClearExcludedRolesButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.danger, label=t(ctx, "EXCLUDED_CLEAR_ROLES"), row=2)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                if not cog.has_admin_privilege(guild_id, btn_interaction.user):
                    return
                cog.excluded_roles.pop(guild_id, None)
                await db.clear_excluded_roles(guild_id)
                self.view_ref._rebuild_limit_usage()
                await btn_interaction.response.edit_message(embed=cog._build_detail_embed(btn_interaction, guild_id, "limit_usage"), view=self.view_ref)

        class AdminUserSelect(discord.ui.UserSelect):
            def __init__(self, view_ref):
                super().__init__(placeholder=t(ctx, "MANAGE_PERMS_USER_PLACEHOLDER"), row=0)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                if not _effective_owner:
                    return
                user = self.values[0]
                if user.id == btn_interaction.guild.owner_id:
                    return await btn_interaction.response.send_message(
                        t(btn_interaction, "MANAGE_PERMS_IS_OWNER"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                adm_set = cog.admin_users.setdefault(guild_id, set())
                if user.id in adm_set:
                    adm_set.discard(user.id)
                    if not adm_set:
                        cog.admin_users.pop(guild_id, None)
                    await db.remove_admin_user(guild_id, user.id)
                else:
                    adm_set.add(user.id)
                    await db.add_admin_user(guild_id, user.id)
                self.view_ref._rebuild_manage_perms()
                await btn_interaction.response.edit_message(embed=cog._build_detail_embed(btn_interaction, guild_id, "manage_perms"), view=self.view_ref)

        class AdminRoleSelect(discord.ui.RoleSelect):
            def __init__(self, view_ref):
                super().__init__(placeholder=t(ctx, "MANAGE_PERMS_ROLE_PLACEHOLDER"), row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                if not _effective_owner:
                    return
                role = self.values[0]
                adm_set = cog.admin_roles.setdefault(guild_id, set())
                if role.id in adm_set:
                    adm_set.discard(role.id)
                    if not adm_set:
                        cog.admin_roles.pop(guild_id, None)
                    await db.remove_admin_role(guild_id, role.id)
                else:
                    adm_set.add(role.id)
                    await db.add_admin_role(guild_id, role.id)
                self.view_ref._rebuild_manage_perms()
                await btn_interaction.response.edit_message(embed=cog._build_detail_embed(btn_interaction, guild_id, "manage_perms"), view=self.view_ref)

        class AdminPrivToggle(discord.ui.Button):
            def __init__(self, view_ref):
                enabled = cog.guild_admin_priv.get(guild_id, 1)
                label = t(ctx, "MANAGE_PERMS_ADMIN_PRIV") + ": " + t(ctx, "STATE_ON" if enabled else "STATE_OFF")
                style = discord.ButtonStyle.success if enabled else discord.ButtonStyle.secondary
                super().__init__(style=style, label=label, row=2)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                if not _effective_owner:
                    return
                current = cog.guild_admin_priv.get(guild_id, 1)
                new_val = 0 if current else 1
                cog.guild_admin_priv[guild_id] = new_val
                await db.set_admin_priv(guild_id, new_val)
                self.view_ref._rebuild_manage_perms()
                await btn_interaction.response.edit_message(embed=cog._build_detail_embed(btn_interaction, guild_id, "manage_perms"), view=self.view_ref)

        class ClearAdminAllButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.danger, label=t(ctx, "MANAGE_PERMS_CLEAR_ALL"), row=3)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                if not _effective_owner:
                    return
                cog.admin_users.pop(guild_id, None)
                cog.admin_roles.pop(guild_id, None)
                await db.clear_admin_users(guild_id)
                await db.clear_admin_roles(guild_id)
                self.view_ref._rebuild_manage_perms()
                await btn_interaction.response.edit_message(embed=cog._build_detail_embed(btn_interaction, guild_id, "manage_perms"), view=self.view_ref)

        class EmbedViewsSelect(discord.ui.Select):
            def __init__(self, view_ref):
                mp_l = cog.get_mp_layout(guild_id)
                q_l = cog.get_queue_layout(guild_id)
                mp_on = sum(1 for v in mp_l.values() if v.get("enabled", True))
                q_on = sum(1 for v in q_l.values() if v.get("enabled", True))
                mp_cm = t(ctx, "STATE_ON") if cog.is_compact(guild_id) else t(ctx, "STATE_OFF")
                q_cm = t(ctx, "STATE_ON") if cog.is_queue_button_compact(guild_id) else t(ctx, "STATE_OFF")
                pp = cog.guild_queue_per_page.get(guild_id, 10)
                qc = cog.guild_queue_compact.get(guild_id, True)
                qd_mode = t(ctx, "SETTINGS_QD_NORMAL") if qc else t(ctx, "SETTINGS_QD_SPACIOUS")
                _VR = {0: "VIEW_RESTRICT_DISABLED", 1: "VIEW_RESTRICT_NON_DJ", 2: "VIEW_RESTRICT_DJ_USER", 3: "VIEW_RESTRICT_ALL"}
                lvl = cog.guild_view_restricts.get(guild_id, 0)
                ch_id = cog.guild_view_channels.get(guild_id)
                ch_obj = ctx.guild.get_channel(ch_id) if ch_id else None
                vr_desc = f"#{ch_obj.name} / {t(ctx, _VR.get(lvl, 'VIEW_RESTRICT_DISABLED'))}" if ch_obj else t(ctx, _VR.get(lvl, "VIEW_RESTRICT_DISABLED"))
                ec = cog.guild_embed_colors.get(guild_id)
                ec_desc = f"#{ec:06X}" if ec is not None else t(ctx, "COLOR_DEFAULT_LABEL")
                mp_f = cog.get_mp_fields(guild_id)
                mf_on = sum(1 for v in mp_f.values() if v.get("enabled", True))
                options = [
                    discord.SelectOption(label=t(ctx, "SETTINGS_SHOW_EMBED_COLOR"), value="embed_color",
                                         description=ec_desc[:100]),
                    discord.SelectOption(label=t(ctx, "EMBED_VIEWS_MP_BUTTONS"), value="mp_buttons",
                                         description=f"{mp_on}/{len(mp_l)}, {t(ctx, 'ABBR_COMPACT')}: {mp_cm}"[:100]),
                    discord.SelectOption(label=t(ctx, "SETTINGS_SHOW_MP_DISPLAY"), value="mp_display",
                                         description=f"{mf_on}/{len(mp_f)}"[:100]),
                    discord.SelectOption(label=t(ctx, "EMBED_VIEWS_QUEUE_BUTTONS"), value="queue_buttons",
                                         description=f"{q_on}/{len(q_l)}, {t(ctx, 'ABBR_COMPACT')}: {q_cm}"[:100]),
                    discord.SelectOption(label=t(ctx, "SETTINGS_SHOW_QUEUE_DISPLAY"), value="queue_display",
                                         description=f"{qd_mode}, {pp}{t(ctx, 'ABBR_PER_PAGE')}"[:100]),
                    discord.SelectOption(label=t(ctx, "SETTINGS_SHOW_VIEW_RESTRICT"), value="view_restrict",
                                         description=vr_desc[:100]),
                ]
                super().__init__(placeholder=t(ctx, "EMBED_VIEWS_SELECT_PLACEHOLDER"), options=options, row=0)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                chosen = self.values[0]
                if chosen == "embed_color":
                    self.view_ref._rebuild_embed_color()
                    embed = cog._build_detail_embed(btn_interaction, guild_id, "embed_color")
                elif chosen == "mp_buttons":
                    self.view_ref._active_layout_view = "mp"
                    self.view_ref._rebuild_embed_layout_buttons()
                    embed = cog._build_layout_detail_embed(btn_interaction, guild_id, "mp")
                elif chosen == "queue_buttons":
                    self.view_ref._active_layout_view = "queue"
                    self.view_ref._rebuild_embed_layout_buttons()
                    embed = cog._build_layout_detail_embed(btn_interaction, guild_id, "queue")
                elif chosen == "queue_display":
                    self.view_ref._rebuild_queue_display()
                    embed = cog._build_detail_embed(btn_interaction, guild_id, "queue_display")
                elif chosen == "mp_display":
                    self.view_ref._rebuild_mp_display_detail()
                    embed = cog._build_mp_display_detail_embed(btn_interaction, guild_id)
                elif chosen == "view_restrict":
                    self.view_ref._rebuild_view_restrict()
                    embed = cog._build_detail_embed(btn_interaction, guild_id, "view_restrict")
                else:
                    return
                await btn_interaction.response.edit_message(embed=embed, view=self.view_ref)

        class EmbedLayoutButtonToggle(discord.ui.Select):
            def __init__(self, view_ref, view_type):
                from core.music_handlers import _MP_BUTTON_LABELS, _QUEUE_BUTTON_LABELS, DEFAULT_MP_LAYOUT, DEFAULT_QUEUE_LAYOUT
                labels = _MP_BUTTON_LABELS if view_type == "mp" else _QUEUE_BUTTON_LABELS
                layout = cog.get_mp_layout(guild_id) if view_type == "mp" else cog.get_queue_layout(guild_id)
                options = []
                for key in labels:
                    cfg = layout.get(key, {})
                    enabled = cfg.get("enabled", True)
                    icon = "✅" if enabled else "❌"
                    r, c = cfg.get("row", 0), cfg.get("col", 0)
                    lbl = t(ctx, labels[key])
                    options.append(discord.SelectOption(
                        label=f"{icon} {lbl}",
                        value=key,
                        description=f"{t(ctx, 'ABBR_ROW')}: {r+1} {t(ctx, 'ABBR_COL')}: {c+1}",
                    ))
                super().__init__(placeholder=t(ctx, "EMBED_LAYOUT_TOGGLE_PLACEHOLDER"), options=options, row=0)
                self.view_ref = view_ref
                self.view_type = view_type

            async def callback(self, btn_interaction: discord.Interaction):
                key = self.values[0]
                full = cog.guild_embed_layouts.get(guild_id, {})
                sub = full.setdefault(self.view_type, {})
                from core.music_handlers import DEFAULT_MP_LAYOUT, DEFAULT_QUEUE_LAYOUT
                defaults = DEFAULT_MP_LAYOUT if self.view_type == "mp" else DEFAULT_QUEUE_LAYOUT
                if key not in sub:
                    sub[key] = dict(defaults.get(key, {"row": 0, "col": 0, "enabled": True}))
                sub[key]["enabled"] = not sub[key].get("enabled", True)
                cog.guild_embed_layouts[guild_id] = full
                await db.set_embed_layout(guild_id, full)
                self.view_ref._rebuild_embed_layout_buttons()
                await btn_interaction.response.edit_message(
                    embed=cog._build_layout_detail_embed(btn_interaction, guild_id, self.view_type),
                    view=self.view_ref)

        class EmbedLayoutEnableAll(discord.ui.Button):
            def __init__(self, view_ref, view_type):
                super().__init__(style=discord.ButtonStyle.success, label=t(ctx, "EMBED_LAYOUT_ENABLE_ALL"), row=1)
                self.view_ref = view_ref
                self.view_type = view_type

            async def callback(self, btn_interaction: discord.Interaction):
                full = cog.guild_embed_layouts.get(guild_id, {})
                sub = full.get(self.view_type, {})
                for v in sub.values():
                    v["enabled"] = True
                cog.guild_embed_layouts[guild_id] = full
                await db.set_embed_layout(guild_id, full)
                self.view_ref._rebuild_embed_layout_buttons()
                await btn_interaction.response.edit_message(
                    embed=cog._build_layout_detail_embed(btn_interaction, guild_id, self.view_type),
                    view=self.view_ref)

        class EmbedLayoutDisableAll(discord.ui.Button):
            def __init__(self, view_ref, view_type):
                super().__init__(style=discord.ButtonStyle.danger, label=t(ctx, "EMBED_LAYOUT_DISABLE_ALL"), row=1)
                self.view_ref = view_ref
                self.view_type = view_type

            async def callback(self, btn_interaction: discord.Interaction):
                from core.music_handlers import DEFAULT_MP_LAYOUT, DEFAULT_QUEUE_LAYOUT
                defaults = DEFAULT_MP_LAYOUT if self.view_type == "mp" else DEFAULT_QUEUE_LAYOUT
                full = cog.guild_embed_layouts.get(guild_id, {})
                sub = full.setdefault(self.view_type, {})
                for key in defaults:
                    if key not in sub:
                        sub[key] = dict(defaults[key])
                    sub[key]["enabled"] = False
                cog.guild_embed_layouts[guild_id] = full
                await db.set_embed_layout(guild_id, full)
                self.view_ref._rebuild_embed_layout_buttons()
                await btn_interaction.response.edit_message(
                    embed=cog._build_layout_detail_embed(btn_interaction, guild_id, self.view_type),
                    view=self.view_ref)

        class EmbedLayoutCompactToggle(discord.ui.Button):
            def __init__(self, view_ref):
                vt = getattr(view_ref, '_active_layout_view', 'mp') or 'mp'
                cm = cog.is_compact(guild_id) if vt == 'mp' else cog.is_queue_button_compact(guild_id)
                label = f"{t(ctx, 'ABBR_COMPACT')}: {t(ctx, 'STATE_ON') if cm else t(ctx, 'STATE_OFF')}"
                super().__init__(style=discord.ButtonStyle.primary if cm else discord.ButtonStyle.secondary, label=label, row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                vt = getattr(self.view_ref, '_active_layout_view', 'mp') or 'mp'
                if vt == 'mp':
                    current = cog.guild_compact_modes.get(guild_id, False)
                    new_val = not current
                    cog.guild_compact_modes[guild_id] = new_val
                    await db.set_compact_mode(guild_id, new_val)
                else:
                    current = bool(cog.guild_queue_button_compact.get(guild_id, 0))
                    new_val = 0 if current else 1
                    cog.guild_queue_button_compact[guild_id] = new_val
                    await db.set_queue_button_compact(guild_id, new_val)
                self.view_ref._rebuild_embed_layout_buttons()
                await btn_interaction.response.edit_message(
                    embed=cog._build_layout_detail_embed(btn_interaction, guild_id, vt),
                    view=self.view_ref)

        class EmbedLayoutResetButton(discord.ui.Button):
            def __init__(self, view_ref, view_type):
                super().__init__(style=discord.ButtonStyle.danger, label=t(ctx, "EMBED_LAYOUT_RESET"), row=1)
                self.view_ref = view_ref
                self.view_type = view_type

            async def callback(self, btn_interaction: discord.Interaction):
                full = cog.guild_embed_layouts.get(guild_id, {})
                full.pop(self.view_type, None)
                if full:
                    cog.guild_embed_layouts[guild_id] = full
                    await db.set_embed_layout(guild_id, full)
                else:
                    cog.guild_embed_layouts.pop(guild_id, None)
                    await db.set_embed_layout(guild_id, {})
                self.view_ref._rebuild_embed_layout_buttons()
                await btn_interaction.response.edit_message(
                    embed=cog._build_layout_detail_embed(btn_interaction, guild_id, self.view_type),
                    view=self.view_ref)

        class EmbedLayoutReorderButton(discord.ui.Button):
            def __init__(self, view_ref, view_type):
                super().__init__(style=discord.ButtonStyle.secondary, label=t(ctx, "EMBED_LAYOUT_REORDER"), row=2)
                self.view_ref = view_ref
                self.view_type = view_type

            async def callback(self, btn_interaction: discord.Interaction):
                await btn_interaction.response.send_modal(EmbedLayoutReorderModal(self.view_ref, self.view_type))

        class EmbedLayoutReorderModal(discord.ui.Modal):
            def __init__(self, view_ref, view_type):
                super().__init__(title=t(ctx, "EMBED_LAYOUT_REORDER_TITLE"))
                self.view_ref = view_ref
                self.view_type = view_type
                from core.music_handlers import _MP_BUTTON_LABELS, _QUEUE_BUTTON_LABELS
                labels = _MP_BUTTON_LABELS if view_type == "mp" else _QUEUE_BUTTON_LABELS
                self._ordered_keys = list(labels.keys())
                total = len(self._ordered_keys)
                self.btn_input = discord.ui.TextInput(
                    label=t(ctx, "EMBED_LAYOUT_BUTTON_ID"),
                    placeholder=f"1-{total}",
                    max_length=2, required=True,
                )
                self.row_input = discord.ui.TextInput(
                    label=t(ctx, "EMBED_LAYOUT_ROW_LABEL"),
                    placeholder="1-5", max_length=1, required=True,
                )
                self.col_input = discord.ui.TextInput(
                    label=t(ctx, "EMBED_LAYOUT_COL_LABEL"),
                    placeholder="1-5", max_length=1, required=True,
                )
                self.add_item(self.btn_input)
                self.add_item(self.row_input)
                self.add_item(self.col_input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                from core.music_handlers import DEFAULT_MP_LAYOUT, DEFAULT_QUEUE_LAYOUT
                defaults = DEFAULT_MP_LAYOUT if self.view_type == "mp" else DEFAULT_QUEUE_LAYOUT
                total = len(self._ordered_keys)
                try:
                    btn_idx = int(self.btn_input.value.strip()) - 1
                except ValueError:
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                if not (0 <= btn_idx < total):
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT_RANGE", min=1, max=total),
                        ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                btn_name = self._ordered_keys[btn_idx]
                if btn_name not in defaults:
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                try:
                    row_val = int(self.row_input.value.strip()) - 1
                    col_val = int(self.col_input.value.strip()) - 1
                except ValueError:
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                if not (0 <= row_val <= 4 and 0 <= col_val <= 4):
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT_RANGE", min=1, max=5), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                full = cog.guild_embed_layouts.get(guild_id, {})
                sub = full.setdefault(self.view_type, {})
                if btn_name not in sub:
                    sub[btn_name] = dict(defaults[btn_name])
                old_row = sub[btn_name].get("row", 0)
                old_col = sub[btn_name].get("col", 0)
                # Swap: if another button occupies the target position, move it to the source position
                for other_key, other_cfg in sub.items():
                    if other_key != btn_name and other_cfg.get("row", 0) == row_val and other_cfg.get("col", 0) == col_val:
                        other_cfg["row"] = old_row
                        other_cfg["col"] = old_col
                        break
                else:
                    for other_key in defaults:
                        if other_key != btn_name and other_key not in sub:
                            d = defaults[other_key]
                            if d.get("row", 0) == row_val and d.get("col", 0) == col_val:
                                sub[other_key] = dict(d)
                                sub[other_key]["row"] = old_row
                                sub[other_key]["col"] = old_col
                                break
                sub[btn_name]["row"] = row_val
                sub[btn_name]["col"] = col_val
                cog.guild_embed_layouts[guild_id] = full
                await db.set_embed_layout(guild_id, full)
                self.view_ref._rebuild_embed_layout_buttons()
                await modal_interaction.response.edit_message(
                    embed=cog._build_layout_detail_embed(modal_interaction, guild_id, self.view_type),
                    view=self.view_ref)

        # --- MP Display (field toggle / reorder) ---

        class MpFieldToggleSelect(discord.ui.Select):
            def __init__(self, view_ref):
                from core.music_handlers import _MP_FIELD_LABELS, DEFAULT_MP_FIELDS
                fields = cog.get_mp_fields(guild_id)
                sorted_fields = sorted(fields.items(), key=lambda x: x[1].get("order", 99))
                options = []
                for idx, (key, cfg) in enumerate(sorted_fields, 1):
                    enabled = cfg.get("enabled", True)
                    icon = "✅" if enabled else "❌"
                    lbl = t(ctx, _MP_FIELD_LABELS.get(key, key))
                    options.append(discord.SelectOption(
                        label=f"{icon} {lbl}",
                        value=key,
                        description=f"#{idx}",
                    ))
                super().__init__(placeholder=t(ctx, "MP_FIELD_TOGGLE_PLACEHOLDER"), options=options, row=0)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                key = self.values[0]
                from core.music_handlers import DEFAULT_MP_FIELDS
                full = cog.guild_embed_layouts.get(guild_id, {})
                sub = full.setdefault("mp_fields", {})
                if key not in sub:
                    sub[key] = dict(DEFAULT_MP_FIELDS.get(key, {"order": 0, "enabled": True}))
                sub[key]["enabled"] = not sub[key].get("enabled", True)
                cog.guild_embed_layouts[guild_id] = full
                await db.set_embed_layout(guild_id, full)
                self.view_ref._rebuild_mp_display_detail()
                await btn_interaction.response.edit_message(
                    embed=cog._build_mp_display_detail_embed(btn_interaction, guild_id),
                    view=self.view_ref)

        class MpFieldEnableAll(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.success, label=t(ctx, "EMBED_LAYOUT_ENABLE_ALL"), row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                from core.music_handlers import DEFAULT_MP_FIELDS
                full = cog.guild_embed_layouts.get(guild_id, {})
                sub = full.setdefault("mp_fields", {})
                for key in DEFAULT_MP_FIELDS:
                    if key not in sub:
                        sub[key] = dict(DEFAULT_MP_FIELDS[key])
                    sub[key]["enabled"] = True
                cog.guild_embed_layouts[guild_id] = full
                await db.set_embed_layout(guild_id, full)
                self.view_ref._rebuild_mp_display_detail()
                await btn_interaction.response.edit_message(
                    embed=cog._build_mp_display_detail_embed(btn_interaction, guild_id),
                    view=self.view_ref)

        class MpFieldDisableAll(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.danger, label=t(ctx, "EMBED_LAYOUT_DISABLE_ALL"), row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                from core.music_handlers import DEFAULT_MP_FIELDS
                full = cog.guild_embed_layouts.get(guild_id, {})
                sub = full.setdefault("mp_fields", {})
                for key in DEFAULT_MP_FIELDS:
                    if key not in sub:
                        sub[key] = dict(DEFAULT_MP_FIELDS[key])
                    sub[key]["enabled"] = False
                cog.guild_embed_layouts[guild_id] = full
                await db.set_embed_layout(guild_id, full)
                self.view_ref._rebuild_mp_display_detail()
                await btn_interaction.response.edit_message(
                    embed=cog._build_mp_display_detail_embed(btn_interaction, guild_id),
                    view=self.view_ref)

        class MpFieldResetButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.danger, label=t(ctx, "EMBED_LAYOUT_RESET"), row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                full = cog.guild_embed_layouts.get(guild_id, {})
                full.pop("mp_fields", None)
                if full:
                    cog.guild_embed_layouts[guild_id] = full
                    await db.set_embed_layout(guild_id, full)
                else:
                    cog.guild_embed_layouts.pop(guild_id, None)
                    await db.set_embed_layout(guild_id, {})
                self.view_ref._rebuild_mp_display_detail()
                await btn_interaction.response.edit_message(
                    embed=cog._build_mp_display_detail_embed(btn_interaction, guild_id),
                    view=self.view_ref)

        class MpFieldReorderButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.secondary, label=t(ctx, "EMBED_LAYOUT_REORDER"), row=2)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                await btn_interaction.response.send_modal(MpFieldReorderModal(self.view_ref))

        class MpFieldReorderModal(discord.ui.Modal):
            def __init__(self, view_ref):
                super().__init__(title=t(ctx, "MP_FIELD_REORDER_TITLE"))
                self.view_ref = view_ref
                from core.music_handlers import _MP_REORDERABLE_FIELDS
                fields = cog.get_mp_fields(guild_id)
                reorderable = {k: v for k, v in fields.items() if k in _MP_REORDERABLE_FIELDS}
                self._sorted_keys = [k for k, _ in sorted(reorderable.items(), key=lambda x: x[1].get("order", 99))]
                total = len(self._sorted_keys)
                self.field_input = discord.ui.TextInput(
                    label=t(ctx, "MP_FIELD_ID_LABEL"),
                    placeholder=f"1-{total}",
                    max_length=2, required=True,
                )
                self.pos_input = discord.ui.TextInput(
                    label=t(ctx, "MP_FIELD_NEW_POS_LABEL"),
                    placeholder=f"1-{total}", max_length=2, required=True,
                )
                self.add_item(self.field_input)
                self.add_item(self.pos_input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                from core.music_handlers import DEFAULT_MP_FIELDS, _MP_REORDERABLE_FIELDS
                total = len(self._sorted_keys)
                try:
                    idx = int(self.field_input.value.strip()) - 1
                except ValueError:
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                if not (0 <= idx < total):
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT_RANGE", min=1, max=total),
                        ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                target_key = self._sorted_keys[idx]
                try:
                    new_order = int(self.pos_input.value.strip()) - 1
                except ValueError:
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                if not (0 <= new_order < total):
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT_RANGE", min=1, max=total),
                        ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                full = cog.guild_embed_layouts.get(guild_id, {})
                sub = full.setdefault("mp_fields", {})
                if target_key not in sub:
                    sub[target_key] = dict(DEFAULT_MP_FIELDS[target_key])
                old_order = sub[target_key].get("order", 0)
                for other_key, other_cfg in sub.items():
                    if other_key != target_key and other_key in _MP_REORDERABLE_FIELDS and other_cfg.get("order", 0) == new_order:
                        other_cfg["order"] = old_order
                        break
                else:
                    for other_key, other_def in DEFAULT_MP_FIELDS.items():
                        if other_key != target_key and other_key in _MP_REORDERABLE_FIELDS and other_key not in sub:
                            if other_def.get("order", 0) == new_order:
                                sub[other_key] = dict(other_def)
                                sub[other_key]["order"] = old_order
                                break
                sub[target_key]["order"] = new_order
                cog.guild_embed_layouts[guild_id] = full
                await db.set_embed_layout(guild_id, full)
                self.view_ref._rebuild_mp_display_detail()
                await modal_interaction.response.edit_message(
                    embed=cog._build_mp_display_detail_embed(modal_interaction, guild_id),
                    view=self.view_ref)

        _ACT_TYPES = [(0, "ACTIVITY_PLAYING"), (2, "ACTIVITY_LISTENING"), (3, "ACTIVITY_WATCHING"), (5, "ACTIVITY_COMPETING")]

        class ActTypeSelect(discord.ui.Select):
            def __init__(self, view_ref, *, current=2, mode='add'):
                options = []
                for val, lkey in _ACT_TYPES:
                    options.append(discord.SelectOption(label=t(ctx, lkey), value=str(val), default=(val == current)))
                super().__init__(placeholder=t(ctx, "ACTIVITY_TYPE_PLACEHOLDER"), options=options, row=0)
                self.view_ref = view_ref
                self._mode = mode

            async def callback(self, sel_interaction: discord.Interaction):
                self.view_ref._act_type_choice = int(self.values[0])
                if self._mode == 'edit':
                    item = getattr(self.view_ref, '_editing_item', None)
                    if item:
                        atype = int(self.values[0])
                        item_id = item["id"]
                        await db.update_bot_activity_item(item_id, atype, item["text"])
                        cog.bot_activity_list = await db.get_bot_activity_list()
                        idx = next((i for i, x in enumerate(cog.bot_activity_list) if x["id"] == item_id), None)
                        if idx is not None:
                            self.view_ref._editing_item = cog.bot_activity_list[idx]
                        is_current = (cog.bot_activity_mode == "static" and idx == cog.bot_activity_selected) or \
                                     (cog.bot_activity_mode != "static" and getattr(cog, '_activity_last_id', None) == item_id)
                        if is_current:
                            await cog._set_presence(atype, item["text"])
                    self.view_ref._rebuild_bot_activity_edit()
                    item = getattr(self.view_ref, '_editing_item', None)
                    embed = cog._build_activity_edit_embed(sel_interaction, item, self.view_ref._act_type_choice)
                else:
                    self.view_ref._rebuild_bot_activity_add()
                    embed = cog._build_activity_add_embed(sel_interaction, self.view_ref._act_type_choice)
                await sel_interaction.response.edit_message(embed=embed, view=self.view_ref)

        class ActAddButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.success, label=t(ctx, "ACTIVITY_ADD"), row=0,
                                 disabled=len(cog.bot_activity_list) >= 10)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                self.view_ref._act_type_choice = 2
                self.view_ref._rebuild_bot_activity_add()
                await btn_interaction.response.edit_message(
                    embed=cog._build_activity_add_embed(btn_interaction, 2), view=self.view_ref)

        class ActAddModal(discord.ui.Modal):
            def __init__(self, view_ref):
                super().__init__(title=t(ctx, "ACTIVITY_ADD_TITLE"))
                self._view = view_ref
                self.text_input = discord.ui.TextInput(
                    label=t(ctx, "ACTIVITY_ADD_LABEL"), placeholder="/play", max_length=128, required=True)
                self.add_item(self.text_input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                text = self.text_input.value.strip()
                if not text:
                    return await modal_interaction.response.send_message(t(modal_interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                if len(cog.bot_activity_list) >= 10:
                    return await modal_interaction.response.send_message(t(modal_interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                was_empty = not cog.bot_activity_list
                atype = getattr(self._view, '_act_type_choice', 2)
                await db.add_bot_activity_item(atype, text)
                cog.bot_activity_list = await db.get_bot_activity_list()
                if was_empty:
                    await cog.update_presence()
                    cog.start_activity_cycle()
                added_item = {"type": atype, "text": text}
                self._view._rebuild_bot_activity_add()
                atype_cur = getattr(self._view, '_act_type_choice', 2)
                await modal_interaction.response.edit_message(
                    embed=cog._build_activity_add_embed(modal_interaction, atype_cur, last_added=added_item), view=self._view)

        class ActAddTextButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.success, label=t(ctx, "ACTIVITY_ADD_TEXT"), row=1,
                                 disabled=len(cog.bot_activity_list) >= 10)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                await btn_interaction.response.send_modal(ActAddModal(self.view_ref))

        class ActAddBackButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.secondary, emoji="◀️", label=t(ctx, "SETTINGS_BACK"), row=2)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                self.view_ref._act_page = 0
                self.view_ref._rebuild_bot_activity()
                await btn_interaction.response.edit_message(
                    embed=cog._build_activity_embed(btn_interaction, page=0, guild_id=guild_id), view=self.view_ref)

        class ActEditButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.secondary, label=t(ctx, "ACTIVITY_EDIT"), row=0,
                                 disabled=not cog.bot_activity_list)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                await btn_interaction.response.send_modal(ActEditIndexModal(self.view_ref))

        class ActEditIndexModal(discord.ui.Modal):
            def __init__(self, view_ref):
                super().__init__(title=t(ctx, "ACTIVITY_EDIT_INDEX_TITLE"))
                self._view = view_ref
                self.index_input = discord.ui.TextInput(
                    label=t(ctx, "ACTIVITY_EDIT_INDEX_LABEL"),
                    placeholder=f"1-{len(cog.bot_activity_list)}",
                    max_length=5, required=True)
                self.add_item(self.index_input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                try:
                    idx = int(self.index_input.value.strip())
                except ValueError:
                    return await modal_interaction.response.send_message(t(modal_interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                lst = cog.bot_activity_list
                if not (1 <= idx <= len(lst)):
                    return await modal_interaction.response.send_message(t(modal_interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                item = lst[idx - 1]
                self._view._editing_item = item
                self._view._act_type_choice = item["type"]
                self._view._rebuild_bot_activity_edit()
                await modal_interaction.response.edit_message(
                    embed=cog._build_activity_edit_embed(modal_interaction, item, item["type"]), view=self._view)

        class ActEditTextButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.primary, label=t(ctx, "ACTIVITY_EDIT_TEXT"), row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                await btn_interaction.response.send_modal(ActEditTextModal(self.view_ref))

        class ActEditTextModal(discord.ui.Modal):
            def __init__(self, view_ref):
                super().__init__(title=t(ctx, "ACTIVITY_EDIT_TEXT_TITLE"))
                self._view = view_ref
                item = getattr(view_ref, '_editing_item', None)
                self.text_input = discord.ui.TextInput(
                    label=t(ctx, "ACTIVITY_EDIT_TEXT_LABEL"),
                    default=item["text"] if item else "",
                    max_length=128, required=True)
                self.add_item(self.text_input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                text = self.text_input.value.strip()
                if not text:
                    return await modal_interaction.response.send_message(t(modal_interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                item = getattr(self._view, '_editing_item', None)
                if not item:
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT"), ephemeral=True,
                        delete_after=cog._resolve_delete_after(guild_id))
                atype = getattr(self._view, '_act_type_choice', item["type"])
                item_id = item["id"]
                await db.update_bot_activity_item(item_id, atype, text)
                cog.bot_activity_list = await db.get_bot_activity_list()
                idx = next((i for i, x in enumerate(cog.bot_activity_list) if x["id"] == item_id), None)
                is_current = (cog.bot_activity_mode == "static" and idx == cog.bot_activity_selected) or \
                             (cog.bot_activity_mode != "static" and getattr(cog, '_activity_last_id', None) == item_id)
                if is_current:
                    await cog._set_presence(atype, text)
                if idx is not None:
                    self._view._editing_item = cog.bot_activity_list[idx]
                self._view._rebuild_bot_activity_edit()
                item_now = getattr(self._view, '_editing_item', None)
                atype_now = getattr(self._view, '_act_type_choice', item_now["type"] if item_now else 2)
                await modal_interaction.response.edit_message(
                    embed=cog._build_activity_edit_embed(modal_interaction, item_now, atype_now), view=self._view)

        class ActEditBackButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.secondary, emoji="◀️", label=t(ctx, "SETTINGS_BACK"), row=2)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                self.view_ref._editing_item = None
                self.view_ref._act_page = 0
                self.view_ref._rebuild_bot_activity()
                await btn_interaction.response.edit_message(
                    embed=cog._build_activity_embed(btn_interaction, page=0, guild_id=guild_id), view=self.view_ref)

        class ActResetButton(discord.ui.Button):
            def __init__(self, view_ref, *, row=0):
                super().__init__(style=discord.ButtonStyle.danger, label=t(ctx, "ACTIVITY_RESET"), row=row)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                await btn_interaction.response.defer()
                await db.reset_bot_activity()
                cog.bot_activity_list = []
                cog.bot_activity_type = 2
                cog.bot_activity_text = "/play"
                cog.bot_activity_mode = "static"
                cog.bot_activity_interval = 120
                cog.bot_activity_selected = 0
                if cog._activity_cycle_task and not cog._activity_cycle_task.done():
                    cog._activity_cycle_task.cancel()
                await cog.update_presence()
                self.view_ref._act_page = 0
                self.view_ref._rebuild_bot_activity()
                await btn_interaction.edit_original_response(
                    embed=cog._build_activity_embed(btn_interaction, page=0, guild_id=guild_id), view=self.view_ref)

        class ActSelectButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.primary, label=t(ctx, "ACTIVITY_SELECT"), row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                await btn_interaction.response.send_modal(ActSelectModal(self.view_ref))

        class ActSelectModal(discord.ui.Modal):
            def __init__(self, view_ref):
                super().__init__(title=t(ctx, "ACTIVITY_SELECT_TITLE"))
                self._view = view_ref
                self.index_input = discord.ui.TextInput(
                    label=t(ctx, "ACTIVITY_SELECT_LABEL"),
                    placeholder=f"1-{len(cog.bot_activity_list)}",
                    max_length=5, required=True)
                self.add_item(self.index_input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                try:
                    idx = int(self.index_input.value.strip())
                except ValueError:
                    return await modal_interaction.response.send_message(t(modal_interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                lst = cog.bot_activity_list
                if not (1 <= idx <= len(lst)):
                    return await modal_interaction.response.send_message(t(modal_interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                cog.bot_activity_selected = idx - 1
                await db.set_bot_activity_selected(idx - 1)
                await cog.update_presence()
                self._view._rebuild_bot_activity()
                await modal_interaction.response.edit_message(
                    embed=cog._build_activity_embed(modal_interaction, page=self._view._act_page, guild_id=guild_id), view=self._view)

        class ActOrderButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.secondary, emoji="↕️", label=t(ctx, "ACTIVITY_ORDER"), row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                await btn_interaction.response.send_modal(ActOrderModal(self.view_ref))

        class ActOrderModal(discord.ui.Modal):
            def __init__(self, view_ref):
                super().__init__(title=t(ctx, "ACTIVITY_ORDER_TITLE"))
                self._view = view_ref
                n = len(cog.bot_activity_list)
                self.from_input = discord.ui.TextInput(
                    label=t(ctx, "ACTIVITY_ORDER_FROM_LABEL"), placeholder=f"1-{n}", max_length=5, required=True)
                self.to_input = discord.ui.TextInput(
                    label=t(ctx, "ACTIVITY_ORDER_TO_LABEL"), placeholder=f"1-{n}", max_length=5, required=True)
                self.add_item(self.from_input)
                self.add_item(self.to_input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                try:
                    f, to = int(self.from_input.value.strip()), int(self.to_input.value.strip())
                except ValueError:
                    return await modal_interaction.response.send_message(t(modal_interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                n = len(cog.bot_activity_list)
                if not (1 <= f <= n and 1 <= to <= n):
                    return await modal_interaction.response.send_message(t(modal_interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                sel_id = cog.bot_activity_list[cog.bot_activity_selected]["id"] if cog.bot_activity_list and cog.bot_activity_selected < len(cog.bot_activity_list) else None
                await db.move_bot_activity_item(f - 1, to - 1)
                cog.bot_activity_list = await db.get_bot_activity_list()
                if sel_id is not None:
                    new_idx = next((i for i, x in enumerate(cog.bot_activity_list) if x["id"] == sel_id), 0)
                    if new_idx != cog.bot_activity_selected:
                        cog.bot_activity_selected = new_idx
                        await db.set_bot_activity_selected(new_idx)
                self._view._rebuild_bot_activity()
                await modal_interaction.response.edit_message(
                    embed=cog._build_activity_embed(modal_interaction, page=self._view._act_page, guild_id=guild_id), view=self._view)

        class ActRemoveButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.danger, emoji="🗑️", label=t(ctx, "ACTIVITY_REMOVE"), row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                await btn_interaction.response.send_modal(ActRemoveModal(self.view_ref))

        class ActRemoveModal(discord.ui.Modal):
            def __init__(self, view_ref):
                super().__init__(title=t(ctx, "ACTIVITY_REMOVE_TITLE"))
                self._view = view_ref
                n = len(cog.bot_activity_list)
                self.index_input = discord.ui.TextInput(
                    label=t(ctx, "ACTIVITY_REMOVE_LABEL"), placeholder=f"1-{n}", max_length=5, required=True)
                self.indexto_input = discord.ui.TextInput(
                    label=t(ctx, "ACTIVITY_REMOVE_TO_LABEL"), placeholder=f"1-{n}", max_length=5, required=False)
                self.add_item(self.index_input)
                self.add_item(self.indexto_input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                try:
                    idx = int(self.index_input.value.strip())
                except ValueError:
                    return await modal_interaction.response.send_message(t(modal_interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                lst = cog.bot_activity_list
                n = len(lst)
                if not (1 <= idx <= n):
                    return await modal_interaction.response.send_message(t(modal_interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                to_val = (self.indexto_input.value or "").strip()
                if to_val:
                    try:
                        idx_to = int(to_val)
                    except ValueError:
                        return await modal_interaction.response.send_message(t(modal_interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                    if idx_to < idx or idx_to > n:
                        return await modal_interaction.response.send_message(t(modal_interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                else:
                    idx_to = idx
                to_remove = [lst[i]["id"] for i in range(idx - 1, idx_to)]
                sel_id = lst[cog.bot_activity_selected]["id"] if cog.bot_activity_list and cog.bot_activity_selected < len(lst) else None
                await modal_interaction.response.defer()
                await db.remove_bot_activity_items(to_remove)
                cog.bot_activity_list = await db.get_bot_activity_list()
                if cog.bot_activity_list:
                    new_idx = next((i for i, x in enumerate(cog.bot_activity_list) if x["id"] == sel_id), 0)
                    if new_idx != cog.bot_activity_selected:
                        cog.bot_activity_selected = new_idx
                        await db.set_bot_activity_selected(new_idx)
                elif cog.bot_activity_selected != 0:
                    cog.bot_activity_selected = 0
                    await db.set_bot_activity_selected(0)
                await cog.update_presence()
                cog.start_activity_cycle()
                pp = cog._ACT_PER_PAGE
                total_pages = max(1, (len(cog.bot_activity_list) + pp - 1) // pp)
                if self._view._act_page >= total_pages:
                    self._view._act_page = total_pages - 1
                self._view._rebuild_bot_activity()
                await modal_interaction.edit_original_response(
                    embed=cog._build_activity_embed(modal_interaction, page=self._view._act_page, guild_id=guild_id), view=self._view)

        class ActTimeButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.secondary, emoji="⏱️", label=t(ctx, "ACTIVITY_TIME"), row=1)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                await btn_interaction.response.send_modal(ActTimeModal(self.view_ref))

        class ActTimeModal(discord.ui.Modal):
            def __init__(self, view_ref):
                super().__init__(title=t(ctx, "ACTIVITY_TIME_TITLE"))
                self._view = view_ref
                self.time_input = discord.ui.TextInput(
                    label=t(ctx, "ACTIVITY_TIME_LABEL", min=ACTIVITY_INTERVAL_RANGE[0], max=ACTIVITY_INTERVAL_RANGE[1]),
                    default=str(cog.bot_activity_interval),
                    max_length=5, required=True)
                self.add_item(self.time_input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                try:
                    val = int(self.time_input.value.strip())
                except ValueError:
                    return await modal_interaction.response.send_message(t(modal_interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                if not (ACTIVITY_INTERVAL_RANGE[0] <= val <= ACTIVITY_INTERVAL_RANGE[1]):
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT_RANGE", min=ACTIVITY_INTERVAL_RANGE[0], max=ACTIVITY_INTERVAL_RANGE[1]), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                cog.bot_activity_interval = val
                await db.set_bot_activity_interval(val)
                cog.start_activity_cycle()
                self._view._rebuild_bot_activity()
                await modal_interaction.response.edit_message(
                    embed=cog._build_activity_embed(modal_interaction, page=self._view._act_page, guild_id=guild_id), view=self._view)

        class ActPageButton(discord.ui.Button):
            def __init__(self, view_ref, *, delta: int):
                _emoji = "◀️" if delta < 0 else "▶️"
                super().__init__(style=discord.ButtonStyle.secondary, emoji=_emoji, row=3)
                self.view_ref = view_ref
                self.delta = delta

            async def callback(self, btn_interaction: discord.Interaction):
                pp = cog._ACT_PER_PAGE
                total_pages = (len(cog.bot_activity_list) + pp - 1) // pp
                if total_pages == 0:
                    return await btn_interaction.response.defer()
                self.view_ref._act_page = (self.view_ref._act_page + self.delta) % total_pages
                self.view_ref._rebuild_bot_activity()
                await btn_interaction.response.edit_message(
                    embed=cog._build_activity_embed(btn_interaction, page=self.view_ref._act_page, guild_id=guild_id), view=self.view_ref)

        class ActModeSelect(discord.ui.Select):
            def __init__(self, view_ref):
                current = cog.bot_activity_mode
                options = [
                    discord.SelectOption(label=t(ctx, "ACTIVITY_MODE_STATIC"), description=t(ctx, "ACTIVITY_MODE_STATIC_DESC"), value="static", default=(current == "static")),
                    discord.SelectOption(label=t(ctx, "ACTIVITY_MODE_RANDOM"), description=t(ctx, "ACTIVITY_MODE_RANDOM_DESC"), value="random", default=(current == "random")),
                    discord.SelectOption(label=t(ctx, "ACTIVITY_MODE_ORDERED"), description=t(ctx, "ACTIVITY_MODE_ORDERED_DESC"), value="ordered", default=(current == "ordered")),
                ]
                super().__init__(placeholder=t(ctx, "ACTIVITY_MODE_PLACEHOLDER"), options=options, row=2)
                self.view_ref = view_ref

            async def callback(self, sel_interaction: discord.Interaction):
                mode = self.values[0]
                await sel_interaction.response.defer()
                cog.bot_activity_mode = mode
                await db.set_bot_activity_mode(mode)
                await cog.update_presence()
                cog.start_activity_cycle()
                self.view_ref._rebuild_bot_activity()
                await sel_interaction.edit_original_response(
                    embed=cog._build_activity_embed(sel_interaction, page=self.view_ref._act_page, guild_id=guild_id), view=self.view_ref)

        class NumberModal(discord.ui.Modal):
            def __init__(self, view_ref, key):
                self._key = key
                if key == "delete_after":
                    lo, hi = 0, 180
                    title = t(ctx, "SETTINGS_SHOW_DELETE_AFTER")
                    label = t(ctx, "SETTINGS_DELETE_AFTER_LABEL", min=5, max=hi)
                    current = str(cog.guild_delete_after.get(guild_id, 10))
                elif key == "max_playlists":
                    lo, hi = 0, 25
                    title = t(ctx, "SETTINGS_SHOW_MAX_PLAYLISTS")
                    label = t(ctx, "SETTINGS_MAX_PL_LABEL", min=lo, max=hi)
                    current = str(cog.guild_max_playlists.get(guild_id, 15))
                elif key == "max_history":
                    lo, hi = 0, 200
                    title = t(ctx, "SETTINGS_MAX_HISTORY_TITLE")
                    label = t(ctx, "SETTINGS_MAX_HISTORY_LABEL", min=lo, max=hi)
                    current = str(cog.guild_max_history.get(guild_id, 50))
                else:
                    lo, hi = 0, 100
                    title = "?"
                    label = "?"
                    current = "0"
                placeholder = f"0 / 5-{hi}" if key == "delete_after" else f"{lo}-{hi}"
                super().__init__(title=title)
                self._lo, self._hi = lo, hi
                self.view_ref = view_ref
                self.num_input = discord.ui.TextInput(
                    label=label, placeholder=placeholder,
                    default=current,
                    max_length=3, required=True,
                )
                self.add_item(self.num_input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                raw = self.num_input.value.strip()
                try:
                    val = int(raw)
                except ValueError:
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT"), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                if self._key == "delete_after":
                    if val != 0 and not (5 <= val <= self._hi):
                        return await modal_interaction.response.send_message(
                            t(modal_interaction, "INVALID_INPUT_RANGE", min=5, max=self._hi), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                elif not (self._lo <= val <= self._hi):
                    return await modal_interaction.response.send_message(
                        t(modal_interaction, "INVALID_INPUT_RANGE", min=self._lo, max=self._hi), ephemeral=True, delete_after=cog._resolve_delete_after(guild_id))
                key = self._key
                if key == "delete_after":
                    cog.guild_delete_after[guild_id] = val
                    await db.set_delete_after(guild_id, val)
                    gs = cog.guild_states.get(guild_id)
                    if gs:
                        gs.delete_after = val
                elif key == "max_playlists":
                    cog.guild_max_playlists[guild_id] = val
                    await db.set_max_playlists(guild_id, val)
                elif key == "max_history":
                    cog.guild_max_history[guild_id] = val
                    await db.set_max_history(guild_id, val)
                await modal_interaction.response.edit_message(embed=cog._build_detail_embed(modal_interaction, guild_id, self._key), view=self.view_ref)

        # --- Main View ---

        class SettingsView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=10800)
                self.page = 0
                self.lang_page = 0
                self._owner_guild_id = guild_id
                self._owner_user_id = user_id
                all_langs = sorted(
                    SUPPORTED_LOCALES.items(), key=lambda kv: kv[1].lower()
                )
                self._lang_list = all_langs
                self._lang_per_page = 23
                self.lang_total_pages = (len(all_langs) - 1) // self._lang_per_page + 1
                self.total_pages = (len(cog._SETTING_DEFS) - 1) // per_page + 1
                self.message: discord.Message | None = None
                self._rebuild_overview()

            def _page_options(self):
                start = self.page * per_page
                end = start + per_page
                opts = []
                for skey, label_key, _, stype in cog._SETTING_DEFS[start:end]:
                    if skey in ("bot_activity", "silent_log") and not is_app_owner:
                        continue
                    if skey == "manage_perms" and not _effective_owner:
                        continue
                    if not is_guild_admin and is_app_owner and skey not in ("bot_activity", "silent_log"):
                        continue
                    label = t(ctx, label_key)
                    val = cog._settings_value_label(ctx, guild_id, skey)
                    if val:
                        desc = f"{t(ctx, 'SETTINGS_CURRENT')}: {val}"
                        if len(desc) > 100:
                            desc = desc[:97] + "..."
                    else:
                        desc = None
                    opts.append(discord.SelectOption(label=label, value=skey, description=desc))
                return opts

            async def interaction_check(self, interaction: discord.Interaction) -> bool:
                if not await cog.check_view_interaction(interaction):
                    return False
                _is_app_owner = await cog.bot.is_owner(interaction.user)
                if not cog.has_admin_privilege(guild_id, interaction.user) and not _is_app_owner:
                    await cog.send_reply(interaction, t(interaction, "NOT_MANAGE_ADMIN"), ephemeral=True)
                    return False
                return True

            async def on_timeout(self):
                cog.active_settings.pop((self._owner_guild_id, self._owner_user_id), None)

            def _rebuild_overview(self):
                self.clear_items()
                self.add_item(SettingsSelect(self, self._page_options()))
                if _effective_owner:
                    self.add_item(ExportButton(self))
                    self.add_item(ImportButton(self))
                    self.add_item(ResetDefaultsButton(self))
                if is_app_owner:
                    self.add_item(BotExportButton(self))
                    self.add_item(BotImportButton(self))
                    self.add_item(BotResetButton(self))
                if self.total_pages > 1:
                    self.add_item(SettingsPageButton(self, -1, _BUTTON_EMOJIS["BUTTON_PREV"], t(ctx, "BUTTON_PREV")))
                    self.add_item(SettingsPageButton(self, 1, _BUTTON_EMOJIS["BUTTON_NEXT"], t(ctx, "BUTTON_NEXT")))

            def _rebuild_reset_confirm(self):
                self.clear_items()
                self.add_item(ExportButton(self))
                self.add_item(ResetConfirmYesButton(self))
                self.add_item(ResetConfirmCancelButton(self))

            def _rebuild_bot_reset_confirm(self):
                self.clear_items()
                self.add_item(BotExportButton(self))
                self.add_item(BotResetConfirmYesButton(self))
                self.add_item(BotResetConfirmCancelButton(self))

            def _rebuild_lang(self):
                self.clear_items()
                start = self.lang_page * self._lang_per_page
                end = start + self._lang_per_page
                opts = []
                for code, label in self._lang_list[start:end]:
                    opts.append(discord.SelectOption(label=label, value=code))
                self.add_item(LangSelect(self, opts))
                if self.lang_total_pages > 1:
                    self.add_item(LangPageButton(self, -1, t(ctx, "BUTTON_PREV")))
                    self.add_item(LangPageButton(self, 1, t(ctx, "BUTTON_NEXT")))
                self.add_item(BackButton(self, row=2))

            def _rebuild_embed_color(self):
                self.clear_items()
                self.add_item(ColorSelect(self))
                self.add_item(CustomColorButton(self))
                self.add_item(RandomColorButton(self))
                self.add_item(EmbedViewsBackButton(self, row=2))

            def _rebuild_queue_display(self):
                self.clear_items()
                self.add_item(QueuePerPageButton(self))
                self.add_item(QueueCompactSelect(self))
                self.add_item(QueueFieldToggleSelect(self))
                self.add_item(EmbedViewsBackButton(self, row=3))

            def _rebuild_view_restrict(self):
                self.clear_items()
                has_ch = guild_id in cog.guild_view_channels
                self.add_item(ViewChannelSelect(self))
                self.add_item(ViewRestrictSelect(self))
                self.add_item(SendViewsButton(self, disabled=not has_ch))
                self.add_item(ClearViewChannelButton(self, disabled=not has_ch))
                self.add_item(EmbedViewsBackButton(self, row=3))

            def _rebuild_dj(self):
                self.clear_items()
                self.add_item(DJRoleSelect(self))
                self.add_item(DJUserSelect(self))
                dj_role = cog.dj_roles.get(guild_id)
                dj_set = cog.dj_users.get(guild_id, set())
                row = 2
                if dj_set:
                    self.add_item(ShowAllDJsButton(self))
                    row = 3
                clear_row = row
                if dj_role:
                    self.add_item(ClearDJRoleButton(self, row=clear_row))
                if dj_set:
                    self.add_item(ClearDJUsersButton(self, row=clear_row))
                if dj_role or dj_set:
                    row = clear_row + 1
                self.add_item(BackButton(self, row=min(row, 4)))

            def _rebuild_limit_usage(self):
                self.clear_items()
                self.add_item(ExcludeUserSelect(self))
                self.add_item(ExcludeRoleSelect(self))
                has_users = bool(cog.excluded_users.get(guild_id))
                has_roles = bool(cog.excluded_roles.get(guild_id))
                if has_users:
                    self.add_item(ClearExcludedUsersButton(self))
                if has_roles:
                    self.add_item(ClearExcludedRolesButton(self))
                self.add_item(LimitsBackButton(self, row=3))

            def _rebuild_manage_perms(self):
                self.clear_items()
                self.add_item(AdminUserSelect(self))
                self.add_item(AdminRoleSelect(self))
                self.add_item(AdminPrivToggle(self))
                has_any = bool(cog.admin_users.get(guild_id) or cog.admin_roles.get(guild_id))
                if has_any:
                    self.add_item(ClearAdminAllButton(self))
                self.add_item(BackButton(self, row=4 if has_any else 3))

            def _rebuild_bot_activity(self):
                self._editing_item = None
                if not hasattr(self, '_act_page'):
                    self._act_page = 0
                self.clear_items()
                lst = cog.bot_activity_list
                self.add_item(ActAddButton(self))
                self.add_item(ActEditButton(self))
                self.add_item(ActResetButton(self))
                if lst:
                    self.add_item(ActSelectButton(self))
                    self.add_item(ActOrderButton(self))
                    self.add_item(ActRemoveButton(self))
                    self.add_item(ActTimeButton(self))
                    self.add_item(ActModeSelect(self))
                pp = cog._ACT_PER_PAGE
                total_pages = (len(lst) + pp - 1) // pp if lst else 1
                if total_pages > 1:
                    self.add_item(ActPageButton(self, delta=-1))
                    self.add_item(ActPageButton(self, delta=1))
                back_row = 4 if lst and total_pages > 1 else 3
                self.add_item(BackButton(self, row=back_row))

            def _rebuild_bot_activity_add(self):
                self.clear_items()
                current_type = getattr(self, '_act_type_choice', 2)
                self.add_item(ActTypeSelect(self, current=current_type, mode='add'))
                self.add_item(ActAddTextButton(self))
                self.add_item(ActAddBackButton(self))

            def _rebuild_bot_activity_edit(self):
                self.clear_items()
                item = getattr(self, '_editing_item', None)
                current_type = getattr(self, '_act_type_choice', item["type"] if item else 2)
                self.add_item(ActTypeSelect(self, current=current_type, mode='edit'))
                self.add_item(ActEditTextButton(self))
                self.add_item(ActEditBackButton(self))

            def _rebuild_embed_views_main(self):
                self.clear_items()
                self._active_layout_view = None
                self.add_item(EmbedViewsSelect(self))
                self.add_item(BackButton(self, row=1))

            def _rebuild_limits_main(self):
                self.clear_items()
                self.add_item(LimitsSelect(self))
                self.add_item(BackButton(self, row=1))

            def _rebuild_limits_sub(self, sub_key):
                self.clear_items()
                if sub_key == "max_playlists":
                    self.add_item(ConfigureButton(self, "max_playlists"))
                    self.add_item(LimitsBackButton(self, row=0))
                elif sub_key == "max_history":
                    self.add_item(ConfigureButton(self, "max_history"))
                    self.add_item(LimitsBackButton(self, row=0))
                elif sub_key == "max_user_tracks":
                    self.add_item(TrackLimitGroupButton(self, "users", row=0))
                    self.add_item(TrackLimitGroupButton(self, "dj", row=0))
                    self.add_item(TrackLimitGroupButton(self, "admin", row=0))
                    self.add_item(TrackLimitClearAllButton(self, row=1))
                    self.add_item(QueueLimitButton(self, row=2))
                    self.add_item(PlaylistTrackLimitButton(self, row=2))
                    self.add_item(LimitsBackButton(self, row=2))
                elif sub_key == "limit_usage":
                    self._rebuild_limit_usage()
                    return

            def _rebuild_timezone(self):
                self.clear_items()
                self.add_item(TimezoneSelect(self, page=0))
                self.add_item(TimezoneSelect(self, page=1))
                self.add_item(BackButton(self, row=2))

            def _rebuild_pause_control(self):
                self.clear_items()
                self.add_item(PausePermSelect(self))
                self.add_item(PauseTimeoutButton(self))
                self.add_item(PauseTimeoutBehaviorSelect(self))
                self.add_item(SeekPermSelect(self))
                self.add_item(SeekLimitButton(self))
                self.add_item(SeekLimitDJButton(self))
                self.add_item(LimitsBackButton(self, row=4))

            def _rebuild_radio_settings(self):
                self.clear_items()
                self.add_item(RadioPermSelect(self))
                self.add_item(RadioEditPermSelect(self))
                self.add_item(RadioCooldownButton(self))
                self.add_item(LimitsBackButton(self, row=3))

            def _rebuild_force_play(self):
                self.clear_items()
                self.add_item(ForcePlayPermSelect(self))
                self.add_item(ForceRadioToggle(self))
                self.add_item(LimitsBackButton(self, row=2))

            def _rebuild_bot_connection(self):
                self.clear_items()
                self.add_item(IdleTimeoutButton(self))
                self.add_item(JoinRestrictLevelSelect(self))
                self.add_item(JoinRestrictChannelSelect(self))
                has_channels = bool(cog.guild_join_restrict_channels.get(guild_id))
                self.add_item(JoinRestrictClearButton(self, disabled=not has_channels))
                self.add_item(LimitsBackButton(self, row=3))

            def _rebuild_live_playback(self):
                self.clear_items()
                self.add_item(LiveEnabledToggle(self))
                self.add_item(LivePermSelect(self))
                self.add_item(LiveMaxHoursButton(self))
                self.add_item(LimitsBackButton(self, row=3))

            def _rebuild_performance(self):
                self.clear_items()
                self.add_item(PrefetchToggle(self))
                self.add_item(SafePrefetchToggle(self))
                self.add_item(MaxWorkersButton(self))
                self.add_item(LimitsBackButton(self, row=1))

            def _rebuild_embed_layout_buttons(self):
                vt = getattr(self, '_active_layout_view', 'mp') or 'mp'
                self.clear_items()
                self.add_item(EmbedLayoutButtonToggle(self, vt))
                self.add_item(EmbedLayoutEnableAll(self, vt))
                self.add_item(EmbedLayoutDisableAll(self, vt))
                self.add_item(EmbedLayoutCompactToggle(self))
                self.add_item(EmbedLayoutResetButton(self, vt))
                self.add_item(EmbedLayoutReorderButton(self, vt))
                self.add_item(EmbedViewsBackButton(self, row=3))

            def _rebuild_mp_display_detail(self):
                self.clear_items()
                self.add_item(MpFieldToggleSelect(self))
                self.add_item(MpFieldEnableAll(self))
                self.add_item(MpFieldDisableAll(self))
                self.add_item(MpFieldResetButton(self))
                self.add_item(MpFieldReorderButton(self))
                self.add_item(EmbedViewsBackButton(self, row=3))

            def _rebuild_dj_list(self):
                dj_set = cog.dj_users.get(guild_id, set())
                self._dj_list = sorted(dj_set)
                per = 20
                self.dj_list_total_pages = max(1, (len(self._dj_list) - 1) // per + 1)
                self.dj_list_page = min(self.dj_list_page, self.dj_list_total_pages - 1)
                self.clear_items()
                on_first = self.dj_list_page == 0
                on_last = self.dj_list_page == self.dj_list_total_pages - 1
                single = self.dj_list_total_pages <= 1
                self.add_item(DJListFirstButton(self, disabled=on_first or single))
                self.add_item(DJListPageButton(self, -1, _BUTTON_EMOJIS["BUTTON_PREV"], t(ctx, "BUTTON_PREV"), disabled=on_first or single))
                self.add_item(DJListGoToButton(self, disabled=single))
                self.add_item(DJListPageButton(self, 1, _BUTTON_EMOJIS["BUTTON_NEXT"], t(ctx, "BUTTON_NEXT"), disabled=on_last or single))
                self.add_item(DJListLastButton(self, disabled=on_last or single))
                self.add_item(DJListRemoveButton(self))
                self.add_item(DJListBackButton(self))

            def _build_dj_list_embed(self):
                per = 20
                start = self.dj_list_page * per
                end = start + per
                page_items = self._dj_list[start:end]
                guild_obj = ctx.guild
                lines = []
                for i, uid in enumerate(page_items, start=start + 1):
                    member = guild_obj.get_member(uid)
                    name = member.display_name if member else str(uid)
                    lines.append(f"`{i}.` <@{uid}> ({name})")
                desc = "\n".join(lines) if lines else t(ctx, "SETTINGS_DJ_USERS_NONE")
                total = len(self._dj_list)
                embed = SafeEmbed(
                    title=f"{t(ctx, 'SETTINGS_SHOW_DJ_USERS')} ({total})",
                    description=desc,
                    color=cog.get_embed_color(guild_id),
                )
                if self.dj_list_total_pages > 1:
                    embed.set_footer(text=f"{self.dj_list_page + 1}/{self.dj_list_total_pages}")
                return embed

            async def _show_setting(self, btn_interaction, key, setting_type):
                self.clear_items()
                if setting_type == "boolean":
                    self.add_item(BoolSelect(self, key))
                    self.add_item(BackButton(self, row=1))
                elif setting_type == "choice":
                    self.add_item(ChoiceSelect(self, key))
                    self.add_item(VoteDeafenedButton(self))
                    self.add_item(BackButton(self, row=2))
                elif setting_type == "language":
                    self.lang_page = 0
                    self._rebuild_lang()
                    await btn_interaction.response.edit_message(embed=cog._build_detail_embed(btn_interaction, guild_id, key), view=self)
                    return
                elif setting_type == "number":
                    self.add_item(ConfigureButton(self, key))
                    self.add_item(BackButton(self, row=0))
                elif setting_type == "manage_perms":
                    self._rebuild_manage_perms()
                    await btn_interaction.response.edit_message(embed=cog._build_detail_embed(btn_interaction, guild_id, key), view=self)
                    return
                elif setting_type == "embed_views":
                    self._rebuild_embed_views_main()
                    await btn_interaction.response.edit_message(embed=cog._build_embed_views_main_embed(btn_interaction, guild_id), view=self)
                    return
                elif setting_type == "limits":
                    self._rebuild_limits_main()
                    await btn_interaction.response.edit_message(embed=cog._build_limits_main_embed(btn_interaction, guild_id, is_app_owner=is_app_owner), view=self)
                    return
                elif setting_type == "bot_activity":
                    self._act_page = 0
                    self._rebuild_bot_activity()
                    await btn_interaction.response.edit_message(embed=cog._build_activity_embed(btn_interaction, page=0, guild_id=guild_id), view=self)
                    return
                elif setting_type == "dj":
                    self._rebuild_dj()
                    await btn_interaction.response.edit_message(embed=cog._build_detail_embed(btn_interaction, guild_id, key), view=self)
                    return
                elif setting_type == "timezone":
                    self._rebuild_timezone()
                    await btn_interaction.response.edit_message(embed=cog._build_detail_embed(btn_interaction, guild_id, key), view=self)
                    return
                await btn_interaction.response.edit_message(embed=cog._build_detail_embed(btn_interaction, guild_id, key), view=self)

        view = SettingsView()
        msg = await interaction.followup.send(embed=self._build_overview_embed(interaction, guild_id, is_app_owner=is_app_owner, is_guild_admin=is_guild_admin, _effective_owner=_effective_owner), view=view, ephemeral=True, wait=True)
        view.message = msg
        self.active_settings[(guild_id, user_id)] = (msg, view)

    # region Playlist

    async def _playlist_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        playlists = await db.get_user_playlists(interaction.guild_id, interaction.user.id)
        choices = []
        for p in playlists:
            if current.lower() in p["name"].lower():
                label = f"{'★ ' if p['is_favourite'] else ''}{p['name']} ({p['track_count']})"
                choices.append(app_commands.Choice(name=label[:100], value=p["name"]))
        return choices[:25]

    @app_commands.command(**l_cmd("CMD_NAME_PLAYLIST", "CMD_DESC_PLAYLIST"))
    @app_commands.rename(
        add=l_opt("OPTNAME_PL_ADD"),
        add_from=l_opt("OPTNAME_PL_ADD_FROM"),
        select_list=l_opt("OPTNAME_PL_SELECT_LIST"),
        show=l_opt("OPTNAME_PL_SHOW"),
        create=l_opt("OPTNAME_PL_CREATE"),
        delete=l_opt("OPTNAME_PL_DELETE"),
        favourite=l_opt("OPTNAME_PL_FAVOURITE"),
        share=l_opt("OPTNAME_PL_SHARE"),
        edit_name=l_opt("OPTNAME_PL_EDIT_NAME"),
        manage_guild=l_opt("OPTNAME_PL_MANAGE_GUILD"),
        play=l_opt("OPTNAME_PL_PLAY"),
        play_shuffle=l_opt("OPTNAME_PL_PLAY_SHUFFLE"),
    )
    @app_commands.describe(
        add=l_opt("OPT_PL_ADD"),
        add_from=l_opt("OPT_PL_ADD_FROM"),
        select_list=l_opt("OPT_PL_SELECT_LIST"),
        show=l_opt("OPT_PL_SHOW"),
        create=l_opt("OPT_PL_CREATE"),
        delete=l_opt("OPT_PL_DELETE"),
        favourite=l_opt("OPT_PL_FAVOURITE"),
        share=l_opt("OPT_PL_SHARE"),
        edit_name=l_opt("OPT_PL_EDIT_NAME"),
        manage_guild=l_opt("OPT_PL_MANAGE_GUILD"),
        play=l_opt("OPT_PL_PLAY"),
        play_shuffle=l_opt("OPT_PL_PLAY_SHUFFLE"),
    )
    @app_commands.choices(
        add_from=[
            localized_choice("current", "PL_ADD_FROM_CURRENT"),
            localized_choice("queue", "PL_ADD_FROM_QUEUE"),
        ],
        play=[app_commands.Choice(name="✅", value="on")],
        play_shuffle=[app_commands.Choice(name="✅", value="on")],
    )
    async def playlist_cmd(
        self,
        interaction: discord.Interaction,
        add: str | None = None,
        add_from: str | None = None,
        play: str | None = None,
        play_shuffle: str | None = None,
        select_list: str | None = None,
        show: str | None = None,
        create: str | None = None,
        delete: str | None = None,
        favourite: str | None = None,
        share: str | None = None,
        edit_name: str | None = None,
        manage_guild: str | None = None,
    ):
        if not await self._check_cooldown(interaction, "playlist", 5): return
        guild_id = interaction.guild_id
        user_id = interaction.user.id
        max_pl = self.get_max_playlists(guild_id)

        # edit_name must be handled before defer (modal needs initial response)
        if edit_name is not None:
            if max_pl == 0:
                return await interaction.response.send_message(
                    t(interaction, "PL_DISABLED"), ephemeral=True, delete_after=self._resolve_delete_after(interaction.guild_id))
            others = sum(x is not None for x in (create, delete, favourite, share, manage_guild, play, play_shuffle))
            if others or show is not None or add is not None or add_from is not None:
                return await interaction.response.send_message(
                    t(interaction, "PL_CONFLICT",
                      create=t(interaction, "OPTNAME_PL_CREATE"), delete=t(interaction, "OPTNAME_PL_DELETE"),
                      favourite=t(interaction, "OPTNAME_PL_FAVOURITE"), share=t(interaction, "OPTNAME_PL_SHARE"),
                      show=t(interaction, "OPTNAME_PL_SHOW"), edit_name=t(interaction, "OPTNAME_PL_EDIT_NAME"),
                      manage_guild=t(interaction, "OPTNAME_PL_MANAGE_GUILD")), ephemeral=True, delete_after=self._resolve_delete_after(interaction.guild_id))
            pl = await db.get_playlist_by_name(guild_id, user_id, edit_name)
            if not pl:
                return await interaction.response.send_message(
                    t(interaction, "PL_NOT_FOUND"), ephemeral=True, delete_after=self._resolve_delete_after(interaction.guild_id))
            modal = PLRenameModalStandalone(self, interaction, guild_id, user_id, pl)
            return await interaction.response.send_modal(modal)

        await interaction.response.defer(ephemeral=True)

        if max_pl == 0:
            return await self.send_reply(interaction, t(interaction, "PL_DISABLED"), ephemeral=True)

        _conflict_msg = lambda: t(interaction, "PL_CONFLICT",
            create=t(interaction, "OPTNAME_PL_CREATE"), delete=t(interaction, "OPTNAME_PL_DELETE"),
            favourite=t(interaction, "OPTNAME_PL_FAVOURITE"), share=t(interaction, "OPTNAME_PL_SHARE"),
            show=t(interaction, "OPTNAME_PL_SHOW"), edit_name=t(interaction, "OPTNAME_PL_EDIT_NAME"),
            manage_guild=t(interaction, "OPTNAME_PL_MANAGE_GUILD"))

        has_add = add is not None or add_from is not None
        has_play = play is not None or play_shuffle is not None
        exclusive = sum(x is not None for x in (create, delete, favourite, share))

        # play / play_shuffle: mutually exclusive, only combinable with select_list
        if play is not None and play_shuffle is not None:
            return await self.send_reply(interaction, _conflict_msg(), ephemeral=True)
        if has_play and (exclusive or manage_guild is not None or has_add):
            return await self.send_reply(interaction, _conflict_msg(), ephemeral=True)

        # manage_guild: only by itself (select_list not applicable)
        if manage_guild is not None and (exclusive or has_add or show is not None or has_play or select_list is not None):
            return await self.send_reply(interaction, _conflict_msg(), ephemeral=True)

        # original exclusive options
        if exclusive > 1 or (exclusive and show is not None) or (exclusive and has_add):
            return await self.send_reply(interaction, _conflict_msg(), ephemeral=True)
        if add is not None and add_from is not None:
            return await self.send_reply(interaction, t(interaction, "PL_ADD_CONFLICT",
                add=t(interaction, "OPTNAME_PL_ADD"), add_from=t(interaction, "OPTNAME_PL_ADD_FROM")), ephemeral=True)

        # --- play / play_shuffle ---
        if has_play:
            if not interaction.user.voice or not interaction.user.voice.channel:
                return await self.send_reply(interaction, t(interaction, "JOIN_VOICE_FIRST"), ephemeral=True)
            target_pl = None
            if select_list:
                target_pl = await db.get_playlist_by_name(guild_id, user_id, select_list)
                if not target_pl:
                    return await self.send_reply(interaction, t(interaction, "PL_NOT_FOUND"), ephemeral=True)
            else:
                target_pl = await db.get_favourite_playlist(guild_id, user_id)
            if not target_pl:
                return await self.send_reply(interaction, t(interaction, "PL_NO_PLAYLISTS"), ephemeral=True)
            tracks = await db.get_playlist_tracks(target_pl["id"])
            if not tracks:
                return await self.send_reply(interaction, t(interaction, "PL_EMPTY"), ephemeral=True)
            await self._play_playlist(interaction, tracks, shuffle=play_shuffle is not None)
            if show is None:
                return

        # --- create ---
        if create is not None:
            msg, pid = await _pl_create(interaction, guild_id, user_id, create, max_pl)
            if pid is not None:
                await self._refresh_active_playlist(guild_id, user_id)
            return await self.send_reply(interaction, msg, ephemeral=True)

        # --- delete ---
        if delete is not None:
            pl = await db.get_playlist_by_name(guild_id, user_id, delete)
            if not pl:
                return await self.send_reply(interaction, t(interaction, "PL_NOT_FOUND"), ephemeral=True)
            msg = await _pl_delete_one(interaction, guild_id, user_id, pl["id"], delete, pl["is_favourite"])
            await self._refresh_active_playlist(guild_id, user_id, deleted_pl_id=pl["id"])
            return await self.send_reply(interaction, msg, ephemeral=True)

        # --- favourite ---
        if favourite is not None:
            pl = await db.get_playlist_by_name(guild_id, user_id, favourite)
            if not pl:
                return await self.send_reply(interaction, t(interaction, "PL_NOT_FOUND"), ephemeral=True)
            msg = await _pl_set_favourite(interaction, guild_id, user_id, pl["id"], favourite, pl["is_favourite"])
            if not pl["is_favourite"]:
                await self._refresh_active_playlist(guild_id, user_id)
            return await self.send_reply(interaction, msg, ephemeral=True)

        # --- share ---
        if share is not None:
            pl = await db.get_playlist_by_name(guild_id, user_id, share)
            if not pl:
                return await self.send_reply(interaction, t(interaction, "PL_NOT_FOUND"), ephemeral=True)
            err = await _pl_share(self, interaction, pl["id"], pl["name"], interaction.user.mention)
            if err:
                return await self.send_reply(interaction, err, ephemeral=True)
            return

        # --- manage_guild ---
        if manage_guild is not None:
            if not self.has_admin_privilege(guild_id, interaction.user):
                return await self.send_reply(interaction, t(interaction, "PL_MANAGE_NO_PERMS"), ephemeral=True)
            # Resolve string to member (could be ID, name, or display name)
            target = None
            try:
                target = interaction.guild.get_member(int(manage_guild))
            except (ValueError, TypeError):
                pass
            if not target:
                target = discord.utils.find(
                    lambda m: m.name == manage_guild or m.display_name == manage_guild,
                    interaction.guild.members)
            if not target:
                return await self.send_reply(interaction, t(interaction, "PL_NOT_FOUND"), ephemeral=True)
            if target.id == user_id:
                return await self.send_reply(interaction, t(interaction, "PL_MANAGE_SELF"), ephemeral=True)
            target_pl = await db.get_favourite_playlist(guild_id, target.id)
            if not target_pl:
                pls = await db.get_user_playlists(guild_id, target.id)
                target_pl = pls[0] if pls else None
            if not target_pl:
                return await self.send_reply(interaction, t(interaction, "PL_MANAGE_NO_PLAYLISTS", user=target.display_name), ephemeral=True)
            tracks = await db.get_playlist_tracks(target_pl["id"])

            key = (guild_id, user_id)
            if key in self.playlist_busy:
                return await self.send_reply(interaction, t(interaction, "PL_VIEW_BUSY"), ephemeral=True)
            old = self.active_playlists.pop(key, None)
            if old:
                old[1].stop()
                try:
                    await old[0].delete()
                except discord.HTTPException:
                    pass

            view = self._make_playlist_view(
                interaction, self, target_pl, tracks,
                managed_for=target.id, managed_name=target.display_name)
            embed = view.get_embed()
            msg = await self.send_reply(interaction, embed=embed, view=view, ephemeral=True, delete_after=None)
            if msg:
                self.active_playlists[key] = (msg, view)
            return

        # --- add (URL/search to playlist) ---
        if add is not None:
            target_pl = None
            if select_list:
                target_pl = await db.get_playlist_by_name(guild_id, user_id, select_list)
                if not target_pl:
                    return await self.send_reply(interaction, t(interaction, "PL_NOT_FOUND"), ephemeral=True)
            else:
                target_pl = await db.get_favourite_playlist(guild_id, user_id)
            if not target_pl:
                return await self.send_reply(interaction, t(interaction, "PL_NO_PLAYLISTS"), ephemeral=True)
            _pl_limit = self.get_playlist_track_limit(guild_id)
            if target_pl["track_count"] >= _pl_limit:
                return await self.send_reply(interaction, t(interaction, "PL_TRACK_LIMIT", limit=_pl_limit), ephemeral=True)
            msg, _ = await _pl_add_tracks(interaction, user_id, target_pl["id"], target_pl["name"],
                add, _pl_limit, self.is_silent_log(guild_id))
            await self._refresh_active_playlist(guild_id, user_id)
            await self.send_reply(interaction, msg, ephemeral=True)
            if show is None:
                return

        # --- add_from ---
        if add_from is not None:
            target_pl = None
            if select_list:
                target_pl = await db.get_playlist_by_name(guild_id, user_id, select_list)
                if not target_pl:
                    return await self.send_reply(interaction, t(interaction, "PL_NOT_FOUND"), ephemeral=True)
            else:
                target_pl = await db.get_favourite_playlist(guild_id, user_id)
            if not target_pl:
                return await self.send_reply(interaction, t(interaction, "PL_NO_PLAYLISTS"), ephemeral=True)
            _pl_limit = self.get_playlist_track_limit(guild_id)
            if target_pl["track_count"] >= _pl_limit:
                return await self.send_reply(interaction, t(interaction, "PL_TRACK_LIMIT", limit=_pl_limit), ephemeral=True)

            if add_from == "current":
                state = self.guild_states.get(guild_id)
                now = state.now if state else None
                if not now:
                    return await self.send_reply(interaction, t(interaction, "NOTHING_PLAYING"), ephemeral=True)
                track_dicts = [_entry_to_track_dict(now)]
                added = await db.add_playlist_tracks(target_pl["id"], track_dicts, _pl_limit, user_id=user_id)
                if added < 0:
                    return await self.send_reply(interaction, t(interaction, "PL_ADD_BUSY"), ephemeral=True)
                await self._refresh_active_playlist(guild_id, user_id)
                if added == 0:
                    await self.send_reply(interaction, t(interaction, "PL_PARTIAL_ADD", added=0, total=1, limit=_pl_limit), ephemeral=True)
                else:
                    await self.send_reply(interaction, _pl_add_msg(interaction, track_dicts, added, target_pl["name"]), ephemeral=True)
                if show is None:
                    return

            elif add_from == "queue":
                state = self.guild_states.get(guild_id)
                now = state.now if state else None
                queue = state.queue if state else []
                if not now and not queue:
                    return await self.send_reply(interaction, t(interaction, "QUEUE_EMPTY"), ephemeral=True)
                all_entries = []
                if now:
                    all_entries.append(now)
                all_entries.extend(queue)
                track_dicts = [_entry_to_track_dict(e) for e in all_entries]
                added = await db.add_playlist_tracks(target_pl["id"], track_dicts, _pl_limit, user_id=user_id)
                if added < 0:
                    return await self.send_reply(interaction, t(interaction, "PL_ADD_BUSY"), ephemeral=True)
                total = len(track_dicts)
                await self._refresh_active_playlist(guild_id, user_id)
                if added < total:
                    await self.send_reply(interaction, t(interaction, "PL_PARTIAL_ADD", added=added, total=total, limit=_pl_limit), ephemeral=True)
                else:
                    await self.send_reply(interaction, _pl_add_msg(interaction, track_dicts, added, target_pl["name"]), ephemeral=True)
                if show is None:
                    return

        # --- show (default: favourite) ---
        target_name = show or select_list
        if target_name:
            pl = await db.get_playlist_by_name(guild_id, user_id, target_name)
            if not pl:
                return await self.send_reply(interaction, t(interaction, "PL_NOT_FOUND"), ephemeral=True)
        else:
            pl = await db.get_favourite_playlist(guild_id, user_id)

        # Remove old active playlist view for this user
        key = (guild_id, user_id)
        if key in self.playlist_busy:
            return await self.send_reply(interaction, t(interaction, "PL_VIEW_BUSY"), ephemeral=True)
        old = self.active_playlists.pop(key, None)
        if old:
            old[1].stop()
            try:
                await old[0].delete()
            except discord.HTTPException:
                pass

        if not pl:
            playlists = await db.get_user_playlists(guild_id, user_id)
            if not playlists:
                view = self._make_playlist_view(interaction, self, None, [])
                embed = view.get_embed()
                msg = await self.send_reply(interaction, embed=embed, view=view, ephemeral=True, delete_after=None)
                if msg:
                    self.active_playlists[key] = (msg, view)
                return
            pl = playlists[0]

        tracks = await db.get_playlist_tracks(pl["id"])
        view = self._make_playlist_view(interaction, self, pl, tracks)
        embed = view.get_embed()
        msg = await self.send_reply(interaction, embed=embed, view=view, ephemeral=True, delete_after=None)
        if msg:
            self.active_playlists[key] = (msg, view)

    @playlist_cmd.autocomplete("select_list")
    async def _pl_select_list_ac(self, interaction: discord.Interaction, current: str):
        return await self._playlist_autocomplete(interaction, current)

    @playlist_cmd.autocomplete("show")
    async def _pl_show_ac(self, interaction: discord.Interaction, current: str):
        return await self._playlist_autocomplete(interaction, current)

    @playlist_cmd.autocomplete("delete")
    async def _pl_delete_ac(self, interaction: discord.Interaction, current: str):
        return await self._playlist_autocomplete(interaction, current)

    @playlist_cmd.autocomplete("favourite")
    async def _pl_fav_ac(self, interaction: discord.Interaction, current: str):
        return await self._playlist_autocomplete(interaction, current)

    @playlist_cmd.autocomplete("share")
    async def _pl_share_ac(self, interaction: discord.Interaction, current: str):
        return await self._playlist_autocomplete(interaction, current)

    @playlist_cmd.autocomplete("edit_name")
    async def _pl_edit_name_ac(self, interaction: discord.Interaction, current: str):
        return await self._playlist_autocomplete(interaction, current)

    @playlist_cmd.autocomplete("manage_guild")
    async def _pl_manage_guild_ac(self, interaction: discord.Interaction, current: str):
        members = interaction.guild.members
        choices = []
        for m in members:
            if m.bot or m.id == interaction.user.id:
                continue
            if current and current.lower() not in m.display_name.lower() and current.lower() not in m.name.lower():
                continue
            choices.append(app_commands.Choice(name=m.display_name[:100], value=str(m.id)))
            if len(choices) >= 25:
                break
        return choices

    async def _play_playlist(self, interaction: discord.Interaction, tracks: list[dict], *, shuffle: bool = False):
        entries = [_playlist_track_to_entry(tr) for tr in tracks]
        if shuffle:
            random.shuffle(entries)
        play_lock = self._command_locks.setdefault(interaction.guild_id, asyncio.Lock())
        async with play_lock:
            played, added = await self.handle_play(interaction, entries)
        total = len(entries)
        guild_id = interaction.guild_id
        if played == "restricted":
            await self.send_reply(interaction, t(interaction, "JOIN_RESTRICTED_CHANNEL"), ephemeral=True)
            return
        if played == "user_limit":
            await self.send_reply(interaction, t(interaction, "USER_TRACK_LIMIT"), ephemeral=True)
            return
        elif played == "connect_failed":
            await self.send_reply(interaction, t(interaction, "VOICE_CONNECT_FAILED"), ephemeral=True)
            return
        elif played == "live_blocked":
            await self.send_reply(interaction, t(interaction, "LIVE_BLOCKED"), ephemeral=True)
            return
        elif played is None:
            _q_lim = self.guild_queue_limit.get(guild_id, MAX_QUEUE)
            await self.send_reply(interaction, t(interaction, "QUEUE_FULL", limit=_q_lim), ephemeral=True)
            return
        elif added < total:
            _q_lim = self.guild_queue_limit.get(guild_id, MAX_QUEUE)
            await self.send_reply(interaction,
                t(interaction, "QUEUE_LIMIT_TRUNCATED", added=added, total=total, limit=_q_lim))
        elif played:
            state = self.get_state(guild_id)
            now = state.now or entries[0]
            await self.send_reply(interaction,
                t(interaction, "ANNOUNCE_NOW", title=now.get("title") or t(interaction, "UNKNOWN"), uploader=now.get("uploader") or t(interaction, "UNKNOWN")))
        else:
            await self.send_reply(interaction, _format_queue_add_msg(interaction, entries, total))
        if not played:
            self._schedule_refresh(guild_id)

    async def _refresh_active_playlist(self, guild_id: int, user_id: int, *, deleted_pl_id: int | None = None):
        """Refresh the user's active playlist view after a slash-command mutation."""
        key = (guild_id, user_id)
        entry = self.active_playlists.get(key)
        if not entry:
            return
        view = entry[1]
        try:
            if deleted_pl_id and view.playlist and view.playlist["id"] == deleted_pl_id:
                fav = await db.get_favourite_playlist(guild_id, user_id)
                await view.switch_playlist(fav)
            else:
                await view.reload_tracks()
            await entry[0].edit(embed=view.get_embed(), view=view)
        except discord.HTTPException:
            pass

    # -- Playlist view factory --

    @staticmethod
    def _make_playlist_view(interaction, cog, playlist, tracks, *, managed_for=None, managed_name=None):
        per_page = cog.get_queue_per_page(interaction.guild_id)
        guild_id = interaction.guild_id
        user_id = managed_for if managed_for is not None else interaction.user.id
        is_managed = managed_for is not None
        viewer_id = interaction.user.id
        _managed_name = managed_name

        class PlaylistView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=10800)
                self.ctx = interaction
                self.cog = cog
                self.guild_id = guild_id
                self.user_id = user_id
                self.viewer_id = viewer_id
                self.is_managed = is_managed
                self.managed_name = _managed_name
                self.playlist = playlist
                self.tracks = tracks
                self.page = 0
                self.per_page = per_page
                self.total_pages = max(1, (len(tracks) - 1) // per_page + 1) if tracks else 1
                self.compact = cog.is_queue_button_compact(guild_id)
                self._busy = False
                self._build()

            def _cl(self, key):
                e = _BUTTON_EMOJIS.get(key)
                l = None if self.compact else t(self.ctx, key)
                return e, l

            def get_playlist_id(self):
                return self.playlist["id"] if self.playlist else None

            def _get_search_tracks(self):
                return self.tracks

            async def on_timeout(self):
                key = (self.guild_id, self.viewer_id)
                self.cog.active_playlists.pop(key, None)
                self.cog.playlist_busy.discard(key)

            async def interaction_check(self, check_interaction):
                if not await self.cog.check_view_interaction(check_interaction):
                    return False
                if self._busy:
                    return False
                if self.is_managed and not self.cog.has_admin_privilege(self.guild_id, check_interaction.user):
                    await self.cog.send_reply(check_interaction,
                        t(check_interaction, "PL_MANAGE_NO_PERMS"), ephemeral=True)
                    return False
                if self.playlist:
                    fresh_pl = await db.get_playlist_by_id(self.playlist["id"])
                    if not fresh_pl:
                        fav = await db.get_favourite_playlist(self.guild_id, self.user_id)
                        await self.switch_playlist(fav)
                        await self._edit_view()
                        await self.cog.send_reply(check_interaction,
                            t(check_interaction, "PL_VIEW_STALE"), ephemeral=True)
                        return False
                    fresh_tracks = await db.get_playlist_tracks(self.playlist["id"])
                    old_snap = (self.playlist.get("name"), self.playlist.get("is_favourite"),
                                tuple(tr.get("url", "") for tr in self.tracks))
                    new_snap = (fresh_pl["name"], fresh_pl["is_favourite"],
                                tuple(tr.get("url", "") for tr in fresh_tracks))
                    if old_snap != new_snap:
                        self.playlist["name"] = fresh_pl["name"]
                        self.playlist["is_favourite"] = fresh_pl["is_favourite"]
                        self.tracks = fresh_tracks
                        self.playlist["track_count"] = len(fresh_tracks)
                        self.total_pages = max(1, (len(self.tracks) - 1) // self.per_page + 1)
                        self.page = min(self.page, self.total_pages - 1)
                        self._build()
                        await self._edit_view()
                        await self.cog.send_reply(check_interaction,
                            t(check_interaction, "PL_VIEW_STALE"), ephemeral=True)
                        return False
                return True

            async def _edit_view(self, *, retry_on_fail=False):
                key = (self.guild_id, self.viewer_id)
                entry = self.cog.active_playlists.get(key)
                if entry:
                    try:
                        await entry[0].edit(embed=self.get_embed(), view=self)
                    except discord.HTTPException:
                        if retry_on_fail:
                            await asyncio.sleep(2)
                            try:
                                await entry[0].edit(embed=self.get_embed(), view=self)
                            except discord.HTTPException:
                                pass

            async def set_busy(self, busy):
                key = (self.guild_id, self.viewer_id)
                if busy:
                    self.cog.playlist_busy.add(key)
                else:
                    self.cog.playlist_busy.discard(key)
                self._busy = busy
                if busy:
                    for item in self.children:
                        item.disabled = True
                else:
                    self._build()
                await self._edit_view(retry_on_fail=not busy)

            def _build(self):
                self.clear_items()
                empty = not self.tracks
                no_playlist = self.playlist is None
                # Row 0 - nav
                self.add_item(_PageButton(self, "⏪", None, "first", disabled=empty or self.page == 0))
                self.add_item(_PageButton(self, "⬅️", None, "prev", disabled=empty or self.page == 0))
                self.add_item(_PageButton(self, "➡️", None, "next", disabled=empty or self.page >= self.total_pages - 1))
                self.add_item(_PageButton(self, "⏩", None, "last", disabled=empty or self.page >= self.total_pages - 1))
                self.add_item(_GoToPageButton(self, disabled=self.total_pages <= 1))
                # Row 1 - options dropdown
                self.add_item(PLOptionsSelect(self))
                # Row 2 - play + track actions
                self.add_item(_PLPlayBtn(self, *self._cl("BUTTON_PLAY_ALL"), disabled=empty or no_playlist, row=2))
                self.add_item(PLRemoveButton(self, *self._cl("BUTTON_REMOVE"), disabled=empty or no_playlist))
                self.add_item(PLMoveButton(self, *self._cl("BUTTON_MOVE"), disabled=empty or no_playlist or len(self.tracks) < 2))
                # Row 3 - copy/search/refresh
                self.add_item(_PLCopyBtn(self, *self._cl("BUTTON_COPY"), disabled=no_playlist))
                self.add_item(_SearchButton(self, label=None if self.compact else t(self.ctx, "BUTTON_SEARCH"), emoji="🔍", disabled=empty or no_playlist, row=3))
                self.add_item(_PLRefreshBtn(self, *self._cl("BUTTON_REFRESH"), row=3))

            def get_embed(self):
                color = self.cog.get_embed_color(self.guild_id)
                if not self.playlist:
                    embed = SafeEmbed(title=t(self.ctx, "CMD_DESC_PLAYLIST"), color=color)
                    if self.is_managed and self.managed_name:
                        embed.description = t(self.ctx, "PL_MANAGE_NO_PLAYLISTS", user=self.managed_name)
                        embed.set_footer(text=t(self.ctx, "PL_MANAGE_FOOTER", user=self.managed_name))
                    else:
                        embed.description = t(self.ctx, "PL_NO_PLAYLISTS")
                    return embed
                name = self.playlist["name"]
                fav = "★ " if self.playlist.get("is_favourite") else ""
                total_tracks = len(self.tracks)
                embed = SafeEmbed(
                    title=t(self.ctx, "PL_HEADER", name=f"{fav}`{name}`"),
                    color=color,
                )
                header = f"{t(self.ctx, 'PL_TRACK_COUNT', count=total_tracks)} ({self.page + 1}/{self.total_pages})"
                if not self.tracks:
                    embed.description = f"{header}\n\n{t(self.ctx, 'PL_EMPTY')}"
                    if self.is_managed and self.managed_name:
                        embed.set_footer(text=t(self.ctx, "PL_MANAGE_FOOTER", user=self.managed_name))
                    return embed
                start = self.page * self.per_page
                end = start + self.per_page
                lines = _fmt_track_lines(self.tracks[start:end], self.ctx, start_index=start + 1)
                sep = "\n" if cog.is_queue_compact(guild_id) else "\n\n"
                embed.description = f"{header}\n\n" + sep.join(lines)
                if self.is_managed and self.managed_name:
                    embed.set_footer(text=t(self.ctx, "PL_MANAGE_FOOTER", user=self.managed_name))
                return embed

            async def reload_tracks(self):
                if self.playlist:
                    fresh = await db.get_playlist_by_id(self.playlist["id"])
                    if fresh:
                        self.playlist["name"] = fresh["name"]
                        self.playlist["is_favourite"] = fresh["is_favourite"]
                    self.tracks = await db.get_playlist_tracks(self.playlist["id"])
                    self.playlist["track_count"] = len(self.tracks)
                else:
                    self.tracks = []
                self.total_pages = max(1, (len(self.tracks) - 1) // self.per_page + 1)
                self.page = min(self.page, self.total_pages - 1)
                self._build()

            async def switch_playlist(self, pl):
                self.playlist = pl
                if pl:
                    self.tracks = await db.get_playlist_tracks(pl["id"])
                    pl["track_count"] = len(self.tracks)
                else:
                    self.tracks = []
                self.page = 0
                self.total_pages = max(1, (len(self.tracks) - 1) // self.per_page + 1)
                self._build()

        # -- Options dropdown --
        class PLOptionsSelect(discord.ui.Select):
            def __init__(self, view):
                options = []
                if not is_managed:
                    options.append(discord.SelectOption(label=t(interaction, "BUTTON_ADD_TRACK"), value="add_track", emoji="➕"))
                    options.append(discord.SelectOption(label=t(interaction, "PL_OPT_ADD_CURRENT"), value="add_current", emoji="🎵"))
                    options.append(discord.SelectOption(label=t(interaction, "PL_OPT_ADD_QUEUE"), value="add_queue", emoji="📋"))
                options.append(discord.SelectOption(label=t(interaction, "PL_OPT_SWITCH"), value="switch", emoji="🔄"))
                if not is_managed:
                    options.append(discord.SelectOption(label=t(interaction, "PL_CREATE_TITLE"), value="create", emoji="📝"))
                options.append(discord.SelectOption(label=t(interaction, "PL_OPT_CLEAR"), value="clear_tracks", emoji="🧹"))
                options.append(discord.SelectOption(label=t(interaction, "PL_OPT_DELETE"), value="delete", emoji="🗑️"))
                options.append(discord.SelectOption(label=t(interaction, "PL_OPT_EDIT_NAME"), value="edit_name", emoji="✏️"))
                if not is_managed:
                    options.append(discord.SelectOption(label=t(interaction, "PL_OPT_FAVOURITE"), value="favourite", emoji="⭐"))
                options.append(discord.SelectOption(label=t(interaction, "PL_OPT_SHARE"), value="share", emoji="🔗"))
                options.append(discord.SelectOption(label=t(interaction, "PL_OPT_EXPORT"), value="export", emoji="📤"))
                if not is_managed:
                    options.append(discord.SelectOption(label=t(interaction, "PL_OPT_IMPORT"), value="import", emoji="📥"))
                if not is_managed and cog.has_admin_privilege(guild_id, interaction.user):
                    options.append(discord.SelectOption(label=t(interaction, "PL_OPT_MANAGE_GUILD"), value="manage_guild", emoji="👥"))
                super().__init__(placeholder=t(interaction, "PL_OPTIONS_PLACEHOLDER"), options=options, min_values=0, row=1)
                self.view_ref = view

            async def callback(self, sel_interaction: discord.Interaction):
                if not self.values:
                    return await sel_interaction.response.defer()
                choice = self.values[0]
                v = self.view_ref
                if not await v.cog._check_cooldown(sel_interaction, "pl:options", 2):
                    return
                v._busy = True

                try:
                    if choice == "add_track":
                        if not v.playlist:
                            return await v.cog.send_reply(sel_interaction, t(sel_interaction, "PL_NO_PLAYLISTS"), ephemeral=True)
                        await sel_interaction.response.send_modal(PLAddTrackModal(v))
                        return

                    elif choice == "add_current":
                        if not v.playlist:
                            return await v.cog.send_reply(sel_interaction, t(sel_interaction, "PL_NO_PLAYLISTS"), ephemeral=True)
                        _pl_limit = v.cog.get_playlist_track_limit(v.guild_id)
                        if len(v.tracks) >= _pl_limit:
                            return await v.cog.send_reply(sel_interaction, t(sel_interaction, "PL_TRACK_LIMIT", limit=_pl_limit), ephemeral=True)
                        state = v.cog.guild_states.get(v.guild_id)
                        now = state.now if state else None
                        if not now:
                            return await v.cog.send_reply(sel_interaction, t(sel_interaction, "NOTHING_PLAYING"), ephemeral=True)
                        v._busy = True
                        v.cog.playlist_busy.add((v.guild_id, v.viewer_id))
                        for item in v.children:
                            item.disabled = True
                        try:
                            await sel_interaction.response.edit_message(embed=v.get_embed(), view=v)
                            track_dicts = [_entry_to_track_dict(now)]
                            added = await db.add_playlist_tracks(v.playlist["id"], track_dicts, _pl_limit, user_id=v.user_id)
                            if added < 0:
                                await v.cog.send_reply(sel_interaction, t(sel_interaction, "PL_ADD_BUSY"), ephemeral=True)
                            elif added == 0:
                                await v.cog.send_reply(sel_interaction,
                                    t(sel_interaction, "PL_PARTIAL_ADD", added=0, total=1, limit=_pl_limit), ephemeral=True)
                            else:
                                await v.cog.send_reply(sel_interaction,
                                    _pl_add_msg(sel_interaction, track_dicts, added, v.playlist["name"]), ephemeral=True)
                            await v.reload_tracks()
                        finally:
                            await v.set_busy(False)

                    elif choice == "add_queue":
                        if not v.playlist:
                            return await v.cog.send_reply(sel_interaction, t(sel_interaction, "PL_NO_PLAYLISTS"), ephemeral=True)
                        _pl_limit = v.cog.get_playlist_track_limit(v.guild_id)
                        if len(v.tracks) >= _pl_limit:
                            return await v.cog.send_reply(sel_interaction, t(sel_interaction, "PL_TRACK_LIMIT", limit=_pl_limit), ephemeral=True)
                        state = v.cog.guild_states.get(v.guild_id)
                        now = state.now if state else None
                        queue = state.queue if state else []
                        if not now and not queue:
                            return await v.cog.send_reply(sel_interaction, t(sel_interaction, "QUEUE_EMPTY"), ephemeral=True)
                        v._busy = True
                        v.cog.playlist_busy.add((v.guild_id, v.viewer_id))
                        for item in v.children:
                            item.disabled = True
                        try:
                            await sel_interaction.response.edit_message(embed=v.get_embed(), view=v)
                            all_entries = []
                            if now:
                                all_entries.append(now)
                            all_entries.extend(queue)
                            track_dicts = [_entry_to_track_dict(e) for e in all_entries]
                            added = await db.add_playlist_tracks(v.playlist["id"], track_dicts, _pl_limit, user_id=v.user_id)
                            if added < 0:
                                await v.cog.send_reply(sel_interaction, t(sel_interaction, "PL_ADD_BUSY"), ephemeral=True)
                            else:
                                total = len(track_dicts)
                                if added < total:
                                    await v.cog.send_reply(sel_interaction,
                                        t(sel_interaction, "PL_PARTIAL_ADD", added=added, total=total, limit=_pl_limit), ephemeral=True)
                                else:
                                    await v.cog.send_reply(sel_interaction,
                                        _pl_add_msg(sel_interaction, track_dicts, added, v.playlist["name"]), ephemeral=True)
                            await v.reload_tracks()
                        finally:
                            await v.set_busy(False)

                    elif choice == "clear_tracks":
                        if not v.playlist:
                            return await v.cog.send_reply(sel_interaction, t(sel_interaction, "PL_NO_PLAYLISTS"), ephemeral=True)
                        if not v.tracks:
                            return await v.cog.send_reply(sel_interaction, t(sel_interaction, "PL_EMPTY"), ephemeral=True)
                        v.cog.playlist_busy.add((v.guild_id, v.viewer_id))
                        for item in v.children:
                            item.disabled = True
                        try:
                            await sel_interaction.response.edit_message(embed=v.get_embed(), view=v)
                            await db.clear_playlist_tracks(v.playlist["id"])
                            await v.cog.send_reply(sel_interaction, t(sel_interaction, "PL_CLEARED", name=v.playlist["name"]), ephemeral=True)
                            await v.reload_tracks()
                        finally:
                            await v.set_busy(False)

                    elif choice == "switch":
                        playlists = await db.get_user_playlists(v.guild_id, v.user_id)
                        if not playlists:
                            return await v.cog.send_reply(sel_interaction, t(sel_interaction, "PL_NO_PLAYLISTS"), ephemeral=True)
                        await sel_interaction.response.send_modal(PLSwitchModal(v, playlists, sel_interaction))
                        return

                    elif choice == "create":
                        max_pl = v.cog.get_max_playlists(v.guild_id)
                        count = await db.get_user_playlist_count(v.guild_id, v.user_id)
                        if count >= max_pl:
                            return await v.cog.send_reply(sel_interaction, t(sel_interaction, "PL_MAX_REACHED", max=max_pl), ephemeral=True)
                        await sel_interaction.response.send_modal(PLCreateModal(v))
                        return

                    elif choice == "delete":
                        playlists = await db.get_user_playlists(v.guild_id, v.user_id)
                        if not playlists:
                            return await v.cog.send_reply(sel_interaction, t(sel_interaction, "PL_NO_PLAYLISTS"), ephemeral=True)
                        await sel_interaction.response.send_modal(PLDeleteModal(v, playlists, sel_interaction))
                        return

                    elif choice == "favourite":
                        playlists = await db.get_user_playlists(v.guild_id, v.user_id)
                        if not playlists:
                            return await v.cog.send_reply(sel_interaction, t(sel_interaction, "PL_NO_PLAYLISTS"), ephemeral=True)
                        await sel_interaction.response.send_modal(PLFavModal(v, playlists, sel_interaction))
                        return

                    elif choice == "share":
                        playlists = await db.get_user_playlists(v.guild_id, v.user_id)
                        if not playlists:
                            return await v.cog.send_reply(sel_interaction, t(sel_interaction, "PL_NO_PLAYLISTS"), ephemeral=True)
                        await sel_interaction.response.send_modal(PLShareModal(v, playlists, sel_interaction))
                        return

                    elif choice == "edit_name":
                        playlists = await db.get_user_playlists(v.guild_id, v.user_id)
                        if not playlists:
                            return await v.cog.send_reply(sel_interaction, t(sel_interaction, "PL_NO_PLAYLISTS"), ephemeral=True)
                        await sel_interaction.response.send_modal(PLRenameModal(v, playlists, sel_interaction))
                        return

                    elif choice == "export":
                        if not v.playlist:
                            return await v.cog.send_reply(sel_interaction, t(sel_interaction, "PL_NO_PLAYLISTS"), ephemeral=True)
                        all_tracks = await db.get_playlist_tracks(v.playlist["id"])
                        def _export_track(tr):
                            d = {"title": tr.get("title"), "uploader": tr.get("uploader"),
                                 "duration": tr.get("duration"), "url": tr.get("url")}
                            if tr.get("is_live"):
                                d["is_live"] = True
                            return d
                        export_data = {
                            "name": v.playlist["name"],
                            "tracks": [_export_track(tr) for tr in all_tracks],
                        }
                        raw = _json.dumps(export_data, ensure_ascii=False, indent=2)
                        buf = io.BytesIO(raw.encode("utf-8"))
                        safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in v.playlist["name"])
                        file = discord.File(buf, filename=f"{safe_name}.json")
                        await sel_interaction.response.send_message(
                            t(sel_interaction, "PL_EXPORT_DONE", name=v.playlist["name"]),
                            file=file, ephemeral=True)

                    elif choice == "import":
                        if not v.playlist:
                            return await v.cog.send_reply(sel_interaction, t(sel_interaction, "PL_NO_PLAYLISTS"), ephemeral=True)
                        await sel_interaction.response.send_modal(PLImportModal(v))
                        return

                    elif choice == "manage_guild":
                        await sel_interaction.response.send_modal(PLManageGuildModal(v, sel_interaction))
                        return
                finally:
                    v._busy = False

        # -- Add Track Modal --
        class PLAddTrackModal(discord.ui.Modal):
            def __init__(self, pl_view):
                super().__init__(title=t(interaction, "BUTTON_ADD_TRACK"))
                self.pl_view = pl_view
                self.query_input = discord.ui.TextInput(
                    label=t(interaction, "PL_ADD_TRACK_LABEL"),
                    placeholder=t(interaction, "OPT_PLAY_QUERY"),
                    max_length=4000,
                    required=True,
                )
                self.shuffle_lbl = discord.ui.Label(
                    text=t(interaction, "SHUFFLE_LABEL"),
                    component=discord.ui.Checkbox(custom_id="shuffle"),
                )
                self.add_item(self.query_input)
                self.add_item(self.shuffle_lbl)

            async def on_submit(self, modal_interaction: discord.Interaction):
                v = self.pl_view
                if not v.playlist:
                    return await v.cog.send_reply(modal_interaction, t(modal_interaction, "PL_NO_PLAYLISTS"), ephemeral=True)
                v._busy = True
                try:
                    _pl_limit = v.cog.get_playlist_track_limit(v.guild_id)
                    if len(v.tracks) >= _pl_limit:
                        return await v.cog.send_reply(modal_interaction, t(modal_interaction, "PL_TRACK_LIMIT", limit=_pl_limit), ephemeral=True)
                    query = self.query_input.value.strip()
                    if not query:
                        return await v.cog.send_reply(modal_interaction, t(modal_interaction, "NO_PLAYABLE"), ephemeral=True)
                    await modal_interaction.response.defer(ephemeral=True)
                    await v.set_busy(True)
                    do_shuffle = self.shuffle_lbl.component.value
                    msg, _ = await _pl_add_tracks(modal_interaction, v.user_id, v.playlist["id"], v.playlist["name"],
                        query, _pl_limit, v.cog.is_silent_log(v.guild_id), shuffle=do_shuffle)
                    await v.cog.send_reply(modal_interaction, msg, ephemeral=True)
                    await v.reload_tracks()
                finally:
                    await v.set_busy(False)

        # -- Import Playlist Modal (FileUpload) --
        class PLImportModal(discord.ui.Modal):
            def __init__(self, pl_view):
                super().__init__(title=t(interaction, "PL_IMPORT_TITLE"))
                self.pl_view = pl_view

            upload = discord.ui.Label(
                text=t(interaction, "PL_IMPORT_FILE_LABEL"),
                description=t(interaction, "PL_IMPORT_FILE_DESC"),
                component=discord.ui.FileUpload(
                    custom_id="pl_import_file",
                    required=True,
                    max_values=1,
                ),
            )

            async def on_submit(self, modal_interaction: discord.Interaction):
                v = self.pl_view
                v._busy = True
                try:
                    if not v.playlist:
                        return await v.cog.send_reply(modal_interaction, t(modal_interaction, "PL_NO_PLAYLISTS"), ephemeral=True)
                    attachments = self.upload.component.values
                    if not attachments:
                        return await v.cog.send_reply(modal_interaction, t(modal_interaction, "PL_IMPORT_NO_FILE"), ephemeral=True)
                    att = attachments[0]
                    if att.size > 3_000_000:
                        return await v.cog.send_reply(modal_interaction, t(modal_interaction, "PL_IMPORT_TOO_LARGE"), ephemeral=True)
                    raw = await att.read()
                    try:
                        data = await asyncio.to_thread(_json.loads, raw)
                    except (ValueError, UnicodeDecodeError, TypeError):
                        return await v.cog.send_reply(modal_interaction, t(modal_interaction, "PL_IMPORT_INVALID"), ephemeral=True)
                    tracks = data.get("tracks") if isinstance(data, dict) else None
                    if not isinstance(tracks, list) or not tracks:
                        return await v.cog.send_reply(modal_interaction, t(modal_interaction, "PL_IMPORT_INVALID"), ephemeral=True)
                    track_dicts = []
                    for tr in tracks:
                        if isinstance(tr, dict) and tr.get("url"):
                            d = {
                                "title": tr.get("title"),
                                "uploader": tr.get("uploader"),
                                "duration": tr.get("duration"),
                                "url": tr["url"],
                            }
                            if tr.get("is_live"):
                                d["is_live"] = True
                            track_dicts.append(d)
                    if not track_dicts:
                        return await v.cog.send_reply(modal_interaction, t(modal_interaction, "PL_IMPORT_INVALID"), ephemeral=True)
                    _pl_limit = v.cog.get_playlist_track_limit(v.guild_id)
                    added = await db.add_playlist_tracks(v.playlist["id"], track_dicts, _pl_limit, user_id=v.user_id)
                    if added < 0:
                        return await v.cog.send_reply(modal_interaction, t(modal_interaction, "PL_ADD_BUSY"), ephemeral=True)
                    total = len(track_dicts)
                    if added < total:
                        msg = t(modal_interaction, "PL_PARTIAL_ADD", added=added, total=total, limit=_pl_limit)
                    else:
                        msg = t(modal_interaction, "PL_IMPORT_DONE", count=added, name=v.playlist["name"])
                    await v.cog.send_reply(modal_interaction, msg, ephemeral=True)
                    await v.reload_tracks()
                finally:
                    await v.set_busy(False)

        # -- Create Playlist Modal --
        class PLCreateModal(discord.ui.Modal):
            def __init__(self, pl_view):
                super().__init__(title=t(interaction, "PL_CREATE_TITLE"))
                self.pl_view = pl_view
                self.name_input = discord.ui.TextInput(
                    label=t(interaction, "PL_CREATE_LABEL"),
                    max_length=50,
                    required=True,
                )
                self.add_item(self.name_input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                v = self.pl_view
                v._busy = True
                try:
                    max_pl = v.cog.get_max_playlists(v.guild_id)
                    msg, pid = await _pl_create(modal_interaction, v.guild_id, v.user_id, self.name_input.value, max_pl)
                    await v.cog.send_reply(modal_interaction, msg, ephemeral=True)
                    if pid is not None:
                        new_pl = await db.get_playlist_by_name(v.guild_id, v.user_id, self.name_input.value.strip()[:50])
                        if new_pl:
                            await v.switch_playlist(new_pl)
                finally:
                    await v.set_busy(False)

        # -- Playlist picker modals --
        def _pl_select_options(playlists, *, show_count=False, mark_current_id=None):
            options = []
            for p in playlists[:25]:
                fav = "★ " if p["is_favourite"] else ""
                count = f" ({p['track_count']})" if show_count else ""
                label = f"{fav}{p['name']}{count}"[:100]
                options.append(discord.SelectOption(
                    label=label, value=str(p["id"]),
                    default=(p["id"] == mark_current_id) if mark_current_id else False,
                ))
            return options

        class PLSwitchModal(discord.ui.Modal):
            def __init__(self, pl_view, playlists, ctx):
                super().__init__(title=t(ctx, "PL_OPT_SWITCH"))
                self.pl_view = pl_view
                self._playlists = {str(p["id"]): p for p in playlists}
                cur_id = pl_view.playlist["id"] if pl_view.playlist else None
                self.pl_select = discord.ui.Label(
                    text=t(ctx, "PL_SELECT_LABEL"),
                    component=discord.ui.Select(
                        custom_id="pl_pick",
                        options=_pl_select_options(playlists, show_count=True, mark_current_id=cur_id),
                    ),
                )
                self.add_item(self.pl_select)

            async def on_submit(self, modal_interaction: discord.Interaction):
                v = self.pl_view
                v._busy = True
                try:
                    pid = int(self.pl_select.component.values[0])
                    playlists = await db.get_user_playlists(v.guild_id, v.user_id)
                    target = next((p for p in playlists if p["id"] == pid), None)
                    if not target:
                        return await v.cog.send_reply(modal_interaction, t(modal_interaction, "PL_NOT_FOUND"), ephemeral=True)
                    await v.cog.send_reply(modal_interaction, t(modal_interaction, "PL_SWITCHED", name=target["name"]), ephemeral=True)
                    await v.switch_playlist(target)
                finally:
                    await v.set_busy(False)

        class PLDeleteModal(discord.ui.Modal):
            def __init__(self, pl_view, playlists, ctx):
                super().__init__(title=t(ctx, "PL_OPT_DELETE"))
                self.pl_view = pl_view
                self._playlists = {str(p["id"]): p for p in playlists}
                self.pl_select = discord.ui.Label(
                    text=t(ctx, "PL_SELECT_LABEL"),
                    component=discord.ui.Select(
                        custom_id="pl_pick",
                        options=_pl_select_options(playlists),
                    ),
                )
                self.scope_radio = discord.ui.Label(
                    text=t(ctx, "PL_DELETE_SCOPE_LABEL"),
                    component=discord.ui.RadioGroup(
                        custom_id="delete_scope",
                        options=[
                            discord.RadioGroupOption(label=t(ctx, "PL_DELETE_ONE_OPTION"), value="one", default=True),
                            discord.RadioGroupOption(label=t(ctx, "PL_DELETE_ALL_OPTION"), value="all"),
                        ],
                    ),
                )
                self.add_item(self.pl_select)
                self.add_item(self.scope_radio)

            async def on_submit(self, modal_interaction: discord.Interaction):
                v = self.pl_view
                v._busy = True
                try:
                    scope = self.scope_radio.component.value or "one"
                    if scope == "all":
                        await db.delete_all_playlists(v.guild_id, v.user_id)
                        await v.cog.send_reply(modal_interaction, t(modal_interaction, "PL_ALL_DELETED"), ephemeral=True)
                        await v.switch_playlist(None)
                    else:
                        pid = int(self.pl_select.component.values[0])
                        playlists = await db.get_user_playlists(v.guild_id, v.user_id)
                        target = next((p for p in playlists if p["id"] == pid), None)
                        if not target:
                            return await v.cog.send_reply(modal_interaction, t(modal_interaction, "PL_NOT_FOUND"), ephemeral=True)
                        msg = await _pl_delete_one(modal_interaction, v.guild_id, v.user_id, pid, target["name"], target["is_favourite"])
                        await v.cog.send_reply(modal_interaction, msg, ephemeral=True)
                        if not v.playlist or v.playlist["id"] == pid:
                            fav = await db.get_favourite_playlist(v.guild_id, v.user_id)
                            await v.switch_playlist(fav)
                finally:
                    await v.set_busy(False)

        class PLFavModal(discord.ui.Modal):
            def __init__(self, pl_view, playlists, ctx):
                super().__init__(title=t(ctx, "PL_OPT_FAVOURITE"))
                self.pl_view = pl_view
                self._playlists = {str(p["id"]): p for p in playlists}
                self.pl_select = discord.ui.Label(
                    text=t(ctx, "PL_SELECT_LABEL"),
                    component=discord.ui.Select(
                        custom_id="pl_pick",
                        options=_pl_select_options(playlists),
                    ),
                )
                self.add_item(self.pl_select)

            async def on_submit(self, modal_interaction: discord.Interaction):
                v = self.pl_view
                v._busy = True
                try:
                    pid = int(self.pl_select.component.values[0])
                    playlists = await db.get_user_playlists(v.guild_id, v.user_id)
                    target = next((p for p in playlists if p["id"] == pid), None)
                    if not target:
                        return await v.cog.send_reply(modal_interaction, t(modal_interaction, "PL_NOT_FOUND"), ephemeral=True)
                    name = target["name"]
                    is_fav = target["is_favourite"]
                    msg = await _pl_set_favourite(modal_interaction, v.guild_id, v.user_id, pid, name, is_fav)
                    await v.cog.send_reply(modal_interaction, msg, ephemeral=True)
                    await v.reload_tracks()
                finally:
                    await v.set_busy(False)

        class PLShareModal(discord.ui.Modal):
            def __init__(self, pl_view, playlists, ctx):
                super().__init__(title=t(ctx, "PL_OPT_SHARE"))
                self.pl_view = pl_view
                self._playlists = {str(p["id"]): p for p in playlists}
                self._ctx = ctx
                self.pl_select = discord.ui.Label(
                    text=t(ctx, "PL_SELECT_LABEL"),
                    component=discord.ui.Select(
                        custom_id="pl_pick",
                        options=_pl_select_options(playlists),
                    ),
                )
                self.add_item(self.pl_select)

            async def on_submit(self, modal_interaction: discord.Interaction):
                v = self.pl_view
                pid = int(self.pl_select.component.values[0])
                playlists = await db.get_user_playlists(v.guild_id, v.user_id)
                target = next((p for p in playlists if p["id"] == pid), None)
                if not target:
                    return await v.cog.send_reply(modal_interaction, t(modal_interaction, "PL_NOT_FOUND"), ephemeral=True)
                share_mention = f"<@{v.user_id}>" if v.is_managed else modal_interaction.user.mention
                await modal_interaction.response.defer(ephemeral=True)
                err = await _pl_share(v.cog, modal_interaction, pid, target["name"], share_mention)
                if err:
                    await v.cog.send_reply(modal_interaction, err, ephemeral=True)

        class PLRenameModal(discord.ui.Modal):
            def __init__(self, pl_view, playlists, ctx):
                super().__init__(title=t(ctx, "PL_RENAME_TITLE"))
                self.pl_view = pl_view
                self._playlists = {str(p["id"]): p for p in playlists}
                self.pl_select = discord.ui.Label(
                    text=t(ctx, "PL_SELECT_LABEL"),
                    component=discord.ui.Select(
                        custom_id="pl_pick",
                        options=_pl_select_options(playlists),
                    ),
                )
                self.name_input = discord.ui.TextInput(
                    label=t(ctx, "PL_RENAME_LABEL"),
                    max_length=50,
                    required=True,
                )
                self.add_item(self.pl_select)
                self.add_item(self.name_input)

            async def on_submit(self, modal_interaction: discord.Interaction):
                v = self.pl_view
                v._busy = True
                try:
                    pid = int(self.pl_select.component.values[0])
                    pl = self._playlists.get(str(pid))
                    old_name = pl["name"] if pl else ""
                    msg, ok = await _pl_rename(modal_interaction, v.guild_id, v.user_id, pid, old_name, self.name_input.value)
                    await v.cog.send_reply(modal_interaction, msg, ephemeral=True)
                    if ok and v.playlist and v.playlist["id"] == pid:
                        v.playlist["name"] = self.name_input.value.strip()[:50]
                finally:
                    await v.set_busy(False)

        # -- Manage guild user picker modal --
        class PLManageGuildModal(discord.ui.Modal):
            def __init__(self, pl_view, ctx):
                super().__init__(title=t(ctx, "PL_OPT_MANAGE_GUILD"))
                self.pl_view = pl_view
                self.user_select = discord.ui.Label(
                    text=t(ctx, "PL_MANAGE_SELECT_USER"),
                    component=discord.ui.UserSelect(
                        custom_id="manage_user",
                        min_values=1, max_values=1,
                    ),
                )
                self.add_item(self.user_select)

            async def on_submit(self, modal_interaction: discord.Interaction):
                v = self.pl_view
                selected = self.user_select.component.values
                if not selected:
                    return await v.cog.send_reply(modal_interaction, t(modal_interaction, "PL_NOT_FOUND"), ephemeral=True)
                target = selected[0]
                if not isinstance(target, discord.Member):
                    target = modal_interaction.guild.get_member(target.id) if hasattr(target, 'id') else None
                if not target:
                    return await v.cog.send_reply(modal_interaction, t(modal_interaction, "PL_NOT_FOUND"), ephemeral=True)
                if target.id == v.viewer_id:
                    return await v.cog.send_reply(modal_interaction, t(modal_interaction, "PL_MANAGE_SELF"), ephemeral=True)

                target_pl = await db.get_favourite_playlist(v.guild_id, target.id)
                if not target_pl:
                    pls = await db.get_user_playlists(v.guild_id, target.id)
                    target_pl = pls[0] if pls else None
                if not target_pl:
                    return await v.cog.send_reply(modal_interaction, t(modal_interaction, "PL_MANAGE_NO_PLAYLISTS", user=target.display_name), ephemeral=True)

                await modal_interaction.response.defer(ephemeral=True)
                key = (v.guild_id, v.viewer_id)
                if key in v.cog.playlist_busy:
                    return await v.cog.send_reply(modal_interaction, t(modal_interaction, "PL_VIEW_BUSY"), ephemeral=True)
                tracks = await db.get_playlist_tracks(target_pl["id"])

                old = v.cog.active_playlists.pop(key, None)
                if old:
                    old[1].stop()
                    try:
                        await old[0].delete()
                    except discord.HTTPException:
                        pass

                new_view = v.cog._make_playlist_view(
                    v.ctx, v.cog, target_pl, tracks,
                    managed_for=target.id, managed_name=target.display_name)
                embed = new_view.get_embed()
                msg = await modal_interaction.followup.send(embed=embed, view=new_view, ephemeral=True)
                if msg:
                    v.cog.active_playlists[key] = (msg, new_view)

        # -- Track buttons --
        class PLRemoveButton(discord.ui.Button):
            def __init__(self, view, emoji, label, *, disabled=False):
                super().__init__(style=discord.ButtonStyle.secondary, emoji=emoji, label=label, disabled=disabled, row=2)
                self.view_ref = view
            async def callback(self, btn_interaction: discord.Interaction):
                if not await self.view_ref.cog._check_cooldown(btn_interaction, "pl:remove", 5): return
                v = self.view_ref
                if not v.tracks:
                    return await v.cog.send_reply(btn_interaction, t(btn_interaction, "PL_EMPTY"), ephemeral=True)
                await btn_interaction.response.send_modal(PLRemoveModal(v))

        class PLRemoveModal(discord.ui.Modal):
            def __init__(self, pl_view):
                super().__init__(title=t(interaction, "BUTTON_REMOVE"))
                self.pl_view = pl_view
                tcount = len(pl_view.tracks)
                self.index_input = discord.ui.TextInput(
                    label=t(interaction, "OPT_REMOVE_INDEX"),
                    placeholder=f"1-{tcount}",
                    max_length=5,
                    required=False,
                )
                self.indexto_input = discord.ui.TextInput(
                    label=t(interaction, "OPT_REMOVE_INDEXTO"),
                    placeholder=f"1-{tcount}",
                    max_length=5,
                    required=False,
                )
                self.search_input = discord.ui.TextInput(
                    label=t(interaction, "REMOVE_SEARCH_LABEL"),
                    placeholder=t(interaction, "REMOVE_SEARCH_PLACEHOLDER"),
                    max_length=100,
                    required=False,
                )
                self.add_item(self.index_input)
                self.add_item(self.indexto_input)
                self.add_item(self.search_input)
            async def on_submit(self, modal_interaction: discord.Interaction):
                v = self.pl_view
                if not v.playlist:
                    return await v.cog.send_reply(modal_interaction, t(modal_interaction, "PL_NO_PLAYLISTS"), ephemeral=True)
                raw_idx = self.index_input.value.strip()
                raw_idx_to = self.indexto_input.value.strip() if self.indexto_input.value else ""
                raw_search = self.search_input.value.strip() if self.search_input.value else ""

                if not raw_idx and not raw_search:
                    return await v.cog.send_reply(modal_interaction, t(modal_interaction, "INVALID_INPUT"), ephemeral=True)

                await modal_interaction.response.defer(ephemeral=True)
                v._busy = True
                try:
                    await v.set_busy(True)
                    tcount = len(v.tracks)
                    pl_id = v.playlist["id"]
                    pl_name = v.playlist["name"]
                    messages = []

                    # --- index removal first ---
                    if raw_idx:
                        try:
                            idx = int(raw_idx)
                        except ValueError:
                            return await v.cog.send_reply(modal_interaction, t(modal_interaction, "INVALID_INPUT"), ephemeral=True)
                        if not (1 <= idx <= tcount):
                            return await v.cog.send_reply(modal_interaction, t(modal_interaction, "SELECT_INVALID"), ephemeral=True)
                        if raw_idx_to:
                            try:
                                idx_to = int(raw_idx_to)
                            except ValueError:
                                return await v.cog.send_reply(modal_interaction, t(modal_interaction, "INVALID_INPUT"), ephemeral=True)
                            if idx_to < idx or idx_to > tcount:
                                return await v.cog.send_reply(modal_interaction, t(modal_interaction, "REMOVE_INVALID_RANGE",
                                    indexto=t(modal_interaction, "OPTNAME_REMOVE_INDEXTO")), ephemeral=True)
                            pos_from = v.tracks[idx - 1]["position"]
                            pos_to = v.tracks[idx_to - 1]["position"]
                            await db.remove_playlist_tracks_range(pl_id, pos_from, pos_to)
                            messages.append(t(modal_interaction, "PL_TRACKS_REMOVED_RANGE", start=idx, end=idx_to, name=pl_name))
                        else:
                            track = v.tracks[idx - 1] if idx - 1 < len(v.tracks) else {}
                            await db.remove_playlist_track(pl_id, track.get("position", idx))
                            messages.append(t(modal_interaction, "PL_TRACK_REMOVED", index=idx, name=pl_name))
                        # Reload tracks after index removal so search operates on updated list
                        await v.reload_tracks()

                    # --- search removal ---
                    if raw_search:
                        matched = _match_tracks(v.tracks, raw_search)
                        matching_positions = [v.tracks[i]["position"] for i in matched]
                        if matching_positions:
                            await db.remove_playlist_tracks_by_positions(pl_id, matching_positions)
                            messages.append(t(modal_interaction, "PL_SEARCH_REMOVED", count=len(matching_positions), name=pl_name, search=raw_search))
                        else:
                            messages.append(t(modal_interaction, "REMOVE_SEARCH_NONE", search=raw_search))

                    await v.reload_tracks()
                    if messages:
                        await v.cog.send_reply(modal_interaction, "\n".join(messages), ephemeral=True)
                finally:
                    await v.set_busy(False)

        class PLMoveButton(discord.ui.Button):
            def __init__(self, view, emoji, label, *, disabled=False):
                super().__init__(style=discord.ButtonStyle.secondary, emoji=emoji, label=label, disabled=disabled, row=2)
                self.view_ref = view
            async def callback(self, btn_interaction: discord.Interaction):
                if not await self.view_ref.cog._check_cooldown(btn_interaction, "pl:move", 5): return
                v = self.view_ref
                if len(v.tracks) < 2:
                    return await v.cog.send_reply(btn_interaction, t(btn_interaction, "PL_EMPTY"), ephemeral=True)
                await btn_interaction.response.send_modal(PLMoveModal(v))

        class PLMoveModal(discord.ui.Modal):
            def __init__(self, pl_view):
                super().__init__(title=t(interaction, "BUTTON_MOVE"))
                self.pl_view = pl_view
                tcount = len(pl_view.tracks)
                self.from_input = discord.ui.TextInput(
                    label=t(interaction, "OPT_MOVE_FROM"),
                    placeholder=f"1-{tcount}",
                    max_length=5,
                    required=True,
                )
                self.dest_input = discord.ui.TextInput(
                    label=t(interaction, "OPT_MOVE_DEST"),
                    placeholder=f"1-{tcount}",
                    max_length=5,
                    required=True,
                )
                self.to_input = discord.ui.TextInput(
                    label=t(interaction, "OPT_MOVE_TO"),
                    placeholder=f"1-{tcount}",
                    max_length=5,
                    required=False,
                )
                self.add_item(self.from_input)
                self.add_item(self.dest_input)
                self.add_item(self.to_input)
            async def on_submit(self, modal_interaction: discord.Interaction):
                v = self.pl_view
                if not v.playlist:
                    return await v.cog.send_reply(modal_interaction, t(modal_interaction, "PL_NO_PLAYLISTS"), ephemeral=True)
                v._busy = True
                try:
                    try:
                        from_idx = int(self.from_input.value)
                        dest = int(self.dest_input.value)
                    except ValueError:
                        return await v.cog.send_reply(modal_interaction, t(modal_interaction, "INVALID_INPUT"), ephemeral=True)
                    to_raw = self.to_input.value.strip() if self.to_input.value else ""
                    tcount = len(v.tracks)
                    await modal_interaction.response.defer(ephemeral=True)
                    await v.set_busy(True)
                    if to_raw:
                        try:
                            to_idx = int(to_raw)
                        except ValueError:
                            return await v.cog.send_reply(modal_interaction, t(modal_interaction, "INVALID_INPUT"), ephemeral=True)
                        if to_idx < from_idx:
                            return await v.cog.send_reply(modal_interaction, t(modal_interaction, "MOVE_INVALID_RANGE",
                                to_index=t(modal_interaction, "OPTNAME_MOVE_TO"), from_index=t(modal_interaction, "OPTNAME_MOVE_FROM")), ephemeral=True)
                        if not (1 <= from_idx <= tcount) or not (1 <= to_idx <= tcount):
                            return await v.cog.send_reply(modal_interaction, t(modal_interaction, "MOVE_INVALID"), ephemeral=True)
                        dest = max(1, min(dest, tcount))
                        if from_idx <= dest <= to_idx:
                            return await v.cog.send_reply(modal_interaction, t(modal_interaction, "MOVE_SAME"), ephemeral=True)
                        pos_from = v.tracks[from_idx - 1]["position"]
                        pos_to = v.tracks[to_idx - 1]["position"]
                        pos_dest = v.tracks[dest - 1]["position"]
                        await db.move_playlist_tracks(v.playlist["id"], pos_from, pos_to, pos_dest)
                        await v.cog.send_reply(modal_interaction,
                            t(modal_interaction, "MOVE_RANGE_SUCCESS", **{"from": from_idx, "to": to_idx, "dest": dest}), ephemeral=True)
                    else:
                        if not (1 <= from_idx <= tcount):
                            return await v.cog.send_reply(modal_interaction, t(modal_interaction, "MOVE_INVALID"), ephemeral=True)
                        dest = max(1, min(dest, tcount))
                        if from_idx == dest:
                            return await v.cog.send_reply(modal_interaction, t(modal_interaction, "MOVE_SAME"), ephemeral=True)
                        track = v.tracks[from_idx - 1] if from_idx - 1 < len(v.tracks) else {}
                        title = track.get("title") or t(modal_interaction, "UNKNOWN")
                        uploader = track.get("uploader") or t(modal_interaction, "UNKNOWN")
                        pos_from = v.tracks[from_idx - 1]["position"]
                        pos_dest = v.tracks[dest - 1]["position"]
                        await db.move_playlist_tracks(v.playlist["id"], pos_from, pos_dest)
                        await v.cog.send_reply(modal_interaction,
                            t(modal_interaction, "PL_TRACK_MOVED", **{"from": from_idx, "to": dest}), ephemeral=True)
                    await v.reload_tracks()
                finally:
                    await v.set_busy(False)

        return PlaylistView()

    # -- Shared playlist view factory --

    @staticmethod
    def _make_shared_playlist_view(interaction, playlist_id, playlist_name, tracks, user_mention, cog):
        per_page = cog.get_queue_per_page(interaction.guild_id)

        class SharedPlaylistView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=900)
                self.ctx = interaction
                self.cog = cog
                self._pid = playlist_id
                self.playlist_name = playlist_name
                self.user_mention = user_mention
                self.tracks = tracks
                self.page = 0
                self.per_page = per_page
                self.total_pages = max(1, (len(tracks) - 1) // per_page + 1)
                self.compact = cog.is_queue_button_compact(interaction.guild_id)
                self._msg: discord.Message | None = None
                self._busy = False
                self._build()

            def _cl(self, key):
                e = _BUTTON_EMOJIS.get(key)
                l = None if self.compact else t(self.ctx, key)
                return e, l

            async def on_timeout(self):
                for item in self.children:
                    item.disabled = True
                await self._edit_view()

            async def interaction_check(self, check_interaction):
                if not await self.cog.check_view_interaction(check_interaction):
                    return False
                if self._busy:
                    return False
                fresh_pl = await db.get_playlist_by_id(self._pid)
                if not fresh_pl:
                    try:
                        if self._msg:
                            await self._msg.delete()
                    except discord.HTTPException:
                        pass
                    self.stop()
                    await self.cog.send_reply(check_interaction, t(check_interaction, "PL_SHARED_DELETED"), ephemeral=True)
                    return False
                fresh_tracks = await db.get_playlist_tracks(self._pid)
                old_snap = (self.playlist_name,
                            tuple(tr.get("url", "") for tr in self.tracks))
                new_snap = (fresh_pl["name"],
                            tuple(tr.get("url", "") for tr in fresh_tracks))
                if old_snap != new_snap:
                    self.playlist_name = fresh_pl["name"]
                    self.tracks = fresh_tracks
                    self.total_pages = max(1, (len(self.tracks) - 1) // self.per_page + 1)
                    self.page = min(self.page, self.total_pages - 1)
                    self._build()
                    await self._edit_view()
                    await self.cog.send_reply(check_interaction,
                        t(check_interaction, "PL_VIEW_STALE"), ephemeral=True)
                    return False
                return True

            def get_playlist_id(self):
                return self._pid

            def _get_search_tracks(self):
                return self.tracks

            async def reload_tracks(self):
                self.tracks = await db.get_playlist_tracks(self._pid)
                self.total_pages = max(1, (len(self.tracks) - 1) // self.per_page + 1)
                self.page = min(self.page, self.total_pages - 1)
                self._build()

            async def set_busy(self, busy):
                self._busy = busy
                if busy:
                    for item in self.children:
                        item.disabled = True
                else:
                    self._build()
                await self._edit_view()

            async def _edit_view(self):
                if self._msg:
                    try:
                        await self._msg.edit(embed=self.get_embed(), view=self)
                    except discord.HTTPException:
                        pass

            def _build(self):
                self.clear_items()
                empty = not self.tracks
                self.add_item(_PageButton(self, "⏪", None, "first", disabled=empty or self.page == 0))
                self.add_item(_PageButton(self, "⬅️", None, "prev", disabled=empty or self.page == 0))
                self.add_item(_PageButton(self, "➡️", None, "next", disabled=empty or self.page >= self.total_pages - 1))
                self.add_item(_PageButton(self, "⏩", None, "last", disabled=empty or self.page >= self.total_pages - 1))
                self.add_item(_GoToPageButton(self, disabled=self.total_pages <= 1))
                self.add_item(_PLPlayBtn(self, *self._cl("BUTTON_PLAY_ALL"), disabled=empty, cooldown_key="shared:play", row=1))
                self.add_item(_SearchButton(self, label=None if self.compact else t(self.ctx, "BUTTON_SEARCH"), emoji="🔍", disabled=empty, row=1))
                self.add_item(_PLCopyBtn(self, *self._cl("BUTTON_COPY"), cooldown_key="shared:copy", row=1))
                self.add_item(_PLRefreshBtn(self, *self._cl("BUTTON_REFRESH"), cooldown_key="shared:refresh", row=1))

            def get_embed(self):
                color = self.cog.get_embed_color(self.ctx.guild_id)
                embed = SafeEmbed(title=t(self.ctx, "PL_SHARED_HEADER", name=f"`{self.playlist_name}`"), color=color)
                total_tracks = len(self.tracks)
                header = f"{t(self.ctx, 'PL_SHARED_BY', user=self.user_mention)} · {t(self.ctx, 'PL_TRACK_COUNT', count=total_tracks)} ({self.page + 1}/{self.total_pages})"
                if not self.tracks:
                    embed.description = f"{header}\n\n{t(self.ctx, 'PL_EMPTY')}"
                    return embed
                start = self.page * self.per_page
                end = start + self.per_page
                lines = _fmt_track_lines(self.tracks[start:end], self.ctx, start_index=start + 1)
                sep = "\n" if cog.is_queue_compact(self.ctx.guild_id) else "\n\n"
                embed.description = f"{header}\n\n" + sep.join(lines)
                return embed

        return SharedPlaylistView()

    # endregion Playlist

    @app_commands.command(**l_cmd("CMD_NAME_SHUFFLE", "CMD_DESC_SHUFFLE"))
    @app_commands.rename(from_index=l_opt("OPTNAME_SHUFFLE_FROM"), to_index=l_opt("OPTNAME_SHUFFLE_TO"))
    @app_commands.describe(from_index=l_opt("OPT_SHUFFLE_FROM"), to_index=l_opt("OPT_SHUFFLE_TO"))
    async def shuffle_cmd(self, interaction: discord.Interaction, from_index: int | None = None, to_index: int | None = None):
        if not await self._check_cooldown(interaction, "mp:shuffle", 1, per_guild=True): return
        if not await self._check_cooldown(interaction, "mp:shuffle", 5): return
        await interaction.response.defer()
        if self.is_radio_active(interaction.guild_id):
            return await self.send_reply(interaction, t(interaction, "RADIO_CMD_RESTRICTED", cmd_stop=t(interaction, "CMD_NAME_STOP")), ephemeral=True)
        try:
            state = self.ensure_queue_data(interaction, require_queue=True)
        except CommandCheckError as e:
            return await respond_with_error(interaction, e)
        queue = state.queue
        guild_id = interaction.guild.id
        if from_index is not None or to_index is not None:
            if from_index is None:
                from_index = 1
            if to_index is None:
                to_index = len(queue)
            if from_index < 1 or to_index > len(queue) or to_index <= from_index:
                return await self.send_reply(interaction, t(interaction, "SHUFFLE_RANGE_INVALID"), ephemeral=True)
            snapshot = tuple(id(e) for e in queue)
            await _queue_shuffle(self, interaction, guild_id, from_index, to_index, snapshot)
        else:
            if len(queue) < 2:
                return await self.send_reply(interaction, t(interaction, "SHUFFLE_NEED_TWO"), ephemeral=True)
            snapshot = tuple(id(e) for e in queue)
            await _queue_shuffle(self, interaction, guild_id, None, None, snapshot)

    @app_commands.command(**l_cmd("CMD_NAME_SKIP", "CMD_DESC_SKIP"))
    async def skip_cmd(self, interaction: discord.Interaction):
        if not await self._check_cooldown(interaction, "mp:skip", 1, per_guild=True): return
        if not await self._check_cooldown(interaction, "mp:skip", 5): return
        await interaction.response.defer()
        await self.handle_skip(interaction)

    @app_commands.command(**l_cmd("CMD_NAME_STOP", "CMD_DESC_STOP"))
    @app_commands.rename(current=l_opt("OPTNAME_STOP_CURRENT"))
    @app_commands.describe(current=l_opt("OPT_STOP_CURRENT"))
    @app_commands.choices(current=[app_commands.Choice(name="✅", value="on")])
    async def stop_cmd(self, interaction: discord.Interaction, current: app_commands.Choice[str] = None):
        if not await self._check_cooldown(interaction, "mp:stop", 1, per_guild=True): return
        if not await self._check_cooldown(interaction, "mp:stop", 5): return
        await interaction.response.defer()
        vc = interaction.guild.voice_client
        if not vc:
            return await self.send_reply(interaction, t(interaction, "BOT_NOT_IN_VOICE"), ephemeral=True)
        if current:
            guild_id = interaction.guild_id
            if not self.has_control_privilege(guild_id, interaction.user) and \
               not self.has_admin_privilege(guild_id, interaction.user):
                return await self.send_reply(interaction, t(interaction, "NOT_DJ_OR_ADMIN"), ephemeral=True)
            task = self._bg_fetch_tasks.get(guild_id)
            ftask = self._bg_fetch_forced_tasks.get(guild_id)
            has_active = (task and not task.done()) or (ftask and not ftask.done())
            if not has_active:
                return await self.send_reply(interaction, t(interaction, "NO_LOADING_ACTIVE"), ephemeral=True)
            self._cancel_bg_fetch(guild_id)
            return await self.send_reply(interaction, t(interaction, "LOADING_CANCELLED"))
        await _do_stop_full(self, interaction, interaction.guild.id, vc)


    # --- Radio command ---

    @app_commands.command(**l_cmd("CMD_NAME_RADIO", "CMD_DESC_RADIO"))
    async def radio_cmd(self, interaction: discord.Interaction):
        if not await self._check_cooldown(interaction, "radio", 5): return
        await interaction.response.defer(ephemeral=True)
        guild_id = interaction.guild_id
        _da = self._resolve_delete_after(guild_id)
        if guild_id in self._radio_initializing:
            return await self.send_reply(interaction, t(interaction, "RADIO_INITIALIZING"), ephemeral=True, delete_after=_da)
        if self.is_radio_active(guild_id):
            session = self.get_radio_session(guild_id)
            if session and session._initial_fetch and not session._initial_fetch.done():
                return await self.send_reply(interaction, t(interaction, "RADIO_STILL_LOADING"), ephemeral=True, delete_after=_da)
            if not self.has_radio_edit_permission(guild_id, interaction.user):
                return await self.send_reply(interaction, t(interaction, "RADIO_NO_EDIT_PERMISSION"), ephemeral=True, delete_after=_da)
            view = self._RadioEditView(interaction, self, session)
            await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)
            return
        if not self.has_radio_permission(guild_id, interaction.user):
            return await self.send_reply(interaction, t(interaction, "RADIO_NO_PERMISSION"), ephemeral=True, delete_after=_da)
        if not self.has_control_privilege(guild_id, interaction.user) and not self.has_admin_privilege(guild_id, interaction.user):
            cd = self.check_radio_cooldown(guild_id, interaction.user.id)
            if cd is not None:
                remaining = int(cd - time.time())
                return await self.send_reply(interaction, t(interaction, "RADIO_COOLDOWN", seconds=max(remaining, 1)), ephemeral=True, delete_after=_da)
        if not interaction.user.voice or not interaction.user.voice.channel:
            return await self.send_reply(interaction, t(interaction, "JOIN_VOICE_FIRST"), ephemeral=True, delete_after=_da)

        cfg_key = (guild_id, interaction.user.id)
        old_inter = self._radio_config_interactions.pop(cfg_key, None)
        if old_inter:
            try:
                await old_inter.delete_original_response()
            except Exception:
                pass

        view = self._RadioConfigView(interaction, self)
        await interaction.followup.send(embed=view.build_embed(), view=view, ephemeral=True)
        self._radio_config_interactions[cfg_key] = interaction

    # --- Shared radio UI components ---
    # Both _RadioConfigView and _RadioEditView use these.
    # Views must expose: ctx, source, query_value, track_limit,
    #   time_limit, source_select, build_embed(), _sync_action_btn(),
    #   _check_session(interaction).

    class _RadioSourceSelect(discord.ui.Select):
        def __init__(self, view_ref, ctx):
            options = [
                discord.SelectOption(label=t(ctx, "RADIO_SOURCE_QUEUE"), value="queue",
                                     description=t(ctx, "RADIO_SOURCE_QUEUE_DESC")),
                discord.SelectOption(label=t(ctx, "RADIO_SOURCE_HISTORY"), value="history",
                                     description=t(ctx, "RADIO_SOURCE_HISTORY_DESC")),
                discord.SelectOption(label=t(ctx, "RADIO_SOURCE_QUERY"), value="query",
                                     description=t(ctx, "RADIO_SOURCE_QUERY_DESC")),
            ]
            super().__init__(placeholder=t(ctx, "RADIO_SOURCE_PLACEHOLDER"), options=options, min_values=0, row=0)
            self.view_ref = view_ref

        def _sync_defaults(self):
            src = self.view_ref.source
            for opt in self.options:
                opt.default = opt.value == src if opt.value != "query" else False

        async def callback(self, interaction: discord.Interaction):
            if not self.values:
                return await interaction.response.defer()
            if not await self.view_ref._check_session(interaction):
                return
            chosen = self.values[0]
            if chosen == "query":
                modal = MusicCog._RadioQueryModal(self.view_ref)
                await interaction.response.send_modal(modal)
            else:
                self.view_ref.source = chosen
                self.view_ref.query_value = None
                self._sync_defaults()
                self.view_ref._sync_action_btn()
                await interaction.response.edit_message(embed=self.view_ref.build_embed(), view=self.view_ref)

    class _RadioQueryModal(discord.ui.Modal):
        def __init__(self, view_ref):
            super().__init__(title=t(view_ref.ctx, "RADIO_QUERY_MODAL_TITLE"))
            self.view_ref = view_ref
            self.query_input = discord.ui.TextInput(
                label=t(view_ref.ctx, "RADIO_QUERY_LABEL"),
                placeholder=t(view_ref.ctx, "RADIO_QUERY_LABEL"),
                required=True,
                max_length=200,
            )
            self.add_item(self.query_input)

        async def on_submit(self, interaction: discord.Interaction):
            self.view_ref.query_value = self.query_input.value.strip()
            self.view_ref.source = "query"
            self.view_ref.source_select._sync_defaults()
            self.view_ref._sync_action_btn()
            await interaction.response.edit_message(embed=self.view_ref.build_embed(), view=self.view_ref)

    class _RadioLimitsSelect(discord.ui.Select):
        def __init__(self, view_ref, ctx, *, row=1):
            _tmin, _tmax = RADIO_TRACK_LIMIT_RANGE
            _mmin, _mmax = RADIO_TIME_LIMIT_RANGE
            options = [
                discord.SelectOption(label=t(ctx, "RADIO_LIMITS_TRACK"), value="track",
                                     description=t(ctx, "RADIO_LIMITS_TRACK_DESC", min=_tmin, max=_tmax)),
                discord.SelectOption(label=t(ctx, "RADIO_LIMITS_TIME"), value="time",
                                     description=t(ctx, "RADIO_LIMITS_TIME_DESC", min=_mmin, max=_mmax)),
            ]
            super().__init__(placeholder=t(ctx, "RADIO_LIMITS_PLACEHOLDER"), options=options, min_values=0, row=row)
            self.view_ref = view_ref

        async def callback(self, interaction: discord.Interaction):
            if not self.values:
                return await interaction.response.defer()
            if not await self.view_ref._check_session(interaction):
                return
            chosen = self.values[0]
            if chosen == "track":
                modal = MusicCog._RadioTrackLimitModal(self.view_ref)
            else:
                modal = MusicCog._RadioTimeLimitModal(self.view_ref)
            await interaction.response.send_modal(modal)

    class _RadioTrackLimitModal(discord.ui.Modal):
        def __init__(self, view_ref):
            super().__init__(title=t(view_ref.ctx, "RADIO_LIMITS_TRACK"))
            self.view_ref = view_ref
            _min, _max = RADIO_TRACK_LIMIT_RANGE
            current = view_ref.track_limit
            self.limit_input = discord.ui.TextInput(
                label=t(view_ref.ctx, "RADIO_TRACK_LIMIT_LABEL", min=_min, max=_max),
                placeholder=t(view_ref.ctx, "RADIO_TRACK_LIMIT_PLACEHOLDER"),
                default=str(current) if current > 0 else None,
                required=False,
                max_length=5,
            )
            self.add_item(self.limit_input)

        async def on_submit(self, interaction: discord.Interaction):
            _min, _max = RADIO_TRACK_LIMIT_RANGE
            raw = self.limit_input.value.strip()
            if raw and raw.isdigit():
                val = int(raw)
                self.view_ref.track_limit = max(_min, min(val, _max)) if val > 0 else 0
            else:
                self.view_ref.track_limit = 0
            if self.view_ref.track_limit > 0:
                self.view_ref.time_limit = 0
            await interaction.response.edit_message(embed=self.view_ref.build_embed(), view=self.view_ref)

    class _RadioTimeLimitModal(discord.ui.Modal):
        def __init__(self, view_ref):
            super().__init__(title=t(view_ref.ctx, "RADIO_LIMITS_TIME"))
            self.view_ref = view_ref
            _min, _max = RADIO_TIME_LIMIT_RANGE
            current = view_ref.time_limit
            self.limit_input = discord.ui.TextInput(
                label=t(view_ref.ctx, "RADIO_TIME_LIMIT_LABEL", min=_min, max=_max),
                placeholder=t(view_ref.ctx, "RADIO_TIME_LIMIT_PLACEHOLDER"),
                default=str(current) if current > 0 else None,
                required=False,
                max_length=5,
            )
            self.add_item(self.limit_input)

        async def on_submit(self, interaction: discord.Interaction):
            _min, _max = RADIO_TIME_LIMIT_RANGE
            raw = self.limit_input.value.strip()
            if raw and raw.isdigit():
                val = int(raw)
                self.view_ref.time_limit = max(_min, min(val, _max)) if val > 0 else 0
            else:
                self.view_ref.time_limit = 0
            if self.view_ref.time_limit > 0:
                self.view_ref.track_limit = 0
            await interaction.response.edit_message(embed=self.view_ref.build_embed(), view=self.view_ref)

    # --- Radio config view (pre-start) ---

    class _RadioConfigView(discord.ui.View):
        _SOURCE_KEYS = {"queue": "RADIO_SOURCE_QUEUE", "history": "RADIO_SOURCE_HISTORY", "query": "RADIO_SOURCE_QUERY"}

        def __init__(self, interaction, cog):
            super().__init__(timeout=900)
            self.ctx = interaction
            self.cog = cog
            self.guild_id = interaction.guild_id
            self.source: str | None = None
            self.query_value: str | None = None
            self.track_limit = 0
            self.time_limit = 0

            self.source_select = MusicCog._RadioSourceSelect(self, interaction)
            self.limits_select = MusicCog._RadioLimitsSelect(self, interaction)
            self.start_btn = self._StartButton(self, interaction)
            self.start_btn.disabled = True
            self.add_item(self.source_select)
            self.add_item(self.limits_select)
            self.add_item(self.start_btn)

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            return await self.cog.check_view_interaction(interaction)

        async def _check_session(self, interaction: discord.Interaction) -> bool:
            return True

        def _sync_action_btn(self):
            ready = self.source and (self.source != "query" or self.query_value)
            self.start_btn.disabled = not ready

        def build_embed(self):
            desc = t(self.ctx, "RADIO_CONFIG_DESC")
            source_label = t(self.ctx, self._SOURCE_KEYS[self.source]) if self.source else "-"
            desc += f"\n\n**{t(self.ctx, 'RADIO_SOURCE_PLACEHOLDER')}:** {source_label}"
            if self.source == "query" and self.query_value:
                desc += f"\n**{t(self.ctx, 'RADIO_QUERY_LABEL')}:** {self.query_value[:80]}"
            if self.track_limit > 0:
                desc += f"\n**{t(self.ctx, 'RADIO_LIMITS_TRACK')}:** {self.track_limit}"
            if self.time_limit > 0:
                desc += f"\n**{t(self.ctx, 'RADIO_TIME_LIMIT_DISPLAY')}:** {self.time_limit}{t(self.ctx, 'ABBR_MINUTES')}"
            return SafeEmbed(
                title=t(self.ctx, "RADIO_CONFIG_TITLE"),
                description=desc,
                color=self.cog.get_embed_color(self.guild_id),
            )

        class _StartButton(discord.ui.Button):
            def __init__(self, view_ref, ctx):
                super().__init__(
                    style=discord.ButtonStyle.success,
                    emoji="▶️",
                    label=t(ctx, "RADIO_START_BUTTON"),
                    row=2,
                )
                self.view_ref = view_ref
                self.ctx_ref = ctx

            async def callback(self, interaction: discord.Interaction):
                vr = self.view_ref
                cog = vr.cog
                guild_id = vr.guild_id
                ctx = self.ctx_ref
                _da = cog._resolve_delete_after(guild_id)

                if not vr.source or (vr.source == "query" and not vr.query_value):
                    return await interaction.response.send_message(
                        t(ctx, "RADIO_SELECT_SOURCE_FIRST"),
                        ephemeral=True, delete_after=_da,
                    )

                # Pre-validate source availability before committing
                err_msg = None
                if vr.source == "queue":
                    state = cog.guild_states.get(guild_id)
                    has_tracks = state and (state.now or state.queue)
                    if not has_tracks:
                        err_msg = t(ctx, "RADIO_NO_QUEUE")
                elif vr.source == "history":
                    max_h = cog.guild_max_history.get(guild_id, 50)
                    if max_h <= 0:
                        err_msg = t(ctx, "RADIO_HISTORY_DISABLED")

                if err_msg:
                    vr.source = None
                    vr.query_value = None
                    vr.source_select._sync_defaults()
                    vr._sync_action_btn()
                    await interaction.response.edit_message(embed=vr.build_embed(), view=vr)
                    msg = await interaction.followup.send(err_msg, ephemeral=True, wait=True)
                    if _da and msg:
                        await msg.delete(delay=_da)
                    return

                if cog.is_radio_active(guild_id) or guild_id in cog._radio_initializing:
                    vr.stop()
                    cog._radio_config_interactions.pop((guild_id, interaction.user.id), None)
                    _da = cog._resolve_delete_after(guild_id)
                    await interaction.response.send_message(t(ctx, "RADIO_ALREADY_ACTIVE"), ephemeral=True, delete_after=_da)
                    try:
                        await ctx.delete_original_response()
                    except Exception:
                        pass
                    return

                # Disable controls immediately while resolving
                for item in vr.children:
                    item.disabled = True
                await interaction.response.edit_message(view=vr)

                # Re-check after await to prevent race between two simultaneous starts
                if cog.is_radio_active(guild_id) or guild_id in cog._radio_initializing:
                    vr.stop()
                    cog._radio_config_interactions.pop((guild_id, interaction.user.id), None)
                    _da2 = cog._resolve_delete_after(guild_id)
                    _msg2 = await interaction.followup.send(t(ctx, "RADIO_ALREADY_ACTIVE"), ephemeral=True, wait=True)
                    if _da2 and _msg2:
                        await _msg2.delete(delay=_da2)
                    try:
                        await ctx.delete_original_response()
                    except Exception:
                        pass
                    return

                cog._radio_initializing.add(guild_id)
                try:
                    seed_track = None
                    try:
                        seed_track = await cog._resolve_radio_seed(interaction, vr.source, vr.query_value)
                    except Exception as e:
                        for item in vr.children:
                            item.disabled = False
                        vr._sync_action_btn()
                        await interaction.edit_original_response(view=vr)
                        return await interaction.followup.send(str(e)[:200], ephemeral=True)

                    if not seed_track or not seed_track.get("id"):
                        for item in vr.children:
                            item.disabled = False
                        vr._sync_action_btn()
                        await interaction.edit_original_response(view=vr)
                        return await interaction.followup.send(t(ctx, "RADIO_NO_SEED"), ephemeral=True)

                    # Seed resolved, now show "starting" embed and remove buttons
                    vr.stop()
                    cog._radio_config_interactions.pop((guild_id, interaction.user.id), None)
                    starting_embed = SafeEmbed(
                        title=t(ctx, "RADIO_CONFIG_TITLE"),
                        description=t(ctx, "RADIO_STARTING", seed=seed_track.get("title", "?")),
                        color=cog.get_embed_color(guild_id),
                    )
                    await interaction.edit_original_response(embed=starting_embed, view=None)

                    try:
                        await cog._start_radio_session(interaction, seed_track, vr.source, vr.track_limit, vr.time_limit, query=vr.query_value)
                    except Exception as exc:
                        print(f"[Radio start] guild {guild_id}: {type(exc).__name__}: {exc}")
                        cog.radio_sessions.pop(guild_id, None)
                        _da = cog._resolve_delete_after(guild_id)
                        try:
                            await interaction.edit_original_response(
                                embed=SafeEmbed(title=t(ctx, "RADIO_CONFIG_TITLE"),
                                    description=t(ctx, "RADIO_ENDED_STOPPED"), color=cog.get_embed_color(guild_id)),
                                view=None)
                            if _da:
                                await asyncio.sleep(_da)
                                await interaction.delete_original_response()
                        except Exception:
                            pass
                finally:
                    cog._radio_initializing.discard(guild_id)

    # --- Radio edit view (live session) ---

    class _RadioEditView(discord.ui.View):
        _SOURCE_KEYS = {"queue": "RADIO_SOURCE_QUEUE", "history": "RADIO_SOURCE_HISTORY", "query": "RADIO_SOURCE_QUERY"}

        def __init__(self, interaction, cog, session):
            super().__init__(timeout=900)
            self.ctx = interaction
            self.cog = cog
            self.guild_id = interaction.guild_id
            self.session = session
            self.source = session.source_type
            self.query_value = session.source_query

            self.source_select = MusicCog._RadioSourceSelect(self, interaction)
            self.limits_select = MusicCog._RadioLimitsSelect(self, interaction, row=2)
            self.restart_btn = self._RestartButton(self, interaction)
            self.restart_btn.disabled = True
            self.stop_btn = self._StopRadioButton(self, interaction)
            self.refresh_btn = self._RefreshButton(self, interaction)
            self.add_item(self.source_select)
            self.add_item(self.restart_btn)
            self.add_item(self.stop_btn)
            self.add_item(self.refresh_btn)
            self.add_item(self.limits_select)

        @property
        def track_limit(self):
            return self.session.track_limit

        @track_limit.setter
        def track_limit(self, val):
            self.session.track_limit = val

        @property
        def time_limit(self):
            return self.session.timeout_minutes

        @time_limit.setter
        def time_limit(self, val):
            self.session.timeout_minutes = val
            self.cog._schedule_radio_timeout(self.guild_id)

        def build_embed(self):
            s = self.session
            _color = self.cog.get_embed_color(self.guild_id)
            source_label = t(self.ctx, self._SOURCE_KEYS.get(s.source_type, "RADIO_SOURCE_QUEUE"))
            seed_title = s.seed_track.get("title", "?")
            seed_artist = s.seed_track.get("artist") or s.seed_track.get("uploader") or ""
            seed_display = seed_title
            if seed_artist:
                seed_display = f"{seed_title} - {seed_artist}"
            elapsed = int(time.time() - s.started_at)
            time_str = _fmt_uptime(self.ctx, elapsed)

            desc = t(self.ctx, "RADIO_EDIT_DESC")
            desc += f"\n**{t(self.ctx, 'RADIO_SOURCE_PLACEHOLDER')}:** {source_label}"
            desc += f"\n**{t(self.ctx, 'RADIO_QUEUE_SEED')}:** {seed_display[:80]}"
            desc += f"\n**{t(self.ctx, 'RADIO_EDIT_PLAYED')}:** {s.tracks_played}"
            if s.track_limit > 0:
                desc += f" / {s.track_limit}"
            if s.refill_failed:
                desc += f"\n**{t(self.ctx, 'RADIO_REFILL_ERROR_LABEL')}:** {t(self.ctx, 'RADIO_FOOTER_REFILL_FAILED')}"
            desc += f"\n**{t(self.ctx, 'RADIO_QUEUE_STARTED_BY')}:** <@{s.starter_id}>"
            _tz = self.cog.guild_timezones.get(self.guild_id, 0)
            _local = time.gmtime(s.started_at + _tz * 3600)
            _tz_lbl = f"UTC{_tz:+d}" if _tz != 0 else "UTC"
            desc += f"\n**{t(self.ctx, 'RADIO_EDIT_STARTED_AT')}:** {time.strftime('%d/%m/%Y, %H:%M', _local)} ({_tz_lbl})"
            desc += f"\n**{t(self.ctx, 'RADIO_QUEUE_UPTIME')}:** {time_str}"
            if s.timeout_minutes > 0:
                remaining = max(0, int(s.timeout_minutes * 60 - elapsed))
                h, rem = divmod(remaining, 3600)
                m, sec = divmod(rem, 60)
                time_remaining = f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
                desc += f"\n**{t(self.ctx, 'RADIO_TIME_LIMIT_DISPLAY')}:** {time_remaining}"

            return SafeEmbed(
                title=t(self.ctx, "RADIO_EDIT_TITLE"),
                description=desc,
                color=_color,
            )

        def _sync_action_btn(self):
            ready = self.source and (self.source != "query" or self.query_value)
            self.restart_btn.disabled = not ready

        async def interaction_check(self, interaction: discord.Interaction) -> bool:
            if not await self.cog.check_view_interaction(interaction):
                return False
            # If another radio session is initializing or the live session is not ours, reject
            if self.guild_id in self.cog._radio_initializing or self.cog.get_radio_session(self.guild_id) is not self.session:
                self.stop()
                _da = self.cog._resolve_delete_after(self.guild_id)
                await interaction.response.send_message(
                    t(self.ctx, "RADIO_ALREADY_ACTIVE"), ephemeral=True, delete_after=_da)
                try:
                    await self.ctx.delete_original_response()
                except Exception:
                    pass
                return False
            return True

        async def _check_session(self, interaction: discord.Interaction) -> bool:
            session = self.cog.get_radio_session(self.guild_id)
            if session is self.session and session.active:
                return True
            self.stop()
            _da = self.cog._resolve_delete_after(self.guild_id)
            await interaction.response.send_message(
                t(self.ctx, "RADIO_SESSION_EXPIRED"), ephemeral=True, delete_after=_da)
            try:
                await self.ctx.delete_original_response()
            except Exception:
                pass
            return False

        class _RestartButton(discord.ui.Button):
            def __init__(self, view_ref, ctx):
                super().__init__(
                    style=discord.ButtonStyle.success,
                    emoji="🔄",
                    label=t(ctx, "RADIO_EDIT_RESTART"),
                    row=1,
                )
                self.view_ref = view_ref
                self.ctx_ref = ctx

            async def callback(self, interaction: discord.Interaction):
                vr = self.view_ref
                cog = vr.cog
                guild_id = vr.guild_id
                ctx = self.ctx_ref

                if not await vr._check_session(interaction):
                    return

                for item in vr.children:
                    item.disabled = True
                await interaction.response.edit_message(view=vr)

                cog._radio_initializing.add(guild_id)
                try:
                    seed_track = None
                    try:
                        seed_track = await cog._resolve_radio_seed(interaction, vr.source, vr.query_value)
                    except Exception as e:
                        for item in vr.children:
                            item.disabled = False
                        vr._sync_action_btn()
                        await interaction.edit_original_response(view=vr)
                        return await interaction.followup.send(str(e)[:200], ephemeral=True)

                    if not seed_track or not seed_track.get("id"):
                        for item in vr.children:
                            item.disabled = False
                        vr._sync_action_btn()
                        await interaction.edit_original_response(view=vr)
                        return await interaction.followup.send(t(ctx, "RADIO_NO_SEED"), ephemeral=True)

                    track_limit = vr.track_limit
                    time_limit = vr.time_limit

                    await cog._end_radio(guild_id, "restart")
                    vc = interaction.guild.voice_client
                    if vc and (vc.is_playing() or vc.is_paused()):
                        state = cog.guild_states.get(guild_id)
                        if state:
                            state.suppress_after_callback = True
                        vc.stop()

                    vr.stop()
                    starting_embed = SafeEmbed(
                        title=t(ctx, "RADIO_EDIT_TITLE"),
                        description=t(ctx, "RADIO_STARTING", seed=seed_track.get("title", "?")),
                        color=cog.get_embed_color(guild_id),
                    )
                    await interaction.edit_original_response(embed=starting_embed, view=None)
                    try:
                        await cog._start_radio_session(interaction, seed_track, vr.source, track_limit, time_limit, query=vr.query_value)
                    except Exception as exc:
                        print(f"[Radio restart] guild {guild_id}: {type(exc).__name__}: {exc}")
                        cog.radio_sessions.pop(guild_id, None)
                        _da = cog._resolve_delete_after(guild_id)
                        try:
                            await interaction.edit_original_response(
                                embed=SafeEmbed(title=t(ctx, "RADIO_EDIT_TITLE"),
                                    description=t(ctx, "RADIO_ENDED_STOPPED"), color=cog.get_embed_color(guild_id)),
                                view=None)
                            if _da:
                                await asyncio.sleep(_da)
                                await interaction.delete_original_response()
                        except Exception:
                            pass
                finally:
                    cog._radio_initializing.discard(guild_id)

        class _RefreshButton(discord.ui.Button):
            def __init__(self, view_ref, ctx):
                super().__init__(
                    style=discord.ButtonStyle.secondary,
                    emoji="🔃",
                    label=t(ctx, "RADIO_EDIT_REFRESH"),
                    row=1,
                )
                self.view_ref = view_ref

            async def callback(self, interaction: discord.Interaction):
                if not await self.view_ref._check_session(interaction):
                    return
                await interaction.response.edit_message(embed=self.view_ref.build_embed(), view=self.view_ref)

        class _StopRadioButton(discord.ui.Button):
            def __init__(self, view_ref, ctx):
                super().__init__(
                    style=discord.ButtonStyle.danger,
                    emoji="⏹️",
                    label=t(ctx, "RADIO_EDIT_STOP"),
                    row=1,
                )
                self.view_ref = view_ref
                self.ctx_ref = ctx

            async def callback(self, interaction: discord.Interaction):
                vr = self.view_ref
                cog = vr.cog
                guild_id = vr.guild_id
                session = cog.get_radio_session(guild_id)

                if not session or not session.active:
                    vr.stop()
                    await interaction.response.defer()
                    try:
                        await vr.ctx.delete_original_response()
                    except Exception:
                        pass
                    return

                await interaction.response.defer()

                is_self = interaction.user.id == session.starter_id
                reason = "stopped_self" if is_self else "stopped"
                await cog._end_radio(guild_id, reason)

                vr.stop()
                try:
                    await vr.ctx.delete_original_response()
                except Exception:
                    pass

    def _build_radio_queue_embed(self, interaction, session) -> SafeEmbed:
        """Build an embed showing radio session info for the /queue command."""
        guild_id = interaction.guild_id
        _color = self.get_embed_color(guild_id)

        seed_title = session.seed_track.get("title", "?")
        seed_artist = session.seed_track.get("artist") or session.seed_track.get("uploader") or ""
        seed_url = session.seed_track.get("webpage_url") or session.seed_track.get("url", "")
        if seed_url:
            safe_seed = seed_title.replace("[", "⌜").replace("]", "⌝")
            seed_display = f"[{safe_seed}]({seed_url})"
        else:
            seed_display = seed_title
        if seed_artist:
            seed_display += f" - *{seed_artist}*"

        elapsed = int(time.time() - session.started_at)
        uptime_str = _fmt_uptime(interaction, elapsed)

        state = self.guild_states.get(guild_id)
        queue_len = len(state.queue) if state else 0
        related_played = session.tracks_played

        desc = f"**{t(interaction, 'RADIO_QUEUE_SEED')}:** {seed_display}"
        desc += f"\n**{t(interaction, 'RADIO_QUEUE_PLAYED')}:** {related_played}"
        desc += f"\n**{t(interaction, 'RADIO_QUEUE_REMAINING')}:** {queue_len}"
        desc += f"\n**{t(interaction, 'RADIO_QUEUE_UPTIME')}:** {uptime_str}"

        starter = session.starter_id
        desc += f"\n**{t(interaction, 'RADIO_QUEUE_STARTED_BY')}:** <@{starter}>"

        if session.track_limit > 0:
            desc += f"\n**{t(interaction, 'RADIO_LIMITS_TRACK')}:** {related_played}/{session.track_limit}"
        if session.timeout_minutes > 0:
            remaining_min = max(0, session.timeout_minutes - elapsed // 60)
            desc += f"\n**{t(interaction, 'RADIO_LIMITS_TIME')}:** {remaining_min}{t(interaction, 'ABBR_MINUTES')}"

        _title = t(interaction, "RADIO_QUEUE_TITLE")
        if session._fetch_lock.locked():
            _title += f" ({t(interaction, 'RADIO_FETCHING')})"
        embed = SafeEmbed(
            title=_title,
            description=desc,
            color=_color,
        )
        return embed

    async def _resolve_radio_seed(self, interaction, source: str, query_value: str | None) -> dict | None:
        guild_id = interaction.guild_id
        if source == "queue":
            state = self.guild_states.get(guild_id)
            if not state or (not state.now and not state.queue):
                raise ValueError(t(interaction, "RADIO_NO_QUEUE"))
            pool = []
            if state.now and state.now.get("id"):
                pool.append(state.now)
            pool.extend(e for e in state.queue if e.get("id"))
            if not pool:
                raise ValueError(t(interaction, "RADIO_NO_QUEUE"))
            return random.choice(pool)

        if source == "history":
            max_h = self.guild_max_history.get(guild_id, 50)
            if max_h <= 0:
                raise ValueError(t(interaction, "RADIO_HISTORY_DISABLED"))
            history = await db.get_history(guild_id, limit=50)
            yt_tracks = [h for h in history if h.get("url") and ("youtube.com" in h["url"] or "youtu.be" in h["url"])]
            if not yt_tracks:
                raise ValueError(t(interaction, "RADIO_NO_HISTORY"))
            entry = random.choice(yt_tracks)
            vid_match = re.search(r"(?:v=|youtu\.be/)([0-9A-Za-z_-]{11})", entry.get("url", ""))
            if not vid_match:
                raise ValueError(t(interaction, "RADIO_NO_SEED"))
            return {"id": vid_match.group(1), "title": entry.get("title", ""), "url": entry["url"],
                    "webpage_url": entry["url"], "uploader": entry.get("uploader", "")}

        if source == "query" and query_value:
            from urllib.parse import quote_plus
            # Spotify link, resolve tracks, pick random, search YT for a video ID
            if is_spotify_url(query_value):
                try:
                    result = await get_spotify_first_batch(query_value)
                except SpotifyError as e:
                    raise ValueError(str(e)) from e
                tracks = list(result.tracks)
                if not tracks:
                    raise ValueError(t(interaction, "RADIO_QUERY_NO_RESULT"))
                pick = random.choice(tracks)
                yt_query = f"{pick.get('title', '')} {pick.get('uploader', '')}".strip()
                if not yt_query:
                    raise ValueError(t(interaction, "RADIO_NO_SEED"))
                ytm_url = f"https://music.youtube.com/search?q={quote_plus(yt_query)}&sp=EgWKAQIIAWoKEAkQBRAKEAMQBA%3D%3D"
                entries = await extract_entries(ytm_url, silent=True, playlistend=5)
                entries = [e for e in entries if re.fullmatch(r"[0-9A-Za-z_-]{11}", e.get("id") or "")]
                if not entries:
                    raise ValueError(t(interaction, "RADIO_QUERY_NO_RESULT"))
                entry = entries[0]
            elif looks_like_url(query_value):
                if not is_youtube_url(query_value):
                    raise ValueError(t(interaction, "RADIO_QUERY_UNSUPPORTED_LINK"))
                entries = await extract_entries(query_value, silent=True)
                if not entries:
                    raise ValueError(t(interaction, "RADIO_QUERY_NO_RESULT"))
                entry = random.choice(entries)
            else:
                # Plain text query, search YouTube Music songs only
                ytm_url = f"https://music.youtube.com/search?q={quote_plus(query_value)}&sp=EgWKAQIIAWoKEAkQBRAKEAMQBA%3D%3D"
                entries = await extract_entries(ytm_url, silent=True, playlistend=5)
                # Filter to entries with a valid 11-char video ID
                entries = [e for e in entries if re.fullmatch(r"[0-9A-Za-z_-]{11}", e.get("id") or "")]
                if not entries:
                    raise ValueError(t(interaction, "RADIO_QUERY_NO_RESULT"))
                entry = entries[0]

            vid = entry.get("id") or ""
            if not vid:
                url = entry.get("url") or entry.get("webpage_url") or ""
                vid_match = re.search(r"(?:v=|youtu\.be/)([0-9A-Za-z_-]{11})", url)
                vid = vid_match.group(1) if vid_match else ""
            if not vid:
                raise ValueError(t(interaction, "RADIO_NO_SEED"))
            video_url = f"https://www.youtube.com/watch?v={vid}"
            try:
                from core.media import _run_ydl_info, _ydl_options
                full = await _run_ydl_info(
                    video_url,
                    _ydl_options({"quiet": True, "extract_flat": False, "noplaylist": True, "format": "ba/b"}, silent=True),
                )
                return {
                    "id": vid, "title": full.get("title") or entry.get("title", ""),
                    "artist": full.get("artist") or "",
                    "uploader": full.get("uploader") or "",
                    "duration": full.get("duration"),
                    "webpage_url": video_url, "url": video_url,
                }
            except Exception:
                return {"id": vid, "title": entry.get("title", ""), "url": video_url,
                        "webpage_url": video_url, "artist": "", "uploader": entry.get("uploader") or ""}

        raise ValueError(t(interaction, "RADIO_NO_SEED"))

    async def _start_radio_session(self, interaction, seed_track: dict, source_type: str, track_limit: int, time_limit: int = 0, *, query: str | None = None):
        guild_id = interaction.guild_id
        user_id = interaction.user.id

        session = RadioSession(
            guild_id=guild_id,
            starter_id=user_id,
            source_type=source_type,
            seed_track=seed_track,
        )
        session.track_limit = track_limit
        session.timeout_minutes = time_limit
        session.source_query = query
        session.used_seeds.add(seed_track["id"])

        _da = self._resolve_delete_after(guild_id)
        _color = self.get_embed_color(guild_id)

        async def _edit_status(desc: str, *, delete_after: int | None = None):
            embed = SafeEmbed(title=t(interaction, "RADIO_CONFIG_TITLE"), description=desc, color=_color)
            try:
                await interaction.edit_original_response(embed=embed, view=None)
                if delete_after and delete_after > 0:
                    await asyncio.sleep(delete_after)
                    try:
                        await interaction.delete_original_response()
                    except Exception:
                        pass
            except Exception:
                pass

        # --- Connect to voice ---
        vc = interaction.guild.voice_client
        user_voice = interaction.user.voice
        if not vc and user_voice and user_voice.channel:
            if not self.check_join_restriction(guild_id, user_voice.channel.id, interaction.user):
                return await _edit_status(t(interaction, "JOIN_RESTRICTED_CHANNEL"), delete_after=_da)
            try:
                vc = await user_voice.channel.connect()
            except Exception:
                return await _edit_status(t(interaction, "VOICE_CONNECT_FAILED"), delete_after=_da)

        # --- Cancel any in-flight playlist fetch from /play ---
        self._cancel_bg_fetch(guild_id)

        # --- Clear state and queue seed track ---
        state = self.get_state(guild_id)
        state.cancel_tasks()
        if vc and (vc.is_playing() or vc.is_paused()):
            state.suppress_after_callback = True
            vc.stop()
        state.queue.clear()
        state.now = None
        state.playing = False
        state.playing_since = None
        state.loop_mode = "off"
        state.vc = vc
        state.text_channel = interaction.channel

        seed_entry = dict(seed_track)
        seed_entry["requester"] = self.bot.user.id
        state.queue.append(seed_entry)

        self.radio_sessions[guild_id] = session

        # --- Start playing the seed track immediately ---
        state.playing = True
        await self.playback.play_next(guild_id)
        self._schedule_refresh(guild_id)

        # Timer starts after playback begins, not during setup
        session.started_at = time.time()
        if session.timeout_minutes > 0:
            self._schedule_radio_timeout(guild_id)

        # --- Fetch radio pool in background ---
        async def _initial_fetch():
            async with session._fetch_lock:
                if not session.active:
                    return 0
                self._schedule_refresh(guild_id)
                pool = await fetch_radio_pool(seed_track["id"])
                if not pool or not session.active:
                    return 0
                # Remove the seed track itself (already queued) and tracks longer than 60 min
                seed_id = seed_track.get("id")
                pool = [tr for tr in pool if tr.get("id") != seed_id and (tr.get("duration") or 0) <= 3600]
                # Shuffle to spread artists evenly
                random.shuffle(pool)
                # Store shuffled pool for future refill seed picking
                session.original_pool = list(pool)
                # Apply track/time limits
                to_add = []
                total_dur = seed_track.get("duration") or 0
                for tr in pool:
                    if track_limit > 0 and (len(to_add) + 1) >= track_limit:
                        break
                    if time_limit > 0 and total_dur >= time_limit * 60:
                        break
                    tr["requester"] = self.bot.user.id
                    to_add.append(tr)
                    total_dur += tr.get("duration") or 0
                st = self.guild_states.get(guild_id)
                if st and to_add:
                    st.queue.extend(to_add)
                self._schedule_refresh(guild_id)
                return len(to_add)

        session._initial_fetch = self._create_task(_initial_fetch(), name=f"radio-init-{guild_id}")

        # --- Send confirmation after fetch completes ---
        async def _send_started():
            count = 0
            try:
                count = await session._initial_fetch
            except Exception:
                pass
            if count:
                started_text = t(interaction, "RADIO_STARTED",
                                 seed=seed_track.get("title", "?"), count=count)
            else:
                started_text = t(interaction, "RADIO_FETCH_FAILED")
                if self.radio_sessions.get(guild_id) is session:
                    await self._end_radio(guild_id, "fetch_failed", skip_cooldown=True)
            await _edit_status(started_text, delete_after=_da)

        self._create_task(_send_started(), name=f"radio-started-{guild_id}")
        self._schedule_refresh(guild_id)

    # endregion

    # region Manage

    @app_commands.command(**l_cmd("CMD_NAME_MANAGE", "CMD_DESC_MANAGE"))
    async def manage_cmd(self, interaction: discord.Interaction):
        if not await self.bot.is_owner(interaction.user):
            return await self.send_reply(
                interaction, t(interaction, "NOT_APP_OWNER"), ephemeral=True,
                delete_after=self._resolve_delete_after(interaction.guild_id) if interaction.guild_id else None)
        await interaction.response.defer(ephemeral=True)
        embed = self._build_manage_status_embed(interaction)
        view = self._make_manage_status_view(interaction)
        await self.send_reply(interaction, embed=embed, view=view, ephemeral=True, delete_after=None)

    async def _run_manage_action(self, interaction: discord.Interaction, action: str) -> str:
        """Execute a manage action and return the result message."""
        ctx = interaction
        gid = interaction.guild_id

        if action == "clear_votes":
            had_votes = any(
                self.playback.is_vote_key_for_guild(k, gid)
                for store in self.command_votes.values() if isinstance(store, dict)
                for k in store
            )
            if not had_votes:
                return t(ctx, "MNG_NO_VOTES")
            self.playback.cleanup_guild_votes(gid)
            self._schedule_refresh(gid)
            return t(ctx, "MNG_VOTES_CLEARED")

        if action == "garbage_collect":
            counts = gc.collect(0), gc.collect(1), gc.collect(2)
            return t(ctx, "MNG_GC_DONE", gen0=counts[0], gen1=counts[1], gen2=counts[2])

        if action == "ping":
            bot_lat = round(self.bot.latency * 1000)
            vc = interaction.guild.voice_client if interaction.guild else None
            if vc and hasattr(vc, "latency") and vc.latency is not None:
                voice_lat = f"{round(vc.latency * 1000)}ms"
            else:
                voice_lat = t(ctx, "MNG_PING_NO_VOICE")
            return t(ctx, "MNG_PING", bot=bot_lat, voice=voice_lat)

        if action == "reset_voice_state":
            state = self.guild_states.pop(gid, None)
            if state:
                state.suppress_after_callback = True
                state.cancel_tasks()
            self._cleanup_views(gid)
            self._intentional_disconnect.add(gid)
            vc = interaction.guild.voice_client if interaction.guild else None
            if vc:
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
            self.playback.cleanup_guild_votes(gid)
            self.playback._play_locks.pop(gid, None)
            stale_pl_keys = [k for k in self.active_playlists if k[0] == gid]
            for k in stale_pl_keys:
                self.active_playlists.pop(k, None)
            self._command_locks.pop(gid, None)
            self._schedule_refresh(gid)
            return t(ctx, "MNG_RESET_DONE")

        if action == "reload_settings":
            for name, loader, target in self._settings_loaders():
                try:
                    fresh = await loader()
                    target.clear()
                    target.update(fresh)
                except Exception:
                    pass
            self._update_discord_loggers()
            await init_locales_cache(force=True)
            for g, state in self.guild_states.items():
                state.delete_after = self.guild_delete_after.get(g, 10)
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
            except Exception:
                pass
            return t(ctx, "MNG_SETTINGS_RELOADED")

        if action == "reload_locales":
            load_locales()
            refresh_supported_locales()
            count = len(SUPPORTED_LOCALES)
            return t(ctx, "MNG_LOCALES_RELOADED", count=count)

        if action == "cancel_fetches":
            cancelled = 0
            affected_guilds = set()
            for g, task in list(self._bg_fetch_tasks.items()):
                if not task.done():
                    task.cancel()
                    cancelled += 1
                    affected_guilds.add(g)
            self._bg_fetch_tasks.clear()
            for g, task in list(self._bg_fetch_forced_tasks.items()):
                if not task.done():
                    task.cancel()
                    cancelled += 1
                    affected_guilds.add(g)
            self._bg_fetch_forced_tasks.clear()
            for g in affected_guilds:
                self._command_locks.pop(g, None)
            if cancelled:
                return t(ctx, "MNG_FETCHES_CANCELLED", count=cancelled)
            return t(ctx, "MNG_NO_FETCHES")

        if action == "purge_stale":
            count = await self._purge_stale_guilds()
            if count:
                return t(ctx, "MNG_PURGE_DONE", count=count)
            return t(ctx, "MNG_NO_STALE")

        if action == "force_resync":
            import os
            hash_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "resources", ".tree_hash")
            try:
                os.remove(hash_file)
            except FileNotFoundError:
                pass
            return t(ctx, "MNG_RESYNC_DONE")

        return ""

    @app_commands.command(**l_cmd("CMD_NAME_TIMEOUT", "CMD_DESC_TIMEOUT"))
    @app_commands.describe(
        user=l_opt("OPT_TIMEOUT_USER"),
        minutes=l_opt("OPT_TIMEOUT_MINUTES"),
    )
    async def timeout_cmd(self, interaction: discord.Interaction, user: discord.Member, minutes: app_commands.Range[int, 1, 10080]):
        if not await self._check_cooldown(interaction, "timeout", 5): return
        if not self.has_admin_privilege(interaction.guild_id, interaction.user):
            return await self.send_reply(
                interaction, t(interaction, "NOT_MANAGE_ADMIN"), ephemeral=True,
                delete_after=self._resolve_delete_after(interaction.guild_id))
        if user.bot:
            return await self.send_reply(
                interaction, t(interaction, "TIMEOUT_NO_BOT"), ephemeral=True,
                delete_after=self._resolve_delete_after(interaction.guild_id))
        if user.id == interaction.user.id:
            return await self.send_reply(
                interaction, t(interaction, "TIMEOUT_NO_SELF"), ephemeral=True,
                delete_after=self._resolve_delete_after(interaction.guild_id))
        if self.has_admin_privilege(interaction.guild_id, user):
            return await self.send_reply(
                interaction, t(interaction, "TIMEOUT_NO_ADMIN"), ephemeral=True,
                delete_after=self._resolve_delete_after(interaction.guild_id))

        await interaction.response.defer(ephemeral=True)
        expires_at = time.time() + minutes * 60
        self._timeouts[(interaction.guild_id, user.id)] = expires_at
        await db.set_timeout(interaction.guild_id, user.id, expires_at)
        await self.send_reply(
            interaction,
            t(interaction, "TIMEOUT_SET", user=user.mention, minutes=minutes),
            ephemeral=True,
        )

    def _build_manage_status_embed(self, ctx) -> discord.Embed:
        guild_count = len(self.bot.guilds)
        playing = sum(1 for s in self.guild_states.values() if s.playing)
        voice_clients = sum(1 for g in self.bot.guilds if g.voice_client and g.voice_client.is_connected())
        mp_views = len(self.active_mp)
        queue_views = len(self.active_queues)
        pl_views = len(self.active_playlists)
        searches = len(self.active_searches)
        votes = sum(len(v) for v in self.command_votes.values() if isinstance(v, dict))
        bg_tasks = sum(1 for t_obj in self._bg_fetch_tasks.values() if not t_obj.done()) + sum(1 for t_obj in self._bg_fetch_forced_tasks.values() if not t_obj.done())
        elapsed = time.monotonic() - self._start_time
        days, rem = divmod(int(elapsed), 86400)
        hours, rem = divmod(rem, 3600)
        mins, secs = divmod(rem, 60)
        _du = t(ctx, 'ABBR_DAYS')
        _hu = t(ctx, 'ABBR_HOURS')
        _mu = t(ctx, 'ABBR_MINUTES')
        _su = t(ctx, 'ABBR_SECONDS')
        parts = []
        if days:
            parts.append(f"{days}{_du}")
        if hours:
            parts.append(f"{hours}{_hu}")
        if mins:
            parts.append(f"{mins}{_mu}")
        parts.append(f"{secs}{_su}")
        uptime_str = " ".join(parts)
        guild_id = ctx.guild_id if hasattr(ctx, "guild_id") else (ctx.guild.id if hasattr(ctx, "guild") and ctx.guild else None)
        embed = SafeEmbed(title=t(ctx, "MNG_STATUS_TITLE"), color=self.get_embed_color(guild_id))
        embed.add_field(name=t(ctx, "MNG_STATUS_GUILDS"), value=str(guild_count), inline=True)
        embed.add_field(name=t(ctx, "MNG_STATUS_PLAYING"), value=str(playing), inline=True)
        embed.add_field(name=t(ctx, "MNG_STATUS_VOICE"), value=str(voice_clients), inline=True)
        embed.add_field(name=t(ctx, "MNG_STATUS_MP_VIEWS"), value=str(mp_views), inline=True)
        embed.add_field(name=t(ctx, "MNG_STATUS_QUEUE_VIEWS"), value=str(queue_views), inline=True)
        embed.add_field(name=t(ctx, "MNG_STATUS_PL_VIEWS"), value=str(pl_views), inline=True)
        embed.add_field(name=t(ctx, "MNG_STATUS_SEARCHES"), value=str(searches), inline=True)
        embed.add_field(name=t(ctx, "MNG_STATUS_VOTES"), value=str(votes), inline=True)
        embed.add_field(name=t(ctx, "MNG_STATUS_BG_TASKS"), value=str(bg_tasks), inline=True)
        embed.add_field(name=t(ctx, "MNG_STATUS_UPTIME"), value=uptime_str, inline=True)
        na = "N/A"
        if _PSUTIL_PROCESS:
            try:
                mem = _PSUTIL_PROCESS.memory_info().rss
                for child in _PSUTIL_PROCESS.children(recursive=True):
                    try:
                        mem += child.memory_info().rss
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass
                if mem >= 1073741824:
                    ram_str = f"{mem / 1073741824:.1f} GB"
                else:
                    ram_str = f"{mem / 1048576:.1f} MB"
            except Exception:
                ram_str = na
            try:
                cpu_str = f"{_PSUTIL_PROCESS.cpu_percent(interval=None):.1f}%"
            except Exception:
                cpu_str = na
        else:
            ram_str = na
            cpu_str = na
        embed.add_field(name=t(ctx, "MNG_STATUS_RAM"), value=ram_str, inline=True)
        embed.add_field(name=t(ctx, "MNG_STATUS_CPU"), value=cpu_str, inline=True)
        return embed

    _MNG_ACTIONS = [
        ("clear_votes",     "OPT_MNG_CLEAR_VOTES",  True),
        ("garbage_collect",  "OPT_MNG_GC",           False),
        ("ping",            "OPT_MNG_PING",          False),
        ("reset_voice_state","OPT_MNG_RESET_VOICE",  True),
        ("reload_settings", "OPT_MNG_RELOAD_SETTINGS", False),
        ("reload_locales",  "OPT_MNG_RELOAD_LOCALES", False),
        ("cancel_fetches",  "OPT_MNG_CANCEL_FETCHES", False),
        ("purge_stale",     "OPT_MNG_PURGE_STALE",  False),
        ("force_resync",    "OPT_MNG_FORCE_RESYNC",  False),
    ]

    def _make_manage_status_view(self, ctx):
        cog = self

        class ManageActionSelect(discord.ui.Select):
            def __init__(self):
                in_guild = ctx.guild is not None
                options = [
                    discord.SelectOption(label=t(ctx, desc_key), value=action_id)
                    for action_id, desc_key, guild_only in cog._MNG_ACTIONS
                    if in_guild or not guild_only
                ]
                super().__init__(placeholder=t(ctx, "MNG_SELECT_PLACEHOLDER"), options=options, row=0)

            async def callback(self, sel_interaction: discord.Interaction):
                if not await cog.bot.is_owner(sel_interaction.user):
                    return
                action = self.values[0]
                result = await cog._run_manage_action(sel_interaction, action)
                embed = cog._build_manage_status_embed(sel_interaction)
                await sel_interaction.response.edit_message(content=result, embed=embed, view=self.view)

        class ManageRefreshButton(discord.ui.Button):
            def __init__(self):
                super().__init__(style=discord.ButtonStyle.success, emoji=_BUTTON_EMOJIS["BUTTON_REFRESH"], label=t(ctx, "BUTTON_REFRESH"), row=1)

            async def callback(self, btn_interaction: discord.Interaction):
                try:
                    embed = cog._build_manage_status_embed(btn_interaction)
                    await btn_interaction.response.edit_message(content=None, embed=embed)
                except Exception:
                    pass

        class ManageStatusView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=1800)
                self.add_item(ManageActionSelect())
                self.add_item(ManageRefreshButton())

        return ManageStatusView()

    async def _purge_stale_guilds(self) -> int:
        bot_guild_ids = {g.id for g in self.bot.guilds}
        db_guild_ids = await db.get_all_guild_ids()
        stale = db_guild_ids - bot_guild_ids
        for gid in stale:
            await db.delete_guild_data(gid)
        # Cancel background tasks for stale guilds before dropping refs
        for task_cache in (self._bg_fetch_tasks, self._bg_fetch_forced_tasks, self._pending_refresh):
            for gid in stale:
                task = task_cache.pop(gid, None)
                if task and not task.done():
                    task.cancel()
        # Clean in-memory caches (guild-id keyed)
        for cache in (self.guild_states, self.dj_roles,
                      self.vote_modes, self.guild_delete_after, self.guild_embed_colors,
                      self.guild_compact_modes, self.guild_max_playlists, self.guild_max_history,
                      self.guild_max_user_tracks,
                      self.active_mp, self.active_queues,
                      self.radio_sessions,
                      self.guild_queue_per_page, self.guild_queue_compact,
                      self.guild_view_channels, self.guild_view_restricts,
                      self.dj_users, self.excluded_users, self.excluded_roles,
                      self.admin_users, self.admin_roles, self.guild_admin_priv,
                      self.guild_queue_button_compact, self.guild_track_limit_target,
                      self.guild_pause_permission, self.guild_pause_timeout,
                      self.guild_pause_timeout_behavior, self.guild_timezones,
                      self.guild_queue_limit, self.guild_playlist_track_limit,
                      self.guild_seek_permission, self.guild_max_seeks_per_track,
                      self.guild_max_seeks_dj, self.guild_idle_disconnect,
                      self.guild_join_restrict_level, self.guild_join_restrict_channels,
                      self.guild_radio_permissions, self.guild_radio_edit_permissions, self.guild_radio_cooldowns,
                      self.guild_force_play_permission, self.guild_force_radio,
                      self.guild_track_limit_users, self.guild_track_limit_dj,
                      self.guild_track_limit_admin,
                      self.guild_embed_layouts,
                      self.guild_vote_exclude_deafened,
                      self.guild_live_enabled, self.guild_live_permission,
                      self.guild_live_max_hours,
                      self._command_locks, self._last_refresh,
                      self._refresh_backoff, self._refresh_again):
            for gid in stale:
                cache.pop(gid, None)
        self._refresh_active -= stale
        self._radio_initializing -= stale
        self._owner_override_views = {k for k in self._owner_override_views if k[0] not in stale}
        self._intentional_disconnect -= stale
        # Clean tuple-keyed caches ((guild_id, user_id) keyed)
        # Stop views before removing to cancel timeout tasks
        for cache in (self.active_playlists, self.active_settings,
                      self.active_searches, self.active_helps, self.active_history):
            stale_keys = [k for k in cache if k[0] in stale]
            for k in stale_keys:
                entry = cache.pop(k, None)
                if entry:
                    view = entry[1] if isinstance(entry, tuple) else entry
                    if hasattr(view, "stop"):
                        view.stop()
        for cache in (self._timeouts, self._radio_config_interactions,
                      self._radio_cooldowns, self._active_copy_pickers):
            stale_keys = [k for k in cache if k[0] in stale]
            for k in stale_keys:
                cache.pop(k, None)
        self.playlist_busy = {k for k in self.playlist_busy if k[0] not in stale}
        for gid in stale:
            guild_locales.pop(gid, None)
            self.playback.cleanup_guild_votes(gid)
            self.playback._cancel_live_timer(gid)
            self.playback._play_locks.pop(gid, None)
        return len(stale)

    # endregion

    # region Auto tasks

    @tasks.loop(hours=6)
    async def _wal_checkpoint_task(self):
        try:
            await db.wal_checkpoint()
        except Exception as e:
            print(f"[WAL checkpoint] {type(e).__name__}: {e}")

    async def cog_load(self):
        await super().cog_load()
        self._wal_checkpoint_task.start()

    async def cog_unload(self):
        self._wal_checkpoint_task.cancel()
        if self._activity_cycle_task and not self._activity_cycle_task.done():
            self._activity_cycle_task.cancel()
        for session in self.radio_sessions.values():
            session.active = False
            if session._timeout_task and not session._timeout_task.done():
                session._timeout_task.cancel()
            if session._initial_fetch and not session._initial_fetch.done():
                session._initial_fetch.cancel()
        self.radio_sessions.clear()
        for state in self.guild_states.values():
            state.cancel_tasks()
        for guild_id in list(self._bg_fetch_tasks) + list(self._bg_fetch_forced_tasks):
            self._cancel_bg_fetch(guild_id)
        for guild_id in list(self.playback._live_timers):
            self.playback._cancel_live_timer(guild_id)
        for task in self._pending_refresh.values():
            if not task.done():
                task.cancel()
        self._pending_refresh.clear()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.id != self.bot.user.id:
            # Non-bot user left or switched from the bot's VC, prune their votes
            if before.channel and before.channel != getattr(after, "channel", None):
                vc = member.guild.voice_client
                if vc and vc.channel and vc.channel == before.channel:
                    self._prune_user_votes(member.guild.id, member.id)
            return
        # Bot was moved to a different channel, clear all votes (members changed)
        if before.channel and after.channel and before.channel != after.channel:
            guild_id = member.guild.id
            for cmd_name, store in self.command_votes.items():
                if not isinstance(store, dict):
                    continue
                for vote_key in list(store):
                    if PlaybackManager.is_vote_key_for_guild(vote_key, guild_id):
                        store.pop(vote_key, None)
                user_map = self._vote_user_keys.get(cmd_name)
                if user_map:
                    user_map.pop(guild_id, None)
        if before.channel and not after.channel:
            guild_id = member.guild.id

            if guild_id in self._intentional_disconnect:
                self._intentional_disconnect.discard(guild_id)
            else:
                # Force disconnect, attempt reconnect
                state = self.guild_states.get(guild_id)
                session = self.radio_sessions.get(guild_id)
                if state and (state.playing or state.now or state.queue or session):
                    # Suppress stale after-callback from the dying AudioPlayer
                    state.suppress_after_callback = True

                    # discord.py's internal cleanup (VoiceConnectionState.disconnect +
                    # voice_client.cleanup) runs as a concurrent task, wait for it
                    await asyncio.sleep(2)

                    # If state was torn down by another code path during the wait, bail
                    if self.guild_states.get(guild_id) is not state:
                        return

                    # If something else already reconnected (e.g. user ran /join), bail
                    if member.guild.voice_client and member.guild.voice_client.is_connected():
                        state.suppress_after_callback = False
                        return

                    # Force-remove zombie voice client if cleanup hasn't finished
                    zombie = member.guild.voice_client
                    if zombie:
                        try:
                            await zombie.disconnect(force=True)
                        except Exception:
                            try:
                                zombie.cleanup()
                            except Exception:
                                pass

                    try:
                        vc = await before.channel.connect()
                        state.vc = vc
                        state.cancel_tasks()
                        state.playing = True
                        state.suppress_after_callback = False
                        _log.info("Reconnected to voice in guild %s after force disconnect", guild_id)
                        # Resume from elapsed position if we have a current track
                        if state.now:
                            elapsed = 0.0
                            if state.playing_since:
                                elapsed = time.time() - state.playing_since - state._total_paused
                            if elapsed > 1:
                                state.now["seek_time"] = elapsed
                            state.now.pop("_prepared_source", None)
                            state.queue.insert(0, state.now)
                            state.now = None
                        await self.playback.play_next(guild_id)
                        return
                    except Exception as e:
                        _log.warning("Reconnect failed for guild %s: %s", guild_id, e)

            vc = member.guild.voice_client
            if vc:
                try:
                    await vc.disconnect(force=True)
                except Exception:
                    pass
            self._cancel_bg_fetch(guild_id)
            session = self.radio_sessions.pop(guild_id, None)
            if session:
                _deactivate_radio_session(session)
            state = self.guild_states.pop(guild_id, None)
            if state:
                state.suppress_after_callback = True
                state.cancel_tasks()
            self._cleanup_views(guild_id)
            self.playback.cleanup_guild_votes(guild_id)
            self.playback._cancel_live_timer(guild_id)
            self.playback._play_locks.pop(guild_id, None)
            self._schedule_refresh(guild_id)

    @commands.Cog.listener()
    async def on_ready(self):
        await super().on_ready()
        purged = await self._purge_stale_guilds()
        if purged:
            print(f"[startup] Purged data for {purged} stale guild(s).")

    # endregion

    # region Help

    _HELP_COMMANDS = [
        ("CMD_NAME_PLAY", "CMD_DESC_PLAY", "HELP_CMD_PLAY"),
        (("CMD_NAME_PAUSE", "CMD_NAME_RESUME"), None, "HELP_CMD_PAUSE_RESUME"),
        ("CMD_NAME_STOP", "CMD_DESC_STOP", "HELP_CMD_STOP"),
        ("CMD_NAME_SKIP", "CMD_DESC_SKIP", "HELP_CMD_SKIP"),
        ("CMD_NAME_SEEK", "CMD_DESC_SEEK", "HELP_CMD_SEEK"),
        ("CMD_NAME_SELECT", "CMD_DESC_SELECT", "HELP_CMD_SELECT"),
        ("CMD_NAME_PREVIOUS", "CMD_DESC_PREVIOUS", "HELP_CMD_PREVIOUS"),
        ("CMD_NAME_LOOP", "CMD_DESC_LOOP", "HELP_CMD_LOOP"),
        ("CMD_NAME_NOWPLAYING", "CMD_DESC_NOW", "HELP_CMD_NOW"),
        ("CMD_NAME_QUEUE", "CMD_DESC_QUEUE", "HELP_CMD_QUEUE"),
        ("CMD_NAME_HISTORY", "CMD_DESC_HISTORY", "HELP_CMD_HISTORY"),
        ("CMD_NAME_SHUFFLE", "CMD_DESC_SHUFFLE", "HELP_CMD_SHUFFLE"),
        ("CMD_NAME_MOVE", "CMD_DESC_MOVE", "HELP_CMD_MOVE"),
        ("CMD_NAME_REMOVE", "CMD_DESC_REMOVE", "HELP_CMD_REMOVE"),
        ("CMD_NAME_CLEAR", "CMD_DESC_CLEAR", "HELP_CMD_CLEAR"),
        ("CMD_NAME_SEARCH", "CMD_DESC_SEARCH", "HELP_CMD_SEARCH"),
        ("CMD_NAME_JOIN", "CMD_DESC_JOIN", "HELP_CMD_JOIN"),
        ("CMD_NAME_LEAVE", "CMD_DESC_LEAVE", "HELP_CMD_LEAVE"),
        ("CMD_NAME_RADIO", "CMD_DESC_RADIO", "HELP_RADIO"),
    ]

    @app_commands.command(**l_cmd("CMD_NAME_HELP", "CMD_DESC_HELP"))
    async def help_cmd(self, interaction: discord.Interaction):
        if not await self._check_cooldown(interaction, "help", 5): return
        _help_key = (interaction.guild_id, interaction.user.id)
        old = self.active_helps.get(_help_key)
        if old:
            try:
                await old.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
            self.active_helps.pop(_help_key, None)

        cog = self
        ctx = interaction
        color = self.get_embed_color(interaction.guild_id) if interaction.guild else EMBED_COLOR
        footer = t(ctx, "HELP_FOOTER")

        # --- Category keys ---
        # (id, embed_label, embed_desc, opt_label, opt_desc)
        _CATS = [
            ("commands",   "HELP_CAT_COMMANDS",   "HELP_CAT_COMMANDS_DESC"),
            ("playlist",   "HELP_CAT_PLAYLIST",   "HELP_CAT_PLAYLIST_DESC"),
            ("dj_voting",  "HELP_CAT_DJ_VOTING",  "HELP_CAT_DJ_VOTING_DESC"),
            ("settings",   "HELP_CAT_ADMIN",      "HELP_CAT_ADMIN_DESC"),
            ("manage",     "HELP_CAT_MANAGE",     "HELP_CAT_MANAGE_DESC"),
        ]

        def _cmd_field_name(name_key, desc_key):
            if isinstance(name_key, tuple):
                cmd = " / ".join(f"/{t(ctx, nk).lower()}" for nk in name_key)
            else:
                cmd = f"/{t(ctx, name_key).lower()}"
            desc_part = f" - {t(ctx, desc_key)}" if desc_key else ""
            return f"`{cmd}`{desc_part}"

        # --- Embed builders ---
        def build_overview():
            embed = SafeEmbed(title=t(ctx, "HELP_TITLE"), description=t(ctx, "HELP_OVERVIEW_DESC"), color=color)
            # Commands summary
            cmd_names = []
            for nk, _, _ in cog._HELP_COMMANDS:
                if isinstance(nk, tuple):
                    for k in nk:
                        cmd_names.append(f"`/{t(ctx, k).lower()}`")
                else:
                    cmd_names.append(f"`/{t(ctx, nk).lower()}`")
            for _, cat_label, cat_desc in _CATS:
                value = t(ctx, cat_desc)
                if cat_label == "HELP_CAT_COMMANDS":
                    value += "\n" + " ".join(cmd_names)
                embed.add_field(name=t(ctx, cat_label), value=value, inline=False)
            embed.set_footer(text=footer)
            return embed

        def build_commands(page):
            per = 9
            pages = (len(cog._HELP_COMMANDS) + per - 1) // per
            page = max(0, min(page, pages - 1))
            embed = SafeEmbed(title=t(ctx, "HELP_CAT_COMMANDS"), description=t(ctx, "HELP_CAT_COMMANDS_DESC"), color=color)
            start = page * per
            _help_kwargs = {"opt_current": t(ctx, "OPTNAME_STOP_CURRENT")}
            for nk, dk, detail_key in cog._HELP_COMMANDS[start:start + per]:
                embed.add_field(name=_cmd_field_name(nk, dk), value=t(ctx, detail_key, **_help_kwargs), inline=False)
            if pages > 1:
                embed.set_footer(text=f"{footer}  •  {page + 1}/{pages}")
            else:
                embed.set_footer(text=footer)
            return embed, pages

        def build_playlist():
            embed = SafeEmbed(
                title=t(ctx, "HELP_CAT_PLAYLIST"),
                description=t(ctx, "HELP_CAT_PLAYLIST_DESC"),
                color=color,
            )
            embed.add_field(name=f"`/{t(ctx, 'CMD_NAME_PLAYLIST').lower()}`", value=t(ctx, "HELP_PL_OVERVIEW"), inline=False)
            embed.add_field(name=t(ctx, "HELP_CAT_PLAYLIST"), value=t(ctx, "HELP_PL_SLASH"), inline=False)
            _gid = ctx.guild_id if hasattr(ctx, 'guild_id') else 0
            max_pl = self.guild_max_playlists.get(_gid, 15)
            embed.add_field(name="\u200b", value=t(ctx, "HELP_PL_LIMITS", max_playlists=max_pl, max_tracks=self.get_playlist_track_limit(_gid)), inline=False)
            embed.set_footer(text=footer)
            return embed

        def build_dj_voting():
            _cs = t(ctx, "CMD_NAME_SETTINGS").lower()
            _cl = t(ctx, "CMD_NAME_LEAVE").lower()
            _cm = t(ctx, "CMD_NAME_MANAGE").lower()
            embed = SafeEmbed(
                title=t(ctx, "HELP_CAT_DJ_VOTING"),
                description=t(ctx, "HELP_CAT_DJ_VOTING_DESC"),
                color=color,
            )
            embed.add_field(name="\u200b", value=t(ctx, "HELP_DJ_WHAT", cmd_settings=_cs), inline=False)
            embed.add_field(name="\u200b", value=t(ctx, "HELP_DJ_CAN", cmd_leave=_cl), inline=False)
            embed.add_field(name="\u200b", value=t(ctx, "HELP_VOTE_HOW", cmd_settings=_cs), inline=False)
            embed.add_field(name="\u200b", value=t(ctx, "HELP_VOTE_RESET", cmd_manage=_cm), inline=False)
            embed.add_field(name="\u200b", value=t(ctx, "HELP_VOTE_OWNER"), inline=False)
            embed.set_footer(text=footer)
            return embed

        _SETTINGS_PAGES_COUNT = 2

        def build_settings(page):
            page = max(0, min(page, _SETTINGS_PAGES_COUNT - 1))
            embed = SafeEmbed(
                title=t(ctx, "HELP_CAT_ADMIN"),
                description=t(ctx, "HELP_CAT_ADMIN_DESC"),
                color=color,
            )
            if page == 0:
                embed.add_field(name=f"`/{t(ctx, 'CMD_NAME_SETTINGS').lower()}`", value=t(ctx, "HELP_SETTINGS_MAIN"), inline=False)
                embed.add_field(name="\u200b", value=t(ctx, "HELP_SETTINGS_EMBED_VIEWS"), inline=False)
                embed.add_field(name="\u200b", value=t(ctx, "HELP_SETTINGS_PERMS"), inline=False)
            else:
                _cmd_play = t(ctx, "CMD_NAME_PLAY").lower()
                _cmd_settings = t(ctx, "CMD_NAME_SETTINGS").lower()
                embed.add_field(name="\u200b", value=t(ctx, "HELP_SETTINGS_LIMITS"), inline=False)
                embed.add_field(name="\u200b", value=t(ctx, "HELP_SETTINGS_LIMITS2", cmd_play=_cmd_play), inline=False)
                embed.add_field(name="\u200b", value=t(ctx, "HELP_SETTINGS_APPOWNER", cmd_settings=_cmd_settings), inline=False)
            embed.set_footer(text=f"{footer}  \u2022  {page + 1}/{_SETTINGS_PAGES_COUNT}")
            return embed, _SETTINGS_PAGES_COUNT

        def build_manage():
            embed = SafeEmbed(
                title=t(ctx, "HELP_CAT_MANAGE"),
                description=t(ctx, "HELP_CAT_MANAGE_DESC"),
                color=color,
            )
            embed.add_field(name=f"`/{t(ctx, 'CMD_NAME_MANAGE').lower()}`", value=t(ctx, "HELP_MANAGE_CMD"), inline=False)
            embed.add_field(name=f"`/{t(ctx, 'CMD_NAME_TIMEOUT').lower()}`", value=t(ctx, "HELP_ADMIN_TIMEOUT"), inline=False)
            embed.set_footer(text=footer)
            return embed

        # --- View components ---
        class HelpCategorySelect(discord.ui.Select):
            def __init__(self, view_ref, *, selected=None):
                options = []
                for cat_id, cat_label, cat_desc in _CATS:
                    options.append(discord.SelectOption(
                        label=t(ctx, cat_label),
                        description=t(ctx, cat_desc),
                        value=cat_id,
                        default=cat_id == selected,
                    ))
                super().__init__(placeholder=t(ctx, "HELP_SELECT_PLACEHOLDER"), options=options, row=0)
                self.view_ref = view_ref

            async def callback(self, sel_interaction: discord.Interaction):
                v = self.view_ref
                cat_id = self.values[0]
                v._current_cat = cat_id
                v._cmd_page = 0
                v._rebuild_help()
                embed = v._build_current_embed()
                await sel_interaction.response.edit_message(embed=embed, view=v)

        class HelpHomeButton(discord.ui.Button):
            def __init__(self, view_ref):
                super().__init__(style=discord.ButtonStyle.secondary, emoji="🏠", label=t(ctx, "HELP_BACK_TO_MENU"), row=2)
                self.view_ref = view_ref

            async def callback(self, btn_interaction: discord.Interaction):
                v = self.view_ref
                v._current_cat = None
                v._cmd_page = 0
                v._rebuild_help()
                await btn_interaction.response.edit_message(embed=build_overview(), view=v)

        class HelpPageButton(discord.ui.Button):
            def __init__(self, view_ref, delta, emoji, label, *, disabled=False):
                super().__init__(style=discord.ButtonStyle.secondary, emoji=emoji, label=label, disabled=disabled, row=1)
                self.view_ref = view_ref
                self.delta = delta

            async def callback(self, btn_interaction: discord.Interaction):
                v = self.view_ref
                v._cmd_page += self.delta
                v._rebuild_help()
                embed = v._build_current_embed()
                await btn_interaction.response.edit_message(embed=embed, view=v)

        # --- View class ---
        class HelpView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=1800)
                self._current_cat = None
                self._cmd_page = 0
                self._cmd_pages = 1
                self._rebuild_help()

            async def interaction_check(hv_self, interaction_: discord.Interaction) -> bool:
                return await self.check_view_interaction(interaction_)

            def _build_current_embed(self):
                if self._current_cat == "commands":
                    embed, _ = build_commands(self._cmd_page)
                    return embed
                if self._current_cat == "playlist":
                    return build_playlist()
                if self._current_cat == "dj_voting":
                    return build_dj_voting()
                if self._current_cat == "settings":
                    embed, _ = build_settings(self._cmd_page)
                    return embed
                if self._current_cat == "manage":
                    return build_manage()
                return build_overview()

            def _rebuild_help(self):
                self.clear_items()
                cat = self._current_cat
                self.add_item(HelpCategorySelect(self, selected=cat))
                if cat is not None:
                    pages = 1
                    if cat == "commands":
                        _, pages = build_commands(self._cmd_page)
                    elif cat == "settings":
                        _, pages = build_settings(self._cmd_page)
                    self._cmd_pages = pages
                    if pages > 1:
                        self.add_item(HelpPageButton(self, -1, _BUTTON_EMOJIS["BUTTON_PREV"], t(ctx, "BUTTON_PREV"), disabled=self._cmd_page <= 0))
                        self.add_item(HelpPageButton(self, 1, _BUTTON_EMOJIS["BUTTON_NEXT"], t(ctx, "BUTTON_NEXT"), disabled=self._cmd_page >= pages - 1))
                    self.add_item(HelpHomeButton(self))

        view = HelpView()
        msg = await self.send_reply(interaction, embed=build_overview(), view=view, ephemeral=True, delete_after=None)
        if msg:
            self.active_helps[_help_key] = msg

    # endregion


async def setup(bot: commands.Bot):
    await bot.add_cog(MusicCog(bot))
