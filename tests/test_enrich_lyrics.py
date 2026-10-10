import json
from io import BytesIO

import pytest
from PIL import Image

from ytmsync.config import Settings
from ytmsync.httpcache import HttpCache
from ytmsync.lyrics.chain import fetch_lyrics, lyrics_kind
from ytmsync.lyrics.lrc import LyricLine, to_plain
from ytmsync.lyrics.providers.base import LyricsContext
from ytmsync.lyrics.providers.lrclib import LrcLibProvider
from ytmsync.lyrics.providers.ytmusic import YtMusicProvider
from ytmsync.metadata import fetch_cover, merge_ytdlp, meta_from_ytdlp
from ytmsync.models import LyricsResult, TrackMeta
from ytmsync.ytm import YTMeta, best_thumb, parse_length


# ----------------------------------------------------------------- ytmusicapi fakes shaped like the real docstrings
class FakeAPI:
    def __init__(self, watch=None, album=None, lyrics=None, boom=False):
        self.watch, self.album_data, self.lyr, self.boom, self.calls = watch, album, lyrics, boom, []
    def get_watch_playlist(self, videoId, limit=25):
        self.calls.append(("watch", videoId))
        if self.boom: raise RuntimeError("network down")
        return self.watch
    def get_album(self, browseId):
        self.calls.append(("album", browseId))
        if self.boom: raise RuntimeError("network down")
        return self.album_data
    def get_lyrics(self, browseId, timestamps=False):
        self.calls.append(("lyrics", browseId)); return self.lyr

def watch(video_id="vid00000001", vtype="MUSIC_VIDEO_TYPE_ATV", album=None, title="Foolish Of Me (feat. J)"):
    return {"lyrics": "MPLYt_abc", "tracks": [{
        "videoId": video_id, "title": title, "length": "3:07", "videoType": vtype, "year": "2020",
        "thumbnail": [{"url": "https://lh3.googleusercontent.com/x=w60-h60-l90-rj", "width": 60, "height": 60}],
        "artists": [{"name": "Seven Lions", "id": "1"}, {"name": "Jason Ross", "id": "2"}],
        "album": album}]}

ALBUM = {"title": "Foolish Of Me (Deluxe)", "year": "2021", "trackCount": 12, "audioPlaylistId": "OLAK5uy_x",
         "artists": [{"name": "Seven Lions", "id": "1"}], "thumbnails": [{"url": "https://lh3.googleusercontent.com/alb=w120-h120", "width": 120, "height": 120}],
         "tracks": [{"videoId": "other0000001", "title": "Intro", "trackNumber": 0},
                    {"videoId": "vid00000001", "title": "Foolish Of Me", "trackNumber": 0, "isExplicit": True}]}

def test_parse_length():
    assert (parse_length("3:07"), parse_length("1:02:03"), parse_length(None), parse_length("x:y")) == (187.0, 3723.0, None, None)

def test_enrich_album_track_uses_list_position_not_trackNumber():
    api = FakeAPI(watch(album={"name": "Foolish Of Me", "id": "MPREb_1"}), ALBUM)
    ym = YTMeta(lambda: api)
    m = ym.enrich("vid00000001")
    assert (m.track_no, m.track_total) == (2, 12)                       # position in list, though trackNumber says 0
    assert m.album == "Foolish Of Me (Deluxe)" and m.year == 2021 and m.album_artists == ["Seven Lions"]
    assert m.title == "Foolish Of Me (feat. J)"                          # ATV title untouched
    assert m.artists == ["Seven Lions", "Jason Ross"] and m.explicit and m.duration_s == 187 and m.lyrics_browse_id == "MPLYt_abc"
    assert m.cover_url.endswith("=w1200-h1200-l90-rj") and "alb" in m.cover_url    # album art preferred, upscaled
    ym.enrich("vid00000001"); assert [c for c in api.calls if c[0] == "album"] == [("album", "MPREb_1")]   # album cached

def test_enrich_matches_album_track_by_title_when_video_id_differs():
    alb = {**ALBUM, "tracks": [{"videoId": "zzz", "title": "foolish of me (feat. J)"}]}
    m = YTMeta(lambda: FakeAPI(watch(album={"name": "A", "id": "MPREb_1"}), alb)).enrich("vid00000001")
    assert m.track_no == 1

