from __future__ import annotations

import asyncio
import json
from pathlib import Path

import discord

from core.db import db

DEFAULT_LOCALE = "en-US"
LOCALES_DIR = Path(__file__).resolve().parent.parent / "locales"

_strings: dict[str, dict[str, str]] = {}
SUPPORTED_LOCALES: dict[str, str] = {}
guild_locales: dict[int, str] = {}
_locales_loaded = False
_locales_lock = asyncio.Lock()


def _resolve(locale: str, key: str) -> str | None:
    d = _strings.get(locale)
    if d is not None:
        val = d.get(key)
        if val:
            return val
    base = locale.split("-")[0]
    if base != locale:
        d = _strings.get(base)
        if d is not None:
            val = d.get(key)
            if val:
                return val
    d = _strings.get(DEFAULT_LOCALE)
    return d.get(key) if d is not None else None


def load_locales():
    new = {}
    if LOCALES_DIR.exists():
        for path in LOCALES_DIR.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    new[path.stem] = {str(k): str(v) for k, v in data.items() if v is not None}
            except Exception as e:
                print(f"[warn] Failed to load locale {path.name}: {e}")
    _strings.clear()
    _strings.update(new)


def refresh_supported_locales():
    SUPPORTED_LOCALES.clear()
    fallback_labels = {
        "bg": "Български",
        "cs": "Čeština",
        "da": "Dansk",
        "de": "Deutsch",
        "el": "Ελληνικά",
        "en-GB": "English (UK)",
        "en-US": "English (US)",
        "es-ES": "Español",
        "es-419": "Español (LATAM)",
        "fi": "Suomi",
        "fr": "Français",
        "hi": "हिन्दी",
        "hr": "Hrvatski",
        "hu": "Magyar",
        "it": "Italiano",
        "ja": "日本語",
        "ko": "한국어",
        "lt": "Lietuvių",
        "nl": "Nederlands",
        "no": "Norsk",
        "pl": "Polski",
        "pt-BR": "Português (Brasil)",
        "ro": "Română",
        "ru": "Русский",
        "sv-SE": "Svenska",
        "th": "ไทย",
        "tr": "Türkçe",
        "uk": "Українська",
        "vi": "Tiếng Việt",
        "zh-CN": "中文 (简体)",
        "zh-TW": "中文 (繁體)",
    }
    for code in _strings.keys():
        SUPPORTED_LOCALES[code] = fallback_labels.get(code, code)


# Populate locales at import time for command choice generation.
load_locales()
refresh_supported_locales()


async def init_locales_cache(force=False):
    global _locales_loaded
    if _locales_loaded and not force:
        return
    async with _locales_lock:
        if _locales_loaded and not force:
            return
        data = await db.get_all_locales()
        guild_locales.clear()
        guild_locales.update(data)
        _locales_loaded = True


async def set_locale(guild_id: int, locale: str):
    if locale not in SUPPORTED_LOCALES:
        locale = DEFAULT_LOCALE
    await db.set_locale(guild_id, locale)
    guild_locales[guild_id] = locale


def t(interaction_or_locale: discord.Interaction | str | None, key: str, **fmt) -> str:
    if isinstance(interaction_or_locale, discord.Interaction):
        guild_id = interaction_or_locale.guild_id
        guild_locale = guild_locales.get(guild_id)
        locale = guild_locale or DEFAULT_LOCALE
    elif isinstance(interaction_or_locale, str):
        locale = interaction_or_locale
    else:
        guild_id = getattr(interaction_or_locale, 'guild_id', None)
        if not guild_id:
            guild = getattr(interaction_or_locale, 'guild', None)
            if guild:
                guild_id = guild.id
        if guild_id:
            locale = guild_locales.get(guild_id) or DEFAULT_LOCALE
        else:
            locale = DEFAULT_LOCALE

    template = _resolve(locale, key)
    if template is None:
        template = key
    try:
        return template.format(**fmt)
    except (KeyError, IndexError, ValueError, AttributeError) as e:
        print(f"[i18n] format error for key={key!r} locale={locale!r}: {e}")
        return template


class BaseTranslator(discord.app_commands.Translator):
    async def translate(self, string: discord.app_commands.locale_str, locale: discord.Locale, context: discord.app_commands.TranslationContext) -> str | None:
        key = string.extras.get("message", string.message)
        return _resolve(str(locale), key)
