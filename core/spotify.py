from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import AsyncIterator
from urllib.parse import urlparse

from locales.localization import t

_log = logging.getLogger(__name__)

# Suppress noisy SpotAPI logging
logging.getLogger("spotapi").setLevel(logging.CRITICAL)

_EMBED_URL = "https://open.spotify.com/embed/{}/{}"
_TOKEN_URL = "https://accounts.spotify.com/api/token"
_API_BASE = "https://api.spotify.com/v1"
_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36"
)
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)
_SPOTIFY_PATH_RE = re.compile(
    r"/(track|playlist|album)/([A-Za-z0-9]{22})"
)
_API_PAGE_LIMIT = 100
_API_PAGE_DELAY = 0.3

_api_token: str | None = None
_api_token_expiry: float = 0
_token_lock = threading.Lock()


class SpotifyError(Exception):
    """Raised when Spotify scraping or API access fails."""


@dataclass
class SpotifyResult:
    tracks: list[dict] = field(default_factory=list)
    total: int | None = None
    entity_type: str = ""
    entity_id: str = ""
    use_free_fallback: bool = False


def _extract_artists(raw_list) -> list[str]:
    result = []
    for a in raw_list or []:
        name = a.get("name") or (a.get("profile") or {}).get("name") or ""
        name = name.replace("\xa0", " ").strip()
        if name:
            result.append(name)
    return result or [t(None, "UNKNOWN")]


def has_spotify_api() -> bool:
    return bool(os.getenv("SPOTIFY_CLIENT_ID") and os.getenv("SPOTIFY_CLIENT_SECRET"))


def is_spotify_url(url: str) -> bool:
    return _parse_spotify_url(url) is not None


def _parse_spotify_url(url: str) -> tuple[str, str] | None:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    if parsed.scheme not in ("http", "https") or parsed.netloc not in ("open.spotify.com", "www.open.spotify.com"):
        return None
    m = _SPOTIFY_PATH_RE.search(parsed.path)
    return (m.group(1), m.group(2)) if m else None


# --- Embed scraping (no API key needed) ---

def _fetch_embed(entity_type: str, entity_id: str) -> dict:
    url = _EMBED_URL.format(entity_type, entity_id)
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            html = resp.read().decode()
    except Exception as e:
        raise SpotifyError(f"Failed to reach Spotify: {e}") from e

    m = _NEXT_DATA_RE.search(html)
    if not m:
        raise SpotifyError("Failed to parse Spotify embed data")

    try:
        nd = json.loads(m.group(1))
        return nd["props"]["pageProps"]["state"]["data"]["entity"]
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        raise SpotifyError(f"Unexpected Spotify embed structure: {e}") from e


def _track_entry(uri: str, title: str, artists: list[str], duration_ms: int | None = None) -> dict | None:
    if not uri.startswith("spotify:track:"):
        return None
    track_id = uri.rsplit(":", 1)[-1]
    if not track_id:
        return None
    entry: dict = {
        "title": title,
        "uploader": ", ".join(artists),
        "source": "spotify",
        "spotify_url": f"https://open.spotify.com/track/{track_id}",
        "spotify_artists": artists,
    }
    if duration_ms is not None and duration_ms > 0:
        entry["spotify_duration"] = duration_ms / 1000
    return entry


def _resolve_track(entity: dict) -> list[dict]:
    try:
        artists = _extract_artists(entity.get("artists"))
        entry = _track_entry(entity["uri"], entity["name"], artists, entity.get("duration"))
        return [entry] if entry else []
    except (KeyError, TypeError, AttributeError) as e:
        raise SpotifyError(f"Missing field in track data: {e}") from e


def _resolve_collection(entity: dict) -> list[dict]:
    results = []
    for track in entity.get("trackList") or []:
        try:
            subtitle = track.get("subtitle", "")
            if subtitle:
                artists = [a.replace("\xa0", " ").strip() for a in subtitle.split(",") if a.strip()] or [t(None, "UNKNOWN")]
            else:
                artists = [t(None, "UNKNOWN")]
            entry = _track_entry(track["uri"], track["title"], artists, track.get("duration"))
            if entry:
                results.append(entry)
        except (KeyError, TypeError, AttributeError):
            continue
    return results


# --- Spotify Web API (needs SPOTIFY_CLIENT_ID + SPOTIFY_CLIENT_SECRET) ---

