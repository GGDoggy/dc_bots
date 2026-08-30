import os
import asyncio
import configparser
import discord
from discord.ext import commands
from discord import app_commands

config = configparser.ConfigParser()
config.read("config.ini", encoding="utf-8")

TOKEN = config["boss"]["token"]
SERVER_ID = int(config["boss"]["server_id"])
CHANNEL_ID = int(config["boss"]["channel_id"])

guild = discord.Object(id=SERVER_ID)
intents = discord.Intents.all()


class Bot(commands.Bot):
    async def setup_hook(self):
        await load_all()
        self.tree.copy_global_to(guild=guild)
        await self.tree.sync(guild=guild)


bot = Bot(command_prefix=commands.when_mentioned, intents=intents)
bot.channel_id = CHANNEL_ID


def get_cog_extensions():
    return {
        f"cogs.{filename[:-3]}"
        for filename in os.listdir("./cogs")
        if filename.endswith(".py") and not filename.startswith("_")
    }


async def load_all():
    cog_extensions = get_cog_extensions()
    loaded_extensions = {
        extension for extension in bot.extensions if extension.startswith("cogs.")
    }

    unloaded = []
    loaded = []
    reloaded = []

    for extension in loaded_extensions - cog_extensions:
        await bot.unload_extension(extension)
        unloaded.append(extension)

    for extension in cog_extensions - loaded_extensions:
        await bot.load_extension(extension)
        loaded.append(extension)

    for extension in cog_extensions & loaded_extensions:
        await bot.reload_extension(extension)
        reloaded.append(extension)

    return {
        "loaded": sorted(loaded),
        "reloaded": sorted(reloaded),
        "unloaded": sorted(unloaded),
    }


@bot.event
async def on_ready():
    print(f"Logged in as --> {bot.user}")
    channel = bot.get_channel(bot.channel_id) or await bot.fetch_channel(bot.channel_id)
    await channel.send(f"{bot.user} is now online!")


@bot.tree.command(name="reload_all", description="Reload all features")
@app_commands.guilds(guild)
async def reload_all(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    result = await load_all()
    bot.tree.copy_global_to(guild=guild)
    await bot.tree.sync(guild=guild)
    await interaction.followup.send(
        "All features have been reloaded.\n"
        f"Loaded: {len(result['loaded'])}\n"
        f"Reloaded: {len(result['reloaded'])}\n"
        f"Unloaded: {len(result['unloaded'])}",
        ephemeral=True,
    )


@bot.tree.command(name="close", description="Shut down the bot")
@app_commands.guilds(guild)
async def close(interaction: discord.Interaction):
    await interaction.response.send_message(f"Got it! {bot.user} is shutting down ~")
    asyncio.create_task(bot.close())


async def main():
    async with bot:
        await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
