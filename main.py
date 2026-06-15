__version__ = "1.1.0"

import asyncio
import os
import hashlib
import json
from pathlib import Path
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("MyMusicBot_Token")
if not BOT_TOKEN:
    raise RuntimeError("Missing environment variable: MyMusicBot_Token")

class MusicBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, max_messages=0)

    async def setup_hook(self):
        from core.localization import BaseTranslator
        from core.music_cog import _PersistentMPButton, _PersistentQueueButton
        await self.tree.set_translator(BaseTranslator())
        self.add_dynamic_items(_PersistentMPButton, _PersistentQueueButton)
        await self.load_extension("core.music_cog")
        for cmd in self.tree.get_commands():
            if cmd.name != "manage":
                cmd.guild_only = True
        await self._sync_if_changed()

    _TREE_HASH_FILE = Path(__file__).parent / "db" / ".tree_hash"

    async def _sync_if_changed(self):
        from core.localization import _strings
        payload = self.tree.get_commands()
        h = hashlib.sha256(json.dumps([c.to_dict(self.tree) for c in payload], sort_keys=True).encode())
        h.update(json.dumps(_strings, sort_keys=True).encode())
        tree_hash = h.hexdigest()
        try:
            with open(self._TREE_HASH_FILE) as f:
                old_hash = f.read().strip()
        except OSError:
            old_hash = None
        if tree_hash == old_hash:
            return
        try:
            await self.tree.sync()
        except discord.HTTPException as e:
            print(f"[warn] Command tree sync failed (stale commands will be used): {e}")
            return
        try:
            with open(self._TREE_HASH_FILE, "w") as f:
                f.write(tree_hash)
        except OSError as e:
            print(f"[warn] Could not write tree hash file: {e}")

    async def close(self):
        from core.db import db
        try:
            cog = self.cogs.get("MusicCog")
            pending = []
            if cog:
                for state in list(cog.guild_states.values()):
                    state.suppress_after_callback = True
                    state.cancel_tasks()
                for task in list(cog._bg_fetch_tasks.values()):
                    task.cancel()
                    pending.append(task)
                for task in list(cog._pending_refresh.values()):
                    task.cancel()
                    pending.append(task)
                for session in list(cog.radio_sessions.values()):
                    for t in (session._timeout_task, session._initial_fetch):
                        if t and not t.done():
                            t.cancel()
                            pending.append(t)
                if getattr(cog, '_activity_cycle_task', None) and not cog._activity_cycle_task.done():
                    cog._activity_cycle_task.cancel()
                    pending.append(cog._activity_cycle_task)
            if pending:
                await asyncio.wait(pending, timeout=5)
        finally:
            try:
                await super().close()
            finally:
                await db.close()

bot = MusicBot()


def main():
    """Entry point: starts the bot."""
    bot.run(BOT_TOKEN)


if __name__ == "__main__":
    main()
