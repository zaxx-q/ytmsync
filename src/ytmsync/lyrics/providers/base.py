from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ...httpcache import HttpCache
from ...models import LyricsResult, TrackMeta
from ...ytm import YTMeta


@dataclass
class LyricsContext:
    ytm: YTMeta
    http: object  # requests.Session-like (needs .get)
    cache: HttpCache
    info: dict | None = (
        None  # yt-dlp info dict of the downloaded track (used by the optional `ytcaptions` provider)
    )


class Provider(Protocol):
    name: str

    def fetch(self, meta: TrackMeta, ctx: LyricsContext) -> LyricsResult | None:
        """Return None for 'no lyrics'. MAY raise on network errors — the orchestrator isolates every provider."""
