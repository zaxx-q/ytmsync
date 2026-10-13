"""Lyrics orchestration: quality-ordered provider chain + validation (ideas from better-lyrics, re-implemented)."""

from __future__ import annotations

import logging

from ..models import LyricsResult, TrackMeta
from .match import SIMILARITY_MIN, covers, is_instrumental, plausible_synced, similarity
from .providers.base import LyricsContext
from .providers.lrclib import LrcLibProvider
from .providers.synced import SyncedLyricsProvider
from .providers.ytcaptions import YtCaptionsProvider
from .providers.ytmusic import YtMusicProvider

log = logging.getLogger("ytmsync.lyrics")

PROVIDERS = {
    p.name: p
    for p in (
        YtCaptionsProvider(),
        LrcLibProvider(),
        SyncedLyricsProvider(),
        YtMusicProvider(),
    )
}
RANK = {"synced": 2, "plain": 1}


def lyrics_kind(r: LyricsResult | None) -> str:
    """Value stored in tracks.lyrics_kind."""
    return "none" if r is None else "instrumental" if r.instrumental else r.kind


def fetch_lyrics(
    meta: TrackMeta, ctx: LyricsContext, names: list[str], registry: dict | None = None
) -> LyricsResult | None:
    """First plausible SYNCED result wins; otherwise the first acceptable PLAIN one; otherwise None."""
    registry = registry or PROVIDERS
    names = sorted(
        names, key=lambda n: n != "ytmusic"
    )  # stable: YouTube's own text first — it is the validation reference
    reference: str | None = None
    fallback: LyricsResult | None = None
    for name in names:
        prov = registry.get(name)
        if prov is None:
            log.warning("unknown lyrics provider %r (ignored)", name)
            continue
        try:
            r = prov.fetch(meta, ctx)
        except Exception as e:  # noqa: BLE001 -- one broken provider must never block the others
            log.debug("lyrics provider %s failed for %s: %s", name, meta.video_id, e)
            continue
        if r is None:
            continue
        if name == "ytmusic" and r.plain:
            reference = r.plain
        if r.instrumental:
            if reference is None:  # nobody contradicts it
                return r
            continue
        if (
            name != "ytmusic"
            and reference
            and similarity(r.plain, reference) < SIMILARITY_MIN
        ):
            log.debug(
                "%s lyrics for %s rejected: differ from YouTube's text",
                name,
                meta.video_id,
            )
            continue
        if r.kind == "synced":
            if not plausible_synced(r.lines, meta.duration_s):
                continue
            if (
                getattr(prov, "fuzzy", False)
                and reference is None
                and not covers(r.lines, meta.duration_s)
            ):
                log.debug(
                    "%s lyrics for %s rejected: end long before the track does",
                    name,
                    meta.video_id,
                )
                continue  # free-text search + nothing to cross-check against => be stricter
            return r
        if is_instrumental(r.plain):
            continue
        if fallback is None:
            fallback = r
    return fallback
