#!/usr/bin/env python3
"""
Validates locale files against Discord component character limits.

Usage:
    python locales/check_locales.py              # check all locales
    python locales/check_locales.py tr.json      # check a specific locale
"""

import json
import re
import sys
from pathlib import Path

LOCALES_DIR = Path(__file__).parent
REFERENCE = "en-US.json"

# --- Discord character limits ---
MODAL_TITLE            = 45
TEXT_INPUT_LABEL        = 45
TEXT_INPUT_PLACEHOLDER  = 100
BUTTON_LABEL            = 80
SELECT_PLACEHOLDER      = 150
SELECT_OPTION_LABEL     = 100
SELECT_OPTION_DESC      = 100
EMBED_TITLE             = 256
EMBED_FIELD_NAME        = 256
LABEL_TEXT              = 45   # discord.ui.Label text

# --- Keys and their limits ---
# Gathered from music_cog.py and core/music_handlers.py component usage.
# When a key appears in multiple categories, _r() keeps the tighter limit.

LIMITS: dict[str, tuple[int, str]] = {}

def _r(keys: list[str], limit: int, category: str):
    for k in keys:
        existing = LIMITS.get(k)
        if existing and existing[0] <= limit:
            continue                       # keep the tighter limit
        LIMITS[k] = (limit, category)

# Modal title (45)
_r([
    "PL_RENAME_TITLE", "QUEUE_GOTO_TITLE", "QUEUE_SEARCH_TITLE",
    "BUTTON_PLAY_ALL", "PL_COPY_TITLE", "QUEUE_SELECT_TITLE",
    "MP_ADD_TRACK_TITLE", "BUTTON_REMOVE", "BUTTON_MOVE",
    "LOOP_MODAL_TITLE", "BUTTON_SHUFFLE", "CLEAR_MODAL_TITLE",
    "PAUSE_TIMEOUT_TITLE", "BOT_CONN_IDLE_TIMEOUT", "RADIO_COOLDOWN_TITLE",
    "SETTINGS_IMPORT_GUILD", "SETTINGS_IMPORT_BOT", "COLOR_CUSTOM_TITLE", "SETTINGS_SHOW_QUEUE_DISPLAY",
    "EMBED_LAYOUT_REORDER_TITLE", "MP_FIELD_REORDER_TITLE",
    "ACTIVITY_ADD_TITLE", "ACTIVITY_EDIT_INDEX_TITLE",
    "ACTIVITY_EDIT_TEXT_TITLE", "ACTIVITY_SELECT_TITLE",
    "ACTIVITY_ORDER_TITLE", "ACTIVITY_REMOVE_TITLE", "ACTIVITY_TIME_TITLE",
    "BUTTON_ADD_TRACK", "PL_CREATE_TITLE", "PL_OPT_SWITCH",
    "PL_OPT_DELETE", "PL_OPT_FAVOURITE", "PL_OPT_SHARE",
    "PL_OPT_MANAGE_GUILD", "RADIO_QUERY_MODAL_TITLE",
    "SETTINGS_SHOW_DELETE_AFTER",
    "SETTINGS_SHOW_MAX_PLAYLISTS", "SETTINGS_MAX_HISTORY_TITLE",
    "TRACK_LIMIT_GROUP_USERS", "TRACK_LIMIT_GROUP_DJ",
    "TRACK_LIMIT_GROUP_ADMIN", "PL_IMPORT_TITLE",
    "QUEUE_LIMIT_TITLE", "PL_TRACK_LIMIT_BUTTON", "LIVE_MAX_HOURS",
    "SEEK_LIMIT_TITLE", "SEEK_LIMIT_DJ_TITLE",
    "RADIO_LIMITS_TRACK", "RADIO_LIMITS_TIME",
    "SETTINGS_SHOW_MAX_WORKERS",
    "MNG_UPDATES_INTERVAL_TITLE", "MNG_UPDATES_FIXED_TITLE",
], MODAL_TITLE, "Modal title")