def test_enrich_single_and_ugc_title_cleaning():
    m = YTMeta(lambda: FakeAPI(watch(vtype="MUSIC_VIDEO_TYPE_UGC", title="Cool Song【ENGLISH COVER】 ♪"))).enrich("vid00000001")
    assert m.is_single and m.album == "Cool Song [ENGLISH COVER] - Single" and (m.track_no, m.track_total) == (1, 1)
    assert not m.is_official_audio and m.album_artists == m.artists

def test_enrich_survives_failures():
    assert YTMeta(lambda: FakeAPI(boom=True)).enrich("v") is None
    assert YTMeta(lambda: FakeAPI(watch={"tracks": []})).enrich("v") is None
    m = YTMeta(lambda: FakeAPI(watch(album={"name": "Named", "id": "MPREb_1"}), album=None)).enrich("vid00000001")   # album page fails
    assert m.album == "Named" and m.track_no is None

def test_resolve_album_browse():
    assert YTMeta(lambda: FakeAPI(album=ALBUM)).resolve_album_browse("MPREb_1") == "OLAK5uy_x"

def test_best_thumb_picks_largest():
    assert best_thumb([{"url": "https://i/a=w60-h60", "width": 60, "height": 60}, {"url": "https://i/b", "width": 500, "height": 500}]) == "https://i/b"
    assert best_thumb(None) is None


# ----------------------------------------------------------------- yt-dlp fallback / merge
def test_meta_from_ytdlp_official_and_ugc():
    m = meta_from_ytdlp({"id": "v", "track": "Song", "artists": ["A", "B"], "album": "Alb", "release_date": "20190503", "duration": 200, "thumbnail": "u"})
    assert (m.title, m.artists, m.album, m.year, m.date, m.is_single) == ("Song", ["A", "B"], "Alb", 2019, "2019-05-03", False)
    u = meta_from_ytdlp({"id": "v", "title": "Song 【COVER】", "channel": "Someone - Topic"})
    assert u.artists == ["Someone"] and u.is_single and u.album.endswith("- Single") and u.track_no == 1

def test_merge_fills_gaps_only():
    m = TrackMeta("v", "T", ["A"], year=2021, is_official_audio=True)
    merge_ytdlp(m, {"duration": 100, "release_date": "20210302", "release_year": 1999})
    assert (m.duration_s, m.year, m.date) == (100.0, 2021, "2021-03-02")
    m2 = TrackMeta("v", "T", ["A"], year=2021, is_official_audio=True)
    merge_ytdlp(m2, {"release_date": "20150101"}); assert m2.date is None      # disagrees with album year => ignored
    m3 = TrackMeta("v", "T", ["A"], duration_s=5); merge_ytdlp(m3, {"duration": 100}); assert m3.duration_s == 5


# ----------------------------------------------------------------- cover fetching
class Resp:
    def __init__(self, status=200, content=b"", text="", js=None):
        self.status_code, self.content, self._js = status, content, js
        self.text = text or (json.dumps(js) if js is not None else "")
    def json(self): return self._js
    def raise_for_status(self):
        if self.status_code >= 400: raise RuntimeError(f"HTTP {self.status_code}")

class FakeHttp:
    def __init__(self, routes): self.routes, self.calls = routes, []
    def get(self, url, **kw):
        self.calls.append((url, kw))
        for prefix, resp in self.routes.items():
            if url.startswith(prefix): return resp() if callable(resp) else resp
        return Resp(404)

def _jpeg(size):
    b = BytesIO(); Image.new("RGB", size, (9, 90, 200)).save(b, "JPEG", quality=95); return b.getvalue()

def test_fetch_cover_skips_placeholders_and_squares(tmp_path):
    m = TrackMeta("vid", "T", ["A"], cover_url="https://c/bad")
    http = FakeHttp({"https://c/bad": Resp(200, _jpeg((120, 90))),                       # tiny placeholder
                     "https://i.ytimg.com/vi/vid/maxresdefault": Resp(404),
                     "https://i.ytimg.com/vi/vid/hqdefault": Resp(200, _jpeg((640, 480)))})
    out = fetch_cover(m, {}, Settings(library_dir=tmp_path, cover_method="pad"), http)
    assert Image.open(BytesIO(out)).size == (640, 640)                                  # forced pad
    out = fetch_cover(m, {}, Settings(library_dir=tmp_path), http)                       # auto: solid image => flat corners => centre crop
    assert Image.open(BytesIO(out)).size == (480, 480)
    assert fetch_cover(m, {}, Settings(library_dir=tmp_path), FakeHttp({})) is None      # nothing works => None, no exception


