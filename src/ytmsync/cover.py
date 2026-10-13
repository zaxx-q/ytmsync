# Adapted from KraXen72/shira (MIT License, Copyright (c) 2024 KraXen72).
"""Square-cover logic ported from Shira (MIT, KraXen72/shira tagging.py), with guards added."""

from __future__ import annotations

import re
from io import BytesIO
from statistics import mean, stdev

from PIL import Image, ImageFilter, ImageOps

AVG_THRESHOLD = 10
CHANNEL_THRESHOLD = 15


def upscale_google_url(url: str, size: int = 1200) -> str:
    """lh3.googleusercontent.com / yt3.ggpht.com URLs end in '=w60-h60-l90-rj'; rewrite to a big square."""
    if "googleusercontent.com" in url or "ggpht.com" in url:
        return re.sub(r"=[wsh]\d.*$", "", url) + f"=w{size}-h{size}-l90-rj"
    return url


def _corners(img: Image.Image, off: int):
    w, h = img.size
    return [
        img.getpixel(p)
        for p in ((off, off), (w - off, off), (off, h - off), (w - off, h - off))
    ]


def choose_method(img: Image.Image) -> tuple[str, tuple | None]:
    """'crop' if the four corners look like the same flat background, else 'pad'."""
    if min(img.size) < 120:  # guard: Shira would IndexError on tiny images
        return "pad", None
    filt = (
        img.filter(ImageFilter.SMOOTH)
        .convert("P", palette=Image.Palette.ADAPTIVE, colors=64)
        .convert("RGB")
    )
    c50 = _corners(filt, 50)
    devs = [stdev([c[i] for c in c50]) for i in range(3)]
    c0 = _corners(filt, 1)
    fill = c0[0] if len(set(c0)) == 1 else None
    if mean(devs) < AVG_THRESHOLD and all(d < CHANNEL_THRESHOLD for d in devs):
        return "crop", fill
    return "pad", fill


def to_square_jpeg(
    image_bytes: bytes, method: str = "auto", max_px: int = 1200, quality: int = 92
) -> bytes:
    img = Image.open(BytesIO(image_bytes))
    img.load()
    w, h = img.size
    if w != h:
        fill = None
        if method == "auto":
            method, fill = choose_method(img.convert("RGB"))
        if method == "crop":
            half, rect_half = round(w / 2), round(h / 2)
            img = img.crop((half - rect_half, 0, half + rect_half, h))
        else:  # pad to a w x w square (landscape assumed; portrait would pad to h x h)
            side = max(w, h)
            if fill is None:
                fill = (
                    img.convert("RGB")
                    .resize((1, 1), Image.Resampling.BOX)
                    .getpixel((0, 0))
                )
            img = ImageOps.pad(
                img.convert("RGB"), (side, side), color=fill, centering=(0.5, 0.5)
            )
    if max(img.size) > max_px:
        img = img.resize((max_px, max_px), Image.Resampling.LANCZOS)
    out = BytesIO()
    img.convert("RGB").save(
        out, format="JPEG", quality=quality
    )  # RGBA/P -> RGB or JPEG save fails
    return out.getvalue()
