# Localization Guide

This bot supports 31 languages through JSON locale files. Each file contains all translatable strings, command names, descriptions, button labels, modal titles, error messages, and UI text.

---

## Supported Locales

| Code | Language | Code | Language |
|---|---|---|---|
| `bg` | Български | `lt` | Lietuvių |
| `cs` | Čeština | `nl` | Nederlands |
| `da` | Dansk | `no` | Norsk |
| `de` | Deutsch | `pl` | Polski |
| `el` | Ελληνικά | `pt-BR` | Português (Brasil) |
| `en-GB` | English (UK) | `ro` | Română |
| `en-US` | English (US) - Reference | `ru` | Русский |
| `es-ES` | Español | `sv-SE` | Svenska |
| `es-419` | Español (LATAM) | `th` | ไทย |
| `fi` | Suomi | `tr` | Türkçe |
| `fr` | Français | `uk` | Українська |
| `hi` | हिन्दी | `vi` | Tiếng Việt |
| `hr` | Hrvatski | `zh-CN` | 中文 (简体) |
| `hu` | Magyar | `zh-TW` | 中文 (繁體) |
| `it` | Italiano | | |
| `ja` | 日本語 | | |
| `ko` | 한국어 | | |

---

## File Structure

Each locale is a single JSON file named after its locale code (e.g., `tr.json`, `de.json`). All files live in the `locales/` directory.

```
locales/
├── en-US.json          # Reference locale, all keys must exist here
├── tr.json
├── de.json
├── fr.json
├── ...                 # 28 more locale files
├── check_locales.py    # Validation script
└── README.md           # This file
```

---

## How It Works

1. All locale files are loaded at import time when the bot starts.
2. Each server can set its language via `/settings` → Language.
3. When the bot needs a string, it looks up the key in the server's locale. If the key is missing, it falls back to the base language (e.g., `tr` for `tr`), then to `en-US`.
4. Command names and descriptions are translated automatically via Discord's `Translator` API, Discord shows each user the command in their client language.

### Translation Function

The `t(context, key, **kwargs)` function resolves a string:
- `context` can be a `discord.Interaction`, a locale string (e.g., `"tr"`), or any object with a `guild` attribute.
- `key` is the string key (e.g., `"BUTTON_SKIP"`).
- `**kwargs` are format parameters (e.g., `t(ctx, "ANNOUNCE_NOW", title="Song", uploader="Artist")`).

---

## Adding a New Language

All languages currently supported by Discord are already included in the `locales/` folder. If Discord adds a new locale in the future:

1. Copy `en-US.json` as a starting point, naming the file with the new [Discord locale code](https://discord.com/developers/docs/reference#locales):
   ```bash
   cp en-US.json xx.json
   ```

2. Translate all values in the new file. **Do not change the keys**, only translate the values.

3. Preserve format parameters exactly as they are. For example:
   ```json
   "ANNOUNCE_NOW": "🎶 Now playing: `{title}` - `{uploader}`"
   ```
   The `{title}` and `{uploader}` placeholders must remain unchanged.

4. Run the validation script to check character limits:
   ```bash
   python locales/check_locales.py xx.json
   ```

5. Optionally, add a display name for the new locale in the `fallback_labels` dictionary inside `localization.py` → `refresh_supported_locales()`. If you skip this step, the raw locale code (e.g., `xx`) will be shown as the language name in the settings dropdown instead of a native label.

6. Restart the bot (or use `/manage` → Reload Locales if the bot is running). The new language will appear in `/settings` → Language automatically.

> **Note:** The bot dynamically loads all `*.json` files from the `locales/` directory, there are no hardcoded locale lists. Any valid JSON file placed here is automatically picked up as a supported language.

---

## Editing an Existing Locale

1. Open the locale JSON file.
2. Edit the values you want to change.
3. Run the validation script:
   ```bash
   python locales/check_locales.py xx.json
   ```
4. Restart the bot or use `/manage reload_locales:yes`.

---

## Key Naming Conventions

| Prefix | Usage | Example |
|---|---|---|
| `CMD_NAME_*` | Slash command names | `CMD_NAME_PLAY` → `"play"` |
| `CMD_DESC_*` | Slash command descriptions | `CMD_DESC_PLAY` → `"Play a link or search term"` |
| `OPT_*` / `OPTNAME_*` | Command option names and descriptions | `OPT_PLAY_QUERY` |
| `BUTTON_*` | Button labels | `BUTTON_SKIP` → `"Skip"` |
| `SETTINGS_*` | Settings panel text | `SETTINGS_SHOW_LANGUAGE` |
| `HELP_*` | Help command text | `HELP_CMD_PLAY` |
| `MNG_*` | Manage command messages | `MNG_GC_DONE` |
| `PL_*` | Playlist-related text | `PL_CREATE_TITLE` |
| `RADIO_*` | Radio feature text | `RADIO_START_BUTTON` |
| `QUEUE_*` | Queue view text | `QUEUE_TITLE` |
| `MP_*` | Music player text | `MP_TITLE` |
| `VOTE_*` | Vote system text | `VOTE_MODE_HALF` |

---

## Discord Character Limits

Discord enforces strict character limits on UI components. The validation script (`check_locales.py`) checks all keys against these limits:

| Component | Limit |
|---|---|
| Modal title | 45 characters |
| TextInput label | 45 characters |
| TextInput placeholder | 100 characters |
| Button label | 80 characters |
| Select placeholder | 150 characters |
| SelectOption label | 100 characters |
| SelectOption description | 100 characters |
| Embed title | 256 characters |
| Embed field name | 256 characters |

For keys with format parameters (e.g., `{count}`), the script estimates the rendered length with realistic filler values.

### Running the Validator

**Check all locales:**
```bash
python locales/check_locales.py
```

**Check a specific locale:**
```bash
python locales/check_locales.py tr.json
```

The script will report any keys that exceed their limits, showing the current length, the limit, and the component category.

---

## Fallback Behavior

- If a key is missing from a locale, the bot tries the base language (e.g., `fr` for `fr`), then falls back to `en-US`.
- If a format string has a parameter mismatch, the unformatted template is returned and a warning is logged.

---

## Hot Reload

You can reload locale files without restarting the bot:

1. Edit or add locale files in the `locales/` directory.
2. Run `/manage` and select **Reload Locales** from the dropdown (bot owner only).
3. The bot reloads all locale files and refreshes the supported languages list.

Note: Command name/description translations require a command tree re-sync, which happens automatically on the next bot restart if changes are detected.
