import asyncio
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

import aiosqlite

DB_PATH = (Path(__file__).parent / "data.db").resolve()


class Database:
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._lock = asyncio.Lock()
        self._tx_lock = asyncio.Lock()
        self._initialized = False
        self._conn = None
        self._user_add_locks: dict[int, asyncio.Lock] = {}

    async def _ensure_init(self):
        if self._initialized and self._conn:
            return
        async with self._lock:
            if self._initialized and self._conn:
                return
            try:
                self._conn = await aiosqlite.connect(self.db_path)
                await self._conn._execute(setattr, self._conn._conn, "isolation_level", None)
                await self._conn.execute("PRAGMA journal_mode=WAL;")
                await self._conn.execute("PRAGMA synchronous=NORMAL;")
                await self._conn.execute("PRAGMA busy_timeout=5000;")
                await self._conn.execute("PRAGMA foreign_keys=ON;")
                await self._conn.execute("PRAGMA cache_size=-8000;")
                await self._conn.execute("PRAGMA mmap_size=67108864;")
                await self._conn.execute("PRAGMA temp_store=MEMORY;")

                await self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS guild_settings (
                        guild_id INTEGER PRIMARY KEY,
                        locale TEXT,
                        dj_role_id INTEGER,
                        silent_log INTEGER DEFAULT 0,
                        vote_mode TEXT DEFAULT 'half_plus_one',
                        delete_after INTEGER DEFAULT 10,
                        embed_color INTEGER,
                        compact_mode INTEGER DEFAULT 0,
                        max_playlists INTEGER DEFAULT 15,
                        queue_per_page INTEGER DEFAULT 10,
                        queue_compact INTEGER DEFAULT 1,
                        view_channel INTEGER,
                        view_restrict INTEGER DEFAULT 0,
                        max_history INTEGER DEFAULT 50,
                        max_user_tracks INTEGER DEFAULT 0,
                        embed_layout TEXT,
                        admin_priv INTEGER DEFAULT 1,
                        queue_button_compact INTEGER DEFAULT 0,
                        track_limit_target TEXT DEFAULT 'users',
                        pause_permission TEXT DEFAULT 'requester_dj',
                        pause_timeout INTEGER DEFAULT 900,
                        radio_permission TEXT DEFAULT 'dj',
                        radio_edit_permission TEXT DEFAULT 'dj',
                        radio_cooldown INTEGER DEFAULT 3,
                        track_limit_users INTEGER DEFAULT 0,
                        track_limit_dj INTEGER DEFAULT 0,
                        track_limit_admin INTEGER DEFAULT 0,
                        pause_timeout_behavior TEXT DEFAULT 'leave',
                        idle_disconnect_timeout INTEGER DEFAULT 180,
                        join_restrict_level TEXT DEFAULT 'none',
                        force_play_permission TEXT DEFAULT 'dj',
                        force_radio TEXT DEFAULT 'disabled',
                        timezone INTEGER DEFAULT 0,
                        seek_permission TEXT DEFAULT 'requester_dj',
                        max_seeks_per_track INTEGER DEFAULT 3,
                        max_seeks_dj INTEGER DEFAULT 0,
                        queue_limit INTEGER DEFAULT 5000,
                        playlist_track_limit INTEGER DEFAULT 5000,
                        prefetch INTEGER DEFAULT 1,
                        safe_prefetch INTEGER DEFAULT 1,
                        max_workers INTEGER DEFAULT 16,
                        vote_exclude_deafened INTEGER DEFAULT 1,
                        live_enabled INTEGER DEFAULT 0,
                        live_permission TEXT DEFAULT 'admin',
                        live_max_hours INTEGER DEFAULT 1
                    )
                    """
                )

                await self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS playlists (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER NOT NULL,
                        user_id INTEGER NOT NULL,
                        name TEXT NOT NULL,
                        is_favourite INTEGER DEFAULT 0,
                        created_at TEXT DEFAULT (datetime('now')),
                        UNIQUE(guild_id, user_id, name)
                    )
                    """
                )
                await self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS playlist_tracks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        playlist_id INTEGER NOT NULL,
                        position INTEGER NOT NULL,
                        title TEXT,
                        uploader TEXT,
                        duration REAL,
                        url TEXT NOT NULL,
                        is_live INTEGER NOT NULL DEFAULT 0,
                        added_at TEXT DEFAULT (datetime('now')),
                        FOREIGN KEY (playlist_id) REFERENCES playlists(id) ON DELETE CASCADE
                    )
                    """
                )

                await self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pt_playlist_pos ON playlist_tracks(playlist_id, position)"
                )
                await self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_pl_guild_user ON playlists(guild_id, user_id)"
                )

                await self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS active_views (
                        guild_id INTEGER NOT NULL,
                        view_type TEXT NOT NULL,
                        channel_id INTEGER NOT NULL,
                        message_id INTEGER NOT NULL,
                        PRIMARY KEY (guild_id, view_type)
                    )
                    """
                )

                await self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS song_history (
                        id       INTEGER PRIMARY KEY AUTOINCREMENT,
                        guild_id INTEGER NOT NULL,
                        title    TEXT,
                        uploader TEXT,
                        duration REAL,
                        url      TEXT NOT NULL,
                        thumbnail TEXT,
                        requester INTEGER NOT NULL,
                        played_at TEXT DEFAULT (datetime('now'))
                    )
                    """
                )
                await self._conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_sh_guild_time ON song_history(guild_id, played_at DESC)"
                )

                await self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bot_activity (
                        id            INTEGER PRIMARY KEY DEFAULT 1,
                        activity_type INTEGER DEFAULT 2,
                        activity_text TEXT DEFAULT '/play',
                        activity_mode TEXT DEFAULT 'static',
                        activity_interval INTEGER DEFAULT 120,
                        activity_selected INTEGER DEFAULT 0
                    )
                    """
                )
                await self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS bot_activity_list (
                        id            INTEGER PRIMARY KEY AUTOINCREMENT,
                        position      INTEGER NOT NULL,
                        activity_type INTEGER NOT NULL,
                        activity_text TEXT NOT NULL
                    )
                    """
                )
                # Entity tables (dj_users, excluded_*, admin_*, join_restrict_channels)
                for tbl, col in self._ENTITY_TABLES:
                    await self._conn.execute(
                        f"CREATE TABLE IF NOT EXISTS {tbl} ("
                        f"guild_id INTEGER NOT NULL, "
                        f"{col} INTEGER NOT NULL, "
                        f"PRIMARY KEY (guild_id, {col}))"
                    )

                await self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS timeouts (
                        guild_id   INTEGER NOT NULL,
                        user_id    INTEGER NOT NULL,
                        expires_at REAL    NOT NULL,
                        PRIMARY KEY (guild_id, user_id)
                    )
                    """
                )

                # Migrate old track limit to per-group columns
                async with self._conn.execute(
                    "SELECT guild_id, max_user_tracks, track_limit_target FROM guild_settings WHERE max_user_tracks > 0"
                ) as cur:
                    rows = await cur.fetchall()
                for gid, limit_val, target in rows:
                    if target == "all":
                        col = "track_limit_admin"
                    elif target == "dj_users":
                        col = "track_limit_dj"
                    else:
                        col = "track_limit_users"
                    await self._conn.execute(
                        f"UPDATE guild_settings SET {col}=? WHERE guild_id=? AND {col}=0",
                        (limit_val, gid),
                    )

                # Migrate old 'np' view_type to 'mp'
                await self._conn.execute("UPDATE active_views SET view_type='mp' WHERE view_type='np'")

                await self._conn.commit()
                self._initialized = True
            except BaseException:
                if self._conn:
                    await self._conn.close()
                    self._conn = None
                raise

    async def close(self):
        async with self._lock:
            if self._conn:
                await self._conn.close()
                self._conn = None
                self._initialized = False

    async def _exec_commit(self, sql, params=None):
        """Execute a single write + commit, respecting _tx_lock to avoid corrupting open transactions."""
        async with self._tx_lock:
            await self._conn.execute(sql, params)
            await self._conn.commit()

    async def _execmany_commit(self, sql, params_seq):
        """Execute many + commit under _tx_lock."""
        async with self._tx_lock:
            await self._conn.executemany(sql, params_seq)
            await self._conn.commit()

    @asynccontextmanager
    async def _transaction(self):
        await self._tx_lock.acquire()
        try:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield
                await self._conn.commit()
            except BaseException:
                await self._conn.rollback()
                raise
        finally:
            self._tx_lock.release()

    async def _get_all(self, column: str, cast: Callable | None = None) -> dict:
        await self._ensure_init()
        async with self._conn.execute(f"SELECT guild_id, {column} FROM guild_settings") as cursor:
            rows = await cursor.fetchall()
        fn = cast or (lambda x: x)
        return {int(gid): fn(val) for gid, val in rows if val is not None and gid != 0}

    async def get_guild_settings_row(self, guild_id: int) -> dict | None:
        await self._ensure_init()
        async with self._conn.execute(
            "SELECT * FROM guild_settings WHERE guild_id=?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            cols = [d[0] for d in cur.description]
            return dict(zip(cols, row))

    async def get_guild_entity_set(self, table: str, entity_col: str, guild_id: int) -> set[int]:
        await self._ensure_init()
        async with self._conn.execute(
            f"SELECT {entity_col} FROM {table} WHERE guild_id=?", (guild_id,)
        ) as cur:
            rows = await cur.fetchall()
        return {int(r[0]) for r in rows}

    async def _get_global(self, column: str, cast: Callable = int, default: Any = None):
        """Fetch a single global setting (guild_id=0) with a cast and default."""
        await self._ensure_init()
        async with self._conn.execute(
            f"SELECT {column} FROM guild_settings WHERE guild_id = 0"
        ) as cur:
            row = await cur.fetchone()
        return cast(row[0]) if row and row[0] is not None else default

    async def _upsert(self, guild_id: int, column: str, value: Any):
        await self._ensure_init()
        await self._exec_commit(
            f"INSERT INTO guild_settings (guild_id, {column}) VALUES (?, ?) "
            f"ON CONFLICT(guild_id) DO UPDATE SET {column}=excluded.{column}",
            (guild_id, value),
        )

    async def get_all_locales(self) -> dict[int, str]:
        return await self._get_all("locale")

    async def get_all_dj_roles(self) -> dict[int, int]:
        return await self._get_all("dj_role_id", int)

    async def get_silent_log(self) -> bool:
        return await self._get_global("silent_log", lambda v: bool(int(v)), False)

    async def get_all_vote_modes(self) -> dict[int, str]:
        return await self._get_all("vote_mode", str)

    async def get_all_delete_after(self) -> dict[int, int]:
        return await self._get_all("delete_after", int)

    async def set_locale(self, guild_id: int, value: str | None):
        await self._upsert(guild_id, "locale", value)

    async def set_dj_role(self, guild_id: int, role_id: int | None):
        await self._upsert(guild_id, "dj_role_id", role_id)

    async def set_silent_log(self, silent: bool):
        await self._upsert(0, "silent_log", int(silent))

    async def set_vote_mode(self, guild_id: int, mode: str):
        await self._upsert(guild_id, "vote_mode", mode)

    async def get_all_vote_exclude_deafened(self) -> dict[int, int]:
        return await self._get_all("vote_exclude_deafened", int)

    async def set_vote_exclude_deafened(self, guild_id: int, value: int):
        await self._upsert(guild_id, "vote_exclude_deafened", value)

    async def get_all_live_enabled(self) -> dict[int, int]:
        return await self._get_all("live_enabled", int)

    async def set_live_enabled(self, guild_id: int, value: int):
        await self._upsert(guild_id, "live_enabled", value)

    async def get_all_live_permission(self) -> dict[int, str]:
        return await self._get_all("live_permission", str)

    async def set_live_permission(self, guild_id: int, value: str):
        await self._upsert(guild_id, "live_permission", value)

    async def get_all_live_max_hours(self) -> dict[int, int]:
        return await self._get_all("live_max_hours", int)

    async def set_live_max_hours(self, guild_id: int, value: int):
        await self._upsert(guild_id, "live_max_hours", value)

    async def set_delete_after(self, guild_id: int, delete_after: int | None):
        await self._upsert(guild_id, "delete_after", delete_after)

    async def get_all_embed_colors(self) -> dict[int, int]:
        return await self._get_all("embed_color", int)

    async def set_embed_color(self, guild_id: int, color: int | None):
        await self._upsert(guild_id, "embed_color", color)

    async def get_all_compact_modes(self) -> dict[int, bool]:
        return await self._get_all("compact_mode", bool)

    async def set_compact_mode(self, guild_id: int, enabled: bool):
        await self._upsert(guild_id, "compact_mode", int(enabled))

    # -- bot_activity --

    async def get_bot_activity(self) -> dict:
        await self._ensure_init()
        async with self._conn.execute(
            "SELECT activity_type, activity_text, activity_mode, activity_interval, activity_selected FROM bot_activity WHERE id = 1"
        ) as cursor:
            row = await cursor.fetchone()
        if row:
            return {"type": int(row[0]), "text": str(row[1]), "mode": str(row[2]), "interval": int(row[3]), "selected": int(row[4])}
        return {"type": 2, "text": "/play", "mode": "static", "interval": 120, "selected": 0}

    async def set_bot_activity_default(self, activity_type: int, activity_text: str):
        await self._ensure_init()
        await self._exec_commit(
            """
            INSERT INTO bot_activity (id, activity_type, activity_text)
            VALUES (1, ?, ?)
            ON CONFLICT(id) DO UPDATE SET activity_type = excluded.activity_type,
                                          activity_text = excluded.activity_text
            """,
            (activity_type, activity_text),
        )

    async def _set_bot_activity_field(self, column: str, value):
        await self._ensure_init()
        await self._exec_commit(
            f"INSERT INTO bot_activity (id, {column}) VALUES (1, ?) "
            f"ON CONFLICT(id) DO UPDATE SET {column} = excluded.{column}",
            (value,),
        )

    async def set_bot_activity_mode(self, mode: str):
        await self._set_bot_activity_field("activity_mode", mode)

    async def set_bot_activity_interval(self, interval: int):
        await self._set_bot_activity_field("activity_interval", interval)

    async def set_bot_activity_selected(self, selected: int):
        await self._set_bot_activity_field("activity_selected", selected)

    async def get_bot_activity_list(self) -> list[dict]:
        await self._ensure_init()
        async with self._conn.execute(
            "SELECT id, position, activity_type, activity_text FROM bot_activity_list ORDER BY position"
        ) as cursor:
            rows = await cursor.fetchall()
        return [{"id": r[0], "position": r[1], "type": r[2], "text": r[3]} for r in rows]

    async def add_bot_activity_item(self, activity_type: int, activity_text: str) -> int:
        await self._ensure_init()
        async with self._transaction():
            async with self._conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM bot_activity_list") as cur:
                pos = (await cur.fetchone())[0]
            async with self._conn.execute(
                "INSERT INTO bot_activity_list (position, activity_type, activity_text) VALUES (?, ?, ?)",
                (pos, activity_type, activity_text),
            ) as cur:
                row_id = cur.lastrowid
        return row_id

    async def update_bot_activity_item(self, item_id: int, activity_type: int, activity_text: str):
        await self._ensure_init()
        await self._exec_commit(
            "UPDATE bot_activity_list SET activity_type = ?, activity_text = ? WHERE id = ?",
            (activity_type, activity_text, item_id),
        )

    async def remove_bot_activity_items(self, item_ids: list[int]):
        await self._ensure_init()
        if not item_ids:
            return
        async with self._transaction():
            placeholders = ",".join("?" * len(item_ids))
            await self._conn.execute(f"DELETE FROM bot_activity_list WHERE id IN ({placeholders})", item_ids)
            async with self._conn.execute("SELECT id FROM bot_activity_list ORDER BY position") as cur:
                rows = await cur.fetchall()
            await self._conn.executemany(
                "UPDATE bot_activity_list SET position = ? WHERE id = ?",
                [(i, rid) for i, (rid,) in enumerate(rows)],
            )

    async def move_bot_activity_item(self, from_pos: int, to_pos: int):
        await self._ensure_init()
        async with self._transaction():
            async with self._conn.execute("SELECT id FROM bot_activity_list ORDER BY position") as cur:
                ids = [r[0] for r in await cur.fetchall()]
            if not (0 <= from_pos < len(ids) and 0 <= to_pos < len(ids)):
                return
            item = ids.pop(from_pos)
            ids.insert(to_pos, item)
            await self._conn.executemany(
                "UPDATE bot_activity_list SET position = ? WHERE id = ?",
                [(i, rid) for i, rid in enumerate(ids)],
            )

    async def clear_bot_activity_list(self):
        await self._ensure_init()
        await self._exec_commit("DELETE FROM bot_activity_list")

    async def reset_bot_activity(self):
        await self._ensure_init()
        async with self._transaction():
            await self._conn.execute("DELETE FROM bot_activity_list")
            await self._conn.execute(
                "INSERT INTO bot_activity (id, activity_type, activity_text, activity_mode, activity_interval, activity_selected) "
                "VALUES (1, 2, '/play', 'static', 120, 0) "
                "ON CONFLICT(id) DO UPDATE SET "
                "activity_type=2, activity_text='/play', activity_mode='static', "
                "activity_interval=120, activity_selected=0"
            )

    # -- embed_layout --

    async def get_all_embed_layouts(self) -> dict[int, dict]:
        await self._ensure_init()
        async with self._conn.execute(
            "SELECT guild_id, embed_layout FROM guild_settings WHERE embed_layout IS NOT NULL"
        ) as cursor:
            rows = await cursor.fetchall()
        import json as _json
        result: dict[int, dict] = {}
        for gid, raw in rows:
            try:
                result[int(gid)] = _json.loads(raw)
            except Exception as e:
                print(f"[DB] Malformed embed_layout for guild {gid}: {e}")
        return result

    async def set_embed_layout(self, guild_id: int, layout: dict):
        import json as _json
        await self._upsert(guild_id, "embed_layout", _json.dumps(layout, separators=(",", ":")))

    # -- max_playlists setting --

    async def get_all_max_playlists(self) -> dict[int, int]:
        return await self._get_all("max_playlists", int)

    async def set_max_playlists(self, guild_id: int, value: int):
        await self._upsert(guild_id, "max_playlists", value)

    # -- queue display --

    async def get_all_queue_per_page(self) -> dict[int, int]:
        return await self._get_all("queue_per_page", int)

    async def set_queue_per_page(self, guild_id: int, value: int):
        await self._upsert(guild_id, "queue_per_page", value)

    async def get_all_queue_compact(self) -> dict[int, bool]:
        return await self._get_all("queue_compact", bool)

    async def set_queue_compact(self, guild_id: int, enabled: bool):
        await self._upsert(guild_id, "queue_compact", int(enabled))

    # -- view restriction --

    async def get_all_view_channels(self) -> dict[int, int]:
        return await self._get_all("view_channel", int)

    async def set_view_channel(self, guild_id: int, value: int | None):
        await self._upsert(guild_id, "view_channel", value)

    async def get_all_view_restricts(self) -> dict[int, int]:
        return await self._get_all("view_restrict", int)

    async def set_view_restrict(self, guild_id: int, value: int):
        await self._upsert(guild_id, "view_restrict", value)

    async def get_all_admin_priv(self) -> dict[int, int]:
        return await self._get_all("admin_priv", int)

    async def set_admin_priv(self, guild_id: int, value: int):
        await self._upsert(guild_id, "admin_priv", value)

    async def get_all_queue_button_compact(self) -> dict[int, int]:
        return await self._get_all("queue_button_compact", int)

    async def set_queue_button_compact(self, guild_id: int, value: int):
        await self._upsert(guild_id, "queue_button_compact", value)

    async def get_all_track_limit_target(self) -> dict[int, str]:
        return await self._get_all("track_limit_target", str)

    async def set_track_limit_target(self, guild_id: int, value: str):
        await self._upsert(guild_id, "track_limit_target", value)

    # -- per-group track limits --

    async def get_all_track_limit_users(self) -> dict[int, int]:
        return await self._get_all("track_limit_users", int)

    async def set_track_limit_users(self, guild_id: int, value: int):
        await self._upsert(guild_id, "track_limit_users", value)

    async def get_all_track_limit_dj(self) -> dict[int, int]:
        return await self._get_all("track_limit_dj", int)

    async def set_track_limit_dj(self, guild_id: int, value: int):
        await self._upsert(guild_id, "track_limit_dj", value)

    async def get_all_track_limit_admin(self) -> dict[int, int]:
        return await self._get_all("track_limit_admin", int)

    async def set_track_limit_admin(self, guild_id: int, value: int):
        await self._upsert(guild_id, "track_limit_admin", value)

    async def get_all_pause_permission(self) -> dict[int, str]:
        return await self._get_all("pause_permission", str)

    async def set_pause_permission(self, guild_id: int, value: str):
        await self._upsert(guild_id, "pause_permission", value)

    async def get_all_pause_timeout(self) -> dict[int, int]:
        return await self._get_all("pause_timeout", int)

    async def set_pause_timeout(self, guild_id: int, value: int):
        await self._upsert(guild_id, "pause_timeout", value)

    # -- timezone --

    async def get_all_timezones(self) -> dict[int, int]:
        return await self._get_all("timezone", int)

    async def set_timezone(self, guild_id: int, value: int):
        await self._upsert(guild_id, "timezone", value)

    # -- queue / playlist limits --

    async def get_all_queue_limits(self) -> dict[int, int]:
        return await self._get_all("queue_limit", int)

    async def set_queue_limit(self, guild_id: int, value: int):
        await self._upsert(guild_id, "queue_limit", value)

    async def get_all_playlist_track_limits(self) -> dict[int, int]:
        return await self._get_all("playlist_track_limit", int)

    async def set_playlist_track_limit(self, guild_id: int, value: int):
        await self._upsert(guild_id, "playlist_track_limit", value)

    # -- seek settings --

    async def get_all_seek_permissions(self) -> dict[int, str]:
        return await self._get_all("seek_permission", str)

    async def set_seek_permission(self, guild_id: int, value: str):
        await self._upsert(guild_id, "seek_permission", value)

    async def get_all_max_seeks_per_track(self) -> dict[int, int]:
        return await self._get_all("max_seeks_per_track", int)

    async def set_max_seeks_per_track(self, guild_id: int, value: int):
        await self._upsert(guild_id, "max_seeks_per_track", value)

    async def get_all_max_seeks_dj(self) -> dict[int, int]:
        return await self._get_all("max_seeks_dj", int)

    async def set_max_seeks_dj(self, guild_id: int, value: int):
        await self._upsert(guild_id, "max_seeks_dj", value)

    # -- radio settings --

    async def get_all_radio_permissions(self) -> dict[int, str]:
        return await self._get_all("radio_permission", str)

    async def set_radio_permission(self, guild_id: int, value: str):
        await self._upsert(guild_id, "radio_permission", value)

    async def get_all_radio_edit_permissions(self) -> dict[int, str]:
        return await self._get_all("radio_edit_permission", str)

    async def set_radio_edit_permission(self, guild_id: int, value: str):
        await self._upsert(guild_id, "radio_edit_permission", value)

    async def get_all_radio_cooldowns(self) -> dict[int, int]:
        return await self._get_all("radio_cooldown", int)

    async def set_radio_cooldown(self, guild_id: int, value: int):
        await self._upsert(guild_id, "radio_cooldown", value)

    # -- forced play --

    async def get_all_force_play_permission(self) -> dict[int, str]:
        return await self._get_all("force_play_permission", str)

    async def set_force_play_permission(self, guild_id: int, value: str):
        await self._upsert(guild_id, "force_play_permission", value)

    async def get_all_force_radio(self) -> dict[int, str]:
        return await self._get_all("force_radio", str)

    async def set_force_radio(self, guild_id: int, value: str):
        await self._upsert(guild_id, "force_radio", value)

    # -- prefetch --

    async def get_prefetch(self) -> bool:
        return await self._get_global("prefetch", lambda v: bool(int(v)), True)

    async def set_prefetch(self, value: bool):
        await self._upsert(0, "prefetch", int(value))

    async def get_safe_prefetch(self) -> bool:
        return await self._get_global("safe_prefetch", lambda v: bool(int(v)), True)

    async def set_safe_prefetch(self, value: bool):
        await self._upsert(0, "safe_prefetch", int(value))

    async def get_max_workers(self) -> int:
        return await self._get_global("max_workers", int, 16)

    async def set_max_workers(self, value: int):
        await self._upsert(0, "max_workers", value)

    # -- pause timeout behavior --

    async def get_all_pause_timeout_behavior(self) -> dict[int, str]:
        return await self._get_all("pause_timeout_behavior", str)

    async def set_pause_timeout_behavior(self, guild_id: int, value: str):
        await self._upsert(guild_id, "pause_timeout_behavior", value)

    # -- idle disconnect --

    async def get_all_idle_disconnect_timeout(self) -> dict[int, int]:
        return await self._get_all("idle_disconnect_timeout", int)

    async def set_idle_disconnect_timeout(self, guild_id: int, value: int):
        await self._upsert(guild_id, "idle_disconnect_timeout", value)

    # -- join restrict --

    async def get_all_join_restrict_level(self) -> dict[int, str]:
        return await self._get_all("join_restrict_level", str)

    async def set_join_restrict_level(self, guild_id: int, value: str):
        await self._upsert(guild_id, "join_restrict_level", value)

    async def get_all_join_restrict_channels(self) -> dict[int, set[int]]:
        await self._ensure_init()
        result: dict[int, set[int]] = {}
        async with self._conn.execute("SELECT guild_id, channel_id FROM join_restrict_channels") as cur:
            async for row in cur:
                result.setdefault(int(row[0]), set()).add(int(row[1]))
        return result

    async def add_join_restrict_channel(self, guild_id: int, channel_id: int):
        await self._ensure_init()
        await self._exec_commit(
            "INSERT OR IGNORE INTO join_restrict_channels (guild_id, channel_id) VALUES (?, ?)",
            (guild_id, channel_id),
        )

    async def remove_join_restrict_channel(self, guild_id: int, channel_id: int):
        await self._ensure_init()
        await self._exec_commit(
            "DELETE FROM join_restrict_channels WHERE guild_id=? AND channel_id=?",
            (guild_id, channel_id),
        )

    async def clear_join_restrict_channels(self, guild_id: int):
        await self._ensure_init()
        await self._exec_commit("DELETE FROM join_restrict_channels WHERE guild_id=?", (guild_id,))

    # -- active views --

    async def save_active_view(self, guild_id: int, view_type: str, channel_id: int, message_id: int):
        await self._ensure_init()
        await self._exec_commit(
            "INSERT INTO active_views (guild_id, view_type, channel_id, message_id) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, view_type) DO UPDATE SET channel_id=excluded.channel_id, message_id=excluded.message_id",
            (guild_id, view_type, channel_id, message_id),
        )

    async def get_active_view(self, guild_id: int, view_type: str) -> tuple[int, int] | None:
        await self._ensure_init()
        async with self._conn.execute(
            "SELECT channel_id, message_id FROM active_views WHERE guild_id=? AND view_type=?",
            (guild_id, view_type),
        ) as cursor:
            row = await cursor.fetchone()
        return (int(row[0]), int(row[1])) if row else None

    async def delete_active_view(self, guild_id: int, view_type: str):
        await self._ensure_init()
        await self._exec_commit(
            "DELETE FROM active_views WHERE guild_id=? AND view_type=?",
            (guild_id, view_type),
        )

    # -- Generic guild entity-set helpers --

    async def _set_get_all(self, table: str, entity_col: str) -> dict[int, set[int]]:
        await self._ensure_init()
        async with self._conn.execute(f"SELECT guild_id, {entity_col} FROM {table}") as cur:
            rows = await cur.fetchall()
        result: dict[int, set[int]] = {}
        for gid, eid in rows:
            result.setdefault(int(gid), set()).add(int(eid))
        return result

    async def _set_add(self, table: str, entity_col: str, guild_id: int, entity_id: int):
        await self._ensure_init()
        await self._exec_commit(
            f"INSERT OR IGNORE INTO {table} (guild_id, {entity_col}) VALUES (?, ?)",
            (guild_id, entity_id),
        )

    async def _set_remove(self, table: str, entity_col: str, guild_id: int, entity_id: int):
        await self._ensure_init()
        await self._exec_commit(
            f"DELETE FROM {table} WHERE guild_id = ? AND {entity_col} = ?",
            (guild_id, entity_id),
        )

    async def _set_clear(self, table: str, guild_id: int):
        await self._ensure_init()
        await self._exec_commit(f"DELETE FROM {table} WHERE guild_id = ?", (guild_id,))

    _ENTITY_TABLES = [
        ("dj_users", "user_id"),
        ("excluded_users", "user_id"),
        ("excluded_roles", "role_id"),
        ("admin_users", "user_id"),
        ("admin_roles", "role_id"),
        ("join_restrict_channels", "channel_id"),
    ]

    # -- DJ users --

    async def get_all_dj_users(self) -> dict[int, set[int]]:
        return await self._set_get_all("dj_users", "user_id")

    async def add_dj_user(self, guild_id: int, user_id: int):
        await self._set_add("dj_users", "user_id", guild_id, user_id)

    async def remove_dj_user(self, guild_id: int, user_id: int):
        await self._set_remove("dj_users", "user_id", guild_id, user_id)

    async def clear_dj_users(self, guild_id: int):
        await self._set_clear("dj_users", guild_id)

    # -- Excluded users/roles --

    async def get_all_excluded_users(self) -> dict[int, set[int]]:
        return await self._set_get_all("excluded_users", "user_id")

    async def get_all_excluded_roles(self) -> dict[int, set[int]]:
        return await self._set_get_all("excluded_roles", "role_id")

    async def add_excluded_user(self, guild_id: int, user_id: int):
        await self._set_add("excluded_users", "user_id", guild_id, user_id)

    async def remove_excluded_user(self, guild_id: int, user_id: int):
        await self._set_remove("excluded_users", "user_id", guild_id, user_id)

    async def add_excluded_role(self, guild_id: int, role_id: int):
        await self._set_add("excluded_roles", "role_id", guild_id, role_id)

    async def remove_excluded_role(self, guild_id: int, role_id: int):
        await self._set_remove("excluded_roles", "role_id", guild_id, role_id)

    async def clear_excluded_users(self, guild_id: int):
        await self._set_clear("excluded_users", guild_id)

    async def clear_excluded_roles(self, guild_id: int):
        await self._set_clear("excluded_roles", guild_id)

    # -- Admin users/roles (manage permissions) --

    async def get_all_admin_users(self) -> dict[int, set[int]]:
        return await self._set_get_all("admin_users", "user_id")

    async def get_all_admin_roles(self) -> dict[int, set[int]]:
        return await self._set_get_all("admin_roles", "role_id")

    async def add_admin_user(self, guild_id: int, user_id: int):
        await self._set_add("admin_users", "user_id", guild_id, user_id)

    async def remove_admin_user(self, guild_id: int, user_id: int):
        await self._set_remove("admin_users", "user_id", guild_id, user_id)

    async def add_admin_role(self, guild_id: int, role_id: int):
        await self._set_add("admin_roles", "role_id", guild_id, role_id)

    async def remove_admin_role(self, guild_id: int, role_id: int):
        await self._set_remove("admin_roles", "role_id", guild_id, role_id)

    async def clear_admin_users(self, guild_id: int):
        await self._set_clear("admin_users", guild_id)

    async def clear_admin_roles(self, guild_id: int):
        await self._set_clear("admin_roles", guild_id)

    # -- Song History --

    async def get_all_max_user_tracks(self) -> dict[int, int]:
        return await self._get_all("max_user_tracks", int)

    async def set_max_user_tracks(self, guild_id: int, value: int):
        await self._upsert(guild_id, "max_user_tracks", value)

    async def get_all_max_history(self) -> dict[int, int]:
        return await self._get_all("max_history", int)

    async def set_max_history(self, guild_id: int, value: int):
        await self._upsert(guild_id, "max_history", value)

    async def add_history_entry(self, guild_id: int, title: str | None, uploader: str | None,
                                duration: float | None, url: str, requester: int,
                                max_entries: int = 50):
        await self._ensure_init()
        async with self._transaction():
            await self._conn.execute(
                "INSERT INTO song_history (guild_id, title, uploader, duration, url, requester) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, title, uploader, duration, url, requester),
            )
            if max_entries > 0:
                async with self._conn.execute(
                    "SELECT COUNT(*) FROM song_history WHERE guild_id=?", (guild_id,)
                ) as cur:
                    count = (await cur.fetchone())[0]
                if count > max_entries:
                    await self._conn.execute(
                        "DELETE FROM song_history WHERE guild_id=? AND id NOT IN "
                        "(SELECT id FROM song_history WHERE guild_id=? ORDER BY played_at DESC LIMIT ?)",
                        (guild_id, guild_id, max_entries),
                    )

    async def get_history(self, guild_id: int, limit: int = 50) -> list[dict]:
        await self._ensure_init()
        async with self._conn.execute(
            "SELECT id, title, uploader, duration, url, requester, played_at "
            "FROM song_history WHERE guild_id=? ORDER BY played_at DESC LIMIT ?",
            (guild_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {"id": r[0], "title": r[1], "uploader": r[2], "duration": r[3],
             "url": r[4], "requester": r[5], "played_at": r[6]}
            for r in rows
        ]

    async def clear_history(self, guild_id: int):
        await self._ensure_init()
        await self._exec_commit("DELETE FROM song_history WHERE guild_id=?", (guild_id,))

    # -- Playlist CRUD --

    async def create_playlist(self, guild_id: int, user_id: int, name: str) -> int | None:
        await self._ensure_init()
        try:
            async with self._transaction():
                async with self._conn.execute(
                    "SELECT COUNT(*) FROM playlists WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                ) as cur:
                    count = (await cur.fetchone())[0]
                is_fav = 1 if count == 0 else 0
                async with self._conn.execute(
                    "INSERT INTO playlists (guild_id, user_id, name, is_favourite) VALUES (?, ?, ?, ?)",
                    (guild_id, user_id, name, is_fav),
                ) as cur:
                    playlist_id = cur.lastrowid
            return playlist_id
        except sqlite3.IntegrityError:
            return None

    async def delete_playlist(self, playlist_id: int):
        await self._ensure_init()
        await self._exec_commit("DELETE FROM playlists WHERE id=?", (playlist_id,))

    async def delete_all_playlists(self, guild_id: int, user_id: int):
        await self._ensure_init()
        await self._exec_commit(
            "DELETE FROM playlists WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )

    @staticmethod
    def _playlist_row(r) -> dict:
        return {"id": r[0], "name": r[1], "is_favourite": bool(r[2]), "track_count": r[3]}

    async def get_user_playlists(self, guild_id: int, user_id: int) -> list[dict]:
        await self._ensure_init()
        async with self._conn.execute(
            """
            SELECT p.id, p.name, p.is_favourite,
                   (SELECT COUNT(*) FROM playlist_tracks pt WHERE pt.playlist_id = p.id) AS track_count
            FROM playlists p
            WHERE p.guild_id=? AND p.user_id=?
            ORDER BY p.name
            """,
            (guild_id, user_id),
        ) as cur:
            rows = await cur.fetchall()
        return [self._playlist_row(r) for r in rows]

    async def get_playlist_by_name(self, guild_id: int, user_id: int, name: str) -> dict | None:
        await self._ensure_init()
        async with self._conn.execute(
            """
            SELECT p.id, p.name, p.is_favourite,
                   (SELECT COUNT(*) FROM playlist_tracks pt WHERE pt.playlist_id = p.id) AS track_count
            FROM playlists p
            WHERE p.guild_id=? AND p.user_id=? AND p.name=?
            """,
            (guild_id, user_id, name),
        ) as cur:
            r = await cur.fetchone()
        return self._playlist_row(r) if r else None

    async def set_favourite(self, guild_id: int, user_id: int, playlist_id: int):
        await self._ensure_init()
        async with self._transaction():
            await self._conn.execute(
                "UPDATE playlists SET is_favourite=0 WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            )
            await self._conn.execute(
                "UPDATE playlists SET is_favourite=1 WHERE id=? AND guild_id=? AND user_id=?",
                (playlist_id, guild_id, user_id),
            )

    async def get_favourite_playlist(self, guild_id: int, user_id: int) -> dict | None:
        await self._ensure_init()
        async with self._conn.execute(
            """
            SELECT p.id, p.name, p.is_favourite,
                   (SELECT COUNT(*) FROM playlist_tracks pt WHERE pt.playlist_id = p.id) AS track_count
            FROM playlists p
            WHERE p.guild_id=? AND p.user_id=?
            ORDER BY p.is_favourite DESC, p.created_at ASC
            LIMIT 1
            """,
            (guild_id, user_id),
        ) as cur:
            r = await cur.fetchone()
        return self._playlist_row(r) if r else None

    async def get_user_playlist_count(self, guild_id: int, user_id: int) -> int:
        await self._ensure_init()
        async with self._conn.execute(
            "SELECT COUNT(*) FROM playlists WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        ) as cur:
            return (await cur.fetchone())[0]

    # -- Playlist track CRUD --

    async def get_playlist_tracks(self, playlist_id: int, offset: int = 0, limit: int | None = None) -> list[dict]:
        await self._ensure_init()
        sql = "SELECT position, title, uploader, duration, url, is_live FROM playlist_tracks WHERE playlist_id=? ORDER BY position"
        params: list = [playlist_id]
        if limit is not None:
            sql += " LIMIT ? OFFSET ?"
            params += [limit, offset]
        elif offset:
            sql += " LIMIT -1 OFFSET ?"
            params.append(offset)
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [{"position": r[0], "title": r[1], "uploader": r[2], "duration": r[3], "url": r[4], "is_live": bool(r[5])} for r in rows]

    async def add_playlist_tracks(self, playlist_id: int, tracks: list[dict], max_tracks: int = 5000, *, user_id: int = 0) -> int:
        await self._ensure_init()
        lock = self._user_add_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._user_add_locks[user_id] = lock
        async with lock:
            async with self._transaction():
                async with self._conn.execute(
                    "SELECT COUNT(*), COALESCE(MAX(position), 0) FROM playlist_tracks WHERE playlist_id=?",
                    (playlist_id,),
                ) as cur:
                    current, max_pos = await cur.fetchone()
                space = max_tracks - current
                if space <= 0:
                    return 0
                to_add = tracks[:space]
                rows = [
                    (playlist_id, max_pos + i + 1, t.get("title"), t.get("uploader"), t.get("duration"), t["url"], 1 if t.get("is_live") else 0)
                    for i, t in enumerate(to_add)
                ]
                await self._conn.executemany(
                    "INSERT INTO playlist_tracks (playlist_id, position, title, uploader, duration, url, is_live) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                added = len(to_add)
        if not lock.locked():
            self._user_add_locks.pop(user_id, None)
        return added

    async def remove_playlist_track(self, playlist_id: int, position: int):
        await self._ensure_init()
        async with self._transaction():
            await self._conn.execute(
                "DELETE FROM playlist_tracks WHERE playlist_id=? AND position=?",
                (playlist_id, position),
            )
            await self._conn.execute(
                "UPDATE playlist_tracks SET position = position - 1 WHERE playlist_id=? AND position > ?",
                (playlist_id, position),
            )

    async def remove_playlist_tracks_range(self, playlist_id: int, from_pos: int, to_pos: int):
        if from_pos > to_pos:
            return
        await self._ensure_init()
        async with self._transaction():
            count = to_pos - from_pos + 1
            await self._conn.execute(
                "DELETE FROM playlist_tracks WHERE playlist_id=? AND position >= ? AND position <= ?",
                (playlist_id, from_pos, to_pos),
            )
            await self._conn.execute(
                "UPDATE playlist_tracks SET position = position - ? WHERE playlist_id=? AND position > ?",
                (count, playlist_id, to_pos),
            )

    async def remove_playlist_tracks_by_positions(self, playlist_id: int, positions: list[int]):
        if not positions:
            return
        await self._ensure_init()
        async with self._transaction():
            placeholders = ",".join("?" for _ in positions)
            await self._conn.execute(
                f"DELETE FROM playlist_tracks WHERE playlist_id=? AND position IN ({placeholders})",
                [playlist_id, *positions],
            )
            # Resequence remaining tracks
            async with self._conn.execute(
                "SELECT id FROM playlist_tracks WHERE playlist_id=? ORDER BY position",
                (playlist_id,),
            ) as cur:
                rows = await cur.fetchall()
            if rows:
                await self._conn.executemany(
                    "UPDATE playlist_tracks SET position = ? WHERE id = ?",
                    [(i + 1, row[0]) for i, row in enumerate(rows)],
                )

    async def clear_playlist_tracks(self, playlist_id: int):
        await self._ensure_init()
        await self._exec_commit("DELETE FROM playlist_tracks WHERE playlist_id=?", (playlist_id,))

    async def move_playlist_tracks(self, playlist_id: int, from_pos: int, to_pos: int, dest_pos: int | None = None):
        await self._ensure_init()
        async with self._transaction():
            async with self._conn.execute(
                "SELECT id, position FROM playlist_tracks WHERE playlist_id=? ORDER BY position",
                (playlist_id,),
            ) as cur:
                rows = await cur.fetchall()
            if not rows:
                return
            pos_to_idx = {r[1]: i for i, r in enumerate(rows)}
            ids = [r[0] for r in rows]
            old_pos = {r[0]: r[1] for r in rows}
            if dest_pos is None:
                fi = pos_to_idx.get(from_pos)
                ti = pos_to_idx.get(to_pos)
                if fi is None or ti is None or fi == ti:
                    return
                item = ids.pop(fi)
                ids.insert(ti, item)
            else:
                fi = pos_to_idx.get(from_pos)
                ti = pos_to_idx.get(to_pos)
                di = pos_to_idx.get(dest_pos)
                if fi is None or ti is None or di is None:
                    return
                chunk = ids[fi:ti + 1]
                del ids[fi:ti + 1]
                if di > ti:
                    di -= len(chunk)
                ins = max(0, min(di, len(ids)))
                for i, item_id in enumerate(chunk):
                    ids.insert(ins + i, item_id)
            updates = [
                (new_pos, track_id)
                for new_pos, track_id in enumerate(ids, 1)
                if old_pos[track_id] != new_pos
            ]
            if updates:
                await self._conn.executemany(
                    "UPDATE playlist_tracks SET position = ? WHERE id = ?",
                    updates,
                )

    async def copy_playlist(self, source_playlist_id: int, guild_id: int, user_id: int, new_name: str, *, max_tracks: int = 0) -> int | None:
        await self._ensure_init()
        try:
            async with self._transaction():
                async with self._conn.execute(
                    "SELECT COUNT(*) FROM playlists WHERE guild_id=? AND user_id=?",
                    (guild_id, user_id),
                ) as cur:
                    count = (await cur.fetchone())[0]
                is_fav = 1 if count == 0 else 0
                async with self._conn.execute(
                    "INSERT INTO playlists (guild_id, user_id, name, is_favourite) VALUES (?, ?, ?, ?)",
                    (guild_id, user_id, new_name, is_fav),
                ) as cur:
                    new_id = cur.lastrowid
                limit_clause = f"LIMIT {int(max_tracks)}" if max_tracks > 0 else ""
                await self._conn.execute(
                    f"""
                    INSERT INTO playlist_tracks (playlist_id, position, title, uploader, duration, url, is_live)
                    SELECT ?, ROW_NUMBER() OVER (ORDER BY position), title, uploader, duration, url, is_live
                    FROM playlist_tracks WHERE playlist_id=? ORDER BY position {limit_clause}
                    """,
                    (new_id, source_playlist_id),
                )
                return new_id
        except sqlite3.IntegrityError:
            return None

    async def append_tracks_to_playlist(self, source_playlist_id: int, dest_playlist_id: int, max_tracks: int = 0) -> int:
        """Copy tracks from source playlist and append to dest playlist, respecting max_tracks limit. Returns count of tracks added."""
        await self._ensure_init()
        async with self._transaction():
            async with self._conn.execute(
                "SELECT COUNT(*), COALESCE(MAX(position), 0) FROM playlist_tracks WHERE playlist_id=?",
                (dest_playlist_id,),
            ) as cur:
                current, max_pos = await cur.fetchone()
            if max_tracks > 0:
                space = max_tracks - current
                if space <= 0:
                    return 0
            else:
                space = None
            async with self._conn.execute(
                "SELECT title, uploader, duration, url, is_live FROM playlist_tracks WHERE playlist_id=? ORDER BY position",
                (source_playlist_id,),
            ) as cur:
                rows = await cur.fetchall()
            if not rows:
                return 0
            if space is not None:
                rows = rows[:space]
            values = [(dest_playlist_id, max_pos + i + 1, r[0], r[1], r[2], r[3], r[4]) for i, r in enumerate(rows)]
            await self._conn.executemany(
                "INSERT INTO playlist_tracks (playlist_id, position, title, uploader, duration, url, is_live) VALUES (?, ?, ?, ?, ?, ?, ?)",
                values,
            )
            return len(rows)

    # -- Timeouts --

    async def get_all_timeouts(self) -> list[tuple[int, int, float]]:
        await self._ensure_init()
        async with self._conn.execute(
            "SELECT guild_id, user_id, expires_at FROM timeouts"
        ) as cur:
            return [(int(r[0]), int(r[1]), float(r[2])) for r in await cur.fetchall()]

    async def set_timeout(self, guild_id: int, user_id: int, expires_at: float):
        await self._ensure_init()
        await self._exec_commit(
            "INSERT INTO timeouts (guild_id, user_id, expires_at) VALUES (?, ?, ?) "
            "ON CONFLICT(guild_id, user_id) DO UPDATE SET expires_at=excluded.expires_at",
            (guild_id, user_id, expires_at),
        )

    async def remove_timeout(self, guild_id: int, user_id: int):
        await self._ensure_init()
        await self._exec_commit(
            "DELETE FROM timeouts WHERE guild_id=? AND user_id=?",
            (guild_id, user_id),
        )

    async def cleanup_expired_timeouts(self, now: float) -> int:
        await self._ensure_init()
        async with self._tx_lock:
            async with self._conn.execute(
                "DELETE FROM timeouts WHERE expires_at <= ?", (now,)
            ) as cur:
                count = cur.rowcount
            await self._conn.commit()
        return count


    async def get_playlist_by_id(self, playlist_id: int) -> dict | None:
        await self._ensure_init()
        async with self._conn.execute(
            "SELECT p.id, p.guild_id, p.user_id, p.name, p.is_favourite, "
            "(SELECT COUNT(*) FROM playlist_tracks pt WHERE pt.playlist_id = p.id) "
            "FROM playlists p WHERE p.id=?",
            (playlist_id,),
        ) as cur:
            row = await cur.fetchone()
        if not row:
            return None
        return {"id": row[0], "guild_id": row[1], "user_id": row[2], "name": row[3], "is_favourite": bool(row[4]), "track_count": row[5]}

    async def rename_playlist(self, playlist_id: int, new_name: str) -> bool:
        await self._ensure_init()
        try:
            await self._exec_commit(
                "UPDATE playlists SET name=? WHERE id=?",
                (new_name, playlist_id),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    async def wal_checkpoint(self):
        await self._ensure_init()
        async with self._tx_lock:
            await self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    async def export_guild_settings(self, guild_id: int) -> dict:
        await self._ensure_init()
        import json as _json
        result: dict = {"guild_id": guild_id}
        # --- guild_settings (all columns, dynamic) ---
        async with self._conn.execute("PRAGMA table_info(guild_settings)") as cur:
            all_cols = [row[1] for row in await cur.fetchall() if row[1] != "guild_id"]
        if all_cols:
            cols_str = ", ".join(all_cols)
            async with self._conn.execute(
                f"SELECT {cols_str} FROM guild_settings WHERE guild_id=?", (guild_id,)
            ) as cur:
                row = await cur.fetchone()
            if row:
                for col, val in zip(all_cols, row):
                    if val is None:
                        continue
                    if isinstance(val, str) and val.startswith(("{", "[")):
                        try:
                            result[col] = _json.loads(val)
                            continue
                        except Exception:
                            pass
                    result[col] = val
        # --- entity tables (dj_users, excluded_*, admin_*) ---
        for table, col in self._ENTITY_TABLES:
            async with self._conn.execute(
                f"SELECT {col} FROM {table} WHERE guild_id=?", (guild_id,)
            ) as cur:
                ids = [int(r[0]) for r in await cur.fetchall()]
            if ids:
                result[table] = ids
        return result

    async def export_bot_settings(self) -> dict:
        await self._ensure_init()
        result: dict = {}
        activity = await self.get_bot_activity()
        result["bot_activity"] = activity
        lst = await self.get_bot_activity_list()
        if lst:
            result["bot_activity_list"] = [{"type": item["type"], "text": item["text"]} for item in lst]
        result["max_workers"] = await self.get_max_workers()
        result["prefetch"] = int(await self.get_prefetch())
        result["safe_prefetch"] = int(await self.get_safe_prefetch())
        result["silent_log"] = int(await self.get_silent_log())
        return result

    _IMPORT_ALIASES: dict[str, str] = {"ytdlp_silent": "silent_log"}
    _IMPORT_SKIP_KEYS: set[str] = {"bot_activity", "bot_activity_list", "max_workers", "prefetch", "safe_prefetch", "silent_log"}
    _IMPORT_GUILD_SPECIFIC_KEYS: set[str] = {"view_channel", "dj_role_id"}

    _IMPORT_VALID_ENUMS: dict[str, set[str]] = {
        "vote_mode": {"half", "half_plus_one"},
        "pause_permission": {"everyone", "requester_dj", "dj", "admin", "owner"},
        "pause_timeout_behavior": {"leave", "continue", "skip"},
        "join_restrict_level": {"none", "users", "dj", "admin"},
        "seek_permission": {"everyone", "requester_dj", "dj", "admin", "owner"},
        "radio_permission": {"everyone", "dj", "admin", "owner"},
        "radio_edit_permission": {"dj", "admin", "owner"},
        "force_play_permission": {"dj", "admin", "owner"},
        "force_radio": {"disabled", "enabled"},
        "track_limit_target": {"users", "dj", "admin"},
        "live_permission": {"owner", "admin", "dj", "everyone"},
    }
    _IMPORT_VALID_RANGES: dict[str, tuple[int, int]] = {
        "delete_after": (0, 180),
        "embed_color": (0, 0xFFFFFF),
        "max_playlists": (0, 25),
        "queue_per_page": (5, 15),
        "max_history": (0, 200),
        "max_user_tracks": (0, 10000),
        "track_limit_users": (0, 10000),
        "track_limit_dj": (0, 10000),
        "track_limit_admin": (0, 10000),
        "pause_timeout": (60, 3600),
        "idle_disconnect_timeout": (0, 604800),
        "radio_cooldown": (1, 15),
        "timezone": (-12, 14),
        "max_seeks_per_track": (0, 60),
        "max_seeks_dj": (0, 60),
        "queue_limit": (100, 10000),
        "playlist_track_limit": (100, 10000),
        "view_restrict": (0, 3),
        "live_max_hours": (0, 24),
    }
    _IMPORT_VALID_BOOLEANS: frozenset[str] = frozenset({
        "silent_log", "compact_mode", "queue_compact", "queue_button_compact",
        "admin_priv", "prefetch", "safe_prefetch", "vote_exclude_deafened",
        "live_enabled",
    })

    async def import_guild_settings(self, guild_id: int, data: dict):
        await self._ensure_init()
        import json as _json
        same_guild = data.get("guild_id") == guild_id
        async with self._conn.execute("PRAGMA table_info(guild_settings)") as cur:
            col_info = {row[1]: row[2].upper() for row in await cur.fetchall()}
        entity_names = {t[0] for t in self._ENTITY_TABLES}
        async with self._transaction():
            for json_key, val in data.items():
                db_col = self._IMPORT_ALIASES.get(json_key, json_key)
                if db_col == "guild_id" or db_col in entity_names or db_col in self._IMPORT_SKIP_KEYS:
                    continue
                if not same_guild and db_col in self._IMPORT_GUILD_SPECIFIC_KEYS:
                    continue
                if db_col not in col_info:
                    continue
                # --- value validation ---
                if db_col in self._IMPORT_VALID_ENUMS:
                    if not isinstance(val, str) or val not in self._IMPORT_VALID_ENUMS[db_col]:
                        continue
                elif db_col in self._IMPORT_VALID_RANGES:
                    try:
                        val = int(val)
                    except (ValueError, TypeError):
                        continue
                    lo, hi = self._IMPORT_VALID_RANGES[db_col]
                    if db_col == "delete_after" and val != 0 and not (5 <= val <= hi):
                        continue
                    elif db_col != "delete_after" and not (lo <= val <= hi):
                        continue
                elif db_col in self._IMPORT_VALID_BOOLEANS:
                    try:
                        val = int(bool(int(val)))
                    except (ValueError, TypeError):
                        continue
                if isinstance(val, (dict, list)):
                    val = _json.dumps(val, separators=(",", ":"))
                elif isinstance(val, bool):
                    val = int(val)
                await self._conn.execute(
                    f"INSERT INTO guild_settings (guild_id, {db_col}) VALUES (?, ?) "
                    f"ON CONFLICT(guild_id) DO UPDATE SET {db_col}=excluded.{db_col}",
                    (guild_id, val),
                )
            if same_guild:
                for table, col in self._ENTITY_TABLES:
                    if table in data and isinstance(data[table], list):
                        await self._conn.execute(f"DELETE FROM {table} WHERE guild_id=?", (guild_id,))
                        valid_ids = []
                        for eid in data[table]:
                            try:
                                valid_ids.append((guild_id, int(eid)))
                            except (ValueError, TypeError):
                                pass
                        if valid_ids:
                            await self._conn.executemany(
                                f"INSERT OR IGNORE INTO {table} (guild_id, {col}) VALUES (?, ?)",
                                valid_ids,
                            )



    async def reset_guild_settings(self, guild_id: int):
        await self._ensure_init()
        async with self._transaction():
            await self._conn.execute("DELETE FROM guild_settings WHERE guild_id=?", (guild_id,))
            for table, _col in self._ENTITY_TABLES:
                await self._conn.execute(f"DELETE FROM {table} WHERE guild_id=?", (guild_id,))
            await self._conn.execute("DELETE FROM active_views WHERE guild_id=?", (guild_id,))
            await self._conn.execute("DELETE FROM timeouts WHERE guild_id=?", (guild_id,))

    async def import_bot_settings(self, data: dict):
        await self._ensure_init()
        async with self._transaction():
            if "bot_activity" in data and isinstance(data["bot_activity"], dict):
                act = data["bot_activity"]
                try:
                    atype = int(act.get("type", 2))
                    if atype not in (0, 2, 3, 5):
                        atype = 2
                    atext = str(act.get("text", "/play"))
                    mode = str(act.get("mode", "static"))
                    if mode not in ("static", "random", "ordered"):
                        mode = "static"
                    interval = max(1, min(10080, int(act.get("interval", 120))))
                    selected = int(act.get("selected", 0))
                except (ValueError, TypeError):
                    atype, atext, mode, interval, selected = 2, "/play", "static", 120, 0
                await self._conn.execute(
                    "INSERT INTO bot_activity (id, activity_type, activity_text, activity_mode, activity_interval, activity_selected) "
                    "VALUES (1, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                    "activity_type=excluded.activity_type, activity_text=excluded.activity_text, "
                    "activity_mode=excluded.activity_mode, activity_interval=excluded.activity_interval, "
                    "activity_selected=excluded.activity_selected",
                    (atype, atext, mode, interval, selected),
                )
            if "bot_activity_list" in data and isinstance(data["bot_activity_list"], list):
                await self._conn.execute("DELETE FROM bot_activity_list")
                activity_rows = []
                for pos, item in enumerate(data["bot_activity_list"]):
                    if isinstance(item, dict) and "type" in item and "text" in item:
                        try:
                            at = int(item["type"])
                            if at not in (0, 2, 3, 5):
                                continue
                            activity_rows.append((pos, at, str(item["text"])))
                        except (ValueError, TypeError):
                            continue
                if activity_rows:
                    await self._conn.executemany(
                        "INSERT INTO bot_activity_list (position, activity_type, activity_text) VALUES (?, ?, ?)",
                        activity_rows,
                    )
                    # clamp selected index to valid range
                    await self._conn.execute(
                        "UPDATE bot_activity SET activity_selected = MIN(activity_selected, ?) WHERE id=1",
                        (max(0, len(activity_rows) - 1),),
                    )
            for gkey in ("max_workers", "prefetch", "safe_prefetch", "silent_log"):
                if gkey not in data:
                    continue
                raw = data[gkey]
                if gkey == "max_workers":
                    try:
                        val = int(raw)
                    except (ValueError, TypeError):
                        continue
                    val = max(1, min(32, val))
                    await self._conn.execute(
                        "INSERT INTO guild_settings (guild_id, max_workers) VALUES (0, ?) "
                        "ON CONFLICT(guild_id) DO UPDATE SET max_workers=excluded.max_workers",
                        (val,),
                    )
                else:
                    try:
                        val = int(bool(int(raw)))
                    except (ValueError, TypeError):
                        continue
                    await self._conn.execute(
                        f"INSERT INTO guild_settings (guild_id, {gkey}) VALUES (0, ?) "
                        f"ON CONFLICT(guild_id) DO UPDATE SET {gkey}=excluded.{gkey}",
                        (val,),
                    )

    async def reset_bot_settings(self):
        await self._ensure_init()
        async with self._transaction():
            await self._conn.execute("DELETE FROM bot_activity_list")
            await self._conn.execute(
                "INSERT INTO bot_activity (id, activity_type, activity_text, activity_mode, activity_interval, activity_selected) "
                "VALUES (1, 2, '/play', 'static', 120, 0) "
                "ON CONFLICT(id) DO UPDATE SET "
                "activity_type=2, activity_text='/play', activity_mode='static', "
                "activity_interval=120, activity_selected=0"
            )
            await self._conn.execute(
                "INSERT INTO guild_settings (guild_id, silent_log, prefetch, safe_prefetch, max_workers) "
                "VALUES (0, 0, 1, 1, 16) ON CONFLICT(guild_id) DO UPDATE SET "
                "silent_log=0, prefetch=1, safe_prefetch=1, max_workers=16"
            )

    async def delete_guild_data(self, guild_id: int):
        await self._ensure_init()
        async with self._transaction():
            await self._conn.execute("DELETE FROM playlists WHERE guild_id=?", (guild_id,))
            await self._conn.execute("DELETE FROM guild_settings WHERE guild_id=?", (guild_id,))
            await self._conn.execute("DELETE FROM active_views WHERE guild_id=?", (guild_id,))
            await self._conn.execute("DELETE FROM song_history WHERE guild_id=?", (guild_id,))
            await self._conn.execute("DELETE FROM timeouts WHERE guild_id=?", (guild_id,))
            for table, _col in self._ENTITY_TABLES:
                await self._conn.execute(f"DELETE FROM {table} WHERE guild_id=?", (guild_id,))

    async def get_all_guild_ids(self) -> set[int]:
        await self._ensure_init()
        async with self._conn.execute(
            "SELECT guild_id FROM guild_settings WHERE guild_id != 0 UNION SELECT DISTINCT guild_id FROM playlists WHERE guild_id != 0"
        ) as cur:
            return {int(row[0]) for row in await cur.fetchall()}


db = Database()