# Label text - discord.ui.Label(text=...) inside modals (45)
_r([
    "LOOP_MODE_LABEL", "CLEAR_SCOPE_LABEL", "CLEAR_USER_LABEL",
    "PL_SELECT_LABEL", "PL_DELETE_SCOPE_LABEL", "SHUFFLE_LABEL",
    "PL_MANAGE_SELECT_USER",
    "FORCE_PLAY_LABEL", "SETTINGS_IMPORT_FILE_LABEL",
], LABEL_TEXT, "Label text")

# TextInput label (45)
_r([
    "PL_RENAME_LABEL", "QUEUE_GOTO_LABEL", "QUEUE_SEARCH_LABEL",
    "PL_COPY_LABEL", "QUEUE_SELECT_LABEL", "OPT_SELECT_INDEXTO",
    "PL_ADD_TRACK_LABEL", "OPT_REMOVE_INDEX", "OPT_REMOVE_INDEXTO",
    "REMOVE_SEARCH_LABEL", "OPT_MOVE_FROM", "OPT_MOVE_DEST", "OPT_MOVE_TO",
    "OPT_SHUFFLE_FROM", "OPT_SHUFFLE_TO",
    "OPT_SEEK_HOURS", "OPT_SEEK_MINUTES", "OPT_SEEK_SECONDS",
    "COLOR_CUSTOM_LABEL",
    "SETTINGS_DJ_REMOVE_INDEX", "SETTINGS_DJ_REMOVE_INDEXTO",
    "EMBED_LAYOUT_BUTTON_ID", "EMBED_LAYOUT_ROW_LABEL", "EMBED_LAYOUT_COL_LABEL",
    "MP_FIELD_ID_LABEL", "MP_FIELD_NEW_POS_LABEL",
    "ACTIVITY_ADD_LABEL", "ACTIVITY_EDIT_INDEX_LABEL",
    "ACTIVITY_EDIT_TEXT_LABEL", "ACTIVITY_SELECT_LABEL",
    "ACTIVITY_ORDER_FROM_LABEL", "ACTIVITY_ORDER_TO_LABEL",
    "ACTIVITY_REMOVE_LABEL", "ACTIVITY_REMOVE_TO_LABEL",
    "PL_CREATE_LABEL", "RADIO_QUERY_LABEL",
    # Format-string labels
    "SETTINGS_MAX_HISTORY_LABEL", "PAUSE_TIMEOUT_LABEL",
    "BOT_CONN_IDLE_LABEL", "RADIO_COOLDOWN_LABEL",
    "SETTINGS_QD_PER_PAGE_LABEL",
    "SETTINGS_DELETE_AFTER_LABEL", "SETTINGS_MAX_PL_LABEL",
    "ACTIVITY_TIME_LABEL",
    "QUEUE_LIMIT_LABEL", "PL_TRACK_LIMIT_LABEL", "LIVE_MAX_HOURS_LABEL",
    "SEEK_LIMIT_LABEL", "SEEK_LIMIT_DJ_LABEL",
    "RADIO_TRACK_LIMIT_LABEL", "RADIO_TIME_LIMIT_LABEL",
    "MNG_UPDATES_INTERVAL_LABEL", "MNG_UPDATES_FIXED_LABEL",
], TEXT_INPUT_LABEL, "TextInput label")

# TextInput placeholder (100)
_r([
    "QUEUE_SEARCH_PLACEHOLDER", "OPT_PLAY_QUERY",
    "REMOVE_SEARCH_PLACEHOLDER", "COLOR_CUSTOM_PLACEHOLDER",
    "RADIO_QUERY_LABEL",
    "RADIO_TRACK_LIMIT_PLACEHOLDER", "RADIO_TIME_LIMIT_PLACEHOLDER",
], TEXT_INPUT_PLACEHOLDER, "TextInput placeholder")

