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

    def _load_patterns(self):
        if not self.pattern_path.exists():
            print(f"Ani RSS pattern file does not exist: {self.pattern_path}")
            return []

        try:
            lines = self.pattern_path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            print(f"Ani RSS pattern read failed for {self.pattern_path}: {exc}")
            return []

        patterns = []
        for line_number, line in enumerate(lines, start=1):
            pattern_text = line.strip()
            if not pattern_text:
                continue
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
