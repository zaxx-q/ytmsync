from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from .plan import FileRow

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);            -- schema_version = '1'
CREATE TABLE IF NOT EXISTS playlists(
  playlist_id TEXT PRIMARY KEY, title TEXT NOT NULL, url TEXT NOT NULL,
  subscribed INTEGER NOT NULL DEFAULT 1,           -- 0 for one-shot `download` pseudo-playlists
  added_at TEXT NOT NULL, last_synced_at TEXT);
CREATE TABLE IF NOT EXISTS memberships(
  playlist_id TEXT NOT NULL REFERENCES playlists(playlist_id) ON DELETE CASCADE,
  position INTEGER NOT NULL, video_id TEXT NOT NULL,
  PRIMARY KEY(playlist_id, position));                                         -- same video may appear at 2 positions
CREATE INDEX IF NOT EXISTS ix_mem_video ON memberships(video_id);
CREATE TABLE IF NOT EXISTS tracks(
  video_id TEXT PRIMARY KEY,
  status TEXT NOT NULL CHECK(status IN ('ok','failed','unavailable')),
  meta_json TEXT,                                   -- TrackMeta.to_json(); lets us recompute paths without network
  error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
  lyrics_kind TEXT CHECK(lyrics_kind IN ('synced','plain','none','instrumental')),
  lyrics_source TEXT, lyrics_checked_at INTEGER,    -- unix seconds
  updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS files(
  video_id TEXT NOT NULL, scope TEXT NOT NULL DEFAULT '',     -- '' shared, or playlist_id (D6)
  path TEXT NOT NULL UNIQUE, format TEXT NOT NULL, size INTEGER,
  pinned INTEGER NOT NULL DEFAULT 0,                -- 1 = never auto-delete
  PRIMARY KEY(video_id, scope));

"""


class Db:
    """One sqlite connection per thread (WAL). By convention only the MAIN thread writes tracks/files/memberships."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._tl = threading.local()
        with self.conn:
            self.conn.executescript(SCHEMA)
            self.conn.execute("INSERT OR IGNORE INTO meta VALUES('schema_version','1')")

    @property
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._tl, "c", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA journal_mode=WAL")
            self._tl.c = c
        return c

    # ---- playlists / memberships
    def upsert_playlist(
        self, pid: str, title: str, url: str, subscribed: bool = True
    ) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self.conn:
            self.conn.execute(
                """INSERT INTO playlists(playlist_id,title,url,subscribed,added_at) VALUES(?,?,?,?,?)
                   ON CONFLICT(playlist_id) DO UPDATE SET title=excluded.title, url=excluded.url,
                   subscribed = MAX(subscribed, excluded.subscribed)""",
                (pid, title, url, int(subscribed), now),
            )

    def replace_memberships(self, pid: str, items: list[tuple[int, str]]) -> None:
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self.conn:
            self.conn.execute("DELETE FROM memberships WHERE playlist_id=?", (pid,))
            self.conn.executemany(
                "INSERT INTO memberships VALUES(?,?,?)",
                [(pid, pos, vid) for pos, vid in items],
            )
            self.conn.execute(
                "UPDATE playlists SET last_synced_at=? WHERE playlist_id=?", (now, pid)
            )

    def members_all(
        self, subscribed_only: bool = True
    ) -> dict[str, list[tuple[int, str]]]:
        q = "SELECT m.playlist_id, m.position, m.video_id FROM memberships m JOIN playlists p USING(playlist_id)"
        if subscribed_only:
            q += " WHERE p.subscribed=1"
        out: dict[str, list[tuple[int, str]]] = {}
        for r in self.conn.execute(q + " ORDER BY m.playlist_id, m.position"):
            out.setdefault(r["playlist_id"], []).append((r["position"], r["video_id"]))
        return out

    def playlists(self, subscribed_only: bool = True) -> list[sqlite3.Row]:
        q = (
            "SELECT * FROM playlists"
            + (" WHERE subscribed=1" if subscribed_only else "")
            + " ORDER BY title"
        )
        return self.conn.execute(q).fetchall()

    def playlists_of(
        self, video_id: str
    ) -> list[sqlite3.Row]:  # for {playlist}/{playlist_index} template values
        return self.conn.execute(
            "SELECT p.playlist_id, p.title, MIN(m.position) AS position FROM memberships m JOIN playlists p USING(playlist_id) "
            "WHERE m.video_id=? GROUP BY p.playlist_id",
            (video_id,),
        ).fetchall()

    def unsubscribe(self, pid: str, pin_files: bool) -> None:
        with self.conn:
            if pin_files:  # detach: keep the files forever
                self.conn.execute(
                    "UPDATE files SET pinned=1 WHERE EXISTS (SELECT 1 FROM memberships m WHERE m.video_id=files.video_id "
                    "AND m.playlist_id=? AND (files.scope='' OR files.scope=?))",
                    (pid, pid),
                )
            self.conn.execute(
                "UPDATE playlists SET subscribed=0 WHERE playlist_id=?", (pid,)
            )

    # ---- tracks
    def track_status_all(self) -> dict[str, str]:
        return {
            r["video_id"]: r["status"]
            for r in self.conn.execute("SELECT video_id,status FROM tracks")
        }

    def get_track(self, vid: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM tracks WHERE video_id=?", (vid,)
        ).fetchone()

    def set_track_ok(
        self,
        vid: str,
        meta_json: dict,
        lyrics_kind: str | None,
        lyrics_source: str | None,
    ) -> None:
        now = int(time.time())
        with self.conn:
            self.conn.execute(
                """INSERT INTO tracks(video_id,status,meta_json,error,attempts,lyrics_kind,lyrics_source,lyrics_checked_at,updated_at)
                   VALUES(?, 'ok', ?, NULL, 0, ?, ?, ?, ?)
                   ON CONFLICT(video_id) DO UPDATE SET status='ok', meta_json=excluded.meta_json, error=NULL, attempts=0,
                     lyrics_kind=excluded.lyrics_kind, lyrics_source=excluded.lyrics_source,
                     lyrics_checked_at=excluded.lyrics_checked_at, updated_at=excluded.updated_at""",
                (
                    vid,
                    json.dumps(meta_json, ensure_ascii=False),
                    lyrics_kind,
                    lyrics_source,
                    now if lyrics_kind else None,
                    now,
                ),
            )

    def set_track_failed(
        self, vid: str, status: str, error: str
    ) -> None:  # status: 'failed' | 'unavailable'
        with self.conn:
            self.conn.execute(
                """INSERT INTO tracks(video_id,status,error,attempts,updated_at) VALUES(?,?,?,1,?)
                   ON CONFLICT(video_id) DO UPDATE SET status=excluded.status, error=excluded.error,
                     attempts=attempts+1, updated_at=excluded.updated_at""",
                (vid, status, error[:500], int(time.time())),
            )

    def set_lyrics_state(self, vid: str, kind: str, source: str | None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE tracks SET lyrics_kind=?, lyrics_source=?, lyrics_checked_at=? WHERE video_id=?",
                (kind, source, int(time.time()), vid),
            )

    def tracks_needing_lyrics(self, retry_days: int, force: bool) -> list[sqlite3.Row]:
        if force:
            return self.conn.execute(
                "SELECT * FROM tracks WHERE status='ok'"
            ).fetchall()
        cutoff = int(time.time()) - retry_days * 86400
        return self.conn.execute(
            """SELECT * FROM tracks WHERE status='ok' AND (lyrics_kind IS NULL OR
               (lyrics_kind IN ('plain','none') AND COALESCE(lyrics_checked_at,0) < ?))""",
            (cutoff,),
        ).fetchall()

    # ---- files
    def files_all(self) -> dict[tuple[str, str], FileRow]:
        return {
            (r["video_id"], r["scope"]): FileRow(r["path"], bool(r["pinned"]))
            for r in self.conn.execute("SELECT * FROM files")
        }

    def path_owner(self, rel: str) -> tuple[str, str] | None:
        r = self.conn.execute(
            "SELECT video_id, scope FROM files WHERE path=?", (rel,)
        ).fetchone()
        return (r["video_id"], r["scope"]) if r else None

    def upsert_file(
        self, vid: str, scope: str, rel: str, fmt: str, size: int, pinned: bool
    ) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM files WHERE path=? AND NOT (video_id=? AND scope=?)",
                (rel, vid, scope),
            )  # stale row
            self.conn.execute(
                """INSERT INTO files(video_id,scope,path,format,size,pinned) VALUES(?,?,?,?,?,?)
                   ON CONFLICT(video_id,scope) DO UPDATE SET path=excluded.path, format=excluded.format,
                     size=excluded.size, pinned=MAX(pinned, excluded.pinned)""",
                (vid, scope, rel, fmt, size, int(pinned)),
            )

    def delete_file_row(self, vid: str, scope: str) -> None:
        with self.conn:
            self.conn.execute(
                "DELETE FROM files WHERE video_id=? AND scope=?", (vid, scope)
            )
            # forget the track entirely once nothing on disk references it
            self.conn.execute(
                "DELETE FROM tracks WHERE video_id=? AND NOT EXISTS (SELECT 1 FROM files WHERE video_id=?)"
                " AND status='ok'",
                (vid, vid),
            )