# Button label (80)
_r([
    "BUTTON_ADD_TRACK", "BUTTON_CANCEL", "BUTTON_FIRST", "BUTTON_LAST",
    "BUTTON_NEXT", "BUTTON_NEXT_PAGE", "BUTTON_PREV", "BUTTON_PREV_PAGE",
    "BUTTON_REFRESH", "BUTTON_SHUFFLE", "BUTTON_SKIP", "BUTTON_TOGGLE",
    "BUTTON_SELECT", "BUTTON_GOTO_PAGE", "BUTTON_SEARCH",
    "BUTTON_STOP", "BUTTON_REMOVE", "BUTTON_MOVE", "BUTTON_LOOP",
    "BUTTON_SEEK", "BUTTON_CLEAR_QUEUE", "BUTTON_PLAY_ALL",
    "BUTTON_PLAY_SHUFFLE", "BUTTON_COPY", "BUTTON_PREV_TRACK",
    "SETTINGS_BACK", "SETTINGS_EXPORT_GUILD", "SETTINGS_IMPORT_GUILD",
    "SETTINGS_RESET_GUILD", "SETTINGS_RESET_YES",
    "SETTINGS_EXPORT_BOT", "SETTINGS_IMPORT_BOT", "SETTINGS_RESET_BOT",
    "SETTINGS_CLEAR_DJ_ROLE", "SETTINGS_CLEAR_DJ_USERS",
    "SETTINGS_DJ_SHOW_ALL", "VIEW_RESTRICT_SEND_VIEWS",
    "COLOR_CUSTOM_BUTTON", "MANAGE_PERMS_CLEAR_ALL",
    "EXCLUDED_CLEAR_USERS", "EXCLUDED_CLEAR_ROLES",
    "MANAGE_PERMS_CLEAR_ALL",
    "EMBED_LAYOUT_ENABLE_ALL", "EMBED_LAYOUT_DISABLE_ALL",
    "EMBED_LAYOUT_RESET", "EMBED_LAYOUT_REORDER",
    "ACTIVITY_ADD", "ACTIVITY_ADD_TEXT", "ACTIVITY_EDIT",
    "ACTIVITY_EDIT_TEXT", "ACTIVITY_RESET", "ACTIVITY_SELECT",
    "RADIO_START_BUTTON", "RADIO_EDIT_STOP", "RADIO_EDIT_REFRESH", "RADIO_EDIT_RESTART",
    "PL_COPY_NEW", "OWNER_CHANNEL_CONFIRM", "HELP_BACK_TO_MENU",
    "ACTIVITY_ORDER", "ACTIVITY_REMOVE", "ACTIVITY_TIME",
    "MANAGE_PERMS_ADMIN_PRIV", "VOTE_DEAFENED_EXCLUDE",
    "QUEUE_LIMIT_BUTTON", "SEEK_LIMIT_BUTTON", "SEEK_LIMIT_DJ_BUTTON",
    "FORCE_RADIO_BUTTON", "SETTINGS_SHOW_PREFETCH", "SETTINGS_SHOW_SAFE_PREFETCH",
    # Manage updates buttons
    "MNG_UPDATES_PKG_DISABLE", "MNG_UPDATES_PKG_ENABLE",
    "MNG_UPDATES_PKG_NOW", "MNG_UPDATES_BOT_DISABLE",
    "MNG_UPDATES_BOT_ENABLE", "MNG_UPDATES_BOT_NOW",
    "MNG_UPDATES_SET_INTERVAL", "MNG_UPDATES_SET_FIXED",
    "MNG_UPDATES_CHANGE_MODE", "MNG_UPDATES_RESTART",
    # Partial button label fragments (used inside f-strings)
    "ABBR_MINUTES", "ABBR_COMPACT", "DISABLED", "ENABLED",
    "FORCE_RADIO_ENABLED", "STATE_ON", "STATE_OFF",
], BUTTON_LABEL, "Button label")

