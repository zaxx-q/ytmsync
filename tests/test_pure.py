from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from ytmsync.cover import choose_method, to_square_jpeg, upscale_google_url
from ytmsync.lyrics.lrc import LyricLine, format_lrc, parse_lrc, to_plain
from ytmsync.lyrics.match import is_instrumental, plausible_synced, similarity
from ytmsync.paths import (
    PRESETS,
    disambiguate,
    is_playlist_scoped,
    render_path,
    sanitize_component,
    validate_template,
)
from ytmsync.sources import Source, parse_source
from ytmsync.title import clean_title

SRC = """[ti:Song]
[offset:500]
[00:01.00]Hello <00:01.20>there
[00:03.5][01:10.25]Chorus line
[00:05.123]<00:05.2>Word level [bg:(ooh)]
[00:07]
plain untimed line
"""


# ---------------------------------------------------------------- lrc
def test_parse_lrc_all_dialects():
    lines, meta = parse_lrc(SRC)
    assert [(l.start_ms, l.text) for l in lines] == [
        (500, "Hello there"),
        (3000, "Chorus line"),
        (4623, "Word level (ooh)"),
        (6500, ""),
        (69750, "Chorus line"),
    ]
    assert meta["ti"] == "Song"


def test_untimed_text_is_not_synced():
    assert parse_lrc("just words\nno stamps")[0] == []


def test_format_roundtrip_at_centisecond_precision():
    lines, _ = parse_lrc(SRC)
    again, _ = parse_lrc(format_lrc(lines, {"ti": "Song"}))
    assert [(round(l.start_ms / 10), l.text) for l in again] == [
        (round(l.start_ms / 10), l.text) for l in lines
    ]


def test_stamp_rounding_and_carry():
    got = [format_lrc([LyricLine(ms, "x")]).strip() for ms in (59999, 1004, 1005, 0)]
    assert got == ["[01:00.00]x", "[00:01.00]x", "[00:01.01]x", "[00:00.00]x"]


def test_to_plain_collapses_gaps():
    assert (
        to_plain(
            [
                LyricLine(0, "a"),
                LyricLine(1, ""),
                LyricLine(2, ""),
                LyricLine(3, "b"),
                LyricLine(4, ""),
            ]
        )
        == "a\n\nb"
    )


# ---------------------------------------------------------------- match
def test_similarity():
    a = "I want to hold your hand\nI want to hold your hand"
    assert similarity(a, a) == 1.0
    assert similarity(a, "Completely different words about cars and trains") < 0.5
    assert similarity(a, "i want to hold your hand, i want to hold ur hand") > 0.85
    assert similarity("君の名前を呼んで", "君の名前を呼ぶ") > 0.5
    assert similarity("", "x") == 0.0


def test_instrumental_and_plausible():
    assert is_instrumental("♪ Instrumental ♪") and not is_instrumental(
        "real lyrics here"
    )
    lines = [LyricLine(1000, "a"), LyricLine(200_000, "b")]
    assert plausible_synced(lines, 205) and not plausible_synced(lines, 100)
    assert not plausible_synced([LyricLine(0, "only one")], 100)


# ---------------------------------------------------------------- paths
V = {
    "albumartist": "AC/DC",
    "album": "Back In Black: Deluxe",
    "title": "Hells Bells?",
    "track": 1,
    "artist": "AC/DC",
    "year": 1980,
    "video_id": "abc",
}


def test_library_template():
    assert (
        render_path(PRESETS["library"], V, "opus")
        == "AC_DC/Back In Black - Deluxe/01 Hells Bells.opus"
    )


def test_playlist_template():
    assert (
        render_path(
            PRESETS["playlist"],
            {**V, "playlist": "Road Trip", "playlist_index": 7},
            "m4a",
        )
        == "Road Trip/007 AC_DC - Hells Bells.m4a"
    )


def test_edge_cases():
    p = render_path(
        "{albumartist}/{album}/{track:02d} {title}",
        {"albumartist": "CON", "album": None, "title": "x" * 400, "track": None},
        "opus",
    )
    a, b, leaf = p.split("/")
    assert (
        a == "_CON"
        and b == "Unknown"
        and leaf.startswith("00 x")
        and leaf.endswith(".opus")
        and len(leaf.encode()) <= 200
    )
    assert (
        render_path("{album}/{title}", {"album": "Wait...", "title": "End."}, "opus")
        == "Wait/End.opus"
    )
    assert sanitize_component("宇多田ヒカル") == "宇多田ヒカル"
    assert sanitize_component("///") == "___"
    assert sanitize_component("") == "_"


