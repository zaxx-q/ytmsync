from __future__ import annotations

import re

from ...models import LyricsResult, TrackMeta
from ..lrc import LyricLine, to_plain
from .base import LyricsContext

_ANNOTATION = re.compile(
    r"^[\[\(（【♪\s]*(music|instrumental|applause|laughter|singing|intro|outro|♪)[\]\)）】♪\s]*$",
    re.IGNORECASE,
)


def pick_language(subs: dict, video_lang: str | None) -> str | None:
    keys = [
        k
        for k in subs
        if k != "live_chat" and any(f.get("ext") == "json3" for f in subs[k])
    ]
    if not keys:
        return None
    if video_lang:
        base = video_lang.split("-")[0].lower()
        for k in keys:
            if k.lower() == video_lang.lower():
                return k
        for k in keys:
            if k.split("-")[0].lower() == base:
                return k
    return min(keys)


def parse_json3(data: dict) -> list[LyricLine]:
    lines: list[LyricLine] = []
    for ev in data.get("events") or []:
        segs = ev.get("segs")
        if not segs or ev.get("tStartMs") is None:
            continue
        text = re.sub(
            r"\s+", " ", "".join(s.get("utf8", "") for s in segs).replace("\n", " ")
        ).strip()
        if not text or _ANNOTATION.match(text):
            continue
        if lines and lines[-1].text == text:  # rolling captions repeat a line
            continue
        lines.append(LyricLine(int(ev["tStartMs"]), text))
    return lines


class YtCaptionsProvider:
    """Creator-uploaded (MANUAL) YouTube subtitles. Auto-generated captions are never used: they are speech-to-text guesses.
    Needs the yt-dlp `info` dict, so it only works inside a download — not in the later lyrics-upgrade pass."""

    name = "ytcaptions"

    def fetch(self, meta: TrackMeta, ctx: LyricsContext) -> LyricsResult | None:
        subs = (ctx.info or {}).get(
            "subtitles"
        ) or {}  # 'automatic_captions' is a different key: deliberately ignored
        lang = pick_language(subs, (ctx.info or {}).get("language"))
        if not lang:
            return None
        fmt = next(f for f in subs[lang] if f.get("ext") == "json3")
        r = ctx.http.get(fmt["url"], timeout=15)
        if r.status_code != 200:
            return None
        lines = parse_json3(r.json())
        return (
            LyricsResult("synced", self.name, lines, to_plain(lines))
            if len(lines) >= 2
            else None
        )