# Select placeholder (150)
_r([
    "SELECT_OPTION_PLACEHOLDER", "LIMITS_SELECT_PLACEHOLDER",
    "PAUSE_PERM_PLACEHOLDER", "JOIN_RESTRICT_PLACEHOLDER",
    "JOIN_RESTRICT_CHANNEL_PLACEHOLDER", "PAUSE_BEHAVIOR_PLACEHOLDER",
    "RADIO_PERM_PLACEHOLDER", "SETTINGS_SELECT_PLACEHOLDER",
    "LANG_SELECT_PLACEHOLDER", "COLOR_SELECT_PLACEHOLDER",
    "SETTINGS_DJ_ROLE_TITLE", "SETTINGS_SHOW_DJ_USERS",
    "SETTINGS_SHOW_VIEW_RESTRICT",
    "LIMIT_USAGE_USER_PLACEHOLDER", "LIMIT_USAGE_ROLE_PLACEHOLDER",
    "MANAGE_PERMS_USER_PLACEHOLDER", "MANAGE_PERMS_ROLE_PLACEHOLDER",
    "EMBED_VIEWS_SELECT_PLACEHOLDER", "EMBED_LAYOUT_TOGGLE_PLACEHOLDER",
    "MP_FIELD_TOGGLE_PLACEHOLDER",
    "ACTIVITY_TYPE_PLACEHOLDER", "ACTIVITY_MODE_PLACEHOLDER",
    "PL_OPTIONS_PLACEHOLDER", "HELP_SELECT_PLACEHOLDER",
    "RADIO_SOURCE_PLACEHOLDER",
    "PL_COPY_DEST_PLACEHOLDER", "LIVE_PERMISSION",
    "SEEK_PERM_PLACEHOLDER", "RADIO_EDIT_PERM_PLACEHOLDER",
    "FORCE_PERM_PLACEHOLDER", "TIMEZONE_PLACEHOLDER",
    "QUEUE_FOOTER_TOGGLE_PLACEHOLDER", "RADIO_LIMITS_PLACEHOLDER",
    "MNG_SELECT_PLACEHOLDER",
], SELECT_PLACEHOLDER, "Select placeholder")

