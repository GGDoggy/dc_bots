import asyncio
import configparser
import re
import time
from collections import deque
from urllib.parse import parse_qs, urlparse

import discord
import qbittorrentapi
from discord import app_commands
from discord.ext import commands, tasks


MAGNET_HASH_RE = re.compile(r"^[A-Fa-f0-9]{40}$|^[A-Za-z2-7]{32}$")
PENDING_REACTION_TIMEOUT_SECONDS = 120


def load_qbittorrent_config():
    config = configparser.ConfigParser()
    config.read("config.ini", encoding="utf-8")
    section = config["qbittorrent"]

    return {
        "bot_token": section.get("bot_token", fallback=""),
        "server_id": section.getint("server_id"),
        "host": section.get("host"),
        "username": section.get("username"),
        "password": section.get("password"),
        "listen_channel_id": section.getint("listen_channel_id"),
        "status_channel_id": section.getint("status_channel_id"),
    }


def is_valid_magnet_uri(line):
    if not line.startswith("magnet:?") or any(char.isspace() for char in line):
        return False

    parsed = urlparse(line)
    if parsed.scheme != "magnet" or parsed.path:
        return False

    query = parse_qs(parsed.query)
    xt_values = query.get("xt", [])
    for xt in xt_values:
        if not xt.startswith("urn:btih:"):
            continue
        info_hash = xt.removeprefix("urn:btih:")
        if MAGNET_HASH_RE.fullmatch(info_hash):
            return True

    return False


def format_bytes(value):
    value = float(value or 0)
    units = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024


def format_speed(value):
    return f"{format_bytes(value)}/s"


def format_eta(seconds):
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "0s"
    if seconds >= 8640000:
        return "Unknown"

    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)

    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts)


def torrent_value(torrent, key, default=None):
    if isinstance(torrent, dict):
        return torrent.get(key, default)
    return getattr(torrent, key, default)


