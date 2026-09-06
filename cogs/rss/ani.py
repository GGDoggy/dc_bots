import asyncio
import io
from datetime import datetime, timedelta, timezone
import json
from email.message import Message
from email.utils import collapse_rfc2231_value
from pathlib import Path
import re
from urllib.parse import unquote, unquote_to_bytes, urlparse
import urllib.request
import xml.etree.ElementTree as ET

import discord
from discord import app_commands

from cogs.rss import RSSPollingCog, load_rss_config


LOG_RETENTION_DAYS = 10


def fetch_rss_xml(url, timeout=30):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "dc-bots-rss/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def _xml_name(element):
    return element.tag.rsplit("}", 1)[-1].lower()


def _child_text(element, child_name):
    for child in element:
        if _xml_name(child) == child_name:
            return (child.text or "").strip()
    return ""


def _child_enclosure_url(element):
    for child in element:
        if _xml_name(child) != "enclosure":
            continue
        if child.attrib.get("type") != "application/x-bittorrent":
            continue
        enclosure_url = child.attrib.get("url", "").strip()
        if enclosure_url:
            return enclosure_url
    return ""


def parse_feed_items(xml_data):
    root = ET.fromstring(xml_data)
    items = []

    for element in root.iter():
        element_name = _xml_name(element)
        if element_name not in {"item", "entry"}:
            continue

        title = _child_text(element, "title")
        enclosure_url = _child_enclosure_url(element)
        if title and enclosure_url:
            items.append({"title": title, "enclosure_url": enclosure_url})

    return items


async def fetch_feed_items(url):
    xml_data = await asyncio.to_thread(fetch_rss_xml, url)
    return parse_feed_items(xml_data)


def filename_from_url(url):
    filename = unquote(Path(urlparse(url).path).name)
    return filename or "download.torrent"


def filename_from_content_disposition(content_disposition):
    if not content_disposition:
        return ""

    message = Message()
    message["Content-Disposition"] = content_disposition
    params = dict(message.get_params(header="Content-Disposition")[1:])
    filename_star = params.get("filename*")
    if filename_star:
        if isinstance(filename_star, tuple):
            return collapse_rfc2231_value(filename_star)
        try:
            charset, _, encoded_filename = filename_star.split("'", 2)
            return unquote_to_bytes(encoded_filename).decode(charset or "utf-8")
        except (LookupError, UnicodeDecodeError, ValueError):
            pass

    filename = params.get("filename", "")
    if isinstance(filename, tuple):
        return collapse_rfc2231_value(filename)
    if filename:
        return unquote(filename)

    return ""


async def fetch_file(url):
    return await asyncio.to_thread(fetch_file_sync, url)


def fetch_file_sync(url, timeout=30):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "dc-bots-rss/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        filename = (
            filename_from_content_disposition(response.headers.get("Content-Disposition"))
            or filename_from_url(response.geturl())
            or filename_from_url(url)
        )
        return response.read(), filename


