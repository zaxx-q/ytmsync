"""Optional MusicBrainz enrichment (ideas: Shira's musicbrainz.py, MIT). Adds MBIDs, ISRC, disc numbers and a precise release date.
It NEVER rewrites title / artist / album text: a wrong fuzzy match must not be able to corrupt visible tags, so matching is strict."""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable
from urllib.parse import urlencode

import requests

from . import __version__
from .httpcache import HttpCache
from .lyrics.match import similarity
from .models import TrackMeta

log = logging.getLogger("ytmsync.musicbrainz")

BASE = "https://musicbrainz.org/ws/2"
HIT_TTL, MISS_TTL = 30 * 86400, 86400
MIN_SCORE, TITLE_SIM, ARTIST_SIM, ALBUM_SIM, DURATION_TOL_S = 85, 0.85, 0.8, 0.9, 4
MIN_INTERVAL_S = 1.1                       # MusicBrainz allows 1 request/second per client; stay just below the edge

_FEAT = re.compile(r"\s*[\(\[]\s*(?:feat|ft|featuring|with)\b[^\)\]]*[\)\]]|\s+(?:feat|ft)\.?\s.+$", re.I)


def core_title(s: str) -> str:
    """MusicBrainz stores 'Song' where YouTube Music says 'Song (feat. X)'."""
    return _FEAT.sub("", s or "").strip()