# ----------------------------------------------------------------- lyrics providers
@pytest.fixture
def ctx(tmp_path):
    n = iter(range(1000))          # every context gets its OWN cache file, otherwise scenarios contaminate each other
    def make(api=None, http=None): return LyricsContext(YTMeta(lambda: api or FakeAPI()), http or FakeHttp({}), HttpCache(tmp_path / f"h{next(n)}.sqlite"))
    return make

class LL:  # ytmusicapi.LyricLine look-alike
    def __init__(self, text, start): self.text, self.start_time, self.end_time, self.id = text, start, start + 1000, 1

META = TrackMeta("vid", "Hello", ["Adele"], "25", duration_s=295, lyrics_browse_id="MPLYt_1")
PLAIN_YT = "Hello, it's me\nI was wondering if after all these years\nYou'd like to meet"

def test_ytmusic_provider_synced_plain_none(ctx):
    r = YtMusicProvider().fetch(META, ctx(FakeAPI(lyrics={"lyrics": [LL("Hello", 9200), LL(" ", 12000), LL("World", 15000)], "hasTimestamps": True})))
    assert r.kind == "synced" and [(l.start_ms, l.text) for l in r.lines] == [(9200, "Hello"), (12000, ""), (15000, "World")] and r.plain == "Hello\n\nWorld"
    assert YtMusicProvider().fetch(META, ctx(FakeAPI(lyrics={"lyrics": PLAIN_YT, "hasTimestamps": False}))).kind == "plain"
    assert YtMusicProvider().fetch(META, ctx(FakeAPI(lyrics=None))) is None
    assert YtMusicProvider().fetch(TrackMeta("v", "T", ["A"]), ctx()) is None            # no lyrics browse id

LRCLIB_ITEM = {"trackName": "Hello", "artistName": "Adele", "albumName": "25", "duration": 295, "instrumental": False,
               "plainLyrics": PLAIN_YT, "syncedLyrics": "[00:01.00]Hello, it's me\n[00:05.00]I was wondering if after all these years\n[00:09.00]You'd like to meet"}

def test_lrclib_exact_get_and_params(ctx):
    http = FakeHttp({"https://lrclib.net/api/get": Resp(200, text=json.dumps(LRCLIB_ITEM), js=LRCLIB_ITEM)})
    r = LrcLibProvider().fetch(META, ctx(http=http))
    assert r.kind == "synced" and len(r.lines) == 3
    url, kw = http.calls[0]
    assert kw["params"] == {"track_name": "Hello", "artist_name": "Adele", "album_name": "25", "duration": 295} and "ytmsync/" in kw["headers"]["User-Agent"]

def test_lrclib_falls_back_to_search_and_picks_best(ctx):
    results = [{**LRCLIB_ITEM, "duration": 320, "syncedLyrics": "[00:01.00]x"},                        # wrong length
               {**LRCLIB_ITEM, "syncedLyrics": None, "duration": 296},                                    # plain only
               {**LRCLIB_ITEM, "duration": 294},                                                          # synced, off by 1
               {**LRCLIB_ITEM, "trackName": "Totally Different Song", "duration": 295}]
    http = FakeHttp({"https://lrclib.net/api/get": Resp(404), "https://lrclib.net/api/search": Resp(200, text="[]", js=results)})
    r = LrcLibProvider().fetch(META, ctx(http=http))
    assert r.kind == "synced" and [c[0].rsplit("/", 1)[-1] for c in http.calls] == ["get", "search"]

def test_lrclib_instrumental_plain_only_and_miss(ctx):
    inst = {**LRCLIB_ITEM, "instrumental": True, "plainLyrics": None, "syncedLyrics": None}
    assert LrcLibProvider().fetch(META, ctx(http=FakeHttp({"https://lrclib.net/api/get": Resp(200, text="{}", js=inst)}))).instrumental
    plain = {**LRCLIB_ITEM, "syncedLyrics": None}
    assert LrcLibProvider().fetch(META, ctx(http=FakeHttp({"https://lrclib.net/api/get": Resp(200, text="{}", js=plain)}))).kind == "plain"
    assert LrcLibProvider().fetch(META, ctx(http=FakeHttp({"https://lrclib.net/api/": Resp(404)}))) is None

