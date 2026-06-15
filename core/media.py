from __future__ import annotations

import asyncio
import logging
import os
import shutil
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import yt_dlp

_log = logging.getLogger(__name__)

from core.localization import t

_DEFAULT_COOKIE_PATH = Path(__file__).resolve().parent.parent / "db" / "cookies.txt"

YTDLP_COOKIE_FILE = os.getenv("YTDLP_COOKIE_FILE")
if YTDLP_COOKIE_FILE:
    if not os.path.isfile(YTDLP_COOKIE_FILE):
        YTDLP_COOKIE_FILE = None
elif _DEFAULT_COOKIE_PATH.is_file():
    YTDLP_COOKIE_FILE = str(_DEFAULT_COOKIE_PATH)


def _temp_cookie_copy() -> str | None:
    """Create a temporary copy of the cookie file for thread-safe yt-dlp usage.
    Returns the temp file path, or None if no cookie file is configured.
    Caller must delete the temp file when done."""
    if not YTDLP_COOKIE_FILE:
        return None
    try:
        fd, path = tempfile.mkstemp(suffix=".txt", prefix="ytdlp_cookies_")
        os.close(fd)
        shutil.copy2(YTDLP_COOKIE_FILE, path)
        return path
    except OSError:
        try:
            os.unlink(path)
        except (OSError, UnboundLocalError):
            pass
        return None

_YTDLP_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="ytdlp")


def set_max_workers(n: int):
    global _YTDLP_EXECUTOR
    n = max(1, n)
    if _YTDLP_EXECUTOR._max_workers == n:
        return
    old = _YTDLP_EXECUTOR
    _YTDLP_EXECUTOR = ThreadPoolExecutor(max_workers=n, thread_name_prefix="ytdlp")
    old.shutdown(wait=False)


class StaleCookieError(Exception):
    """Raised when yt-dlp fails in a way that indicates expired/stale cookies."""


class JSRuntimeError(Exception):
    """Raised when yt-dlp fails due to a missing or broken JavaScript runtime."""


_COOKIE_ERROR_HINTS = (
    "sign in",
    "not a bot",
    "bot detection",
    "confirm your age",
    "verify your age",
    "age-restricted",
    "age_verification",
    "age gate",
    "login required",
)


def _is_cookie_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(hint in msg for hint in _COOKIE_ERROR_HINTS)


_JS_RUNTIME_HINTS = (
    "no supported javascript runtime",
    "nsig extraction failed",
    "js runtime",
    "without a js runtime",
)


def _is_js_runtime_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(hint in msg for hint in _JS_RUNTIME_HINTS)


_ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
_YTDLP_ERROR_PREFIX = re.compile(r"^ERROR:\s*\[[^\]]+\]\s*[A-Za-z0-9_-]+:\s*")


def _clean_ytdlp_error(exc: Exception) -> str:
    """Extracts the human-readable part from a yt-dlp error message."""
    msg = _ANSI_ESCAPE.sub("", str(exc)).strip()
    cleaned = _YTDLP_ERROR_PREFIX.sub("", msg)
    return cleaned if cleaned else msg

_UNSUPPORTED_EXTENSIONS = (
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff",
    ".pdf", ".svg", ".ico", ".txt",
)
_UNSUPPORTED_EXT_RE = re.compile(
    r"(?:" + "|".join(re.escape(ext) for ext in _UNSUPPORTED_EXTENSIONS) + r")(?:$|/)"
)
def _normalize_urlish(value: str):
    parsed = urlparse(value)
    if (not parsed.scheme or (not parsed.netloc and "." in parsed.scheme)) and parsed.path:
        parsed = urlparse(f"//{value}", scheme="https")
    return parsed


def _is_youtube_netloc(netloc: str) -> bool:
    host = netloc.lower().split(":")[0]
    return host in ("youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com") or host.endswith(".youtube.com") or host in ("youtu.be", "www.youtu.be")


class _CapturingLogger:
    """Logger that captures the last error message from yt-dlp."""
    __slots__ = ("last_error",)
    def __init__(self):
        self.last_error: str | None = None
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg):
        self.last_error = str(msg)


def _ydl_options(base: dict, *, silent: bool = False) -> dict:
    """Applies common configuration (timeouts, retries, silent logging) to yt-dlp options."""
    opts = dict(base)
    opts.setdefault("socket_timeout", 15)
    opts.setdefault("retries", 2)
    opts.setdefault("extractor_retries", 2)
    if silent:
        opts["quiet"] = True
        opts["no_warnings"] = True
    return opts



_YT_TIME_RE = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s?)?$")
_YT_VIDEO_ID_RE = re.compile(r"(?:v=|/v/|/embed/|youtu\.be/|/shorts/|/live/)([0-9A-Za-z_-]{11})")


