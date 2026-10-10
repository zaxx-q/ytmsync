from __future__ import annotations

import json
import re
import time
from urllib.parse import urlencode

from ... import __version__
from ...models import LyricsResult, TrackMeta
from ..lrc import parse_lrc, to_plain
from ..match import similarity
from .base import LyricsContext

BASE = "https://lrclib.net/api"
USER_AGENT = f"ytmsync/{__version__} (https://github.com/your-name/ytmsync)"   # LRCLIB asks clients to identify themselves
HIT_TTL, MISS_TTL = 30 * 86400, 86400
DURATION_TOL_S = 3


def _strip_brackets(t: str) -> str:
    return re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", t).strip() or t


class LrcLibProvider:
    name = "lrclib"

    def _get(self, ctx: LyricsContext, path: str, params: dict):
        key = f"lrclib:{path}?{urlencode(sorted(params.items()))}"
        hit = ctx.cache.get(key)
        if hit:
            status, body, at = hit
            if time.time() - at < (HIT_TTL if status == 200 else MISS_TTL):
                return status, (json.loads(body) if status == 200 else None)
        r = ctx.http.get(f"{BASE}/{path}", params=params, timeout=15, headers={"User-Agent": USER_AGENT})
        if r.status_code == 200:
            ctx.cache.put(key, 200, r.text)
            return 200, r.json()
        if r.status_code == 404:
            ctx.cache.put(key, 404, "")             # negative caching (better-lyrics idea): don't hammer the API on every sync
            return 404, None
        r.raise_for_status()                        # 429/5xx: raise => provider skipped, nothing cached
        return r.status_code, None

    @staticmethod
    def _pick(results: list[dict], meta: TrackMeta, artist: str, dur: int | None) -> dict | None:
        ok = []
        for r in results:
            if dur and abs((r.get("duration") or 0) - dur) > DURATION_TOL_S:
                continue
            if similarity(r.get("trackName", ""), meta.title) < 0.5:
                continue
            if artist and similarity(r.get("artistName", ""), artist) < 0.5 and artist.casefold() not in (r.get("artistName") or "").casefold():
                continue
            ok.append(r)
        ok.sort(key=lambda r: (not r.get("syncedLyrics"), abs((r.get("duration") or 0) - (dur or 0))))   # synced first, then closest length
        return ok[0] if ok else None

    def fetch(self, meta: TrackMeta, ctx: LyricsContext) -> LyricsResult | None:
        artist = meta.artists[0] if meta.artists else ""
        dur = round(meta.duration_s) if meta.duration_s else None
        item = None
        if dur and meta.album and not meta.is_single:
            st, data = self._get(ctx, "get", {"track_name": meta.title, "artist_name": artist, "album_name": meta.album, "duration": dur})
            item = data if st == 200 else None
        if item is None:
            for q in dict.fromkeys([meta.title, _strip_brackets(meta.title)]):
                st, data = self._get(ctx, "search", {"track_name": q, "artist_name": artist})
                item = self._pick(data or [], meta, artist, dur)
                if item:
                    break
        if item is None:
            return None
        if item.get("instrumental"):
            return LyricsResult("plain", self.name, [], "", instrumental=True)
        if item.get("syncedLyrics"):
            lines, _ = parse_lrc(item["syncedLyrics"])
            if lines:
                return LyricsResult("synced", self.name, lines, to_plain(lines))
        plain = (item.get("plainLyrics") or "").strip()
        return LyricsResult("plain", self.name, [], plain) if plain else None
