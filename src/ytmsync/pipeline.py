"""One track, start to finish, inside a worker thread. Produces files in a temp dir ONLY; never touches the library or the DB."""

from __future__ import annotations

import logging
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path

import requests

from . import __version__
from .config import Settings
from .httpcache import HttpCache
from .lyrics.chain import fetch_lyrics
from .lyrics.lrc import format_lrc
from .lyrics.providers.base import LyricsContext
from .metadata import fetch_cover, merge_ytdlp, meta_from_ytdlp
from .models import LyricsResult, TrackMeta
from .musicbrainz import MusicBrainz
from .tagging import write_tags
from .ytdl import DownloadFailure, download_audio
from .ytm import YTMeta

log = logging.getLogger("ytmsync.pipeline")


class Ctx:
    """Shared, thread-safe-by-construction services. Each thread gets its own requests.Session."""

    def __init__(
        self,
        settings: Settings,
        ytm: YTMeta,
        cache: HttpCache,
        http_factory=None,
        mb: MusicBrainz | None = None,
    ):
        self.settings, self.ytm, self.cache, self.mb = settings, ytm, cache, mb
        self._factory = http_factory or self._new_session
        self._tl = threading.local()

    @staticmethod
    def _new_session() -> requests.Session:
        s = requests.Session()
        s.headers["User-Agent"] = f"ytmsync/{__version__}"
        return s

    @property
    def http(self):
        h = getattr(self._tl, "h", None)
        if h is None:
            h = self._tl.h = self._factory()
        return h

    def lyrics_ctx(self, info: dict | None = None) -> LyricsContext:
        return LyricsContext(self.ytm, self.http, self.cache, info)


@dataclass
class JobResult:
    video_id: str
    ok: bool
    status: str = "ok"  # ok | failed | unavailable
    meta: TrackMeta | None = None
    audio: Path | None = None  # tagged file in the temp dir
    sidecar: Path | None = None  # .lrc in the temp dir
    lyrics: LyricsResult | None = None
    error: str | None = None


def lrc_header(meta: TrackMeta) -> dict[str, str]:
    h = {
        "ti": meta.title,
        "ar": ", ".join(meta.artists),
        "al": meta.album or "",
        "by": "ytmsync",
    }
    if meta.duration_s:
        m, s = divmod(int(meta.duration_s), 60)
        h["length"] = f"{m:02d}:{s:02d}"
    return h


def write_sidecar(path: Path, meta: TrackMeta, lyrics: LyricsResult) -> None:
    path.write_text(format_lrc(lyrics.lines, lrc_header(meta)), encoding="utf-8")


def process_track(video_id: str, ctx: Ctx, on_progress=None) -> JobResult:
    """NEVER raises: every failure becomes a JobResult(ok=False) so one bad track cannot abort a sync."""
    s = ctx.settings
    work = s.tmp_dir / video_id
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        meta = ctx.ytm.enrich(
            video_id
        )  # may be None => fall back to yt-dlp's own metadata
        audio, info = download_audio(s, video_id, work, on_progress)
        meta = merge_ytdlp(meta, info) if meta else meta_from_ytdlp(info)
        if ctx.mb:
            meta = ctx.mb.enrich(meta)  # best effort, never raises
        cover = fetch_cover(meta, info, s, ctx.http)
        lyrics = (
            fetch_lyrics(meta, ctx.lyrics_ctx(info), s.lyrics_providers)
            if s.lyrics
            else None
        )
        write_tags(audio, meta, cover, lyrics, s.artist_join, s.embed_lyrics)
        sidecar = None
        if s.lrc and lyrics and lyrics.kind == "synced" and not lyrics.instrumental:
            sidecar = work / f"{video_id}.lrc"
            write_sidecar(sidecar, meta, lyrics)
        return JobResult(video_id, True, "ok", meta, audio, sidecar, lyrics)
    except DownloadFailure as e:
        shutil.rmtree(work, ignore_errors=True)
        return JobResult(
            video_id,
            False,
            "unavailable" if e.kind == "unavailable" else "failed",
            meta=meta,
            error=f"{e.kind}: {e}"[:500],
        )
    except Exception as e:  # tagging bug, disk full, ...
        log.debug("unexpected failure for %s", video_id, exc_info=True)
        shutil.rmtree(work, ignore_errors=True)
        return JobResult(
            video_id, False, "failed", error=f"{type(e).__name__}: {e}"[:500]
        )