def extract_youtube_start_time(url: str) -> int | None:
    parsed = _normalize_urlish(url)
    t_val = (parse_qs(parsed.query).get("t") or [None])[0]
    if not t_val:
        return None
    if t_val.isdigit():
        return int(t_val)
    m = _YT_TIME_RE.match(t_val)
    if not m or not any(m.groups()):
        return None
    h, mn, s = (int(g) if g else 0 for g in m.groups())
    return h * 3600 + mn * 60 + s


def is_youtube_url(url: str) -> bool:
    """Checks if the provided URL belongs to a YouTube domain."""
    parsed = _normalize_urlish(url)
    return _is_youtube_netloc(parsed.netloc)


def yt_playlist_start_id(url: str) -> str | None:
    """If URL has both a video ID and list=, return the video ID to start from."""
    parsed = _normalize_urlish(url)
    if not _is_youtube_netloc(parsed.netloc):
        return None
    qs = parse_qs(parsed.query)
    if "list" not in qs:
        return None
    if "v" in qs:
        return qs["v"][0]
    match = _YT_VIDEO_ID_RE.search(url)
    return match.group(1) if match else None


def slice_from_video(entries: list, video_id: str | None) -> list:
    """Slice a playlist's entries starting from the given video ID onward."""
    if not video_id or not entries:
        return entries
    for i, e in enumerate(entries):
        eid = e.get("id", "")
        if eid == video_id:
            return entries[i:]
        url = e.get("webpage_url") or e.get("url") or ""
        if video_id in url:
            return entries[i:]
    return entries


def has_unsupported_extension(url: str) -> bool:
    """Checks if the URL path contains an unsupported extension."""
    path = _normalize_urlish(url).path.lower()
    return bool(_UNSUPPORTED_EXT_RE.search(path))


def unavailable_reason(entry: dict) -> str | None:
    """Returns a reason string if the entry is unavailable, None otherwise."""
    if not entry:
        return None
    avail = entry.get("availability")
    if avail in ("subscriber_only", "premium_only"):
        return "members_only"
    title = entry.get("title")
    if not isinstance(title, str) or entry.get("duration") is not None:
        return None
    lower = title.strip().lower()
    if lower == "[deleted video]":
        return "deleted"
    if lower == "[private video]":
        return "private"
    return None


def is_playable_entry(entry: dict) -> bool:
    """
    Validates if a yt-dlp entry is a playable audio/video track.
    Filters out static images, text-only tweets, and unsupported formats.
    """
    if not entry:
        return False

    title = entry.get("title")
    if not isinstance(title, str) or not title.strip():
        return False

    ie_key = entry.get("ie_key", "")
    if ie_key in {"Generic", "Image", "ImgurAlbum"}:
        return False

    url = entry.get("url") or ""
    webpage_url = entry.get("webpage_url") or ""

    if has_unsupported_extension(url) or has_unsupported_extension(webpage_url):
        return False

    # Platform specific checks
    if ie_key == "Twitter":
        if entry.get("duration") is None and not entry.get("is_live"):
            return False

    return True


_HEAVY_KEYS = ("formats", "automatic_captions", "subtitles", "heatmap",
               "chapters", "thumbnails", "requested_downloads",
               "http_headers", "requested_subtitles", "cookies",
               "__x_forwarded_for_ip", "_type",
               "channel_id", "channel_is_verified", "channel_url",
               "description", "live_status", "release_timestamp",
               "timestamp", "uploader_id", "uploader_url", "original_url")


def _resolve_url_from_formats(entry: dict) -> None:
    """If entry has formats but no url, pick the best audio URL before slim_entry discards them."""
    if entry.get("url") or entry.get("_prepared_source"):
        return
    formats = entry.get("formats")
    if not formats:
        return
    audio_url = None
    fallback_url = None
    for f in reversed(formats):
        if f.get("acodec") == "none" or "url" not in f:
            continue
        if f.get("vcodec") == "none":
            audio_url = f["url"]
            break
        if fallback_url is None:
            fallback_url = f["url"]
    audio_url = audio_url or fallback_url
    if audio_url:
        entry["url"] = audio_url
        entry["_prepared_source"] = True


def slim_entry(entry: dict) -> None:
    """Removes heavy yt-dlp fields that the bot never uses, saving RAM."""
    for k in _HEAVY_KEYS:
        entry.pop(k, None)


_youtube_checked = False


def log_youtube_status():
    """Logs YouTube-related warnings at startup."""
    global _youtube_checked
    if _youtube_checked:
        return
    _youtube_checked = True

    if not has_js_runtime():
        print("[YouTube] No JS runtime found (deno/node/quickjs). Some content may not work.")


def has_js_runtime() -> bool:
    return bool(shutil.which("deno") or shutil.which("node") or shutil.which("quickjs") or shutil.which("quickjs-ng"))


def has_valid_cookies() -> bool:
    """Returns True if a cookie file is configured and exists."""
    return bool(YTDLP_COOKIE_FILE)