class AniRSSCog(RSSPollingCog):
    def __init__(self, bot):
        settings = load_rss_config()
        self.url = settings["ani_url"]
        self.channel_id = int(settings["ani_channel_id"])
        self.log_path = Path(settings["ani_log_path"])
        self.pattern_path = Path(settings["ani_pattern_path"])
        super().__init__(bot=bot, interval_seconds=settings["ani_interval_seconds"])

    async def run_once(self):
        items = await fetch_feed_items(self.url)
        records = self._load_log()
        sent_urls = {record["enclosure_url"] for record in records if record.get("enclosure_url")}

        patterns = self._load_patterns()
        new_items = [
            item
            for item in items
            if item["enclosure_url"] not in sent_urls
            and self._matches_patterns(item["title"], patterns)
        ]
        if not new_items:
            return

        channel = await self._get_channel()
        sent_at = self._now()
        for item in reversed(new_items):
            enclosure_url = item["enclosure_url"]
            try:
                file_data, filename = await fetch_file(enclosure_url)
                await channel.send(
                    file=discord.File(io.BytesIO(file_data), filename=filename)
                )
            except discord.HTTPException as exc:
                print(f"Ani RSS file send failed for {self.channel_id}: {exc}")
                continue
            except Exception as exc:
                print(f"Ani RSS file fetch failed for {enclosure_url}: {exc}")
                continue

            records.append(
                {
                    "title": item["title"],
                    "enclosure_url": enclosure_url,
                    "filename": filename,
                    "sent_at": sent_at.isoformat(),
                }
            )

        self._save_pruned_log(records)

    async def _get_channel(self):
        channel = self.bot.get_channel(self.channel_id)
        if channel is not None:
            return channel
        return await self.bot.fetch_channel(self.channel_id)

    def _matches_patterns(self, title, patterns):
        return any(pattern.search(title) for pattern in patterns)

    def _read_pattern_lines(self):
        if not self.pattern_path.exists():
            return []

        lines = self.pattern_path.read_text(encoding="utf-8").splitlines()
        return [line.strip() for line in lines if line.strip()]

    def _load_pattern_lines(self):
        try:
            return self._read_pattern_lines()
        except OSError as exc:
            print(f"Ani RSS pattern read failed for {self.pattern_path}: {exc}")
            return []

    def _save_pattern_lines(self, pattern_lines):
        self.pattern_path.parent.mkdir(parents=True, exist_ok=True)
        text = "\n".join(pattern_lines)
        if text:
            text += "\n"
        self.pattern_path.write_text(text, encoding="utf-8")

    def _parse_pattern_indexes(self, value):
        tokens = [token for token in re.split(r"[\s,]+", value.strip()) if token]
        if not tokens:
            raise ValueError("Please provide at least one pattern index.")

        indexes = []
        for token in tokens:
            try:
                index = int(token)
            except ValueError as exc:
                raise ValueError(f"Invalid pattern index: {token}") from exc
            indexes.append(index)

        return sorted(set(indexes))

    def _format_pattern_lines(self, pattern_lines):
        if not pattern_lines:
            return "No Ani RSS patterns are configured."

        lines = [f"{index}: {pattern}" for index, pattern in enumerate(pattern_lines)]
        output = "\n".join(lines)
        max_code_block_content = 1900
        if len(output) > max_code_block_content:
            output = output[:max_code_block_content].rstrip() + "\n..."
        return f"```text\n{output}\n```"

    @app_commands.command(
        name="add_ani_pattern",
        description="Add an Ani RSS title regex pattern",
    )
    async def add_ani_pattern(self, interaction: discord.Interaction, pattern: str):
        pattern_text = pattern.strip()
        if not pattern_text:
            await interaction.response.send_message(
                "Pattern cannot be empty.",
                ephemeral=True,
            )
            return

        try:
            re.compile(pattern_text)
        except re.error as exc:
            await interaction.response.send_message(
                f"Invalid regex pattern: {exc}",
                ephemeral=True,
            )
            return

        try:
            pattern_lines = self._read_pattern_lines()
        except OSError as exc:
            await interaction.response.send_message(
                f"Failed to read pattern file: {exc}",
                ephemeral=True,
            )
            return

        if pattern_text in pattern_lines:
            await interaction.response.send_message(
                f"Pattern already exists at index {pattern_lines.index(pattern_text)}.",
                ephemeral=True,
            )
            return

        pattern_lines.append(pattern_text)
        try:
            self._save_pattern_lines(pattern_lines)
        except OSError as exc:
            await interaction.response.send_message(
                f"Failed to save pattern file: {exc}",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"Added Ani RSS pattern at index {len(pattern_lines) - 1}.",
            ephemeral=True,
        )

    @app_commands.command(
        name="list_ani_pattern",
        description="List Ani RSS title regex patterns",
    )
    async def list_ani_pattern(self, interaction: discord.Interaction):
        try:
            pattern_lines = self._read_pattern_lines()
        except OSError as exc:
            await interaction.response.send_message(
                f"Failed to read pattern file: {exc}",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            self._format_pattern_lines(pattern_lines),
            ephemeral=True,
        )

    @app_commands.command(
        name="remove_ani_pattern",
        description="Remove Ani RSS title regex patterns by index",
    )
    async def remove_ani_pattern(self, interaction: discord.Interaction, indexes: str):
        try:
            parsed_indexes = self._parse_pattern_indexes(indexes)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        try:
            pattern_lines = self._read_pattern_lines()
        except OSError as exc:
            await interaction.response.send_message(
                f"Failed to read pattern file: {exc}",
                ephemeral=True,
            )
            return

        if not pattern_lines:
            await interaction.response.send_message(
                "No Ani RSS patterns are configured.",
                ephemeral=True,
            )
            return

        invalid_indexes = [
            index
            for index in parsed_indexes
            if index < 0 or index >= len(pattern_lines)
        ]
        if invalid_indexes:
            invalid_text = ", ".join(str(index) for index in invalid_indexes)
            await interaction.response.send_message(
                f"Pattern index out of range: {invalid_text}",
                ephemeral=True,
            )
            return

        remaining_lines = [
            pattern
            for index, pattern in enumerate(pattern_lines)
            if index not in parsed_indexes
        ]

        try:
            self._save_pattern_lines(remaining_lines)
        except OSError as exc:
            await interaction.response.send_message(
                f"Failed to save pattern file: {exc}",
                ephemeral=True,
            )
            return

        removed_text = "\n".join(
            f"{index}: {pattern_lines[index]}" for index in parsed_indexes
        )
        max_code_block_content = 1600
        if len(removed_text) > max_code_block_content:
            removed_text = removed_text[:max_code_block_content].rstrip() + "\n..."

        await interaction.response.send_message(
            "Removed Ani RSS patterns:\n"
            f"```text\n{removed_text}\n```\n"
            f"Remaining patterns: {len(remaining_lines)}",
            ephemeral=True,
        )

    def _load_patterns(self):
        if not self.pattern_path.exists():
            print(f"Ani RSS pattern file does not exist: {self.pattern_path}")
            return []

        patterns = []
        for line_number, pattern_text in enumerate(self._load_pattern_lines(), start=1):
            try:
                patterns.append(re.compile(pattern_text))
            except re.error as exc:
                print(
                    "Ani RSS pattern compile failed "
                    f"for {self.pattern_path}:{line_number}: {exc}"
                )
        return patterns

    def _load_log(self):
        if not self.log_path.exists():
            return []

        try:
            data = json.loads(self.log_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Ani RSS log read failed for {self.log_path}: {exc}")
            return []

        if not isinstance(data, list):
            return []

        records = []
        for item in data:
            if not isinstance(item, dict):
                continue
            title = item.get("title")
            sent_at = item.get("sent_at")
            enclosure_url = item.get("enclosure_url")
            filename = item.get("filename")
            if isinstance(title, str) and isinstance(sent_at, str):
                record = {"title": title, "sent_at": sent_at}
                if isinstance(enclosure_url, str):
                    record["enclosure_url"] = enclosure_url
                if isinstance(filename, str):
                    record["filename"] = filename
                records.append(record)
        return records

    def _save_pruned_log(self, records):
        pruned_records = self._prune_old_records(records)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.write_text(
            json.dumps(pruned_records, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _prune_old_records(self, records):
        cutoff = self._now() - timedelta(days=LOG_RETENTION_DAYS)
        pruned_records = []
        for record in records:
            sent_at = self._parse_sent_at(record["sent_at"])
            if sent_at is None or sent_at >= cutoff:
                pruned_records.append(record)
        return pruned_records

    def _parse_sent_at(self, value):
        try:
            sent_at = datetime.fromisoformat(value)
        except ValueError:
            return None
        if sent_at.tzinfo is None:
            sent_at = sent_at.replace(tzinfo=timezone.utc)
        return sent_at

    def _now(self):
        return datetime.now(timezone.utc)


async def setup(bot):
    await bot.add_cog(AniRSSCog(bot))
