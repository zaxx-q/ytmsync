from __future__ import annotations

from ...models import LyricsResult, TrackMeta
from ..lrc import parse_lrc, to_plain
from .base import LyricsContext

SOURCES = ["Musixmatch", "NetEase"]      # LRCLIB is handled by its own, exact-match provider


class SyncedLyricsProvider:
    """Musixmatch / NetEase through the `syncedlyrics` package (unofficial endpoints => fuzzy + fragile => validated harder)."""
    name = "syncedlyrics"
    fuzzy = True

    def fetch(self, meta: TrackMeta, ctx: LyricsContext) -> LyricsResult | None:
        try:
            import syncedlyrics
        except ImportError:                                    # optional at runtime: silently unavailable
            return None
        if not meta.artists:
            return None
        lrc = syncedlyrics.search(f"{meta.title} {meta.artists[0]}", synced_only=True, providers=SOURCES)
        if not lrc:
            return None
        lines, _ = parse_lrc(lrc)                              # also strips Musixmatch word-level <mm:ss.xx> tags
        return LyricsResult("synced", self.name, lines, to_plain(lines)) if lines else None