async def _run_ydl_info(target: str, options: dict) -> dict:
    """Runs yt-dlp extraction in a separate thread executor."""
    loop = asyncio.get_running_loop()

    cap_logger = _CapturingLogger()

    def _inner():
        local_opts = dict(options)
        tmp_cookie = _temp_cookie_copy()
        if tmp_cookie:
            local_opts["cookiefile"] = tmp_cookie
        elif YTDLP_COOKIE_FILE:
            _log.warning("Failed to create temp cookie copy; running without cookies to protect the original file")
            local_opts.pop("cookiefile", None)
        local_opts["logger"] = cap_logger
        try:
            with yt_dlp.YoutubeDL(local_opts) as ydl:
                return ydl.extract_info(target, download=False)
        finally:
            if tmp_cookie:
                try:
                    os.unlink(tmp_cookie)
                except OSError:
                    pass

    try:
        result = await asyncio.wait_for(
            loop.run_in_executor(_YTDLP_EXECUTOR, _inner),
            timeout=180,
        )
    except asyncio.TimeoutError:
        raise ValueError("Extraction timed out (180s)")
    except Exception as e:
        if _is_cookie_error(e):
            raise StaleCookieError(str(e)) from e
        if _is_js_runtime_error(e):
            raise JSRuntimeError(str(e)) from e
        raise
    if result is None:
        reason = cap_logger.last_error
        if reason:
            cleaned = _YTDLP_ERROR_PREFIX.sub("", _ANSI_ESCAPE.sub("", reason)).strip()
            raise ValueError(cleaned or reason)
        raise ValueError(f"yt-dlp returned no info for: {target}")
    return result


def looks_like_url(text: str) -> bool:
    """Returns True if the text appears to be a valid URL."""
    candidate = text.strip()
    if " " in candidate:
        return False
    if "://" not in candidate and "/" not in candidate:
        return False
    parsed = _normalize_urlish(candidate)
    return bool(parsed.netloc) and "." in parsed.netloc


_YT_PLAYLIST_BATCH = 100


def _prepare_query(query: str) -> str:
    prepared_query = query.strip()
    is_link = looks_like_url(prepared_query)
    normalized = _normalize_urlish(prepared_query)

    if is_link and not prepared_query.startswith(("http://", "https://")):
        prepared_query = normalized.geturl()
        normalized = _normalize_urlish(prepared_query)
    if is_link and not prepared_query.startswith(("http://", "https://")):
        return f"ytsearch1:{query.strip()}"

    is_youtube_link = _is_youtube_netloc(normalized.netloc)
    if is_youtube_link:
        qs = parse_qs(normalized.query)
        video_id_match = _YT_VIDEO_ID_RE.search(prepared_query)
        if video_id_match and "list" in qs:
            prepared_query = f"https://www.youtube.com/playlist?list={qs['list'][0]}"
        elif video_id_match:
            video_id = video_id_match.group(1)
            prepared_query = f"https://www.youtube.com/watch?v={video_id}"
    elif not prepared_query.startswith("ytsearch") and not is_link:
        prepared_query = f"ytsearch1:{prepared_query}"

    return prepared_query


async def extract_entries(query: str, *, silent: bool = False, playlistend: int | None = None, return_count: bool = False) -> list | tuple[list, int | None]:
    """
    Searches or extracts video information using yt-dlp.
    Handles YouTube video IDs, raw searches, and direct links.
    If playlistend is set, only fetches up to that many entries.
    If return_count is True, returns (entries, playlist_count) tuple.
    """
    prepared_query = _prepare_query(query)

    opts = {
        'quiet': True,
        'default_search': 'ytsearch',
        'extract_flat': True,
        'noplaylist': False,
        'ignoreerrors': True,
    }
    if playlistend is not None:
        opts['playlistend'] = playlistend

    ydl_opts = _ydl_options(opts, silent=silent)

    try:
        info = await _run_ydl_info(prepared_query, ydl_opts)
    except (StaleCookieError, JSRuntimeError):
        raise
    except Exception as e:
        print(t(None, "EXTRACT_ERR", error=e))
        raise

    entries = info.get("entries")
    if entries is not None:
        result = [e for e in entries if e]
    else:
        result = [info]
    for e in result:
        _resolve_url_from_formats(e)
        slim_entry(e)
    if return_count:
        return result, info.get("playlist_count")
    return result


async def extract_entries_from(query: str, *, silent: bool = False, playliststart: int = 1) -> list:
    """Fetches playlist entries starting from a given index."""
    prepared_query = _prepare_query(query)

    ydl_opts = _ydl_options({
        'quiet': True,
        'default_search': 'ytsearch',
        'extract_flat': True,
        'noplaylist': False,
        'playliststart': playliststart,
        'ignoreerrors': True,
    }, silent=silent)

    try:
        info = await _run_ydl_info(prepared_query, ydl_opts)
    except (StaleCookieError, JSRuntimeError):
        raise
    except Exception as e:
        print(t(None, "EXTRACT_ERR", error=e))
        return []

    entries = info.get("entries")
    if entries is not None:
        result = [e for e in entries if e]
        for e in result:
            _resolve_url_from_formats(e)
            slim_entry(e)
        return result
    return []