# SelectOption / RadioGroupOption label (100)
_r([
    # Radio config
    "RADIO_SOURCE_QUEUE", "RADIO_SOURCE_HISTORY", "RADIO_SOURCE_QUERY",
    # Activity mode
    "ACTIVITY_MODE_STATIC", "ACTIVITY_MODE_RANDOM", "ACTIVITY_MODE_ORDERED",
    # Pause permission
    "PAUSE_PERM_EVERYONE", "PERM_REQUESTER_DJ", "PERM_ADMIN_ONLY",
    # Pause behavior
    "PAUSE_BEHAVIOR_LEAVE", "PAUSE_BEHAVIOR_CONTINUE", "PAUSE_BEHAVIOR_SKIP",
    # Clear modal radio group
    "CLEAR_ALL_OPTION", "CLEAR_MINE_OPTION", "CLEAR_USER_OPTION",
    # Playlist delete radio group
    "PL_DELETE_ONE_OPTION", "PL_DELETE_ALL_OPTION",
    # Settings LimitsSelect
    "SETTINGS_SHOW_MAX_PLAYLISTS", "SETTINGS_SHOW_MAX_HISTORY",
    "SETTINGS_SHOW_TRACK_LIMIT", "SETTINGS_SHOW_LIMIT_USAGE",
    "SETTINGS_SHOW_PAUSE_CONTROL", "SETTINGS_SHOW_RADIO",
    "SETTINGS_SHOW_BOT_CONNECTION",
    # Settings main select
    "SETTINGS_SHOW_LANGUAGE",
    "SETTINGS_SHOW_VOTE_MODE", "SETTINGS_SHOW_DJ",
    "SETTINGS_SHOW_DELETE_AFTER", "SETTINGS_SHOW_SILENT_LOG",
    "SETTINGS_SHOW_EMBED_VIEW_SETTINGS",
    "SETTINGS_SHOW_LIMITS", "SETTINGS_SHOW_MANAGE_PERMS",
    "SETTINGS_SHOW_BOT_ACTIVITY",
    # Embed views sub-select
    "SETTINGS_SHOW_EMBED_COLOR", "EMBED_VIEWS_MP_BUTTONS",
    "SETTINGS_SHOW_MP_DISPLAY", "EMBED_VIEWS_QUEUE_BUTTONS",
    "SETTINGS_SHOW_QUEUE_DISPLAY", "SETTINGS_SHOW_VIEW_RESTRICT",
    # Vote mode select
    "VOTE_MODE_HALF", "VOTE_MODE_HALF_PLUS_ONE",
    # Join restrict select
    "JOIN_RESTRICT_NONE", "JOIN_RESTRICT_USERS",
    "JOIN_RESTRICT_DJ", "JOIN_RESTRICT_ADMIN",
    # View restrict select
    "VIEW_RESTRICT_DISABLED", "VIEW_RESTRICT_NON_DJ",
    "VIEW_RESTRICT_DJ_USER", "VIEW_RESTRICT_ALL",
    # Loop mode select (inside modal)
    "LOOP_OFF", "LOOP_SONG", "LOOP_QUEUE",
    # Activity type select
    "ACTIVITY_PLAYING", "ACTIVITY_LISTENING",
    "ACTIVITY_WATCHING", "ACTIVITY_COMPETING",
    # Search results select
    "SEARCH_OPTION",
    # Playlist options select
    "BUTTON_ADD_TRACK", "PL_OPT_ADD_CURRENT", "PL_OPT_ADD_QUEUE",
    "PL_CREATE_TITLE", "PL_OPT_CLEAR", "PL_OPT_EDIT_NAME",
    "PL_OPT_EXPORT", "PL_OPT_IMPORT",
    # Shared permission labels
    "PERM_EVERYONE", "PERM_DJ_ADMIN", "PERM_ADMIN_ONLY", "PERM_OWNER_ONLY",
    # Pause permission
    "PERM_DJ_ADMIN",
    # Seek permission
    "PERM_REQUESTER_DJ", "PERM_DJ_ADMIN", "PERM_ADMIN_ONLY",
    # Settings LimitsSelect
    "SETTINGS_SHOW_FORCE_PLAY", "SETTINGS_SHOW_LIVE_PLAYBACK",
    "SETTINGS_SHOW_PERFORMANCE",
    # Settings main select
    "SETTINGS_SHOW_TIMEZONE",
    # Radio limits select
    "RADIO_LIMITS_TRACK", "RADIO_LIMITS_TIME",
    # Manage select
    "OPT_MNG_CLEAR_VOTES", "OPT_MNG_GC", "OPT_MNG_PING",
    "OPT_MNG_RESET_VOICE", "OPT_MNG_RELOAD_SETTINGS",
    "OPT_MNG_RELOAD_LOCALES", "OPT_MNG_CANCEL_FETCHES",
    "OPT_MNG_PURGE_STALE", "OPT_MNG_FORCE_RESYNC", "OPT_MNG_UPDATES",
    # Help category select (also used as embed titles)
    "HELP_CAT_COMMANDS", "HELP_CAT_PLAYLIST",
    "HELP_CAT_DJ_VOTING", "HELP_CAT_ADMIN", "HELP_CAT_MANAGE",
    # MP field toggle select
    "MP_FIELD_THUMBNAIL", "MP_FIELD_VOTE_INFO",
    # Queue footer toggle select
    "QUEUE_FOOTER_TOTAL_DURATION", "QUEUE_FOOTER_PLAYING_SINCE",
    "SETTINGS_ACTIVE", "SETTINGS_INACTIVE",
    "SETTINGS_QD_NORMAL", "SETTINGS_QD_SPACIOUS",
], SELECT_OPTION_LABEL, "SelectOption label")

