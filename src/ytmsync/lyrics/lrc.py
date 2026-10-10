"""LRC parsing / formatting. Line-level only (word-level tags are stripped)."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class LyricLine:
    start_ms: int
    text: str  # may be "" (an instrumental gap / end-of-line marker)


# [mm:ss.xx]  [mm:ss.xxx]  [mm:ss]  [mm:ss:xx]  (minutes may exceed 59)
_TS = re.compile(r"\[(\d+):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_META = re.compile(r"^\[([A-Za-z#]+):(.*)\]\s*$")  # [ar:Artist]; digits-only keys never match
_WORD_TS = re.compile(r"<\d+:\d{1,2}(?:[.:]\d{1,3})?>")  # enhanced-LRC <mm:ss.xx>
_BG = re.compile(r"\[bg:(.*?)\]\s*$")  # better-lyrics' background-vocal extension


def _to_ms(mm: str, ss: str, frac: str | None) -> int:
    # "5" -> 500 ms, "50" -> 500 ms, "05" -> 50 ms, "500" -> 500 ms
    ms = int((frac or "0").ljust(3, "0")[:3])
    return (int(mm) * 60 + int(ss)) * 1000 + ms


def parse_lrc(text: str) -> tuple[list[LyricLine], dict[str, str]]:
    """Return (lines sorted by time, metadata). Empty list => text is not synced."""
    meta: dict[str, str] = {}
    lines: list[LyricLine] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        raw = raw.strip()
        if not raw:
            continue
        stamps = list(_TS.finditer(raw))
        if not stamps:
            m = _META.match(raw)
            if m:
                meta[m.group(1).lower()] = m.group(2).strip()
            continue  # untimed text line: ignore
        # timestamps are all at the start: "[00:10.00][01:30.00]chorus"
        body = raw[stamps[-1].end():]
        body = _WORD_TS.sub("", body)
        body = _BG.sub(lambda m: f" ({m.group(1).strip().strip('()').strip()})" if m.group(1).strip().strip('()').strip() else "", body)
        body = re.sub(r"\s{2,}", " ", body).strip()
        for s in stamps:
            lines.append(LyricLine(_to_ms(*s.groups()), body))
    offset = 0
    if "offset" in meta:
        try:
            offset = int(meta["offset"])
        except ValueError:
            pass
    if offset:
        # LRC spec: milliseconds; positive offset => lyrics appear EARLIER (t - offset).
        lines = [LyricLine(max(0, l.start_ms - offset), l.text) for l in lines]
    lines.sort(key=lambda l: l.start_ms)  # stable: keeps file order for equal times
    return lines, meta


def _stamp(ms: int) -> str:
    cs = (ms + 5) // 10  # round to centiseconds, carry handled by integer maths
    m, rem = divmod(cs, 6000)
    s, c = divmod(rem, 100)
    return f"[{m:02d}:{s:02d}.{c:02d}]"


def format_lrc(lines: list[LyricLine], header: dict[str, str] | None = None) -> str:
    out = [f"[{k}:{v}]" for k, v in (header or {}).items() if v]
    out += [f"{_stamp(l.start_ms)}{l.text}" for l in lines]
    return "\n".join(out) + "\n"


def to_plain(lines: list[LyricLine]) -> str:
    """Timestamps removed; blank timing lines become single blank separators."""
    out: list[str] = []
    for l in lines:
        if l.text == "":
            if out and out[-1] != "":
                out.append("")
        else:
            out.append(l.text)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)
