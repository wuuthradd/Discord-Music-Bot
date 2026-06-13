"""Radio feature - RDAMVM fetch helpers."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from pathlib import Path

from . import media

_YTDLP_BIN: str | None = None
_radio_semaphore: asyncio.Semaphore | None = None


def set_radio_concurrency(max_workers: int):
    global _radio_semaphore
    _radio_semaphore = asyncio.Semaphore(max(1, max_workers - 1))


def _find_ytdlp() -> str:
    global _YTDLP_BIN
    if _YTDLP_BIN:
        return _YTDLP_BIN
    venv = Path(__file__).resolve().parent.parent / ".venv" / "bin" / "yt-dlp"
    if venv.exists():
        _YTDLP_BIN = str(venv)
    else:
        _YTDLP_BIN = "yt-dlp"
    return _YTDLP_BIN


class RadioFetchError(Exception):
    """Raised when yt-dlp radio fetch fails."""


def _make_anon_cookie_file() -> str:
    """Create a temp Netscape cookie file with consent-rejection cookie.
    Mimics incognito, YouTube returns location-based results without
    behavioral personalization from IP-level tracking."""
    fd, path = tempfile.mkstemp(suffix=".txt", prefix="ytdlp_anon_")
    with os.fdopen(fd, "w") as f:
        f.write("# Netscape HTTP Cookie File\n")
        f.write(".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSOCS\tCAI=\n")
    return path


def _run_flat_once(url: str) -> list[dict]:
    """Run yt-dlp --flat-playlist synchronously (single attempt), return list of track dicts."""
    cookie_path = _make_anon_cookie_file()
    try:
        args = [
            _find_ytdlp(),
            "--flat-playlist", "--dump-json",
            "--no-warnings", "--quiet",
            "--no-cache-dir",
            "--socket-timeout", "15",
            "--retries", "2",
            "--cookies", cookie_path,
            url,
        ]

        proc = subprocess.run(args, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip()[:200] or f"exit code {proc.returncode}")
        tracks: list[dict] = []
        for line in proc.stdout.strip().split("\n"):
            if not line:
                continue
            try:
                d = json.loads(line)
                if not isinstance(d, dict):
                    continue
                tracks.append({
                    "id": d.get("id", ""),
                    "title": d.get("title", ""),
                    "url": d.get("url") or d.get("webpage_url") or f"https://www.youtube.com/watch?v={d.get('id', '')}",
                    "webpage_url": f"https://www.youtube.com/watch?v={d.get('id', '')}",
                    "uploader": d.get("uploader") or d.get("channel") or "",
                    "duration": d.get("duration"),
                    "thumbnail": d.get("thumbnails", [{}])[-1].get("url") if d.get("thumbnails") else None,
                })
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
        if tracks:
            return tracks
        raise RuntimeError("empty result")
    finally:
        try:
            os.unlink(cookie_path)
        except OSError:
            pass


def _dedupe_pool(tracks: list[dict]) -> list[dict]:
    """Remove YouTube pagination duplicates while preserving order."""
    seen: dict[str, None] = {}
    result: list[dict] = []
    for t in tracks:
        vid = t["id"]
        if vid and vid not in seen:
            seen[vid] = None
            result.append(t)
    return result


async def fetch_radio_pool(seed_video_id: str) -> list[dict]:
    """Fetch the full RDAMVM pool for a seed video (~300-600 unique tracks).

    Runs yt-dlp in an executor thread. Returns deduplicated tracks in
    YouTube's native order (which has built-in artist spread).
    """
    url = f"https://music.youtube.com/watch?v={seed_video_id}&list=RDAMVM{seed_video_id}"
    loop = asyncio.get_running_loop()
    sem = _radio_semaphore
    if sem:
        await sem.acquire()
    try:
        raw = await loop.run_in_executor(media._YTDLP_EXECUTOR, _run_flat_once, url)
        return _dedupe_pool(raw)
    except Exception as e:
        raise RadioFetchError(f"Radio fetch failed: {e}") from e
    finally:
        if sem:
            sem.release()