# SelectOption / RadioGroupOption description (100)
_r([
    "RADIO_SOURCE_QUEUE_DESC", "RADIO_SOURCE_HISTORY_DESC",
    "RADIO_SOURCE_QUERY_DESC",
    "PAUSE_PERM_EVERYONE_DESC", "PAUSE_PERM_REQUESTER_DJ_DESC", "PAUSE_PERM_ADMIN_DESC",
    "PAUSE_BEHAVIOR_LEAVE_DESC", "PAUSE_BEHAVIOR_CONTINUE_DESC", "PAUSE_BEHAVIOR_SKIP_DESC",
    "RADIO_PERM_EVERYONE_DESC", "RADIO_PERM_DJ_DESC",
    "RADIO_PERM_ADMIN_DESC",
    "ACTIVITY_MODE_STATIC_DESC", "ACTIVITY_MODE_RANDOM_DESC",
    "ACTIVITY_MODE_ORDERED_DESC",
    # Label description (discord.ui.Label description=..., also 100 char limit)
    "SHUFFLE_PLAY_DESC", "SETTINGS_IMPORT_FILE_DESC",
    # Pause permission descriptions
    "PAUSE_PERM_DJ_DESC", "PAUSE_PERM_OWNER_DESC",
    # Seek permission descriptions
    "SEEK_PERM_EVERYONE_DESC", "SEEK_PERM_REQUESTER_DJ_DESC",
    "SEEK_PERM_DJ_DESC", "SEEK_PERM_ADMIN_DESC", "SEEK_PERM_OWNER_DESC",
    # Radio permission descriptions
    "RADIO_PERM_OWNER_DESC",
    # Radio edit permission descriptions
    "RADIO_EDIT_PERM_DJ_DESC", "RADIO_EDIT_PERM_ADMIN_DESC",
    "RADIO_EDIT_PERM_OWNER_DESC",
    # Force play permission descriptions
    "FORCE_PERM_DJ_DESC", "FORCE_PERM_ADMIN_DESC", "FORCE_PERM_OWNER_DESC",
    # Radio limits descriptions
    "RADIO_LIMITS_TRACK_DESC", "RADIO_LIMITS_TIME_DESC",
    # Help category select descriptions (also used as embed field names)
    "HELP_CAT_COMMANDS_DESC", "HELP_CAT_PLAYLIST_DESC",
    "HELP_CAT_DJ_VOTING_DESC", "HELP_CAT_ADMIN_DESC", "HELP_CAT_MANAGE_DESC",
    # Playlist track count (used in SelectOption description)
    "PL_TRACK_COUNT",
    # Join restriction level descriptions
    "JOIN_RESTRICT_NONE_DESC", "JOIN_RESTRICT_USERS_DESC",
    "JOIN_RESTRICT_DJ_DESC", "JOIN_RESTRICT_ADMIN_DESC",
    "PL_IMPORT_FILE_DESC",
], SELECT_OPTION_DESC, "SelectOption description")

# Embed title (256)
_r([
    "QUEUE_TITLE", "CMD_DESC_PLAYLIST", "RADIO_CONFIG_TITLE", "RADIO_EDIT_TITLE",
    "MNG_STATUS_TITLE", "MNG_UPDATES_TITLE", "HELP_TITLE",
    "MP_TITLE", "MP_TITLE_RADIO", "MP_IDLE_TITLE",
    "SETTINGS_TITLE", "SETTINGS_RESET_CONFIRM_TITLE", "SETTINGS_BOT_RESET_CONFIRM_TITLE",
    "SEARCH_TITLE", "HISTORY_TITLE",
    "PL_HEADER", "PL_SHARED_HEADER",
    "QUEUE_SEARCH_RESULTS",
    # Help category embed titles
    "HELP_CAT_COMMANDS", "HELP_CAT_PLAYLIST",
    "HELP_CAT_DJ_VOTING", "HELP_CAT_ADMIN", "HELP_CAT_MANAGE",
    # Settings section embed titles
    "SETTINGS_SHOW_PERFORMANCE", "SETTINGS_SHOW_FORCE_PLAY",
    "SETTINGS_SHOW_LIVE_PLAYBACK",
    "COOLDOWN_TITLE", "RADIO_QUEUE_TITLE",
], EMBED_TITLE, "Embed title")

