from __future__ import annotations

import asyncio
import logging

import time
import traceback
from typing import Callable

import discord
from discord.ext import commands

from locales.localization import t

_log = logging.getLogger(__name__)


def _task_done_cb(task: asyncio.Task):
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        _log.error("Unhandled exception in task %s", task.get_name(), exc_info=exc)


_ENTRY_STRIP_KEYS = frozenset((
    "__x_forwarded_for_ip", "_type",
    "channel_id", "channel_is_verified", "channel_url",
    "description", "live_status", "release_timestamp",
    "thumbnails", "timestamp", "uploader_id", "uploader_url",
))


def _apply_hydrated(entry: dict, resolved: dict):
    """Merge resolved yt-dlp data into entry, preserving requester and stripping heavy keys."""
    requester = entry.get("requester")
    entry.update(resolved)
    entry["requester"] = requester
    for k in _ENTRY_STRIP_KEYS:
        entry.pop(k, None)


from core.media import (
    extract_entries,
    _run_ydl_info,
    _ydl_options,
    StaleCookieError,
    JSRuntimeError,
    has_valid_cookies,
)

class GuildMusicState:
    def __init__(self, guild_id: int):
        self.guild_id = guild_id
        self.queue: list[dict] = []
        self.vc: discord.VoiceClient | None = None
        self.playing = False
        self.now: dict | None = None
        self.loop_mode = "off"  # "off", "song", "queue"
        self.text_channel: discord.TextChannel | None = None
        self.idle_disconnect_task: asyncio.Task | None = None
        self.pause_disconnect_task: asyncio.Task | None = None
        self.skip_current_song_once = False
        self.suppress_after_callback = False
        self.delete_after: int | None = 5
        self.age_restricted_skips: int = 0
        self._looped_ref: dict | None = None
        self.playing_since: float | None = None
        self.paused_at: float | None = None
        self._total_paused: float = 0.0
        self._seeking: bool = False
        self._prefetch_task: asyncio.Task | None = None
        self._mp_rev: int = 0
        self._queue_rev: int = 0

    @property
    def effective_delete_after(self) -> int | None:
        return self.delete_after if self.delete_after is not None and self.delete_after > 0 else None

    def cancel_tasks(self):
        """Cancels any pending disconnect, idle, or prefetch tasks for this guild."""
        if self.idle_disconnect_task and not self.idle_disconnect_task.done():
            self.idle_disconnect_task.cancel()
        self.idle_disconnect_task = None
        if self.pause_disconnect_task and not self.pause_disconnect_task.done():
            self.pause_disconnect_task.cancel()
        self.pause_disconnect_task = None
        if self._prefetch_task and not self._prefetch_task.done():
            self._prefetch_task.cancel()
        self._prefetch_task = None


class CommandCheckError(Exception):
    def __init__(self, message, *, ephemeral=True):
        self.message = message
        self.ephemeral = ephemeral
        super().__init__(message)


