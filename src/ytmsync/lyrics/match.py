"""Lyrics validation helpers (idea: better-lyrics cross-checks candidates against YouTube's own lyrics)."""

from __future__ import annotations

import re
from collections import Counter

from .lrc import LyricLine

SIMILARITY_MIN = 0.5  # same threshold better-lyrics uses


def _norm(s: str) -> str:
    return re.sub(r"[\W_]+", " ", s.casefold()).strip()


def similarity(a: str, b: str) -> float:
    """Sørensen–Dice on character bigrams, 0..1. Works for CJK (no spaces needed)."""
    a, b = _norm(a), _norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    if len(a) < 2 or len(b) < 2:
        return 0.0
    A = Counter(a[i : i + 2] for i in range(len(a) - 1))
    B = Counter(a_ for a_ in (b[i : i + 2] for i in range(len(b) - 1)))
    overlap = sum((A & B).values())
    return 2 * overlap / (sum(A.values()) + sum(B.values()))


_INSTR = re.compile(
    r"^(?:instrumental|instrumental music|no lyrics|music|\(?instrumental\)?)$"
)


def is_instrumental(plain: str) -> bool:
    n = _norm(plain)
    return n == "" or bool(_INSTR.match(n))


def plausible_synced(
    lines: list[LyricLine], duration_s: float | None, slack_s: float = 8.0
) -> bool:
    """Reject LRC that clearly belongs to another version of the song."""
    nonempty = [l for l in lines if l.text]
    if len(nonempty) < 2:
        return False
    return not (
        duration_s and max(l.start_ms for l in lines) / 1000 > duration_s + slack_s
    )


def covers(
    lines: list[LyricLine], duration_s: float | None, ratio: float = 0.5
) -> bool:
    """Fuzzy providers (search by free text) can return a shorter/other song. Real lyrics reach past `ratio` of the track."""
    if not duration_s:
        return True
    return max((l.start_ms for l in lines), default=0) / 1000 >= duration_s * ratio