# Embed field name (256)
_r([
    "ACTIVITY_ADD_CURRENT_TYPE", "ACTIVITY_EDIT_CURRENT",
    "MNG_STATUS_GUILDS", "MNG_STATUS_PLAYING", "MNG_STATUS_VOICE",
    "MNG_STATUS_MP_VIEWS", "MNG_STATUS_QUEUE_VIEWS", "MNG_STATUS_PL_VIEWS",
    "MNG_STATUS_SEARCHES", "MNG_STATUS_VOTES", "MNG_STATUS_BG_TASKS",
    "MNG_STATUS_UPTIME", "MNG_STATUS_RAM", "MNG_STATUS_CPU",
    "MNG_UPDATES_PKG_TITLE", "MNG_UPDATES_BOT_TITLE", "MNG_UPDATES_TZ",
    # MP embed field names (core/music_handlers.py)
    "DURATION", "REQUESTER", "LOOP", "URL_LABEL", "VIEWS", "UPLOADER",
    # Queue embed field name
    "QUEUE_LABEL",
    # Help embed field names
    "HELP_CAT_COMMANDS", "HELP_CAT_PLAYLIST",
    "HELP_CAT_DJ_VOTING", "HELP_CAT_ADMIN", "HELP_CAT_MANAGE",
], EMBED_FIELD_NAME, "Embed field name")


# --- Per-key param max-rendered-widths (worst case char count) ---
# Values derived from code constants in music_cog.py.
# {min}/{max}: len(str(range_value)).  {page}/{total}: max page count.
# {count}: max item count.  {index}: max index.  {name}: max playlist
# name length (50) + decoration chars added at call site.
_KEY_PARAMS: dict[str, dict[str, int]] = {
    # SEEK_LIMIT_RANGE = (0, 60)
    "SEEK_LIMIT_LABEL":            {"min": 1, "max": 2},
    "SEEK_LIMIT_DJ_LABEL":         {"min": 1, "max": 2},
    # delete_after: 5-180
    "SETTINGS_DELETE_AFTER_LABEL":  {"min": 1, "max": 3},
    # max_playlists: 0-25
    "SETTINGS_MAX_PL_LABEL":       {"min": 1, "max": 2},
    # generic "Enter a number" label; worst case: TRACK_LIMIT_RANGE max=10000 (5 digits)
    "SETTINGS_MAX_HISTORY_LABEL":  {"min": 1, "max": 5},
    # QUEUE_PER_PAGE_RANGE = (5, 15)
    "SETTINGS_QD_PER_PAGE_LABEL":  {"min": 1, "max": 2},
    # PAUSE_TIMEOUT_RANGE = (1, 60)
    "PAUSE_TIMEOUT_LABEL":         {"min": 1, "max": 2},
    # IDLE_TIMEOUT_RANGE = (0, 10080)
    "BOT_CONN_IDLE_LABEL":         {"min": 1, "max": 5},
    # RADIO_COOLDOWN_RANGE = (1, 15)
    "RADIO_COOLDOWN_LABEL":        {"min": 1, "max": 2},
    # RADIO_TRACK_LIMIT_RANGE = (15, 10000)
    "RADIO_TRACK_LIMIT_LABEL":     {"min": 2, "max": 5},
    # RADIO_TIME_LIMIT_RANGE = (1, 10080)
    "RADIO_TIME_LIMIT_LABEL":      {"min": 1, "max": 5},
    # ACTIVITY_INTERVAL_RANGE = (1, 10080)
    "ACTIVITY_TIME_LABEL":         {"min": 1, "max": 5},
    # QUEUE_PL_LIMIT_RANGE = (100, 10000)
    "QUEUE_LIMIT_LABEL":           {"min": 3, "max": 5},
    "PL_TRACK_LIMIT_LABEL":        {"min": 3, "max": 5},
    # RADIO_TRACK_LIMIT_RANGE = (15, 10000)
    "RADIO_LIMITS_TRACK_DESC":     {"min": 2, "max": 5},
    # RADIO_TIME_LIMIT_RANGE = (1, 10080)
    "RADIO_LIMITS_TIME_DESC":      {"min": 1, "max": 5},
    # queue pages: max 10000/5 = 2000
    "QUEUE_TITLE":                 {"page": 4, "total": 4},
    # history pages: max 200/5 = 40
    "HISTORY_TITLE":               {"page": 2, "total": 2},
    # search pages: max 25/5 = 5
    "SEARCH_TITLE":                {"page": 1, "total": 1},
    # playlist tracks: max 10000
    "PL_TRACK_COUNT":              {"count": 5},
    # queue search results: max 10000
    "QUEUE_SEARCH_RESULTS":        {"count": 5},
    # search index: max 25
    "SEARCH_OPTION":               {"index": 2},
    # playlist name (50) + "★ `...`" decoration at call site
    "PL_HEADER":                   {"name": 54},
    # playlist name (50) + "`...`" decoration at call site
    "PL_SHARED_HEADER":            {"name": 52},
}