def test_lrclib_negative_cache_and_server_errors(ctx):
    http = FakeHttp({"https://lrclib.net/api/": Resp(404)})
    c = ctx(http=http); p = LrcLibProvider()
    assert p.fetch(META, c) is None; n = len(http.calls)
    assert p.fetch(META, c) is None and len(http.calls) == n                                     # second run served from cache
    boom = ctx(http=FakeHttp({"https://lrclib.net/api/": Resp(503)}))
    with pytest.raises(RuntimeError): p.fetch(META, boom)
    assert boom.cache.get("lrclib:get?album_name=25&artist_name=Adele&duration=295&track_name=Hello") is None   # 5xx never cached


# ----------------------------------------------------------------- orchestrator
class Stub:
    def __init__(self, name, result=None, exc=None): self.name, self.result, self.exc, self.called = name, result, exc, 0
    def fetch(self, meta, ctx):
        self.called += 1
        if self.exc: raise self.exc
        return self.result

def S(name, lines, dur=None): 
    ls = [LyricLine(t, x) for t, x in lines]; return LyricsResult("synced", name, ls, to_plain(ls))
def P(name, text): return LyricsResult("plain", name, [], text)
SYNC_OK = [(1000, "Hello, it's me"), (5000, "I was wondering if after all these years"), (9000, "You'd like to meet")]
SYNC_WRONG = [(1000, "Completely different words about cars"), (5000, "and trucks and long roads to nowhere"), (9000, "engine noise")]

def run(reg, names, meta=META): return fetch_lyrics(meta, None, names, {s.name: s for s in reg})

def test_chain_prefers_synced_and_stops_early():
    yt, lr = Stub("ytmusic", S("ytmusic", SYNC_OK)), Stub("lrclib", S("lrclib", SYNC_OK))
    assert run([yt, lr], ["lrclib", "ytmusic"]).source == "ytmusic" and lr.called == 0        # ytmusic always runs first
def test_chain_upgrades_plain_to_synced():
    yt, lr = Stub("ytmusic", P("ytmusic", PLAIN_YT)), Stub("lrclib", S("lrclib", SYNC_OK))
    assert run([yt, lr], ["ytmusic", "lrclib"]).kind == "synced"
def test_chain_rejects_candidate_that_contradicts_youtube_text():
    yt, lr = Stub("ytmusic", P("ytmusic", PLAIN_YT)), Stub("lrclib", S("lrclib", SYNC_WRONG))
    r = run([yt, lr], ["ytmusic", "lrclib"]); assert r.source == "ytmusic" and r.kind == "plain"
def test_chain_rejects_implausible_length_and_skips_broken_providers():
    long_ = S("lrclib", [(1000, "a"), (900_000, "b")])
    assert run([Stub("ytmusic", exc=RuntimeError("x")), Stub("lrclib", long_)], ["ytmusic", "lrclib"]) is None
    assert run([Stub("lrclib", S("lrclib", SYNC_OK))], ["nope", "lrclib"]).source == "lrclib"            # unknown name ignored
def test_chain_instrumental_rules():
    inst = LyricsResult("plain", "lrclib", [], "", instrumental=True)
    assert run([Stub("ytmusic"), Stub("lrclib", inst)], ["ytmusic", "lrclib"]).instrumental                # nobody contradicts
    assert not run([Stub("ytmusic", P("ytmusic", PLAIN_YT)), Stub("lrclib", inst)], ["ytmusic", "lrclib"]).instrumental
    assert run([Stub("ytmusic", P("ytmusic", "Instrumental"))], ["ytmusic"]) is None
def test_lyrics_kind_values():
    assert lyrics_kind(None) == "none" and lyrics_kind(P("x", "t")) == "plain" and lyrics_kind(S("x", SYNC_OK)) == "synced"
    assert lyrics_kind(LyricsResult("plain", "x", [], "", instrumental=True)) == "instrumental"
