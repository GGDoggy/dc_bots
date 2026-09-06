import asyncio
import configparser
import importlib.util
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks


RSS_COG_DIR = Path(__file__).with_name("rss")


def load_rss_config():
    config = configparser.ConfigParser()
    config.read("config.ini", encoding="utf-8")
    section = config["rss"] if config.has_section("rss") else {}

    return {
        "bot_token": section.get("bot_token", ""),
        "server_id": int(section.get("server_id", 0)),
        "ani_url": section.get("ani_url", ""),
        "ani_channel_id": int(section.get("ani_channel_id", 0)),
        "ani_interval_seconds": float(section.get("ani_interval_seconds", 300.0)),
        "ani_log_path": section.get("ani_log_path", "rss_ani_log.json"),
        "ani_pattern_path": section.get("ani_pattern_path", "rss_ani_patterns.txt"),
    }


class RSSPollingCog(commands.Cog):
    def __init__(self, bot, interval_seconds):
        self.bot = bot
        self.interval_seconds = float(interval_seconds)
        self.poll_feed.change_interval(seconds=self.interval_seconds)
        self.poll_feed.start()

    def cog_unload(self):
        self.poll_feed.cancel()

    async def run_once(self):
        raise NotImplementedError

    @tasks.loop(seconds=300.0)
    async def poll_feed(self):
        try:
            await self.run_once()
        except Exception as exc:
            print(f"RSS polling task failed: {exc}")

    @poll_feed.before_loop
    async def before_poll_feed(self):
        await self.bot.wait_until_ready()


def get_rss_cog_paths():
    if not RSS_COG_DIR.exists():
        return []
    return sorted(
        path
        for path in RSS_COG_DIR.glob("*.py")
        if not path.name.startswith("_")
    )


async def load_rss_cog(bot, path):
    module_name = f"_rss_cogs_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load RSS cog: {path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    setup_func = getattr(module, "setup", None)
    if setup_func is None:
        return

    result = setup_func(bot)
    if result is not None:
        await result


class RSSBot(commands.Bot):
    def __init__(self, settings):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.settings = settings
        self.guild = discord.Object(id=settings["server_id"])

    async def setup_hook(self):
        for path in get_rss_cog_paths():
            await load_rss_cog(self, path)
        if self.settings["server_id"]:
            self.tree.copy_global_to(guild=self.guild)
            await self.tree.sync(guild=self.guild)

    async def on_ready(self):
        print(f"RSS bot logged in as --> {self.user}")


class RSSManagerCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.rss_bot = None
        self.rss_task = None
        self.start_task = asyncio.create_task(self.start_rss_bot())
        self.start_task.add_done_callback(self._log_start_result)

    async def cog_unload(self):
        if self.start_task and not self.start_task.done():
            self.start_task.cancel()
        await self.stop_rss_bot()

    def _log_start_result(self, task):
        if task.cancelled():
            return
        try:
            print(task.result())
        except Exception as exc:
            print(f"RSS bot start failed: {exc}")

    def _log_rss_bot_result(self, task):
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            print(f"RSS bot stopped with error: {exc}")

    async def start_rss_bot(self):
        if self.rss_task and not self.rss_task.done():
            return "RSS bot is already running."

        settings = load_rss_config()
        if not settings["bot_token"] or settings["bot_token"] == "rss.bot_token":
            return "RSS bot token is not configured."

        self.rss_bot = RSSBot(settings)
        self.rss_task = asyncio.create_task(self.rss_bot.start(settings["bot_token"]))
        self.rss_task.add_done_callback(self._log_rss_bot_result)
        return "RSS bot start requested."

    async def stop_rss_bot(self):
        if self.rss_bot is None:
            return "RSS bot is not running."

        await self.rss_bot.close()
        if self.rss_task:
            try:
                await self.rss_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                print(f"RSS bot stopped with error: {exc}")

        self.rss_bot = None
        self.rss_task = None
        return "RSS bot stopped."

    async def restart_rss_bot(self):
        stop_result = await self.stop_rss_bot()
        start_result = await self.start_rss_bot()
        return f"{stop_result}\n{start_result}"

    @app_commands.command(
        name="restart_rss",
        description="Restart the RSS bot",
    )
    async def restart_rss(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        result = await self.restart_rss_bot()
        await interaction.followup.send(result, ephemeral=True)


async def setup(bot):
    await bot.add_cog(RSSManagerCog(bot))


async def main():
    settings = load_rss_config()
    if not settings["bot_token"] or settings["bot_token"] == "rss.bot_token":
        raise RuntimeError("Missing [rss] bot_token in config.ini")

    bot = RSSBot(settings)
    async with bot:
        await bot.start(settings["bot_token"])


if __name__ == "__main__":
    asyncio.run(main())
