import asyncio
import configparser
from pathlib import Path
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
        "report_channel_id": section.getint("report_channel_id"),
        "download_path": section.get("download_path"),
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


def file_value(file_info, key, default=None):
    if isinstance(file_info, dict):
        return file_info.get(key, default)
    return getattr(file_info, key, default)


def format_peer_count(value):
    if value is None:
        return "Unknown"

    try:
        count = int(value)
    except (TypeError, ValueError):
        return "Unknown"

    if count < 0:
        return "Unknown"
    return str(count)


def torrent_file_top_level_name(name):
    parts = str(name or "").replace("\\", "/").strip("/").split("/")
    parts = [part for part in parts if part]
    if not parts:
        return None
    return parts[0]


def completed_download_items(files):
    items = {}

    for file_info in files:
        try:
            priority = int(file_value(file_info, "priority", 1) or 0)
        except (TypeError, ValueError):
            priority = 1
        if priority == 0:
            continue

        top_level_name = torrent_file_top_level_name(file_value(file_info, "name", ""))
        if top_level_name is None:
            continue

        try:
            size = int(file_value(file_info, "size", 0) or 0)
        except (TypeError, ValueError):
            size = 0

        try:
            progress = float(file_value(file_info, "progress", 0) or 0)
        except (TypeError, ValueError):
            progress = 0

        item = items.setdefault(
            top_level_name,
            {"name": top_level_name, "size": 0, "complete": True},
        )
        item["size"] += size
        item["complete"] = item["complete"] and progress >= 1

    return list(items.values())


def is_torrent_paused(torrent):
    state = str(torrent_value(torrent, "state", "")).lower()
    return state.startswith("paused") or state.startswith("stopped")


