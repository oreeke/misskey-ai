import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite
from loguru import logger

from .config import Config
from .constants import ConfigKeys

__all__ = ("PersistenceManager", "ConnectionPool")


class ConnectionPool:
    def __init__(self, db_path: str, max_connections: int = 10):
        self.db_path = db_path
        self.max_connections = max_connections
        self._pool = asyncio.Queue(maxsize=max_connections)
        self._created_connections = 0
        self._lock = asyncio.Lock()

    async def get_connection(self) -> aiosqlite.Connection:
        try:
            return self._pool.get_nowait()
        except asyncio.QueueEmpty:
            async with self._lock:
                if self._created_connections < self.max_connections:
                    conn = await aiosqlite.connect(
                        self.db_path, timeout=30.0, isolation_level=None
                    )
                    await conn.execute("PRAGMA journal_mode=WAL")
                    await conn.execute("PRAGMA synchronous=NORMAL")
                    await conn.execute("PRAGMA cache_size=10000")
                    await conn.execute("PRAGMA busy_timeout=30000")
                    self._created_connections += 1
                    return conn
            return await self._pool.get()

    async def return_connection(self, conn: aiosqlite.Connection) -> None:
        try:
            self._pool.put_nowait(conn)
        except asyncio.QueueFull:
            await conn.close()
            async with self._lock:
                self._created_connections -= 1

    async def close_all(self) -> None:
        connections = []
        while not self._pool.empty():
            try:
                connections.append(self._pool.get_nowait())
            except asyncio.QueueEmpty:
                break
        for conn in connections:
            await conn.close()
        self._created_connections = 0


class PersistenceManager:
    def __init__(self, db_path: str | None = None, max_connections: int = 10):
        self.config = Config()
        if db_path is None:
            db_path = self.config._get_builtin_default(ConfigKeys.DB_PATH)
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(exist_ok=True)
        self._pool = ConnectionPool(str(self.db_path), max_connections)
        self._initialized = False

    async def __aenter__(self):
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
        return False

    async def initialize(self) -> None:
        if self._initialized:
            return
        await self._create_tables()
        self._initialized = True
        logger.info(f"Persistence 管理器初始化完成: {self.db_path}")

    async def close(self) -> None:
        await self._pool.close_all()
        logger.debug("Persistence 管理器已关闭")

    async def _create_tables(self) -> None:
        conn = await self._pool.get_connection()
        try:
            await self._execute_schema(conn)
        finally:
            await self._pool.return_connection(conn)

    @staticmethod
    async def _execute_schema(conn: aiosqlite.Connection) -> None:
        schema_statements = [
            """
            CREATE TABLE IF NOT EXISTS plugin_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                plugin_name TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(plugin_name, key)
            )
            """,
        ]
        index_statements = [
            "CREATE INDEX IF NOT EXISTS idx_plugin_data_name_key ON plugin_data(plugin_name, key)",
        ]
        async with conn.execute("BEGIN TRANSACTION"):
            for statement in schema_statements:
                await conn.execute(statement)
            for index_sql in index_statements:
                await conn.execute(index_sql)
            await conn.commit()

    async def _execute(self, query: str, params: tuple = (), fetch_type: str = "one"):
        conn = await self._pool.get_connection()
        try:
            async with conn.execute(query, params) as cursor:
                if fetch_type == "all":
                    return await cursor.fetchall()
                if fetch_type == "one":
                    return await cursor.fetchone()
                if fetch_type == "insert":
                    await conn.commit()
                    return cursor.lastrowid
                if fetch_type == "update":
                    await conn.commit()
                    return cursor.rowcount
        except aiosqlite.Error as e:
            if fetch_type in ("insert", "update"):
                await conn.rollback()
                logger.error(f"数据库{fetch_type}操作失败: {e}")
            raise
        finally:
            await self._pool.return_connection(conn)

    async def get_plugin_data(self, plugin_name: str, key: str) -> str | None:
        result = await self._execute(
            "SELECT value FROM plugin_data WHERE plugin_name = ? AND key = ?",
            (plugin_name, key),
        )
        return result[0] if result else None

    async def set_plugin_data(self, plugin_name: str, key: str, value: str) -> None:
        await self._execute(
            "INSERT OR REPLACE INTO plugin_data (plugin_name, key, value, updated_at) VALUES (?, ?, ?, ?)",
            (plugin_name, key, value, datetime.now()),
            "update",
        )

    async def delete_plugin_data(self, plugin_name: str, key: str | None = None) -> int:
        if key:
            query = "DELETE FROM plugin_data WHERE plugin_name = ? AND key = ?"
            params = (plugin_name, key)
        else:
            query = "DELETE FROM plugin_data WHERE plugin_name = ?"
            params = (plugin_name,)
        return await self._execute(query, params, "update")

    async def get_table_stats(self) -> dict[str, Any]:
        tables_query = "SELECT name FROM sqlite_master WHERE type='table'"
        tables_result = await self._execute(tables_query, (), "all")
        table_stats = {}
        for table_row in tables_result:
            table_name = table_row[0]
            if not table_name.replace("_", "").isalnum():
                continue
            count_query = f'SELECT COUNT(*) FROM "{table_name}"'
            count_result = await self._execute(count_query)
            size_query = "SELECT SUM(pgsize) FROM dbstat WHERE name = ?"
            size_result = await self._execute(size_query, (table_name,))
            size_bytes = size_result[0] if size_result[0] else 0
            table_stats[table_name] = {
                "row_count": count_result[0],
                "size_bytes": size_bytes,
                "size_kb": round(size_bytes / 1024, 2),
                "size_mb": round(size_bytes / 1024 / 1024, 2),
            }
        return table_stats

    async def vacuum(self) -> None:
        conn = await self._pool.get_connection()
        try:
            await conn.execute("VACUUM")
            logger.debug("数据库优化完成")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"数据库优化失败: {e}")
        finally:
            await self._pool.return_connection(conn)
