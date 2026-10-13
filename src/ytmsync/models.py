from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

from .lyrics.lrc import LyricLine


@dataclass
class TrackMeta:
    video_id: str
    title: str
    artists: list[str]  # individual names, order = credit order
    album: str | None = None
    album_id: str | None = None  # YTM browseId (MPREb_...) or None
    album_artists: list[str] = field(default_factory=list)
    track_no: int | None = None
    track_total: int | None = None
    disc_no: int | None = None
    disc_total: int | None = None
    year: int | None = None
    date: str | None = None  # "YYYY-MM-DD" only when genuinely known
    duration_s: float | None = None
    cover_url: str | None = None
    is_single: bool = False
    is_official_audio: bool = False  # YTM videoType == MUSIC_VIDEO_TYPE_ATV
    explicit: bool = False
    lyrics_browse_id: str | None = None
    # --- filled only by the optional MusicBrainz step (all default to "unknown") ---
    mb_recording_id: str | None = None
    mb_release_id: str | None = None
    mb_release_group_id: str | None = None
    mb_artist_ids: list[str] = field(default_factory=list)
    mb_album_artist_ids: list[str] = field(default_factory=list)
    isrc: str | None = None

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> TrackMeta:
        return cls(**d)


@dataclass
class LyricsResult:
    kind: Literal["synced", "plain"]
    source: str  # provider name
    lines: list[LyricLine] = field(
        default_factory=list
    )  # required when kind == "synced"
    plain: str = ""  # always filled (derived from lines when synced)
    instrumental: bool = False
