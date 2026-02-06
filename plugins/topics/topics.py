import asyncio
import calendar
import hashlib
import json
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import aiohttp
import anyio
import feedparser
from loguru import logger

from twipsybot.plugin import PluginBase


class TopicsPlugin(PluginBase):
    description = "主题插件，为自动发帖提供内容源"

    def __init__(self, context):
        super().__init__(context)
        self.source = str(self.config.get("source") or "txt").strip().lower()
        self.txt_ai_prefix = self.config.get("txt_ai_prefix") or ""
        self.txt_start_line = self.config.get("txt_start_line", 1)
        self.rss_list = self.config.get("rss_list") or []
        self.rss_ai = bool(self.config.get("rss_ai", False))
        self.rss_post_mode = (
            str(self.config.get("rss_post_mode") or "batch").strip().lower()
        )
        if self.rss_post_mode not in {"batch", "rotate"}:
            self.rss_post_mode = "batch"
        self.rss_ai_prefix = (
            self.config.get("rss_ai_prefix")
            or "发表一段感想和相关知识（不超过150字），"
            "不加链接，不加引号：\n\n{summary}\n\n{title}\n{link}"
        )
        self.topics = []

    async def initialize(self) -> bool:
        try:
            if not self.db:
                logger.error("Topics plugin missing db instance")
                return False
            if self.source == "rss":
                await self._initialize_rss_data()
            else:
                await self._load_topics()
                await self._initialize_plugin_data()
            if self.source == "rss":
                self._log_plugin_action(
                    "initialized", f"RSS feeds: {len(self._get_rss_urls())}"
                )
            else:
                self._log_plugin_action(
                    "initialized", f"Custom topics: {len(self.topics)}"
                )
            return True
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.error(f"Topics plugin initialization failed: {e}")
            return False

    async def cleanup(self) -> None:
        await super().cleanup()

    async def on_auto_post(self) -> dict[str, Any] | None:
        try:
            if self.source == "rss":
                if contents := await self._get_next_rss_posts():
                    self._log_plugin_action("direct post", f"count={len(contents)}")
                    return {"contents": contents, "plugin_name": self.name}
                return None
            topic = await self._get_next_topic()
            if self._is_pure_url(topic):
                self._log_plugin_action("direct post", topic)
                return {"content": topic, "plugin_name": self.name}
            return {
                "modify_prompt": True,
                "plugin_prompt": self.txt_ai_prefix.format(topic=topic),
                "plugin_name": self.name,
            }
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.error(f"Topics plugin auto-post hook failed: {e}")
            return None

    @staticmethod
    def _is_pure_url(text: str) -> bool:
        s = text.strip()
        if not s or s != text:
            return False
        parsed = urlparse(s)
        if parsed.scheme not in {"http", "https"}:
            return False
        return bool(parsed.netloc)

    async def _initialize_plugin_data(self) -> None:
        try:
            last_used_line = await self.db.get_plugin_data("Topics", "last_used_line")
            if last_used_line is None:
                initial_index = max(0, self.txt_start_line - 1)
                if self.topics:
                    initial_index %= len(self.topics)
                await self.db.set_plugin_data(
                    "Topics", "last_used_line", str(initial_index)
                )
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.warning(f"Topics plugin DB initialization failed: {e}")
            raise

    async def _initialize_rss_data(self) -> None:
        try:
            recent = await self.db.get_plugin_data("Topics", "rss_recent_keys")
            if recent is None:
                await self.db.set_plugin_data("Topics", "rss_recent_keys", "[]")
            last_feed_idx = await self.db.get_plugin_data("Topics", "rss_last_feed_idx")
            if last_feed_idx is None:
                await self.db.set_plugin_data("Topics", "rss_last_feed_idx", "0")
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.warning(f"Topics plugin RSS DB initialization failed: {e}")
            raise

    def _use_default_topics(self) -> None:
        self.topics = ["Technology", "Life", "Learning", "Reflection", "Innovation"]
        logger.info(f"Using default topics: {self.topics}")

    async def _load_topics(self) -> None:
        try:
            topics_file_path = Path(__file__).parent / "topics.txt"
            if not topics_file_path.exists():
                logger.warning(f"Topics file not found: {topics_file_path}")
                self._use_default_topics()
                return
            async with await anyio.open_file(
                topics_file_path, "r", encoding="utf-8"
            ) as f:
                content = await f.read()
            self.topics = [
                line.strip() for line in content.splitlines() if line.strip()
            ]
            if not self.topics:
                logger.warning("Topics file is empty")
                self._use_default_topics()
                return
        except Exception as e:
            logger.warning(f"Failed to load topics file: {e}")
            self._use_default_topics()

    async def _get_next_rss_posts(self) -> list[str]:
        urls = self._get_rss_urls()
        if not urls:
            logger.warning("RSS source enabled but rss_list is empty")
            return []

        if self.rss_post_mode == "rotate":
            return await self._get_next_rss_posts_rotate(urls)

        recent_keys = await self._get_recent_rss_keys()
        recent_set = set(recent_keys)
        candidates = await self._fetch_all_rss_candidates(urls)
        selected = self._select_latest_per_feed(urls, candidates, recent_set)
        if not selected:
            return []

        contents, updated_recent = await self._render_selected_rss_entries(
            selected, recent_keys
        )
        await self._set_recent_rss_keys(updated_recent)
        return contents

    async def _get_next_rss_posts_rotate(self, urls: list[str]) -> list[str]:
        recent_keys = await self._get_recent_rss_keys()
        recent_set = set(recent_keys)
        start_idx = await self._get_last_rss_feed_idx()
        if start_idx >= len(urls):
            start_idx = 0

        timeout = aiohttp.ClientTimeout(total=60)
        headers = {"User-Agent": "twipsy-rss"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            for step in range(len(urls)):
                feed_idx = (start_idx + step) % len(urls)
                url = urls[feed_idx]
                try:
                    candidates = await self._fetch_rss_candidates(
                        session, url, feed_idx=feed_idx
                    )
                except Exception as e:
                    logger.warning(f"RSS fetch failed: {e}")
                    candidates = []

                filtered = [c for c in candidates if c.get("key") not in recent_set]
                best = self._pick_latest_entry(filtered)
                if not best:
                    continue

                contents, updated_recent = await self._render_selected_rss_entries(
                    [best], recent_keys
                )
                await self._set_recent_rss_keys(updated_recent)
                await self._set_last_rss_feed_idx((feed_idx + 1) % len(urls))
                return contents

        await self._set_last_rss_feed_idx((start_idx + 1) % len(urls))
        return []

    def _get_rss_urls(self) -> list[str]:
        return [
            u.strip() for u in (self.rss_list or []) if isinstance(u, str) and u.strip()
        ]

    async def _fetch_all_rss_candidates(self, urls: list[str]) -> list[dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=60)
        headers = {"User-Agent": "twipsybot TopicsPlugin/rss"}
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            tasks = [
                self._fetch_rss_candidates(session, url, feed_idx=i)
                for i, url in enumerate(urls)
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
        return self._collect_fetch_results(results)

    @staticmethod
    def _collect_fetch_results(results: list[Any]) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for r in results:
            if isinstance(r, Exception):
                logger.warning(f"RSS fetch failed: {r}")
                continue
            candidates.extend(r)
        return candidates

    @staticmethod
    def _select_latest_per_feed(
        urls: list[str], candidates: list[dict[str, Any]], recent_set: set[str]
    ) -> list[dict[str, Any]]:
        by_feed_idx: dict[int, list[dict[str, Any]]] = {}
        for c in candidates:
            if c["key"] in recent_set:
                continue
            by_feed_idx.setdefault(int(c["feed_idx"]), []).append(c)

        selected: list[dict[str, Any]] = []
        for feed_idx in range(len(urls)):
            best = TopicsPlugin._pick_latest_entry(by_feed_idx.get(feed_idx))
            if best:
                selected.append(best)
        return selected

    @staticmethod
    def _pick_latest_entry(
        entries: list[dict[str, Any]] | None,
    ) -> dict[str, Any] | None:
        if not entries:
            return None
        return max(entries, key=lambda c: (c["ts"], -c["entry_idx"]))

    async def _render_selected_rss_entries(
        self, selected: list[dict[str, Any]], recent_keys: list[str]
    ) -> tuple[list[str], list[str]]:
        contents: list[str] = []
        updated_recent = recent_keys
        for entry in selected:
            primary = await self._render_rss_primary_text(entry)
            link = entry["link"]
            contents.append(f"📡 {primary}\n\n📎 {link}")
            updated_recent = self._append_recent_key(
                updated_recent, entry["key"], limit=200
            )
        return contents, updated_recent

    async def _render_rss_primary_text(self, entry: dict[str, Any]) -> str:
        title = entry["title"]
        link = entry["link"]
        summary = entry.get("summary") or ""
        primary = (summary or title).strip()
        if not self.rss_ai:
            return primary or title
        rewritten = await self._rewrite_rss_title_with_ai(title, link, summary=summary)
        return rewritten or title

    async def _fetch_rss_candidates(
        self, session: aiohttp.ClientSession, url: str, *, feed_idx: int
    ) -> list[dict[str, Any]]:
        try:
            async with session.get(url) as resp:
                if resp.status >= 400:
                    raise ValueError(f"{url} HTTP {resp.status}")
                raw = await resp.read()
        except Exception as e:
            raise ValueError(f"{url} fetch error: {e}") from e

        parsed = feedparser.parse(raw)
        entries = getattr(parsed, "entries", None) or []
        out: list[dict[str, Any]] = []
        for entry_idx, entry in enumerate(entries[:20]):
            if not isinstance(entry, dict):
                continue
            title = str(entry.get("title") or "").strip()
            link = str(entry.get("link") or "").strip()
            if not title or not link:
                continue
            summary = self._extract_entry_summary(entry)
            ts = self._get_entry_timestamp(entry)
            key = self._make_entry_key(url, entry, title, link)
            out.append(
                {
                    "ts": ts,
                    "key": key,
                    "title": title,
                    "link": link,
                    "summary": summary,
                    "feed_idx": feed_idx,
                    "entry_idx": entry_idx,
                }
            )
        return out

    class _HTMLTextExtractor(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self._parts: list[str] = []

        def handle_data(self, data: str) -> None:
            if data:
                self._parts.append(data)

        def get_text(self) -> str:
            return " ".join(self._parts)

    @classmethod
    def _strip_html(cls, text: str) -> str:
        if not text:
            return ""
        if "<" not in text and "&" not in text:
            return text.strip()
        parser = cls._HTMLTextExtractor()
        parser.feed(text)
        parser.close()
        return unescape(parser.get_text()).strip()

    @classmethod
    def _normalize_entry_text(cls, text: str, *, max_len: int) -> str:
        s = cls._strip_html(text)
        s = " ".join(s.split())
        if max_len > 0 and len(s) > max_len:
            return s[:max_len]
        return s

    @classmethod
    def _extract_entry_summary(cls, entry: dict[str, Any]) -> str:
        summary = str(entry.get("summary") or "").strip()
        return cls._normalize_entry_text(summary, max_len=1200)

    @staticmethod
    def _get_entry_timestamp(entry: dict[str, Any]) -> int:
        for k in ("published_parsed", "updated_parsed"):
            t = entry.get(k)
            if t:
                try:
                    return int(calendar.timegm(t))
                except Exception:
                    continue
        return 0

    @staticmethod
    def _make_entry_key(
        feed_url: str, entry: dict[str, Any], title: str, link: str
    ) -> str:
        raw = (
            entry.get("id")
            or entry.get("guid")
            or entry.get("link")
            or f"{title}\n{link}"
        )
        base = f"{feed_url}\n{raw}".encode(errors="ignore")
        return hashlib.sha256(base).hexdigest()

    async def _rewrite_rss_title_with_ai(
        self, title: str, link: str, *, summary: str
    ) -> str:
        openai = getattr(getattr(self, "bot", None), "openai", None) or getattr(
            self, "openai", None
        )
        if not openai:
            return title
        system_prompt = getattr(getattr(self, "bot", None), "system_prompt", None)
        ai_config = dict(getattr(getattr(self, "bot", None), "ai_config", {}) or {})
        max_tokens = ai_config.get("max_tokens")
        if isinstance(max_tokens, int):
            ai_config["max_tokens"] = min(max_tokens, 120)
        else:
            ai_config["max_tokens"] = 120

        try:
            prompt = str(self.rss_ai_prefix).format(
                title=title, link=link, summary=summary
            )
        except Exception as e:
            logger.warning(f"Invalid rss_ai_prefix format: {e}")
            return title
        try:
            text = await openai.generate_text(prompt, system_prompt, **ai_config)
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.warning(f"RSS title rewrite failed: {e}")
            return title
        first_line = (text or "").strip().splitlines()[0:1]
        return first_line[0] if first_line and first_line[0] else title

    async def _get_recent_rss_keys(self) -> list[str]:
        try:
            raw = await self.db.get_plugin_data("Topics", "rss_recent_keys")
            if not raw:
                return []
            obj = json.loads(raw)
            if not isinstance(obj, list):
                return []
            return [x for x in obj if isinstance(x, str) and x]
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.warning(f"Failed to load rss_recent_keys: {e}")
            return []

    async def _set_recent_rss_keys(self, keys: list[str]) -> None:
        try:
            await self.db.set_plugin_data("Topics", "rss_recent_keys", json.dumps(keys))
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.warning(f"Failed to save rss_recent_keys: {e}")

    async def _get_last_rss_feed_idx(self) -> int:
        try:
            raw = await self.db.get_plugin_data("Topics", "rss_last_feed_idx")
            return max(0, int(raw)) if raw else 0
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.warning(f"Failed to load rss_last_feed_idx: {e}")
            return 0

    async def _set_last_rss_feed_idx(self, idx: int) -> None:
        try:
            await self.db.set_plugin_data(
                "Topics", "rss_last_feed_idx", str(max(0, idx))
            )
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.warning(f"Failed to save rss_last_feed_idx: {e}")

    @staticmethod
    def _append_recent_key(keys: list[str], key: str, *, limit: int) -> list[str]:
        if key in keys:
            keys = [k for k in keys if k != key]
        keys.append(key)
        if limit > 0 and len(keys) > limit:
            keys = keys[-limit:]
        return keys

    async def _get_next_topic(self) -> str:
        fallback = self.topics[0] if self.topics else "Life"
        if not self.topics:
            return fallback
        try:
            last_used_line = await self._get_last_used_line()
            index = last_used_line % len(self.topics)
            topic = self.topics[index]
            await self._update_last_used_line((index + 1) % len(self.topics))
            self._log_plugin_action("selected topic", f"{topic} (line: {index + 1})")
            return topic
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.warning(f"Failed to get next topic: {e}")
            return fallback

    async def _get_last_used_line(self) -> int:
        try:
            result = await self.db.get_plugin_data("Topics", "last_used_line")
            return max(0, int(result)) if result else 0
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.warning(f"Failed to get last used line: {e}")
            return 0

    async def _update_last_used_line(self, line_number: int) -> None:
        try:
            await self.db.set_plugin_data("Topics", "last_used_line", str(line_number))
        except Exception as e:
            if isinstance(e, asyncio.CancelledError):
                raise
            logger.warning(f"Failed to update last used line: {e}")
