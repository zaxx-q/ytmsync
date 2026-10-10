import sys

import pytest

from ytmsync.config import Settings
from ytmsync.httpcache import HttpCache
from ytmsync.lyrics import chain
from ytmsync.lyrics.chain import PROVIDERS, fetch_lyrics
from ytmsync.lyrics.lrc import LyricLine, to_plain
from ytmsync.lyrics.match import covers
from ytmsync.lyrics.providers import synced as synced_mod
from ytmsync.lyrics.providers.base import LyricsContext
from ytmsync.lyrics.providers.synced import SyncedLyricsProvider
from ytmsync.lyrics.providers.ytcaptions import YtCaptionsProvider, parse_json3, pick_language
from ytmsync.models import LyricsResult, TrackMeta

META = TrackMeta("vid", "Hello", ["Adele"], "25", duration_s=200)


class Resp:
    def __init__(self, status=200, js=None): self.status_code, self._js = status, js
    def json(self): return self._js

class Http:
    def __init__(self, resp): self.resp, self.calls = resp, []
    def get(self, url, **kw): self.calls.append((url, kw)); return self.resp

def ctx(tmp_path, info=None, http=None):
    return LyricsContext(None, http or Http(Resp(404)), HttpCache(tmp_path / "c.sqlite"), info)


# ------------------------------------------------------------------ ytcaptions
def fmts(*exts, url="https://yt/sub"): return [{"ext": e, "url": f"{url}?fmt={e}"} for e in exts]

def test_pick_language():
    subs = {"en": fmts("json3", "vtt"), "de": fmts("json3"), "live_chat": fmts("json3"), "fr": fmts("vtt")}
    assert pick_language(subs, "de") == "de" and pick_language(subs, "en-US") == "en"       # base-language match
    assert pick_language(subs, None) == "de"                                                # deterministic: sorted, live_chat & non-json3 skipped
    assert pick_language({"fr": fmts("vtt")}, "fr") is None and pick_language({}, "en") is None

def test_parse_json3_filters_noise():
    data = {"events": [
        {"tStartMs": 0, "dDurationMs": 500},                                        # window event without segs
        {"tStartMs": 1000, "segs": [{"utf8": "[Music]"}]},
        {"tStartMs": 2000, "segs": [{"utf8": "♪"}]},
        {"tStartMs": 3000, "segs": [{"utf8": "Hello,"}, {"utf8": " it's me"}]},
        {"tStartMs": 3500, "segs": [{"utf8": "Hello, it's me"}]},                    # rolling duplicate
        {"tStartMs": 5000, "segs": [{"utf8": "I was\nwondering"}]},
        {"segs": [{"utf8": "no start time"}]},
        {"tStartMs": 9000, "segs": [{"utf8": "   "}]}]}
    assert [(l.start_ms, l.text) for l in parse_json3(data)] == [(3000, "Hello, it's me"), (5000, "I was wondering")]

JSON3 = {"events": [{"tStartMs": 1000, "segs": [{"utf8": "line one"}]}, {"tStartMs": 4000, "segs": [{"utf8": "line two"}]}]}

def test_ytcaptions_uses_manual_subtitles_only(tmp_path):
    p, http = YtCaptionsProvider(), Http(Resp(200, JSON3))
    r = p.fetch(META, ctx(tmp_path, {"subtitles": {"en": fmts("json3")}, "language": "en"}, http))
    assert r.kind == "synced" and r.source == "ytcaptions" and [l.text for l in r.lines] == ["line one", "line two"]
    assert http.calls[0][0] == "https://yt/sub?fmt=json3"
    auto = {"subtitles": {}, "automatic_captions": {"en": fmts("json3")}, "language": "en"}         # speech-to-text guesses: never used
    assert p.fetch(META, ctx(tmp_path, auto, Http(Resp(200, JSON3)))) is None
    assert p.fetch(META, ctx(tmp_path, None)) is None                                              # lyrics-upgrade pass has no yt-dlp info

def test_ytcaptions_failures_are_soft(tmp_path):
    info = {"subtitles": {"en": fmts("json3")}}
    assert YtCaptionsProvider().fetch(META, ctx(tmp_path, info, Http(Resp(429)))) is None
    one = {"events": [{"tStartMs": 1, "segs": [{"utf8": "only one line"}]}]}
    assert YtCaptionsProvider().fetch(META, ctx(tmp_path, info, Http(Resp(200, one)))) is None