def _get_api_token() -> str:
    global _api_token, _api_token_expiry
    with _token_lock:
        if _api_token and time.time() < _api_token_expiry - 60:
            return _api_token

        client_id = os.getenv("SPOTIFY_CLIENT_ID", "")
        client_secret = os.getenv("SPOTIFY_CLIENT_SECRET", "")
        credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

        data = "grant_type=client_credentials".encode()
        req = urllib.request.Request(_TOKEN_URL, data=data, headers={
            "Authorization": f"Basic {credentials}",
            "Content-Type": "application/x-www-form-urlencoded",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode())
            _api_token = body["access_token"]
            _api_token_expiry = time.time() + body.get("expires_in", 3600)
            return _api_token
        except Exception as e:
            raise SpotifyError(f"Failed to get Spotify API token: {e}") from e


def _force_refresh_api_token() -> str:
    global _api_token_expiry
    with _token_lock:
        _api_token_expiry = 0
    return _get_api_token()


def _api_track_entry(item: dict) -> dict | None:
    track = item.get("track") or item
    if not track or not track.get("name"):
        return None
    try:
        artists = _extract_artists(track.get("artists"))
        uri = track.get("uri", "")
        duration = track.get("duration_ms")
        return _track_entry(uri, track["name"], artists, duration)
    except (KeyError, TypeError):
        return None


def _fetch_api_page(entity_type: str, entity_id: str, token: str, offset: int, limit: int = _API_PAGE_LIMIT) -> tuple[list[dict], int | None, bool]:
    if entity_type == "playlist":
        url = f"{_API_BASE}/playlists/{entity_id}/tracks?offset={offset}&limit={limit}"
    elif entity_type == "album":
        url = f"{_API_BASE}/albums/{entity_id}/tracks?offset={offset}&limit={limit}"
    else:
        return [], None, False

    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "User-Agent": _UA,
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode())

    items = data.get("items") or []
    total = data.get("total")
    has_next = data.get("next") is not None
    tracks = []
    for item in items:
        entry = _api_track_entry(item)
        if entry:
            tracks.append(entry)
    return tracks, total, has_next


# --- SpotAPI fallback (no API key needed, full playlists) ---

def _fetch_via_spotapi(entity_type: str, entity_id: str) -> list[dict]:
    from spotapi.public import PublicPlaylist, PublicAlbum, Public

    tracks: list[dict] = []
    if entity_type == "track":
        info = Public.song_info(entity_id)
        try:
            track = info["data"]["trackUnion"]
            uri = track["uri"]
            title = track["name"]
            artists = _extract_artists((track.get("firstArtist") or {}).get("items") or [])
            dur = track.get("duration") or {}
            duration_ms = dur.get("totalMilliseconds")
            entry = _track_entry(uri, title, artists, duration_ms)
            if entry:
                tracks.append(entry)
        except (KeyError, TypeError):
            pass
        return tracks
    if entity_type == "playlist":
        for chunk in PublicPlaylist(entity_id).paginate_playlist():
            for item in chunk.get("items") or []:
                try:
                    v3 = item["itemV3"]["data"]
                    v2 = item["itemV2"]["data"]
                    uri = v3["uri"]
                    title = v3["identityTrait"]["name"]
                    artists = _extract_artists(v3["identityTrait"]["contributors"]["items"])
                    duration_ms = v2["trackDuration"]["totalMilliseconds"]
                    entry = _track_entry(uri, title, artists, duration_ms)
                    if entry:
                        tracks.append(entry)
                except (KeyError, TypeError):
                    continue
    elif entity_type == "album":
        for chunk in PublicAlbum(entity_id).paginate_album():
            items = chunk.get("items") if isinstance(chunk, dict) else chunk if isinstance(chunk, list) else []
            for item in items or []:
                try:
                    track = item.get("track") or item
                    uri = track["uri"]
                    title = track["name"]
                    raw_artists = track.get("artists")
                    if isinstance(raw_artists, dict):
                        raw_artists = raw_artists.get("items", [])
                    artists = _extract_artists(raw_artists)
                    dur = track.get("duration")
                    duration_ms = dur.get("totalMilliseconds") if isinstance(dur, dict) else dur
                    entry = _track_entry(uri, title, artists, duration_ms)
                    if entry:
                        tracks.append(entry)
                except (KeyError, TypeError):
                    continue
    return tracks


# --- Public interface ---

def _try_api_first_page(entity_type: str, entity_id: str, result: SpotifyResult) -> bool:
    """Try the official Spotify API for the first page. Returns True on success."""
    if not has_spotify_api():
        return False
    try:
        token = _get_api_token()
        tracks, total, _ = _fetch_api_page(entity_type, entity_id, token, 0)
        result.tracks = tracks
        result.total = total
        return True
    except Exception:
        _log.debug("API fallback failed for %s %s", entity_type, entity_id, exc_info=True)
        return False


