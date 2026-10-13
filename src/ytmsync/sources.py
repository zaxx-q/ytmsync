from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

_PL_ID = re.compile(
    r"^(?:PL|OLAK5uy_|RDCLAK|RDAMPL|UU|FL|LL|OL)[A-Za-z0-9_-]{8,}$|^LM$|^VL.+$"
)
_VIDEO_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


@dataclass(frozen=True)
class Source:
    kind: str  # "playlist" | "video" | "album_browse"
    id: str

    @property
    def url(self) -> str:
        if self.kind == "playlist":
            return f"https://music.youtube.com/playlist?list={self.id}"
        if self.kind == "video":
            return f"https://music.youtube.com/watch?v={self.id}"
        return f"https://music.youtube.com/browse/{self.id}"


def parse_source(arg: str) -> Source:
    arg = arg.strip()
    if arg.startswith("VL") and len(arg) > 2 and _PL_ID.match(arg):
        arg = arg[2:]
    if _PL_ID.match(arg):
        return Source("playlist", arg)
    u = urlparse(arg if "://" in arg else "https://" + arg)
    if not u.netloc.endswith(("youtube.com", "youtu.be")):
        raise ValueError(f"Not a YouTube / YouTube Music URL or playlist id: {arg!r}")
    q = parse_qs(u.query)
    if "list" in q:  # playlist wins even on watch?v=..&list=..
        return Source("playlist", q["list"][0])
    if u.path.startswith(
        "/browse/MPRE"
    ):  # album page -> resolved to playlist via ytmusicapi.get_album
        return Source("album_browse", u.path.rsplit("/", 1)[-1])
    if "v" in q and _VIDEO_ID.match(q["v"][0]):
        return Source("video", q["v"][0])
    if u.netloc == "youtu.be" and _VIDEO_ID.match(u.path.lstrip("/")):
        return Source("video", u.path.lstrip("/"))
    raise ValueError(f"Could not find a playlist or video id in {arg!r}")
