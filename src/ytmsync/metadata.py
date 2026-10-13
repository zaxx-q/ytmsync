"""Assemble TrackMeta from ytmusicapi + yt-dlp info, and fetch/normalise cover art."""

from __future__ import annotations

import datetime as dt
import logging
import re
from io import BytesIO

from PIL import Image

from .config import Settings
from .cover import to_square_jpeg
from .models import TrackMeta
from .title import clean_title

log = logging.getLogger("ytmsync.metadata")


def _iso_date(d) -> str | None:
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", str(d or ""))
    if not m:
        return None
    try:
        return dt.date(*map(int, m.groups())).isoformat()
    except ValueError:
        return None


def meta_from_ytdlp(info: dict) -> TrackMeta:
    """Full fallback when ytmusicapi gave nothing. yt-dlp fills track/artists/album for auto-generated YT Music uploads."""
    vid = info["id"]
    title = info.get("track") or clean_title(info.get("title") or vid)
    artists = list(info.get("artists") or [])
    if not artists and info.get("artist"):
        artists = [a.strip() for a in str(info["artist"]).split(",") if a.strip()]
    if not artists:
        ch = info.get("channel") or info.get("uploader") or "Unknown Artist"
        artists = [re.sub(r"\s*-\s*Topic$", "", ch)]
    album = info.get("album")
    date = _iso_date(info.get("release_date"))
    year = info.get("release_year") or (int(date[:4]) if date else None)
    m = TrackMeta(
        video_id=vid,
        title=title,
        artists=artists,
        album=album or f"{title} - Single",
        is_single=not album,
        track_no=None if album else 1,
        track_total=None if album else 1,
        album_artists=list(artists),
        year=year,
        date=date,
        duration_s=info.get("duration"),
        cover_url=info.get("thumbnail"),
    )
    return m


def merge_ytdlp(meta: TrackMeta, info: dict) -> TrackMeta:
    """Fill GAPS in ytmusicapi metadata from yt-dlp; never overwrite what ytmusicapi provided."""
    if not meta.duration_s and info.get("duration"):
        meta.duration_s = float(info["duration"])
    date = _iso_date(info.get("release_date"))
    if (
        meta.is_official_audio
        and date
        and not meta.date
        and (meta.year is None or int(date[:4]) == meta.year)
    ):
        meta.date, meta.year = (
            date,
            int(date[:4]),
        )  # a track-level date agreeing with the album year is more precise
    if not meta.year and info.get("release_year"):
        meta.year = int(info["release_year"])
    return meta


def fetch_cover(meta: TrackMeta, info: dict | None, s: Settings, http) -> bytes | None:
    """Try candidates in quality order; a failure is never fatal (returns None => file simply has no cover)."""
    cands = [
        meta.cover_url,
        f"https://i.ytimg.com/vi/{meta.video_id}/maxresdefault.jpg",
        (info or {}).get("thumbnail"),
        f"https://i.ytimg.com/vi/{meta.video_id}/hqdefault.jpg",
    ]
    for url in dict.fromkeys(u for u in cands if u):
        try:
            r = http.get(url, timeout=20)
            if r.status_code != 200 or len(r.content) < 1500:
                continue
            if (
                min(Image.open(BytesIO(r.content)).size) < 200
            ):  # YouTube serves a 120x90 placeholder for missing maxres
                continue
            return to_square_jpeg(r.content, s.cover_method, s.cover_size)
        except Exception as e:  # noqa: BLE001 -- try remaining cover candidates
            log.debug("cover candidate %s failed: %s", url, e)
    return None