def _resolve_spotify(url: str) -> SpotifyResult:
    parsed = _parse_spotify_url(url)
    if not parsed:
        return SpotifyResult()

    entity_type, entity_id = parsed
    result = SpotifyResult(entity_type=entity_type, entity_id=entity_id)

    if entity_type == "track":
        try:
            entity = _fetch_embed(entity_type, entity_id)
            result.tracks = _resolve_track(entity)
        except SpotifyError as original_err:
            try:
                result.tracks = _fetch_via_spotapi(entity_type, entity_id)
            except Exception:
                _log.debug("spotapi fallback failed for track %s", entity_id, exc_info=True)
            if not result.tracks:
                raise original_err
        if result.tracks:
            result.total = 1
        return result

    # Collections: embed first for fast start
    try:
        entity = _fetch_embed(entity_type, entity_id)
        result.tracks = _resolve_collection(entity)
    except SpotifyError:
        if _try_api_first_page(entity_type, entity_id, result):
            return result
        try:
            result.tracks = _fetch_via_spotapi(entity_type, entity_id)
            if result.tracks:
                result.total = len(result.tracks)
                return result
        except Exception:
            _log.debug("spotapi fallback failed for collection %s", entity_id, exc_info=True)
        raise

    if not result.tracks:
        if _try_api_first_page(entity_type, entity_id, result):
            return result
        try:
            result.tracks = _fetch_via_spotapi(entity_type, entity_id)
            if result.tracks:
                result.total = len(result.tracks)
                return result
        except Exception:
            _log.debug("spotapi fallback failed for empty collection %s", entity_id, exc_info=True)

    raw_embed_count = len(entity.get("trackList") or [])
    if raw_embed_count < _API_PAGE_LIMIT:
        result.total = len(result.tracks)
        return result

    # Got ~100 tracks, playlist likely has more
    if has_spotify_api():
        try:
            token = _get_api_token()
            _, total, _ = _fetch_api_page(entity_type, entity_id, token, 0, limit=1)
            result.total = total
        except Exception as e:
            _log.warning("Spotify API probe failed for %s: %s - falling back to SpotAPI", entity_id, e)
            result.use_free_fallback = True
    else:
        result.use_free_fallback = True

    return result


async def get_spotify_first_batch(url: str) -> SpotifyResult:
    return await asyncio.to_thread(_resolve_spotify, url)


_MAX_API_OFFSET = 10000

async def fetch_remaining_tracks(entity_type: str, entity_id: str, offset: int) -> AsyncIterator[list[dict]]:
    token = await asyncio.to_thread(_get_api_token)
    retries = 0
    while offset < _MAX_API_OFFSET:
        try:
            tracks, _, has_next = await asyncio.to_thread(
                _fetch_api_page, entity_type, entity_id, token, offset
            )
        except urllib.error.HTTPError as e:
            if e.code == 401:
                try:
                    token = await asyncio.to_thread(_force_refresh_api_token)
                    tracks, _, has_next = await asyncio.to_thread(
                        _fetch_api_page, entity_type, entity_id, token, offset
                    )
                except Exception as e2:
                    print(f"[Spotify API] Retry failed at offset {offset}: {e2}")
                    break
            elif e.code == 429 or e.code >= 500:
                retries += 1
                if retries > 2:
                    print(f"[Spotify API] {e.code} at offset {offset}, giving up after {retries} retries")
                    break
                retry_after = int(e.headers.get("Retry-After", 2)) if hasattr(e, "headers") and e.headers else 2
                print(f"[Spotify API] {e.code} at offset {offset}, retrying after {retry_after}s")
                await asyncio.sleep(retry_after)
                continue
            else:
                print(f"[Spotify API] Error at offset {offset}: {e}")
                break
        except Exception as e:
            print(f"[Spotify API] Error at offset {offset}: {e}")
            break
        if not tracks:
            break
        retries = 0
        yield tracks
        if not has_next:
            break
        offset += _API_PAGE_LIMIT
        await asyncio.sleep(_API_PAGE_DELAY)


async def fetch_all_via_spotapi(entity_type: str, entity_id: str) -> list[dict]:
    return await asyncio.wait_for(
        asyncio.to_thread(_fetch_via_spotapi, entity_type, entity_id),
        timeout=180,
    )