class PlaybackManager:
    def __init__(
        self,
        bot: commands.Bot,
        guild_states: dict[int, GuildMusicState],
        command_votes: dict,
        vote_user_keys: dict,
        silent_log_resolver: Callable[[int], bool] | None = None,
        prefetch_resolver: Callable[[int], bool] | None = None,
    ):
        self.bot = bot
        self.guild_states = guild_states
        self.command_votes = command_votes
        self._vote_user_keys = vote_user_keys
        self.is_silent_log = silent_log_resolver or (lambda guild_id: False)
        self.is_prefetch = prefetch_resolver or (lambda guild_id: True)
        self._play_locks: dict[int, asyncio.Lock] = {}
        self._live_timers: dict[int, asyncio.Task] = {}
        self.on_track_change: Callable[[int], None] | None = None
        self.on_disconnect: Callable[[int], None] | None = None
        self.on_track_played: Callable[[int, dict], None] | None = None
        self.on_radio_track_finished: Callable | None = None

    _SONG_VOTES = ("skip", "previous", "select", "remove", "move", "pause")

    def cleanup_guild_votes(self, guild_id: int, command_names: list[str] | None = None):
        """Removes votes for the given guild. If command_names is None, clears all."""
        targets = command_names or list(self.command_votes.keys())
        for key in targets:
            votes = self.command_votes.get(key)
            if votes is None:
                continue
            if isinstance(votes, dict):
                self.command_votes[key] = {
                    k: v for k, v in votes.items()
                    if not self.is_vote_key_for_guild(k, guild_id)
                }
            elif isinstance(votes, set):
                votes.discard(guild_id)
            user_map = self._vote_user_keys.get(key)
            if user_map:
                user_map.pop(guild_id, None)

    @staticmethod
    def is_vote_key_for_guild(vote_key, guild_id):
        if isinstance(vote_key, tuple):
            return vote_key and vote_key[0] == guild_id
        return vote_key == guild_id

    def _ensure_voice_client(self, guild_id: int, state: GuildMusicState | None) -> discord.VoiceClient | None:
        if state and state.vc:
            return state.vc
        guild = self.bot.get_guild(guild_id)
        return guild.voice_client if guild else None

    async def disconnect_and_cleanup(self, guild_id: int):
        async with self._guild_lock(guild_id):
            intentional = getattr(self, '_intentional_disconnect', None)
            if intentional is not None:
                intentional.add(guild_id)
            state = self.guild_states.pop(guild_id, None)
            if state:
                idle_task = state.idle_disconnect_task
                pause_task = state.pause_disconnect_task
                prefetch_task = state._prefetch_task
                state.idle_disconnect_task = None
                state.pause_disconnect_task = None
                state._prefetch_task = None
                if idle_task and not idle_task.done():
                    idle_task.cancel()
                if pause_task and not pause_task.done():
                    pause_task.cancel()
                if prefetch_task and not prefetch_task.done():
                    prefetch_task.cancel()
            self.cleanup_guild_votes(guild_id)
            self._cancel_live_timer(guild_id)
            if self.on_disconnect:
                self.on_disconnect(guild_id)
            vc = self._ensure_voice_client(guild_id, state)
            if vc:
                try:
                    await vc.disconnect(force=True)
                except Exception as e:
                    print(f"[disconnect_and_cleanup] Disconnect failed: {e}")
        self._play_locks.pop(guild_id, None)

    async def auto_disconnect_after(self, guild_id: int, delay: int):
        await asyncio.sleep(delay)
        state = self.guild_states.get(guild_id)
        if state and (state.now or state.playing):
            return
        await self.disconnect_and_cleanup(guild_id)

    async def handle_pause_timeout(self, guild_id: int, delay: int):
        """Handle pause timeout based on guild behavior setting (leave/continue/skip)."""
        await asyncio.sleep(delay)
        behavior = getattr(self, 'guild_pause_timeout_behavior', {}).get(guild_id, "leave")

        if behavior == "leave":
            async with self._guild_lock(guild_id):
                state = self.guild_states.get(guild_id)
                text_channel = state.text_channel if state else None
                delete_after = state.effective_delete_after if state else None
            await self.disconnect_and_cleanup(guild_id)
            if text_channel:
                try:
                    await text_channel.send(
                        t(text_channel, "PAUSE_TIMEOUT_LEFT"),
                        delete_after=delete_after)
                except discord.HTTPException:
                    pass
            return

        msg_key = None
        text_channel = None
        delete_after = None
        async with self._guild_lock(guild_id):
            state = self.guild_states.get(guild_id)
            vc = state.vc if state else None
            if not vc or not vc.is_connected():
                return
            text_channel = state.text_channel if state else None
            delete_after = state.effective_delete_after if state else None
            if behavior == "continue":
                if vc.is_paused():
                    vc.resume()
                    if state:
                        state.playing = True
                        if state.paused_at is not None:
                            state._total_paused += time.time() - state.paused_at
                            state.paused_at = None
                    if self.on_track_change:
                        self.on_track_change(guild_id)
                    msg_key = "PAUSE_TIMEOUT_RESUMED"
            elif behavior == "skip":
                if vc.is_paused() or vc.is_playing():
                    if state:
                        state.suppress_after_callback = False
                    vc.stop()
                    msg_key = "PAUSE_TIMEOUT_SKIPPED"
        if msg_key and text_channel:
            try:
                await text_channel.send(
                    t(text_channel, msg_key),
                    delete_after=delete_after)
            except discord.HTTPException:
                pass

    async def _live_timeout(self, guild_id: int, seconds: int):
        """Auto-skip a live track after the configured max duration."""
        await asyncio.sleep(seconds)
        self._live_timers.pop(guild_id, None)
        state = self.guild_states.get(guild_id)
        if not state or not state.playing or not state.now:
            return
        if not state.now.get("is_live"):
            return
        text_channel = state.text_channel
        vc = self._ensure_voice_client(guild_id, state)
        if vc and (vc.is_playing() or vc.is_paused()):
            state.suppress_after_callback = False
            vc.stop()
            if text_channel:
                try:
                    await text_channel.send(
                        t(text_channel, "LIVE_TIME_LIMIT_REACHED"),
                        delete_after=state.effective_delete_after)
                except discord.HTTPException:
                    pass

    def _cancel_live_timer(self, guild_id: int):
        lt = self._live_timers.pop(guild_id, None)
        if lt and not lt.done():
            lt.cancel()

    def _mark_idle(self, state: GuildMusicState, guild_id: int):
        state.now = None
        state.playing = False
        state.playing_since = None
        state.paused_at = None
        state._total_paused = 0.0
        self.cleanup_guild_votes(guild_id)
        self._cancel_live_timer(guild_id)
        state.cancel_tasks()
        timeout = getattr(self, 'guild_idle_disconnect', {}).get(guild_id, 180)
        if timeout > 0:
            task = asyncio.create_task(self.auto_disconnect_after(guild_id, timeout), name=f"idle-dc-{guild_id}")
            task.add_done_callback(_task_done_cb)
            state.idle_disconnect_task = task

    async def _report_skipped(self, state: GuildMusicState, text_channel) -> bool:
        """Reports age-restricted skips accumulated during playlist playback. Returns True if any were reported."""
        if not state.age_restricted_skips or not text_channel:
            state.age_restricted_skips = 0
            return False

        msg = t(text_channel, "SKIPPED_AGE_RESTRICTED", count=state.age_restricted_skips)
        key = "YTDLP_COOKIE_STALE" if has_valid_cookies() else "YTDLP_AGE_RESTRICTED"
        msg += "\n\n" + t(text_channel, key)
        state.age_restricted_skips = 0
        try:
            await text_channel.send(msg, delete_after=state.effective_delete_after)
        except Exception:
            pass
        return True

    def _select_next_entry(self, state: GuildMusicState, guild_id: int, *, failed: bool = False) -> dict | None:
        loop_mode = state.loop_mode
        if loop_mode == "song" and not state.skip_current_song_once and state.now:
            if not failed:
                return state.now

        if loop_mode == "song":
            state.skip_current_song_once = False

        if not state.queue:
            self._mark_idle(state, guild_id)
            return None

        next_entry = state.queue.pop(0)
        state._looped_ref = None
        if loop_mode == "queue":
            looped = next_entry.copy()
            looped.pop("_prepared_source", None)
            looped.pop("formats", None)
            looped.pop("seek_time", None)
            looped.pop("suppress_announce", None)
            looped.pop("_seek_counts", None)
            state.queue.append(looped)
            state._looped_ref = looped
        return next_entry

    async def _hydrate_spotify_entry(self, entry: dict, guild_id: int):
        if (entry.get("_prepared_source") or entry.get("formats")) and entry.get("url"):
            return
        search_term = f"{entry.get('title', '')} {entry.get('uploader', '')}".strip()
        sp_dur = entry.get("spotify_duration")
        count = 5 if sp_dur else 1
        yt_results = await extract_entries(f"ytsearch{count}:{search_term}", silent=self.is_silent_log(guild_id))
        if not yt_results:
            raise ValueError(t(None, "YOUTUBE_NO_MATCH"))
        if sp_dur and len(yt_results) > 1:
            sp_title = entry.get("title", "").lower()
            sp_artists = [a.lower() for a in entry.get("spotify_artists", [])]

            def _artist_match(r):
                return sp_artists and any(a in (r.get("uploader") or r.get("channel") or "").lower() for a in sp_artists)

            def _title_match(r):
                return sp_title and sp_title in (r.get("title") or "").lower()

            def _best_in(pool):
                return min(pool, key=lambda r: abs((r.get("duration") or 0) - sp_dur))

            confident = [r for r in yt_results if _title_match(r) and _artist_match(r)]
            if confident:
                best = _best_in(confident)
                if abs((best.get("duration") or 0) - sp_dur) <= 5:
                    yt_entry = best
                else:
                    yt_entry = yt_results[0]
            else:
                artist_pool = [r for r in yt_results if _artist_match(r)]
                if artist_pool:
                    best = _best_in(artist_pool)
                    yt_entry = best if abs((best.get("duration") or 0) - sp_dur) <= 5 else yt_results[0]
                else:
                    yt_entry = yt_results[0]
        else:
            yt_entry = yt_results[0]
        _apply_hydrated(entry, yt_entry)

    async def _hydrate_url_entry(self, entry: dict, guild_id: int):
        url = entry.get("webpage_url") or entry.get("url")
        results = await extract_entries(url, silent=self.is_silent_log(guild_id))
        if not results:
            raise ValueError(t(None, "YOUTUBE_NO_MATCH"))
        _apply_hydrated(entry, results[0])

    async def _hydrate_entry(self, entry: dict, guild_id: int):
        """Resolve a spotify or lazy-url entry to a full yt-dlp entry."""
        source = entry.get("source")
        if source == "spotify":
            await self._hydrate_spotify_entry(entry, guild_id)
        elif source == "url":
            await self._hydrate_url_entry(entry, guild_id)

    _STREAM_KEYS = ("title", "uploader", "channel", "duration", "thumbnail", "view_count", "webpage_url", "id", "is_live")

    async def _ensure_stream_url(self, entry: dict, guild_id: int):
        url = entry.get("url")
        already_prepared = entry.get("_prepared_source") or entry.get("formats")
        looks_like_watch_url = isinstance(url, str) and ("youtube.com/watch" in url or "youtu.be/" in url)
        needs_fetch = not url or looks_like_watch_url or not already_prepared
        if not needs_fetch:
            return

        video_id = entry.get("id")
        video_url = entry.get("webpage_url") or url
        if not video_url and video_id:
            video_url = f"https://www.youtube.com/watch?v={video_id}"
        if not video_url:
            raise ValueError("Both 'webpage_url' and 'id' are missing.")

        full = await _run_ydl_info(
            video_url,
            _ydl_options({
                "quiet": True,
                "extract_flat": False,
                "noplaylist": True,
                "format": "ba/b",
                "format_sort": ["acodec:opus", "proto:https:http"],
            }, silent=self.is_silent_log(guild_id))
        )

        audio_url = full.get("url")
        if not audio_url:
            fallback_url = None
            for f in reversed(full.get("formats", [])):
                if f.get("acodec") == "none" or "url" not in f:
                    continue
                if f.get("vcodec") == "none":
                    audio_url = f["url"]
                    break
                if fallback_url is None:
                    fallback_url = f["url"]
            audio_url = audio_url or fallback_url

        if not audio_url:
            raise ValueError(t(None, "AUDIO_FORMAT_MISSING"))

        for key in self._STREAM_KEYS:
            if key in full:
                entry[key] = full[key]
        if full.get("track") and full.get("artist"):
            entry["title"] = full["track"]
        entry["url"] = audio_url
        entry["_prepared_source"] = True
        from core.media import slim_entry
        slim_entry(entry)

    async def _build_audio_source(self, entry: dict, guild_id: int) -> discord.FFmpegOpusAudio:
        seek_time = entry.pop("seek_time", None)
        seek_prefix = ""
        if isinstance(seek_time, (int, float)) and seek_time > 0:
            seek_prefix = f"-ss {seek_time} "

        silent = self.is_silent_log(guild_id)
        before = (
            f"{seek_prefix}"
            f"-reconnect 1 -reconnect_streamed 1 -reconnect_on_network_error 1 "
            f'-reconnect_on_http_error "429,500,502,503" '
            f"-reconnect_delay_max 15 "
            f"-rw_timeout 15000000 -probesize 131072 -analyzeduration 131072 "
            f"-nostdin"
        )

        opts = {"before_options": before}
        if silent:
            opts["options"] = "-loglevel quiet"

        try:
            codec, _ = await asyncio.wait_for(
                discord.FFmpegOpusAudio.probe(entry["url"]), timeout=10
            )
        except (asyncio.TimeoutError, Exception):
            codec = "copy"
        return discord.FFmpegOpusAudio(entry["url"], codec=codec, bitrate=128, **opts)

    def _guild_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._play_locks.get(guild_id)
        if not lock:
            lock = asyncio.Lock()
            self._play_locks[guild_id] = lock
        return lock

    async def play_next(self, guild_id: int, manual: bool = False):
        """
        Advances the queue and plays the next song.
        Handles fetching stream URLs, processing Spotify tracks, and error recovery.
        """
        async with self._guild_lock(guild_id):
            await self._play_next_inner(guild_id, manual)

    def _unloop_failed(self, state: GuildMusicState):
        """Removes the re-queued copy of a failed entry in queue-loop mode,
        and skips the broken track in song-loop mode to avoid retry spam."""
        ref = state._looped_ref
        if ref and state.queue:
            for i in range(len(state.queue) - 1, -1, -1):
                if state.queue[i] is ref:
                    state.queue.pop(i)
                    break
        state._looped_ref = None
        if state.loop_mode == "song":
            state.skip_current_song_once = True

    async def _prefetch_next(self, guild_id: int):
        state = self.guild_states.get(guild_id)
        if not state or not state.playing or not state.queue:
            return
        entry = state.queue[0]
        if (entry.get("_prepared_source") or entry.get("formats")) and entry.get("url"):
            return
        await self._hydrate_entry(entry, guild_id)
        await self._ensure_stream_url(entry, guild_id)

    def _start_prefetch(self, state: GuildMusicState, guild_id: int):
        if not self.is_prefetch(guild_id):
            return
        if state._prefetch_task and not state._prefetch_task.done():
            state._prefetch_task.cancel()
        state._prefetch_task = asyncio.create_task(
            self._prefetch_next(guild_id), name=f"prefetch-{guild_id}")
        state._prefetch_task.add_done_callback(lambda t: t.exception() if not t.cancelled() and t.exception() else None)

    async def _play_next_inner(self, guild_id: int, manual: bool = False):
        try:
            state = self.guild_states.get(guild_id)
            if not state:
                return
            pf = state._prefetch_task
            if pf and not pf.done():
                try:
                    await asyncio.wait_for(asyncio.shield(pf), timeout=5)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                    pass
            state._prefetch_task = None

            vc = self._ensure_voice_client(guild_id, state)
            text_channel = state.text_channel

            if not vc or not vc.is_connected():
                self._mark_idle(state, guild_id)
                self._fire_track_change(guild_id)
                return
            state.vc = vc

            self.cleanup_guild_votes(guild_id, self._SONG_VOTES)

            attempts = 0
            max_attempts = max(1, len(state.queue) + (1 if state.loop_mode == "song" and state.now else 0))
            started = False
            load_failures = 0

            def safe_after(e):
                if e:
                    print(f"[FFmpeg Error] {e}")
                    current = self.guild_states.get(guild_id)
                    if current:
                        current._consecutive_ffmpeg_errors = getattr(current, '_consecutive_ffmpeg_errors', 0) + 1
                    now = current.now if current else None
                    if now:
                        now.pop("_prepared_source", None)
                def _schedule_after():
                    task = self.bot.loop.create_task(self._handle_after_callback(guild_id), name=f"after-cb-{guild_id}")
                    task.add_done_callback(_task_done_cb)
                try:
                    self.bot.loop.call_soon_threadsafe(_schedule_after)
                except RuntimeError:
                    pass

            while attempts < max_attempts:
                next_entry = self._select_next_entry(state, guild_id, failed=attempts > 0)
                if not next_entry:
                    self._fire_track_change(guild_id)
                    return
                attempts += 1
                try:
                    if next_entry.get("source") in ("spotify", "url"):
                        try:
                            await self._hydrate_entry(next_entry, guild_id)
                        except StaleCookieError:
                            raise
                        except Exception as e:
                            label = "Spotify" if next_entry.get("source") == "spotify" else "URL"
                            print(f"[{label} error] {next_entry.get('title', '?')}: {e}")
                            load_failures += 1
                            self._unloop_failed(state)
                            continue

                    await self._ensure_stream_url(next_entry, guild_id)
                except StaleCookieError:
                    state.age_restricted_skips += 1
                    self._unloop_failed(state)
                    continue
                except JSRuntimeError:
                    if text_channel:
                        await text_channel.send(t(text_channel, "YTDLP_NO_JS_RUNTIME"), delete_after=state.effective_delete_after)
                    self._mark_idle(state, guild_id)
                    self._fire_track_change(guild_id)
                    return
                except Exception as e:
                    print(f"[Track load error] {next_entry.get('title', '?')}: {e}")
                    load_failures += 1
                    self._unloop_failed(state)
                    continue

                if not state.playing:
                    self._fire_track_change(guild_id)
                    return

                state.now = next_entry
                state.paused_at = None
                state._total_paused = 0.0
                state.playing_since = time.time()
                state._seeking = False
                if not vc.is_connected():
                    self._mark_idle(state, guild_id)
                    self._fire_track_change(guild_id)
                    return
                source = await self._build_audio_source(state.now, guild_id)

                if not state.playing:
                    source.cleanup()
                    self._fire_track_change(guild_id)
                    return

                try:
                    vc.play(source, after=safe_after)
                except Exception:
                    source.cleanup()
                    self._mark_idle(state, guild_id)
                    self._fire_track_change(guild_id)
                    return
                started = True
                state._consecutive_ffmpeg_errors = 0
                # Cancel any existing live timer, start a new one if this is a live track
                old_lt = self._live_timers.pop(guild_id, None)
                if old_lt and not old_lt.done():
                    old_lt.cancel()
                if next_entry.get("is_live"):
                    max_hours = getattr(self, 'guild_live_max_hours', {}).get(guild_id, 1)
                    if max_hours > 0:
                        lt = asyncio.create_task(
                            self._live_timeout(guild_id, max_hours * 3600),
                            name=f"live-timer-{guild_id}")
                        lt.add_done_callback(_task_done_cb)
                        self._live_timers[guild_id] = lt
                if self.on_track_played:
                    self.on_track_played(guild_id, next_entry)
                suppress_announce = next_entry.pop("suppress_announce", False)

                try:
                    if not manual and not suppress_announce:
                        if text_channel:
                            await text_channel.send(
                                t(text_channel, "ANNOUNCE_NOW", title=next_entry.get("title") or t(text_channel, "UNKNOWN"), uploader=next_entry.get("uploader") or t(text_channel, "UNKNOWN")),
                                delete_after=state.effective_delete_after
                            )

                    await self._report_skipped(state, text_channel)
                    if load_failures and text_channel:
                        await text_channel.send(
                            t(text_channel, "TRACKS_SKIPPED_FAILED", count=load_failures),
                            delete_after=state.effective_delete_after)
                except discord.HTTPException:
                    pass
                self._fire_track_change(guild_id)
                self._start_prefetch(state, guild_id)
                break

            if not started:
                has_skips = await self._report_skipped(state, text_channel)
                if load_failures and text_channel:
                    try:
                        await text_channel.send(
                            t(text_channel, "TRACKS_SKIPPED_FAILED", count=load_failures),
                            delete_after=state.effective_delete_after)
                    except discord.HTTPException:
                        pass
                    has_skips = True
                if not has_skips and text_channel:
                    await text_channel.send(t(text_channel, "ALL_TRACKS_FAILED"), delete_after=state.effective_delete_after)
                self._mark_idle(state, guild_id)
                self._fire_track_change(guild_id)
        except Exception as e:
            print(f"[play_next error] {e}\n{traceback.format_exc()}")
            state = self.guild_states.get(guild_id)
            if state:
                if state.text_channel:
                    try:
                        await state.text_channel.send(t(state.text_channel, "PLAYBACK_FATAL"), delete_after=state.effective_delete_after)
                    except Exception:
                        pass
                self._mark_idle(state, guild_id)
                self._fire_track_change(guild_id)

    def _fire_track_change(self, guild_id: int):
        if self.on_track_change:
            self.on_track_change(guild_id)

    async def _handle_after_callback(self, guild_id: int):
        state = self.guild_states.get(guild_id)
        if not state or not state.playing:
            if state:
                state.suppress_after_callback = False
            return
        if state.suppress_after_callback:
            state.suppress_after_callback = False
            return
        errs = getattr(state, '_consecutive_ffmpeg_errors', 0)
        if errs >= 3:
            await asyncio.sleep(min(errs, 10))
        if self.on_radio_track_finished:
            await self.on_radio_track_finished(guild_id)
            if not state.playing:
                return
        await self.play_next(guild_id)