def lucene_quote(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def build_query(meta: TrackMeta, with_release: bool) -> str:
    q = f"recording:{lucene_quote(core_title(meta.title))} AND artist:{lucene_quote(meta.artists[0])}"
    if with_release and meta.album and not meta.is_single:
        q += f" AND release:{lucene_quote(core_title(meta.album))}"
    return q


def credit_names(rec: dict) -> list[str]:
    out = []
    for ac in rec.get("artist-credit") or []:
        for n in (ac.get("name"), (ac.get("artist") or {}).get("name")):
            if n:
                out.append(n)
    return out


def credit_ids(obj: dict) -> list[str]:
    return [ac["artist"]["id"] for ac in obj.get("artist-credit") or [] if (ac.get("artist") or {}).get("id")]


def pick_recording(recs: list[dict], meta: TrackMeta) -> dict | None:
    ok = []
    want = core_title(meta.title)
    for r in recs:
        if int(r.get("score", 100)) < MIN_SCORE:
            continue
        if similarity(core_title(r.get("title", "")), want) < TITLE_SIM:
            continue
        if not any(similarity(n, meta.artists[0]) >= ARTIST_SIM for n in credit_names(r)):
            continue
        length = r.get("length")
        if length and meta.duration_s and abs(length / 1000 - meta.duration_s) > DURATION_TOL_S:
            continue                                       # a live / remix / radio edit of the same song
        ok.append(r)
    ok.sort(key=lambda r: (-int(r.get("score", 100)), abs((r.get("length") or 0) / 1000 - (meta.duration_s or 0))))
    return ok[0] if ok else None


def pick_release(rec: dict, meta: TrackMeta) -> dict | None:
    target = core_title(meta.title if meta.is_single else (meta.album or ""))
    if not target:
        return None
    cands = [r for r in rec.get("releases") or [] if similarity(core_title(r.get("title", "")), target) >= ALBUM_SIM]
    cands.sort(key=lambda r: (r.get("status") != "Official", r.get("date") or "9999"))     # official first, then earliest
    return cands[0] if cands else None


def pick_medium(rel: dict, rec: dict) -> dict | None:
    media = rel.get("media") or []
    if len(media) == 1:
        return media[0]
    for m in media:
        if any(similarity(core_title(t.get("title", "")), core_title(rec.get("title", ""))) >= TITLE_SIM for t in m.get("track") or []):
            return m
    return None


def first_isrc(rec: dict) -> str | None:
    for i in rec.get("isrcs") or []:                       # tolerate ["USXXX"] and [{"id": "USXXX"}]
        v = i if isinstance(i, str) else (i or {}).get("id")
        if v:
            return v
    return None


def apply_match(meta: TrackMeta, rec: dict, rel: dict | None) -> None:
    meta.mb_recording_id = rec.get("id")
    meta.mb_artist_ids = credit_ids(rec)
    meta.isrc = first_isrc(rec) or meta.isrc
    if not rel:
        return
    meta.mb_release_id = rel.get("id")
    meta.mb_release_group_id = (rel.get("release-group") or {}).get("id")
    meta.mb_album_artist_ids = credit_ids(rel) or (meta.mb_artist_ids if meta.album_artists == meta.artists else [])
    d = rel.get("date") or ""
    if re.fullmatch(r"\d{4}(-\d{2}(-\d{2})?)?", d):
        if meta.year is None:
            meta.year = int(d[:4])
        if len(d) == 10 and not meta.date and int(d[:4]) == meta.year:
            meta.date = d
    medium = pick_medium(rel, rec)
    total = rel.get("medium-count") or (len(rel["media"]) if len(rel.get("media") or []) > 1 else None)
    if medium is not None:
        pos = medium.get("position")
        if pos and ((total and total > 1) or pos > 1):
            meta.disc_no, meta.disc_total = int(pos), total
            # multi-disc: YouTube's album list is global (track 15), tags want per-disc (disc 2, track 3/12)
            if medium.get("track-offset") is not None and medium.get("track-count"):
                meta.track_no, meta.track_total = int(medium["track-offset"]) + 1, int(medium["track-count"])
        elif total == 1:
            meta.disc_no, meta.disc_total = 1, 1


class MusicBrainz:
    def __init__(self, contact: str, cache: HttpCache, *, http_factory: Callable[[], requests.Session] = requests.Session,
                 clock: Callable[[], float] = time.monotonic, sleep: Callable[[float], None] = time.sleep):
        self.ua = f"ytmsync/{__version__} ( {contact} )"     # MusicBrainz REQUIRES a User-Agent with contact info
        self.cache, self._factory, self._clock, self._sleep = cache, http_factory, clock, sleep
        self._tl, self._lock, self._last = threading.local(), threading.Lock(), None

    @property
    def http(self):
        h = getattr(self._tl, "h", None)
        if h is None:
            h = self._tl.h = self._factory()
        return h

    def _throttle(self) -> None:
        """Global (all threads) spacing between REAL requests. Holding the lock while sleeping is what serialises them."""
        with self._lock:
            if self._last is not None:
                wait = self._last + MIN_INTERVAL_S - self._clock()
                if wait > 0:
                    self._sleep(wait)
            self._last = self._clock()

    def _search(self, params: dict) -> dict | None:
        key = f"mb:recording?{urlencode(sorted(params.items()))}"
        hit = self.cache.get(key)
        if hit and hit[0] == 200:
            data = json.loads(hit[1])
            if time.time() - hit[2] < (HIT_TTL if data.get("recordings") else MISS_TTL):
                return data
        for attempt in range(1, 4):
            self._throttle()
            r = self.http.get(f"{BASE}/recording", params=params, timeout=20,
                              headers={"User-Agent": self.ua, "Accept": "application/json"})
            if r.status_code == 200:
                self.cache.put(key, 200, r.text)
                return r.json()
            if r.status_code in (429, 503):                  # rate limited / busy: back off, don't cache
                self._sleep(2 * attempt)
                continue
            log.debug("MusicBrainz HTTP %s for %s", r.status_code, params.get("query"))
            return None
        return None

    def enrich(self, meta: TrackMeta) -> TrackMeta:
        """Best effort: on ANY problem the input metadata is returned unchanged."""
        if not meta.artists:
            return meta
        try:
            for with_release in ([True, False] if (meta.album and not meta.is_single) else [False]):
                data = self._search({"query": build_query(meta, with_release), "fmt": "json", "limit": 10})
                rec = pick_recording((data or {}).get("recordings") or [], meta)
                if rec:
                    apply_match(meta, rec, pick_release(rec, meta))
                    break
        except Exception as e:
            log.debug("MusicBrainz enrichment failed for %s: %s", meta.video_id, e)
        return meta