def test_disambiguate_and_scope():
    assert disambiguate("a/b/01 T.opus", "dQw4w9WgXcQ") == "a/b/01 T [dQw4w9WgXcQ].opus"
    assert not is_playlist_scoped(PRESETS["library"]) and is_playlist_scoped(
        PRESETS["playlist"]
    )


@pytest.mark.parametrize("bad", ["{nope}/{title}", "{album}/{track}"])
def test_bad_templates(bad):
    with pytest.raises(ValueError):
        validate_template(bad)


# ---------------------------------------------------------------- title
def test_clean_title_shira_example():
    assert (
        clean_title(
            "IDOL【ENGLISH EDM COVER】「アイドル」 by ARTIST【Artist1 x @Artist2 】 ♪"
        )
        == "IDOL [ENGLISH EDM COVER] [アイドル] by ARTIST"
    )


# ---------------------------------------------------------------- sources
@pytest.mark.parametrize(
    "arg,expected",
    [
        ("PLabc123456789", Source("playlist", "PLabc123456789")),
        (
            "https://music.youtube.com/playlist?list=PLxyz987654321&si=foo",
            Source("playlist", "PLxyz987654321"),
        ),
        (
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=RDdQw4w9WgXcQ",
            Source("playlist", "RDdQw4w9WgXcQ"),
        ),
        (
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ",
            Source("video", "dQw4w9WgXcQ"),
        ),
        ("https://youtu.be/dQw4w9WgXcQ", Source("video", "dQw4w9WgXcQ")),
        (
            "https://music.youtube.com/browse/MPREb_abcdef",
            Source("album_browse", "MPREb_abcdef"),
        ),
        ("LM", Source("playlist", "LM")),
        ("VLPLabc123456789", Source("playlist", "PLabc123456789")),
    ],
)
def test_parse_source(arg, expected):
    assert parse_source(arg) == expected


def test_parse_source_rejects_foreign_urls():
    with pytest.raises(ValueError):
        parse_source("https://example.com/x")


# ---------------------------------------------------------------- cover
def _png(draw_fn, size=(640, 360)):
    im = Image.new("RGB", size, (20, 20, 20))
    draw_fn(ImageDraw.Draw(im))
    b = BytesIO()
    im.save(b, "PNG")
    return b.getvalue()


def test_cover_crop_vs_pad():
    flat = _png(lambda d: d.ellipse((220, 60, 420, 300), fill=(220, 40, 40)))
    busy = _png(
        lambda d: [
            d.line((x, 0, x, 360), fill=(x // 3, 255 - x // 3, (x * 7) % 255))
            for x in range(640)
        ]
    )
    assert choose_method(Image.open(BytesIO(flat)).convert("RGB"))[0] == "crop"
    assert choose_method(Image.open(BytesIO(busy)).convert("RGB"))[0] == "pad"
    assert Image.open(BytesIO(to_square_jpeg(flat))).size == (360, 360)
    assert Image.open(BytesIO(to_square_jpeg(busy))).size == (640, 640)


def test_cover_edge_cases():
    rgba = BytesIO()
    Image.new("RGBA", (300, 300), (1, 2, 3, 128)).save(rgba, "PNG")
    assert Image.open(BytesIO(to_square_jpeg(rgba.getvalue()))).mode == "RGB"
    tiny = BytesIO()
    Image.new("RGB", (100, 60)).save(tiny, "PNG")
    assert Image.open(BytesIO(to_square_jpeg(tiny.getvalue()))).size == (100, 100)


def test_upscale_google_url():
    assert upscale_google_url(
        "https://lh3.googleusercontent.com/AbC=w60-h60-l90-rj"
    ).endswith("=w1200-h1200-l90-rj")
    assert (
        upscale_google_url("https://i.ytimg.com/vi/x/maxresdefault.jpg")
        == "https://i.ytimg.com/vi/x/maxresdefault.jpg"
    )
