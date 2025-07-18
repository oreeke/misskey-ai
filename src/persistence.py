#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sqlite3
import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional
from loguru import logger


class PersistenceManager:
    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = "data/misskey_ai.db"
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(exist_ok=True)
        self._connection = None
        self._lock = asyncio.Lock()

    async def initialize(self) -> None:
        async with self._lock:
            self._connection = sqlite3.connect(
                self.db_path,
                check_same_thread=False,
                timeout=30.0
            )
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.execute("PRAGMA cache_size=10000")
            await self._create_tables()
            logger.debug(f"持久化管理器已初始化，数据库: {self.db_path}")

    async def _create_tables(self) -> None:
        await self._execute_schema()

    async def _execute_schema(self) -> None:
        schema_statements = [
            """
            CREATE TABLE IF NOT EXISTS processed_mentions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                note_id TEXT UNIQUE NOT NULL,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                user_id TEXT,
                username TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS processed_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                message_id TEXT UNIQUE NOT NULL,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                user_id TEXT,
                chat_type TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS plugin_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plugin_name TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(plugin_name, key)
            )
            """
        ]
        index_statements = [
            "CREATE INDEX IF NOT EXISTS idx_mentions_note_id ON processed_mentions(note_id)",
            "CREATE INDEX IF NOT EXISTS idx_mentions_processed_at ON processed_mentions(processed_at)",
            "CREATE INDEX IF NOT EXISTS idx_messages_message_id ON processed_messages(message_id)",
            "CREATE INDEX IF NOT EXISTS idx_messages_processed_at ON processed_messages(processed_at)",
            "CREATE INDEX IF NOT EXISTS idx_plugin_data_name_key ON plugin_data(plugin_name, key)"
        ]
        cursor = self._connection.cursor()
        for statement in schema_statements:
            cursor.execute(statement)
        for index_sql in index_statements:
            cursor.execute(index_sql)
        self._connection.commit()

    async def _execute_query(self, query: str, params: tuple = ()) -> Optional[tuple]:
        async with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(query, params)
            return cursor.fetchone()

    async def _execute_fetchall(self, query: str, params: tuple = ()) -> List[tuple]:
        async with self._lock:
            cursor = self._connection.cursor()
            cursor.execute(query, params)
            return cursor.fetchall()

    async def _execute_insert(self, query: str, params: tuple = ()) -> None:
        async with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute(query, params)
                self._connection.commit()
            except sqlite3.Error as e:
                logger.error(f"数据库插入操作失败: {e}")
                self._connection.rollback()
                raise

    async def is_mention_processed(self, note_id: str) -> bool:
        results = await self.execute_query(
            "SELECT 1 FROM processed_mentions WHERE note_id = ? LIMIT 1",
            (note_id,)
        )
        return bool(results)

    async def is_message_processed(self, message_id: str) -> bool:
        results = await self.execute_query(
            "SELECT 1 FROM processed_messages WHERE message_id = ? LIMIT 1",
            (message_id,)
        )
        return bool(results)

    async def mark_mention_processed(
        self,
        note_id: str,
        user_id: Optional[str] = None,
        username: Optional[str] = None
    ) -> None:
        await self.execute_insert(
            "INSERT OR IGNORE INTO processed_mentions (note_id, user_id, username) VALUES (?, ?, ?)",
            (note_id, user_id, username)
        )

    async def mark_message_processed(
        self,
        message_id: str,
        user_id: Optional[str] = None,
        chat_type: Optional[str] = None
    ) -> None:
        await self.execute_insert(
            "INSERT OR IGNORE INTO processed_messages (message_id, user_id, chat_type) VALUES (?, ?, ?)",
            (message_id, user_id, chat_type)
        )

    async def get_processed_mentions_count(self) -> int:
        results = await self.execute_query("SELECT COUNT(*) FROM processed_mentions")
        return results[0][0] if results else 0

    async def get_processed_messages_count(self) -> int:
        results = await self.execute_query("SELECT COUNT(*) FROM processed_messages")
        return results[0][0] if results else 0

    async def cleanup_old_records(self, days: int = 30) -> int:
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
        mentions_deleted = await self.execute_update(
            "DELETE FROM processed_mentions WHERE processed_at < ?",
            (cutoff_date,)
        )
        messages_deleted = await self.execute_update(
            "DELETE FROM processed_messages WHERE processed_at < ?",
            (cutoff_date,)
        )
        total_deleted = mentions_deleted + messages_deleted
        if total_deleted > 0:
            logger.debug(
                f"已清理 {total_deleted} 条过期记录 (提及: {mentions_deleted}, 消息: {messages_deleted})")
        return total_deleted

    async def get_plugin_data(self, plugin_name: str, key: str) -> Optional[str]:
        results = await self.execute_query(
            "SELECT value FROM plugin_data WHERE plugin_name = ? AND key = ?",
            (plugin_name, key)
        )
        return results[0][0] if results else None

    async def set_plugin_data(self, plugin_name: str, key: str, value: str) -> None:
        await self.execute_update(
            "INSERT OR REPLACE INTO plugin_data (plugin_name, key, value, updated_at) VALUES (?, ?, ?, ?)",
            (plugin_name, key, value, datetime.now(timezone.utc))
        )

    async def delete_plugin_data(self, plugin_name: str, key: str = None) -> int:
        if key:
            return await self.execute_update(
                "DELETE FROM plugin_data WHERE plugin_name = ? AND key = ?",
                (plugin_name, key)
            )
        else:
            return await self.execute_update(
                "DELETE FROM plugin_data WHERE plugin_name = ?",
                (plugin_name,)
            )

    async def get_recent_mentions(self, limit: int = 100) -> List[dict]:
        rows = await self.execute_query(
            "SELECT note_id, processed_at, user_id, username FROM processed_mentions ORDER BY processed_at DESC LIMIT ?",
            (limit,)
        )
        return [
            {
                'note_id': row[0],
                'processed_at': row[1],
                'user_id': row[2],
                'username': row[3]
            }
            for row in rows
        ]

    async def get_recent_messages(self, limit: int = 100) -> List[dict]:
        rows = await self.execute_query(
            "SELECT message_id, processed_at, user_id, chat_type FROM processed_messages ORDER BY processed_at DESC LIMIT ?",
            (limit,)
        )
        return [
            {
                'message_id': row[0],
                'processed_at': row[1],
                'user_id': row[2],
                'chat_type': row[3]
            }
            for row in rows
        ]

    async def get_statistics(self) -> dict:
        async with self._lock:
            cursor = self._connection.cursor()
            cursor.execute("SELECT COUNT(*) FROM processed_mentions")
            total_mentions = cursor.fetchone()[0]
            cursor.execute("SELECT COUNT(*) FROM processed_messages")
            total_messages = cursor.fetchone()[0]
            today = datetime.now(timezone.utc).date()
            cursor.execute(
                "SELECT COUNT(*) FROM processed_mentions WHERE DATE(processed_at) = ?",
                (today,)
            )
            today_mentions = cursor.fetchone()[0]
            cursor.execute(
                "SELECT COUNT(*) FROM processed_messages WHERE DATE(processed_at) = ?",
                (today,)
            )
            today_messages = cursor.fetchone()[0]
            db_size = self.db_path.stat().st_size if self.db_path.exists() else 0
            return {
                'total_mentions': total_mentions,
                'total_messages': total_messages,
                'today_mentions': today_mentions,
                'today_messages': today_messages,
                'db_size_bytes': db_size,
                'db_size_mb': round(db_size / 1024 / 1024, 2)
            }

    async def vacuum(self) -> None:
        async with self._lock:
            try:
                self._connection.execute("VACUUM")
                logger.debug("数据库优化完成")
            except sqlite3.Error as e:
                logger.error(f"数据库优化失败: {e}")

    async def close(self) -> None:
        async with self._lock:
            if self._connection:
                self._connection.close()
                self._connection = None
                logger.debug("持久化管理器已关闭")

    async def execute_query(self, query: str, params: tuple = ()) -> List[tuple]:
        async with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute(query, params)
                return cursor.fetchall()
            except sqlite3.Error as e:
                logger.error(f"执行查询失败: {e}")
                raise

    async def execute_update(self, query: str, params: tuple = ()) -> int:
        async with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute(query, params)
                affected_rows = cursor.rowcount
                self._connection.commit()
                return affected_rows
            except sqlite3.Error as e:
                logger.error(f"执行更新失败: {e}")
                self._connection.rollback()
                raise

    async def execute_insert(self, query: str, params: tuple = ()) -> int:
        async with self._lock:
            cursor = self._connection.cursor()
            try:
                cursor.execute(query, params)
                last_row_id = cursor.lastrowid
                self._connection.commit()
                return last_row_id
            except sqlite3.Error as e:
                logger.error(f"执行插入失败: {e}")
                self._connection.rollback()
                raise

    async def __aenter__(self):
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
        return False
