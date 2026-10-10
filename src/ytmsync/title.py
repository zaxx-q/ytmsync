# Adapted from KraXen72/shira (MIT License, Copyright (c) 2024 KraXen72).
"""Port of Shira's clean_title (MIT, KraXen72/shira). Use ONLY for user-uploaded/music-video titles."""
import re

BRACKETS = [("[", "]"), ("【", "】"), ("「", "」"), ("（", "）")]
BANNED = ["♪"]
_EMOJI = re.compile(
    "[" "\U0001F600-\U0001F64F" "\U0001F300-\U0001F5FF" "\U0001F680-\U0001F6FF" "\U0001F1E0-\U0001F1FF"
    "\U00002700-\U000027BF" "\U0001F900-\U0001F9FF" "\U00002600-\U000026FF" "]+", flags=re.UNICODE)
_CJK = re.compile(r"^[一-龠]+|[ぁ-ゔ]+|[ァ-ヴー]+|[々〆〤ヶ]+|\s+$")


def clean_title(title: str) -> str:
    for ch in BANNED:
        title = title.replace(ch, "")
    for lb, rb in BRACKETS:
        lbe, rbe = re.escape(lb), re.escape(rb)
        for m in re.finditer(rf"{lbe}([^{lbe}{rbe}]+){rbe}", title):
            keep = f"[{m.group(1)}]" if ("cover" in m.group(0).lower() or _CJK.match(m.group(1))) else ""
            title = title.replace(m.group(0), keep)
    title = _EMOJI.sub("", title)
    title = re.sub(r"\*\b[A-Z ]+\b\*", "", title)          # *NOW ON ALL PLATFORMS*
    title = re.sub(r"(\S)\[", r"\g<1> [", title)             # "abc[def]" -> "abc [def]"
    title = re.sub(r"\s{2,}", " ", title)
    return title.replace("_", "-").strip()