# --- Checker logic ---

def _estimate_rendered(key: str, template: str) -> int:
    """Compute worst-case rendered length using known param widths."""
    params = _KEY_PARAMS.get(key)
    if not params:
        def _fallback(m):
            return "X" * 8
        return len(re.sub(r"\{(\w+)(?::[^}]*)?\}", _fallback, template))
    def _replacer(m):
        name = m.group(1)
        return "X" * params.get(name, 8)
    return len(re.sub(r"\{(\w+)(?::[^}]*)?\}", _replacer, template))


def check_file(path: Path) -> list[str]:
    """Check a single locale file. Returns list of warning strings."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    warnings = []
    for key, (limit, category) in LIMITS.items():
        value = data.get(key)
        if value is None:
            continue
        has_params = "{" in value
        if has_params:
            length = _estimate_rendered(key, value)
            prefix = f"~{length}"
            suffix = " (rendered)"
        else:
            length = len(value)
            prefix = str(length)
            suffix = ""
        if length > limit:
            warnings.append(
                f"  {key}: {prefix}/{limit} chars{suffix} ({category}) "
                f'-> "{value}"'
            )
    return warnings


def main():
    targets = []
    if len(sys.argv) > 1:
        for arg in sys.argv[1:]:
            p = LOCALES_DIR / arg
            if p.exists():
                targets.append(p)
            else:
                print(f"File not found: {p}")
                sys.exit(1)
    else:
        targets = sorted(LOCALES_DIR.glob("*.json"))

    total_warnings = 0
    for path in targets:
        warnings = check_file(path)
        if warnings:
            print(f"\n{path.name} ({len(warnings)} issue{'s' if len(warnings) != 1 else ''}):")
            for w in warnings:
                print(w)
            total_warnings += len(warnings)

    print()
    checked = sum(1 for _ in LIMITS)
    print(f"Checked {checked} keys across {len(targets)} locale(s).")
    if total_warnings == 0:
        print("All locale files pass character limit checks.")
    else:
        print(f"{total_warnings} issue(s) found.")
        sys.exit(1)


if __name__ == "__main__":
    main()
