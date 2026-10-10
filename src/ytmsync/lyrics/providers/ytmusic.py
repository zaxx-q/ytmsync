from __future__ import annotations

from ...models import LyricsResult, TrackMeta
from ..lrc import LyricLine, to_plain
from .base import LyricsContext


class YtMusicProvider:
    """YouTube Music's own lyrics (LyricFind/Musixmatch). Timed when available. Also serves as the validation reference."""
    name = "ytmusic"

    def fetch(self, meta: TrackMeta, ctx: LyricsContext) -> LyricsResult | None:
        if not meta.lyrics_browse_id:
            return None
        r = ctx.ytm.api.get_lyrics(meta.lyrics_browse_id, timestamps=True)
        if not r or not r.get("lyrics"):
            return None
        if r.get("hasTimestamps"):
            lines = [LyricLine(int(l.start_time), (l.text or "").strip()) for l in r["lyrics"]]
            if any(l.text for l in lines):
                return LyricsResult("synced", self.name, lines, to_plain(lines))
            return None
        text = str(r["lyrics"]).strip()
        return LyricsResult("plain", self.name, [], text) if text else None
