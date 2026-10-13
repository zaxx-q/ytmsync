from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path


class HttpCache:
    """Tiny persistent response cache (thread-safe: one connection per thread). TTL decisions belong to the caller."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path, self._tl = path, threading.local()
        with self.conn:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS http_cache(key TEXT PRIMARY KEY, status INTEGER, body TEXT, fetched_at INTEGER)"
            )

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._tl, "c", None)
        if c is None:
            c = self._tl.c = sqlite3.connect(self.path, timeout=30)
            c.execute("PRAGMA journal_mode=WAL")
        return c

    def get(self, key: str) -> tuple[int, str, int] | None:
        r = self.conn.execute(
            "SELECT status, body, fetched_at FROM http_cache WHERE key=?", (key,)
        ).fetchone()
        return (r[0], r[1], r[2]) if r else None

    def put(self, key: str, status: int, body: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO http_cache VALUES(?,?,?,?)",
                (key, status, body, int(time.time())),
            )