# ------------------------------------------------------------------ syncedlyrics
def test_syncedlyrics_provider(tmp_path, monkeypatch):
    import syncedlyrics
    seen = {}
    def fake_search(term, **kw): seen.update(term=term, **kw); return "[00:01.00]<00:01.10>Hello <00:01.50>there\n[00:05.00]Second line"
    monkeypatch.setattr(syncedlyrics, "search", fake_search)
    r = SyncedLyricsProvider().fetch(META, ctx(tmp_path))
    assert seen == {"term": "Hello Adele", "synced_only": True, "providers": ["Musixmatch", "NetEase"]}
    assert r.kind == "synced" and [l.text for l in r.lines] == ["Hello there", "Second line"]        # word-level tags stripped
    monkeypatch.setattr(syncedlyrics, "search", lambda *a, **k: None)
    assert SyncedLyricsProvider().fetch(META, ctx(tmp_path)) is None
    assert SyncedLyricsProvider().fetch(TrackMeta("v", "T", []), ctx(tmp_path)) is None              # no artist => no blind search

def test_syncedlyrics_missing_package_is_silent(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "syncedlyrics", None)                                          # makes `import syncedlyrics` raise ImportError
    assert SyncedLyricsProvider().fetch(META, ctx(tmp_path)) is None


# ------------------------------------------------------------------ coverage guard + registry
def test_covers():
    lines = [LyricLine(1000, "a"), LyricLine(100_000, "b")]
    assert covers(lines, 200) and not covers(lines, 300) and covers(lines, None) and not covers([], 100)

def test_default_providers_are_all_registered_and_ordered_by_quality():
    assert list(PROVIDERS) == ["ytmusic", "lrclib", "syncedlyrics", "ytcaptions"]
    assert Settings().lyrics_providers == list(PROVIDERS)
    assert PROVIDERS["syncedlyrics"].fuzzy and not getattr(PROVIDERS["lrclib"], "fuzzy", False)

class Stub:
    def __init__(self, name, result, fuzzy=False): self.name, self.result, self.fuzzy = name, result, fuzzy
    def fetch(self, meta, ctx): return self.result

def S(name, pairs):
    ls = [LyricLine(t, x) for t, x in pairs]; return LyricsResult("synced", name, ls, to_plain(ls))

SHORT = [(1000, "a line"), (5000, "another line"), (20_000, "third line")]          # ends at 20s of a 200s track
FULL = [(1000, "a line"), (60_000, "another line"), (150_000, "third line")]

def run(stubs, meta=META): return fetch_lyrics(meta, None, [s.name for s in stubs], {s.name: s for s in stubs})

def test_fuzzy_provider_needs_coverage_when_nothing_can_cross_check_it():
    assert run([Stub("syncedlyrics", S("syncedlyrics", SHORT), fuzzy=True)]) is None
    assert run([Stub("syncedlyrics", S("syncedlyrics", FULL), fuzzy=True)]).source == "syncedlyrics"
    assert run([Stub("lrclib", S("lrclib", SHORT))]).source == "lrclib"                            # exact-match providers are not held to it

def test_fuzzy_provider_accepted_when_youtube_text_corroborates():
    yt = LyricsResult("plain", "ytmusic", [], "a line\nanother line\nthird line")
    r = run([Stub("ytmusic", yt), Stub("syncedlyrics", S("syncedlyrics", SHORT), fuzzy=True)])
    assert r.source == "syncedlyrics"                                                              # reference exists => similarity check instead
    other = LyricsResult("plain", "ytmusic", [], "totally unrelated words about something else entirely")
    assert run([Stub("ytmusic", other), Stub("syncedlyrics", S("syncedlyrics", SHORT), fuzzy=True)]).source == "ytmusic"

def test_provider_order_lrclib_before_syncedlyrics_before_captions():
    order = []
    class Rec(Stub):
        def fetch(self, meta, ctx): order.append(self.name); return None
    run([Rec("ytcaptions", None), Rec("syncedlyrics", None), Rec("lrclib", None), Rec("ytmusic", None)])
    assert order == ["ytmusic", "ytcaptions", "syncedlyrics", "lrclib"]                            # only ytmusic is hoisted; the rest keep CONFIG order