class QBittorrentCog(commands.Cog):
    def __init__(self, bot, settings=None):
        self.bot = bot
        self.settings = settings or load_qbittorrent_config()
        self.client = qbittorrentapi.Client(
            host=self.settings["host"],
            username=self.settings["username"],
            password=self.settings["password"],
        )
        self._auth_lock = asyncio.Lock()
        self._logged_in = False
        self._messages = {}
        self._pending_reactions = deque()
        self.poll_torrents.start()

    def cog_unload(self):
        self.poll_torrents.cancel()

    async def _call(self, func, *args, **kwargs):
        await self._ensure_logged_in()
        return await asyncio.to_thread(func, *args, **kwargs)

    async def _ensure_logged_in(self):
        if self._logged_in:
            return

        async with self._auth_lock:
            if self._logged_in:
                return
            await asyncio.to_thread(self.client.auth_log_in)
            self._logged_in = True

    async def _get_channel(self, channel_id):
        channel = self.bot.get_channel(channel_id)
        if channel is not None:
            return channel
        return await self.bot.fetch_channel(channel_id)

    async def _add_torrent_file(self, attachment):
        data = await attachment.read()
        return await self._call(
            self.client.torrents_add,
            torrent_files={attachment.filename: data},
        )

    async def _add_magnet(self, magnet_uri):
        return await self._call(self.client.torrents_add, urls=magnet_uri)

    async def _add_source_reaction(self, message, emoji):
        try:
            await message.add_reaction(emoji)
        except discord.NotFound:
            pass
        except discord.HTTPException as exc:
            print(f"Failed to add qBittorrent source reaction: {exc}")

    async def _mark_next_pending_source_message(self, success=True):
        while self._pending_reactions:
            pending = self._pending_reactions[0]
            if not success:
                self._pending_reactions.popleft()
                await self._add_source_reaction(
                    pending["message"],
                    "\N{WARNING SIGN}\ufe0f",
                )
                return

            pending["remaining"] -= 1
            if pending["remaining"] > 0:
                return

            self._pending_reactions.popleft()
            await self._add_source_reaction(
                pending["message"],
                "\N{WHITE HEAVY CHECK MARK}",
            )
            return

    async def _mark_expired_pending_source_messages(self):
        now = time.monotonic()
        while self._pending_reactions:
            pending = self._pending_reactions[0]
            if now - pending["created_at"] < PENDING_REACTION_TIMEOUT_SECONDS:
                return

            self._pending_reactions.popleft()
            await self._add_source_reaction(
                pending["message"],
                "\N{WARNING SIGN}\ufe0f",
            )

    def _build_embed(self, torrent):
        name = torrent_value(torrent, "name", "Unknown")
        state = torrent_value(torrent, "state", "unknown")
        progress = float(torrent_value(torrent, "progress", 0) or 0)
        downloaded = torrent_value(torrent, "downloaded", 0)
        total_size = torrent_value(torrent, "total_size", 0)
        uploaded = torrent_value(torrent, "uploaded", 0)
        download_speed = torrent_value(torrent, "dlspeed", 0)
        upload_speed = torrent_value(torrent, "upspeed", 0)
        eta = torrent_value(torrent, "eta", 0)
        torrent_hash = torrent_value(torrent, "hash", "")

        embed = discord.Embed(
            title=name,
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Status", value=str(state), inline=True)
        embed.add_field(name="Progress", value=f"{progress * 100:.2f}%", inline=True)
        embed.add_field(name="ETA", value=format_eta(eta), inline=True)
        embed.add_field(
            name="Speed",
            value=f"DL {format_speed(download_speed)}\nUL {format_speed(upload_speed)}",
            inline=True,
        )
        embed.add_field(
            name="Downloaded",
            value=f"{format_bytes(downloaded)} / {format_bytes(total_size)}",
            inline=True,
        )
        embed.add_field(name="Uploaded", value=format_bytes(uploaded), inline=True)
        embed.set_footer(text=f"hash {str(torrent_hash)[:12]}")
        return embed

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return
        if message.guild is None or message.guild.id != self.settings["server_id"]:
            return
        if message.channel.id != self.settings["listen_channel_id"]:
            return

        torrent_attachments = [
            attachment
            for attachment in message.attachments
            if attachment.filename.lower().endswith(".torrent")
        ]
        magnet_uris = [
            line
            for line in (line.strip() for line in message.content.splitlines())
            if is_valid_magnet_uri(line)
        ]

        if not torrent_attachments and not magnet_uris:
            return

        added = []
        errors = []

        for attachment in torrent_attachments:
            try:
                await self._add_torrent_file(attachment)
                added.append(attachment.filename)
            except Exception as exc:
                errors.append(f"{attachment.filename}: {exc}")

        for magnet_uri in magnet_uris:
            try:
                await self._add_magnet(magnet_uri)
                added.append(magnet_uri[:80])
            except Exception as exc:
                errors.append(f"{magnet_uri[:80]}: {exc}")

        if errors:
            await self._add_source_reaction(message, "\N{WARNING SIGN}\ufe0f")
            print("qBittorrent add failed: " + " | ".join(errors[:5]))
        elif added:
            self._pending_reactions.append(
                {
                    "message": message,
                    "remaining": len(added),
                    "created_at": time.monotonic(),
                }
            )

    @tasks.loop(seconds=2.0)
    async def poll_torrents(self):
        try:
            torrents = await self._call(self.client.torrents_info)
            status_channel = await self._get_channel(self.settings["status_channel_id"])
        except Exception as exc:
            print(f"qBittorrent poll failed: {exc}")
            return

        active_hashes = {
            str(torrent_value(torrent, "hash"))
            for torrent in torrents
            if torrent_value(torrent, "hash")
        }

        await self._mark_expired_pending_source_messages()

        for torrent in torrents:
            torrent_hash = str(torrent_value(torrent, "hash", ""))
            if not torrent_hash:
                continue

            embed = self._build_embed(torrent)
            existing_message = self._messages.get(torrent_hash)
            if existing_message is None:
                try:
                    self._messages[torrent_hash] = await status_channel.send(embed=embed)
                    await self._mark_next_pending_source_message()
                except discord.HTTPException as exc:
                    print(f"Failed to send qBittorrent status message: {exc}")
                    await self._mark_next_pending_source_message(success=False)
                continue

            try:
                await existing_message.edit(embed=embed)
            except discord.NotFound:
                self._messages.pop(torrent_hash, None)
            except discord.HTTPException as exc:
                print(f"Failed to edit qBittorrent status message: {exc}")

        stale_hashes = set(self._messages) - active_hashes
        for torrent_hash in stale_hashes:
            message = self._messages.pop(torrent_hash)
            try:
                await message.delete()
            except discord.NotFound:
                pass
            except discord.HTTPException as exc:
                print(f"Failed to delete qBittorrent status message: {exc}")

    @poll_torrents.before_loop
    async def before_poll_torrents(self):
        await self.bot.wait_until_ready()


class QBittorrentBot(commands.Bot):
    def __init__(self, settings):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.settings = settings

    async def setup_hook(self):
        await self.add_cog(QBittorrentCog(self, self.settings))

    async def on_ready(self):
        print(f"qBittorrent bot logged in as --> {self.user}")


class QBittorrentManagerCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.qbittorrent_bot = None
        self.qbittorrent_task = None
        self.start_task = asyncio.create_task(self.start_qbittorrent_bot())
        self.start_task.add_done_callback(self._log_start_result)

    async def cog_unload(self):
        if self.start_task and not self.start_task.done():
            self.start_task.cancel()
        await self.stop_qbittorrent_bot()

    def _log_start_result(self, task):
        if task.cancelled():
            return
        try:
            print(task.result())
        except Exception as exc:
            print(f"qBittorrent bot start failed: {exc}")

    def _log_qbittorrent_bot_result(self, task):
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            print(f"qBittorrent bot stopped with error: {exc}")

    async def start_qbittorrent_bot(self):
        if self.qbittorrent_task and not self.qbittorrent_task.done():
            return "qBittorrent bot is already running."

        settings = load_qbittorrent_config()
        if not settings["bot_token"] or settings["bot_token"] == "qbittorrent.bot_token":
            return "qBittorrent bot token is not configured."

        self.qbittorrent_bot = QBittorrentBot(settings)
        self.qbittorrent_task = asyncio.create_task(
            self.qbittorrent_bot.start(settings["bot_token"])
        )
        self.qbittorrent_task.add_done_callback(self._log_qbittorrent_bot_result)
        return "qBittorrent bot start requested."

    async def stop_qbittorrent_bot(self):
        if self.qbittorrent_bot is None:
            return "qBittorrent bot is not running."

        await self.qbittorrent_bot.close()
        if self.qbittorrent_task:
            try:
                await self.qbittorrent_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                print(f"qBittorrent bot stopped with error: {exc}")

        self.qbittorrent_bot = None
        self.qbittorrent_task = None
        return "qBittorrent bot stopped."

    async def restart_qbittorrent_bot(self):
        stop_result = await self.stop_qbittorrent_bot()
        start_result = await self.start_qbittorrent_bot()
        return f"{stop_result}\n{start_result}"

    @app_commands.command(
        name="restart_qbittorrent",
        description="Restart the qBittorrent bot",
    )
    async def restart_qbittorrent(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        result = await self.restart_qbittorrent_bot()
        await interaction.followup.send(result, ephemeral=True)


async def setup(bot):
    await bot.add_cog(QBittorrentManagerCog(bot))


async def main():
    settings = load_qbittorrent_config()
    if not settings["bot_token"] or settings["bot_token"] == "qbittorrent.bot_token":
        raise RuntimeError("Missing [qbittorrent] bot_token in config.ini")

    bot = QBittorrentBot(settings)
    async with bot:
        await bot.start(settings["bot_token"])


if __name__ == "__main__":
    asyncio.run(main())
