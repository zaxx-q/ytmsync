"""YouTube Music enrichment through ytmusicapi (UNAUTHENTICATED: works per-videoId for any public track)."""

from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable

from ytmusicapi import YTMusic

from .cover import upscale_google_url
from .models import TrackMeta
from .title import clean_title

log = logging.getLogger("ytmsync.ytm")


def parse_length(s: str | None) -> float | None:
    """'3:07' -> 187.0, '1:02:03' -> 3723.0, junk -> None"""
    if not s:
        return None
    try:
        secs = 0
        for part in s.split(":"):
            secs = secs * 60 + int(part)
        return float(secs)
    except ValueError:
        return None


def _int(v) -> int | None:
    try:
        return int(str(v)[:4]) if v else None
    except ValueError:
        return None


def _norm(s: str) -> str:
    return re.sub(r"[\W_]+", " ", (s or "").casefold()).strip()


def best_thumb(thumbs: list[dict] | None) -> str | None:
    if not thumbs:
        return None
    t = max(thumbs, key=lambda x: (x.get("width") or 0) * (x.get("height") or 0))
    return upscale_google_url(t["url"]) if t.get("url") else None


class YTMeta:
    def __init__(self, api_factory: Callable[[], YTMusic] = YTMusic):
        self._factory = api_factory
        self._tl = threading.local()  # one YTMusic (requests.Session) per thread
        self._albums: dict[str, dict | None] = {}
        self._lock = threading.Lock()

    @property
    def api(self) -> YTMusic:
        a = getattr(self._tl, "api", None)
        if a is None:
            a = self._tl.api = self._factory()
        return a

    def album(self, browse_id: str) -> dict | None:
        """Cached per run: 30 tracks of one album => 1 request, not 30."""
        with self._lock:
            if browse_id in self._albums:
                return self._albums[browse_id]
        try:
            alb = self.api.get_album(browse_id)
        except Exception as e:  # noqa: BLE001 -- network/parse/shape changes must never kill a download
            log.debug("get_album(%s) failed: %s", browse_id, e)
            alb = None
        with self._lock:
            self._albums[browse_id] = alb
        return alb

    def resolve_album_browse(self, browse_id: str) -> str | None:
        alb = self.album(browse_id)
        return (alb or {}).get("audioPlaylistId")

    def enrich(self, video_id: str) -> TrackMeta | None:
        try:
            wp = self.api.get_watch_playlist(videoId=video_id, limit=1)
        except Exception as e:  # noqa: BLE001 -- metadata failure must not kill a download
            log.debug("get_watch_playlist(%s) failed: %s", video_id, e)
            return None
        tracks = wp.get("tracks") or []
        t = next(
            (x for x in tracks if x.get("videoId") == video_id),
            tracks[0] if tracks else None,
        )
        if not t:
            return None
        is_atv = (
            t.get("videoType") or ""
        ) == "MUSIC_VIDEO_TYPE_ATV"  # official "Art Track" audio
        raw_title = t.get("title") or video_id
        meta = TrackMeta(
            video_id=video_id,
            title=raw_title
            if is_atv
            else clean_title(raw_title),  # ATV titles are already clean
            artists=[a["name"] for a in (t.get("artists") or []) if a.get("name")],
            duration_s=parse_length(t.get("length")),
            year=_int(t.get("year")),
            cover_url=best_thumb(t.get("thumbnail")),
            is_official_audio=is_atv,
            lyrics_browse_id=wp.get("lyrics"),
        )
        ref = t.get("album") or {}
        album = self.album(ref["id"]) if ref.get("id") else None
        if album:
            atracks = album.get("tracks") or []
            # D8: position in the album's track list, NOT the unreliable `trackNumber` field
            idx = next(
                (i for i, x in enumerate(atracks, 1) if x.get("videoId") == video_id),
                None,
            )
            if idx is None:
                idx = next(
                    (
                        i
                        for i, x in enumerate(atracks, 1)
                        if _norm(x.get("title", "")) == _norm(raw_title)
                    ),
                    None,
                )
            meta.album = album.get("title") or ref.get("name")
            meta.album_id = ref["id"]
            meta.album_artists = [
                a["name"] for a in (album.get("artists") or []) if a.get("name")
            ]
            meta.year = _int(album.get("year")) or meta.year
            meta.cover_url = best_thumb(album.get("thumbnails")) or meta.cover_url
            if idx:
                meta.track_no = idx
                meta.track_total = album.get("trackCount") or len(atracks)
                meta.explicit = bool(atracks[idx - 1].get("isExplicit"))
        elif ref.get("name"):  # album name known but album page unreachable
            meta.album = ref["name"]
        if not meta.album:  # D9
            meta.album, meta.is_single, meta.track_no, meta.track_total = (
                f"{meta.title} - Single",
                True,
                1,
                1,
            )
            meta.album_artists = list(meta.artists)
        return (
            meta if meta.artists else None
        )  # no artist => let the yt-dlp fallback decide