class QBittorrentControlView(discord.ui.View):
    def __init__(self, cog, torrent_hash, paused=False):
        super().__init__(timeout=None)
        self.cog = cog
        self.torrent_hash = torrent_hash
        self.paused = paused
        self.pause_button.emoji = self._pause_emoji()

    def _pause_emoji(self):
        if self.paused:
            return "\N{BLACK RIGHT-POINTING TRIANGLE}\ufe0f"
        return "\N{DOUBLE VERTICAL BAR}\ufe0f"

    @discord.ui.button(
        style=discord.ButtonStyle.secondary,
        custom_id="qbittorrent:pause",
    )
    async def pause_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        try:
            if self.paused:
                await self.cog._call(
                    self.cog.client.torrents_resume,
                    torrent_hashes=self.torrent_hash,
                )
                self.paused = False
                action = "resumed"
            else:
                await self.cog._call(
                    self.cog.client.torrents_pause,
                    torrent_hashes=self.torrent_hash,
                )
                self.paused = True
                action = "paused"

            button.emoji = self._pause_emoji()
            await interaction.response.edit_message(view=self)
            print(f"qBittorrent torrent {action}: {self.torrent_hash}")
        except Exception as exc:
            print(f"Failed to toggle qBittorrent torrent: {exc}")
            await interaction.response.send_message(
                "Failed to update torrent state.",
                ephemeral=True,
            )

    @discord.ui.button(
        emoji="\N{BLACK SQUARE FOR STOP}\ufe0f",
        style=discord.ButtonStyle.danger,
        custom_id="qbittorrent:stop",
    )
    async def stop_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        try:
            await self.cog._call(
                self.cog.client.torrents_delete,
                delete_files=True,
                torrent_hashes=self.torrent_hash,
            )
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(view=self)
            print(f"qBittorrent torrent deleted with files: {self.torrent_hash}")
        except Exception as exc:
            print(f"Failed to delete qBittorrent torrent: {exc}")
            await interaction.response.send_message(
                "Failed to stop and delete torrent.",
                ephemeral=True,
            )


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
        self._views = {}
        self._pending_reactions = deque()
        self._source_requests = {}
        self._torrent_sources = {}
        self._pending_download_items = {}
        self._completed_reports = {}
        self.poll_torrents.start()
        self.poll_completed_downloads.start()

    def cog_unload(self):
        self.poll_torrents.cancel()
        self.poll_completed_downloads.cancel()

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

    async def _mark_next_pending_source_message(self, success=True, torrent_hash=None):
        while self._pending_reactions:
            pending = self._pending_reactions[0]
            if not success:
                self._pending_reactions.popleft()
                await self._add_source_reaction(
                    pending["message"],
                    "\N{WARNING SIGN}\ufe0f",
                )
                return

            if torrent_hash:
                source = self._source_requests.get(pending["source_id"])
                if source is not None:
                    source["torrent_hashes"].add(torrent_hash)
                    self._torrent_sources[torrent_hash] = pending["source_id"]

            pending["remaining"] -= 1
            if pending["remaining"] > 0:
                return

            self._pending_reactions.popleft()
            if pending["success_reaction"]:
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

    def _build_completed_embed(self, item):
        embed = discord.Embed(
            title="Download complete",
            color=discord.Color.green(),
        )
        embed.add_field(name="Name", value=str(item["name"]), inline=False)
        embed.add_field(name="Size", value=format_bytes(item["size"]), inline=True)
        return embed

    def _download_item_path(self, item):
        return Path(self.settings["download_path"]) / item["name"]

    async def _record_torrent_download_items(self, torrent_hash, torrent=None):
        source_id = self._torrent_sources.get(torrent_hash)
        source = self._source_requests.get(source_id)
        if source is None:
            return

        try:
            files = await self._call(self.client.torrents_files, torrent_hash)
            file_count = len(files)
            items = completed_download_items(files)
        except Exception as exc:
            print(f"qBittorrent files poll failed for {torrent_hash}: {exc}")
            file_count = 0
            items = []

        if not items and file_count == 0 and torrent is not None:
            items = [
                {
                    "name": torrent_value(torrent, "name", "Unknown"),
                    "size": torrent_value(
                        torrent,
                        "total_size",
                        torrent_value(torrent, "size", 0),
                    ),
                    "complete": False,
                }
            ]

        for item in items:
            report_key = (torrent_hash, item["name"])
            source["known_items"].add(report_key)
            source["known_torrents"].add(torrent_hash)
            if report_key in self._completed_reports:
                continue

            self._pending_download_items[report_key] = {
                "name": item["name"],
                "size": item["size"],
                "source_id": source_id,
                "path": self._download_item_path(item),
            }

    async def _delete_message(self, message, log_context):
        try:
            await message.delete()
            return True
        except discord.NotFound:
            return True
        except discord.HTTPException as exc:
            print(f"Failed to delete {log_context}: {exc}")
            return False

    async def _remove_missing_completed_reports(self):
        for report_key, report in list(self._completed_reports.items()):
            if report["path"].exists():
                continue

            deleted = await self._delete_message(
                report["message"],
                "qBittorrent completed report message",
            )
            if deleted:
                self._completed_reports.pop(report_key, None)
                source = self._source_requests.get(report["source_id"])
                if source is not None:
                    source["completed_items"].discard(report_key)

    async def _send_completed_report(self, report_channel, torrent_hash, source_id, item):
        report_key = (torrent_hash, item["name"])
        if report_key in self._completed_reports:
            return

        path = self._download_item_path(item)
        if not path.exists():
            return

        try:
            message = await report_channel.send(embed=self._build_completed_embed(item))
        except discord.HTTPException as exc:
            print(f"Failed to send qBittorrent completed report: {exc}")
            return

        self._completed_reports[report_key] = {
            "message": message,
            "path": path,
            "source_id": source_id,
        }
        source = self._source_requests.get(source_id)
        if source is not None:
            source["completed_items"].add(report_key)
        self._pending_download_items.pop(report_key, None)

    async def _delete_finished_source_messages(self):
        for source_id, source in list(self._source_requests.items()):
            if source["deleted"]:
                continue
            if not source["delete_when_complete"]:
                continue
            if not self._source_has_all_download_records(source):
                continue
            if not source["known_items"]:
                continue
            if not source["known_items"].issubset(source["completed_items"]):
                continue

            deleted = await self._delete_message(
                source["message"],
                "qBittorrent source message",
            )
            if deleted:
                source["deleted"] = True

    def _source_has_all_download_records(self, source):
        if len(source["torrent_hashes"]) < source["expected_torrents"]:
            return False
        return source["torrent_hashes"].issubset(source["known_torrents"])

    def _cleanup_finished_execution_records(self):
        for source_id, source in list(self._source_requests.items()):
            if not self._source_has_all_download_records(source):
                continue
            if not source["known_items"]:
                continue
            if not source["known_items"].issubset(source["completed_items"]):
                continue

            for torrent_hash in source["torrent_hashes"]:
                self._torrent_sources.pop(torrent_hash, None)
            self._source_requests.pop(source_id, None)

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
        seeds_connected = torrent_value(torrent, "num_seeds")
        seeds_total = torrent_value(torrent, "num_complete")
        leechers_connected = torrent_value(torrent, "num_leechs")
        leechers_total = torrent_value(torrent, "num_incomplete")

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
        embed.add_field(
            name="Peers",
            value=(
                "Seeds "
                f"{format_peer_count(seeds_connected)}/{format_peer_count(seeds_total)}\n"
                "Leechers "
                f"{format_peer_count(leechers_connected)}/"
                f"{format_peer_count(leechers_total)}"
            ),
            inline=True,
        )
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

        if added:
            source_id = message.id
            self._source_requests[source_id] = {
                "message": message,
                "expected_torrents": len(added),
                "torrent_hashes": set(),
                "known_torrents": set(),
                "known_items": set(),
                "completed_items": set(),
                "deleted": False,
                "delete_when_complete": not errors,
            }
            self._pending_reactions.append(
                {
                    "message": message,
                    "source_id": source_id,
                    "remaining": len(added),
                    "created_at": time.monotonic(),
                    "success_reaction": not errors,
                }
            )

        if errors:
            await self._add_source_reaction(message, "\N{WARNING SIGN}\ufe0f")
            print("qBittorrent add failed: " + " | ".join(errors[:5]))

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
            view = self._views.get(torrent_hash)
            if view is None:
                view = QBittorrentControlView(
                    self,
                    torrent_hash,
                    paused=is_torrent_paused(torrent),
                )
                self._views[torrent_hash] = view
            existing_message = self._messages.get(torrent_hash)
            if existing_message is None:
                try:
                    self._messages[torrent_hash] = await status_channel.send(
                        embed=embed,
                        view=view,
                    )
                    await self._mark_next_pending_source_message(
                        torrent_hash=torrent_hash
                    )
                    await self._record_torrent_download_items(torrent_hash, torrent)
                except discord.HTTPException as exc:
                    print(f"Failed to send qBittorrent status message: {exc}")
                    await self._mark_next_pending_source_message(success=False)
                continue

            try:
                view.paused = is_torrent_paused(torrent)
                view.pause_button.emoji = view._pause_emoji()
                await existing_message.edit(embed=embed, view=view)
                await self._record_torrent_download_items(torrent_hash, torrent)
            except discord.NotFound:
                self._messages.pop(torrent_hash, None)
                self._views.pop(torrent_hash, None)
            except discord.HTTPException as exc:
                print(f"Failed to edit qBittorrent status message: {exc}")

        stale_hashes = set(self._messages) - active_hashes
        for torrent_hash in stale_hashes:
            message = self._messages.pop(torrent_hash)
            self._views.pop(torrent_hash, None)
            try:
                await message.delete()
            except discord.NotFound:
                pass
            except discord.HTTPException as exc:
                print(f"Failed to delete qBittorrent status message: {exc}")

    @tasks.loop(seconds=10.0)
    async def poll_completed_downloads(self):
        try:
            report_channel = await self._get_channel(self.settings["report_channel_id"])
        except Exception as exc:
            print(f"qBittorrent completed report channel lookup failed: {exc}")
            return

        await self._remove_missing_completed_reports()

        for (torrent_hash, _), item in list(self._pending_download_items.items()):
            if item["path"].exists():
                await self._send_completed_report(
                    report_channel,
                    torrent_hash,
                    item["source_id"],
                    item,
                )

        await self._delete_finished_source_messages()
        self._cleanup_finished_execution_records()

    @poll_completed_downloads.before_loop
    async def before_poll_completed_downloads(self):
        await self.bot.wait_until_ready()

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
