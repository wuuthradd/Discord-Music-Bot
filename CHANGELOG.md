# Changelog

## v1.1.2

- Added automatic YouTube PO token provider setup and updates using Docker.
- Added Docker diagnostics and setup guidance to `/manage`.
- Added Instagram Reels playback support.
- Fixed Bandcamp extraction.
- Improved audio playback reliability and fixed skipped audio at playback start.
- Fixed Deno compatibility with PM2.

## v1.1.1

- Added yt-dlp piped extraction for sources that needs it (TikTok).
- Separated bot detection errors from age restricted errors.

## v1.1.0

- Auto-update system for pip packages and bot code via `/manage`.
- Moved `db.py` and `localization.py` into `core/`.
- Cookie path moved to `db/cookies.txt`.
- SpotAPI moved to PyPI in `requirements.txt`.

## v1.0.0

Initial release.
