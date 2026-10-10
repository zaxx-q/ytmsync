from __future__ import annotations

import os
from pathlib import Path

from .paths import sanitize_component

PLAYLIST_DIR = "_Playlists"      # underscore: sorts first and cannot collide with an "Artist" folder in normal libraries


def write_m3u(lib: Path, title: str, entries: list[tuple[str, str, int | None]], taken: set[str] | None = None,
              playlist_id: str = "") -> Path:
    """entries = [(relative audio path, 'Artist - Title', duration_s)]. Paths inside are relative to the .m3u8 itself."""
    d = lib / PLAYLIST_DIR
    d.mkdir(parents=True, exist_ok=True)
    name = sanitize_component(title)
    if taken is not None:
        if name.casefold() in taken:                 # two playlists with the same title in one run
            name = f"{name} [{playlist_id}]"
        taken.add(name.casefold())
    out = d / f"{name}.m3u8"
    lines = ["#EXTM3U"]
    for rel, label, dur in entries:
        lines.append(f"#EXTINF:{int(dur) if dur else -1},{label}")
        lines.append(os.path.relpath(lib / rel, d).replace(os.sep, "/"))
    tmp = out.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, out)
    return out
