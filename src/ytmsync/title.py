# Adapted from KraXen72/shira (MIT License, Copyright (c) 2024 KraXen72).
"""Port of Shira's clean_title (MIT, KraXen72/shira). Use ONLY for user-uploaded/music-video titles."""

import re

BRACKETS = [("[", "]"), ("【", "】"), ("「", "」"), ("（", "）")]
BANNED = ["♪"]
_EMOJI = re.compile(
    "["
    "\U0001f600-\U0001f64f"
    "\U0001f300-\U0001f5ff"
    "\U0001f680-\U0001f6ff"
    "\U0001f1e0-\U0001f1ff"
    "\U00002700-\U000027bf"
    "\U0001f900-\U0001f9ff"
    "\U00002600-\U000026ff"
    "]+",
    flags=re.UNICODE,
)
_CJK = re.compile(r"^[一-龠]+|[ぁ-ゔ]+|[ァ-ヴー]+|[々〆〤ヶ]+|\s+$")


def clean_title(title: str) -> str:
    for ch in BANNED:
        title = title.replace(ch, "")
    for lb, rb in BRACKETS:
        lbe, rbe = re.escape(lb), re.escape(rb)
        for m in re.finditer(rf"{lbe}([^{lbe}{rbe}]+){rbe}", title):
            keep = (
                f"[{m.group(1)}]"
                if ("cover" in m.group(0).lower() or _CJK.match(m.group(1)))
                else ""
            )
            title = title.replace(m.group(0), keep)
    title = _EMOJI.sub("", title)
    title = re.sub(r"\*\b[A-Z ]+\b\*", "", title)  # *NOW ON ALL PLATFORMS*
    title = re.sub(r"(\S)\[", r"\g<1> [", title)  # "abc[def]" -> "abc [def]"
    title = re.sub(r"\s{2,}", " ", title)
    return title.replace("_", "-").strip()
