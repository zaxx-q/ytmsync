"""Filename sanitising + template rendering (ideas: SpotDL formatter.py, Shira util)."""

from __future__ import annotations

import re
import string
from pathlib import PurePosixPath

_FORBIDDEN = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WIN_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
MAX_COMPONENT_BYTES = (
    180  # ext + " [videoid]" suffix headroom under the common 255-byte limit
)

PLAYLIST_VARS = {"playlist", "playlist_index", "playlist_id"}
ALL_VARS = {
    "artist",
    "artists",
    "albumartist",
    "album",
    "title",
    "track",
    "tracktotal",
    "disc",
    "disctotal",
    "year",
    "video_id",
    *PLAYLIST_VARS,
}

PRESETS = {
    "library": "{albumartist}/{album}/{track:02d} {title}",
    "playlist": "{playlist}/{playlist_index:03d} {artist} - {title}",
}


def sanitize_component(s: str, max_bytes: int = MAX_COMPONENT_BYTES) -> str:
    s = (
        s.replace(":", " -").replace('"', "'").replace("/", "_").replace("\\", "_")
    )  # AC/DC -> AC_DC
    s = _FORBIDDEN.sub("", s)
    s = (
        re.sub(r"\s+", " ", s).strip().rstrip(". ")
    )  # Windows dislikes trailing dots/spaces
    b = s.encode("utf-8")
    if len(b) > max_bytes:
        s = (
            b[:max_bytes].decode("utf-8", errors="ignore").rstrip(". ")
        )  # never cut mid-character
    if not s:
        return "_"
    if s.split(".")[0].upper() in _WIN_RESERVED:
        s = "_" + s
    return s


def template_vars(template: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(template) if f}


def validate_template(template: str) -> None:
    unknown = template_vars(template) - ALL_VARS
    if unknown:
        raise ValueError(
            f"Unknown template variable(s): {', '.join(sorted(unknown))}. Allowed: {', '.join(sorted(ALL_VARS))}"
        )
    if "title" not in template_vars(template) and "video_id" not in template_vars(
        template
    ):
        raise ValueError(
            "Template must contain {title} or {video_id} so files stay distinguishable"
        )


def is_playlist_scoped(template: str) -> bool:
    return bool(template_vars(template) & PLAYLIST_VARS)


def render_path(template: str, values: dict, ext: str) -> str:
    """Return a POSIX relative path like 'Artist/Album/03 Title.opus'.

    Sanitises each *value* (so a '/' inside a title can't create folders) and then each
    path component. Integers keep their format spec ({track:02d}); missing ints become 0.
    """
    safe: dict = {}
    for k, v in values.items():
        if isinstance(v, (int, float)):
            safe[k] = v
        else:
            safe[k] = sanitize_component(str(v)) if v not in (None, "") else "Unknown"
    for k in ("track", "tracktotal", "disc", "disctotal", "playlist_index", "year"):
        if k in safe and not isinstance(safe[k], int):
            safe[k] = 0
        safe.setdefault(k, 0)
    rendered = template.format_map(_Default(safe))
    parts = [
        sanitize_component(p) for p in rendered.split("/") if p not in ("", ".", "..")
    ]
    if not parts:
        parts = ["_"]
    leaf = parts[-1]
    parts[-1] = sanitize_component(leaf, MAX_COMPONENT_BYTES - len(ext) - 1) + "." + ext
    return str(PurePosixPath(*parts))


class _Default(dict):
    def __missing__(
        self, key
    ):  # unknown/absent var -> "Unknown" instead of KeyError at runtime
        return "Unknown"


def disambiguate(rel_path: str, video_id: str) -> str:
    """Used when a different track already owns rel_path: 'a/b/03 Title.opus' -> 'a/b/03 Title [vid].opus'."""
    p = PurePosixPath(rel_path)
    return str(p.with_name(f"{p.stem} [{video_id}]{p.suffix}"))
