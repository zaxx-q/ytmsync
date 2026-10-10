import itertools
import json
import threading

import pytest
from mediafile import MediaFile

from ytmsync import musicbrainz as mbmod
from ytmsync.httpcache import HttpCache
from ytmsync.models import TrackMeta
from ytmsync.musicbrainz import (MusicBrainz, build_query, core_title, first_isrc, lucene_quote, pick_medium, pick_recording, pick_release)


# ------------------------------------------------------------------ payload builders (shape = MusicBrainz JSON search results)
def ac(name, aid): return {"name": name, "joinphrase": " & ", "artist": {"id": aid, "name": name, "sort-name": name}}

def medium(pos=1, count=12, offset=1, title="Foolish Of Me"):
    return {"position": pos, "format": "Digital Media", "track-count": count, "track-offset": offset,
            "track": [{"id": "t", "number": str(offset + 1), "title": title, "length": 187000}]}

def release(id="rel1", title="Foolish Of Me (Deluxe)", date="2021-03-05", status="Official", rg="rg1", media=None, artists=None, **extra):
    return {"id": id, "title": title, "date": date, "status": status, "release-group": {"id": rg},
            "artist-credit": artists if artists is not None else [ac("Seven Lions", "a1")], "media": media or [medium()], **extra}

def rec(id="rec1", title="Foolish Of Me", score=100, length=187000, artists=None, releases=None, isrcs=None):
    d = {"id": id, "title": title, "score": score, "length": length,
         "artist-credit": artists or [ac("Seven Lions", "a1"), ac("Jason Ross", "a2")],
         "releases": releases if releases is not None else [release()]}
    if isrcs is not None: d["isrcs"] = isrcs
    return d

def meta(**kw):
    base = dict(video_id="v", title="Foolish Of Me (feat. Jason Ross)", artists=["Seven Lions", "Jason Ross"], album="Foolish Of Me (Deluxe)",
                album_artists=["Seven Lions"], track_no=2, track_total=12, year=2021, duration_s=187)
    return TrackMeta(**{**base, **kw})


# ------------------------------------------------------------------ fake transport
class Resp:
    def __init__(self, status, data=None): self.status_code, self._d = status, data; self.text = json.dumps(data) if data is not None else ""
    def json(self): return self._d

class Http:
    def __init__(self, *responses): self.q, self.calls = list(responses), []
    def get(self, url, **kw):
        self.calls.append((url, kw))
        r = self.q.pop(0) if len(self.q) > 1 else self.q[0]
        if isinstance(r, Exception): raise r
        return r

class Clock:
    def __init__(self): self.t, self.sleeps, self._l = 1000.0, [], threading.Lock()
    def now(self): return self.t
    def sleep(self, s): self.sleeps.append(s); self.t += s

_n = itertools.count()

def make(tmp_path, *responses, contact="me@example.com"):
    """Every call gets its OWN cache file, otherwise scenarios answer each other's queries from the cache."""
    clk, http = Clock(), Http(*responses)
    mb = MusicBrainz(contact, HttpCache(tmp_path / f"c{next(_n)}.sqlite"), http_factory=lambda: http, clock=clk.now, sleep=clk.sleep)
    return mb, http, clk

def ok(*recs): return Resp(200, {"recordings": list(recs)})


# ------------------------------------------------------------------ pure helpers
def test_query_building_and_escaping():
    m = meta(title='Say "Hi" \\ (feat. X)', album='The "Best" Album')
    q = build_query(m, with_release=True)
    assert q == 'recording:"Say \\"Hi\\" \\\\" AND artist:"Seven Lions" AND release:"The \\"Best\\" Album"'
    assert "release:" not in build_query(m, with_release=False)
    assert "release:" not in build_query(meta(is_single=True), with_release=True)                  # synthetic "<title> - Single" album is useless
    assert lucene_quote('a"b') == '"a\\"b"' and core_title("Song (feat. X & Y)") == "Song" and core_title("Song ft. X") == "Song"

def test_pick_recording_is_strict():
    m = meta()
    assert pick_recording([rec()], m)["id"] == "rec1"                                              # 'feat.' in YT title ignored
    assert pick_recording([rec(score=70)], m) is None                                              # weak search score
    assert pick_recording([rec(title="Completely Different")], m) is None
    assert pick_recording([rec(artists=[ac("Someone Else", "x")])], m) is None
    assert pick_recording([rec(length=260000)], m) is None                                         # live/remix: duration differs >4s
    assert pick_recording([rec(length=None)], m) is not None and pick_recording([rec()], meta(duration_s=None)) is not None
    assert pick_recording([rec("far", length=190000), rec("near", length=187500)], m)["id"] == "near"       # same score: closest length wins
    assert pick_recording([rec("low", score=90, length=187000), rec("far", length=190000)], m)["id"] == "far"   # score dominates length

def test_pick_release_prefers_official_then_earliest_and_requires_album_match():
    r = rec(releases=[release("bootleg", status="Bootleg", date="2019"), release("late", date="2022-01-01"),
                      release("early", date="2021-03-05"), release("other", title="Some Other Album")])
    assert pick_release(r, meta())["id"] == "early"
    assert pick_release(rec(releases=[release(title="Unrelated Album")]), meta()) is None
    single = rec(releases=[release("s", title="Foolish Of Me (feat. Jason Ross)"), release("a", title="Big Album")])
    assert pick_release(single, meta(is_single=True, album="Foolish Of Me (feat. Jason Ross) - Single"))["id"] == "s"

def test_pick_medium_and_isrc_variants():
    two = release(media=[medium(1, title="Other"), medium(2, title="Foolish Of Me")])
    assert pick_medium(two, rec())["position"] == 2 and pick_medium(release(media=[medium(1, title="X")]), rec())["position"] == 1
    assert pick_medium(release(media=[medium(1, title="A"), medium(2, title="B")]), rec()) is None
    assert first_isrc({"isrcs": ["USX1"]}) == "USX1" and first_isrc({"isrcs": [{"id": "USX2"}]}) == "USX2" and first_isrc({}) is None


# ------------------------------------------------------------------ enrichment
def test_enrich_adds_ids_but_never_changes_visible_text(tmp_path):
    mb, http, _ = make(tmp_path, ok(rec(isrcs=["GBAAA2100001"])))
    m = meta(year=None, date=None)
    before = (m.title, m.artists, m.album, m.album_artists)
    mb.enrich(m)
    assert (m.title, m.artists, m.album, m.album_artists) == before
    assert (m.mb_recording_id, m.mb_release_id, m.mb_release_group_id, m.isrc) == ("rec1", "rel1", "rg1", "GBAAA2100001")
    assert m.mb_artist_ids == ["a1", "a2"] and m.mb_album_artist_ids == ["a1"]
    assert (m.year, m.date) == (2021, "2021-03-05")
    url, kw = http.calls[0]
    assert url.endswith("/ws/2/recording") and kw["params"]["fmt"] == "json" and kw["params"]["limit"] == 10
    assert kw["headers"]["User-Agent"].startswith("ytmsync/") and "me@example.com" in kw["headers"]["User-Agent"]

def test_date_and_year_rules(tmp_path):
    def run(date, **kw):
        mb, *_ = make(tmp_path, ok(rec(releases=[release(date=date)]))); m = meta(date=None, **kw); mb.enrich(m); return m
    assert (run("2021-03-05").date) == "2021-03-05"
    assert run("2019-03-05").date is None and run("2019-03-05").year == 2021          # re-issue date disagrees with album year: ignored
    m = run("2021", year=None); assert (m.year, m.date) == (2021, None)               # partial date: year only
    assert run("2021-03-05", year=None).date == "2021-03-05"

def test_no_release_match_still_gives_recording_and_artist_ids(tmp_path):
    mb, *_ = make(tmp_path, ok(rec(releases=[release(title="Wrong Album")])))
    m = mb.enrich(meta())
    assert m.mb_recording_id == "rec1" and m.mb_artist_ids == ["a1", "a2"] and m.mb_release_id is None and m.disc_no is None

def test_multidisc_gets_disc_numbers_and_per_disc_track_numbers(tmp_path):
    rel = release(media=[medium(pos=2, count=10, offset=2)], **{"medium-count": 2})
    mb, *_ = make(tmp_path, ok(rec(releases=[rel])))
    m = mb.enrich(meta(track_no=15, track_total=22))                                   # YT: global position 15 of 22
    assert (m.disc_no, m.disc_total, m.track_no, m.track_total) == (2, 2, 3, 10)

def test_single_disc_rules(tmp_path):
    mb, *_ = make(tmp_path, ok(rec(releases=[release(media=[medium(1, 12, 1)], **{"medium-count": 1})])))
    m = mb.enrich(meta()); assert (m.disc_no, m.disc_total, m.track_no) == (1, 1, 2)   # track number untouched
    mb, *_ = make(tmp_path, ok(rec(releases=[release(media=[medium(1, 12, 1)])])))       # count unknown, position 1 => claim nothing
    m = mb.enrich(meta()); assert (m.disc_no, m.disc_total) == (None, None)

def test_second_query_without_release_when_first_finds_nothing(tmp_path):
    mb, http, _ = make(tmp_path, ok(), ok(rec()))
    m = mb.enrich(meta())
    assert m.mb_recording_id == "rec1" and len(http.calls) == 2
    assert "release:" in http.calls[0][1]["params"]["query"] and "release:" not in http.calls[1][1]["params"]["query"]
    mb, http, _ = make(tmp_path, ok())
    mb.enrich(meta(video_id="s", is_single=True, title="Unique One", album="Unique One - Single"))
    assert len(http.calls) == 1                                                                 # singles have no album to relax => one query

def test_failures_leave_metadata_untouched(tmp_path):
    for resp in (Resp(400), Resp(404), RuntimeError("offline"), ok()):
        mb, *_ = make(tmp_path, resp)
        m = meta(); snap = m.to_json(); mb.enrich(m)
        assert m.to_json() == snap
    mb, *_ = make(tmp_path, ok(rec()))
    assert mb.enrich(TrackMeta("v", "T", [])).mb_recording_id is None                         # no artist => nothing to search for


# ------------------------------------------------------------------ throttling / caching / retries
def test_requests_are_spaced_and_cache_hits_are_free(tmp_path):
    mb, http, clk = make(tmp_path, ok(rec()))
    mb.enrich(meta(video_id="1", title="Foolish Of Me A")); mb.enrich(meta(video_id="2", title="Foolish Of Me B")); mb.enrich(meta(video_id="3", title="Foolish Of Me C"))
    assert len(http.calls) == 3 and len(clk.sleeps) == 2 and all(s >= mbmod.MIN_INTERVAL_S - 1e-9 for s in clk.sleeps)   # first call never waits
    n = len(http.calls); mb.enrich(meta(video_id="1", title="Foolish Of Me A"))
    assert len(http.calls) == n and len(clk.sleeps) == 2                                        # cached: no request, no waiting

def test_negative_results_expire_sooner_than_positive(tmp_path, monkeypatch):
    real = mbmod.time.time
    mb, http, _ = make(tmp_path, ok())                                                        # nothing ever found (2 queries per enrich)
    mb.enrich(meta()); assert len(http.calls) == 2
    mb.enrich(meta()); assert len(http.calls) == 2                                            # within a day: served from cache
    monkeypatch.setattr(mbmod.time, "time", lambda: real() + 2 * 86400)
    mb.enrich(meta()); assert len(http.calls) == 4                                            # >1 day: asked again
    monkeypatch.undo()
    mb2, http2, _ = make(tmp_path, ok(rec()))
    mb2.enrich(meta()); n = len(http2.calls)
    monkeypatch.setattr(mbmod.time, "time", lambda: real() + 10 * 86400)
    mb2.enrich(meta()); assert len(http2.calls) == n                                          # positive hits live 30 days

def test_retry_on_busy_then_success_and_giving_up(tmp_path):
    mb, http, clk = make(tmp_path, Resp(503), Resp(429), ok(rec()))
    assert mb.enrich(meta()).mb_recording_id == "rec1" and len(http.calls) == 3
    assert 2 in clk.sleeps and 4 in clk.sleeps                                                # back-off 2 s then 4 s
    mb, http, _ = make(tmp_path, Resp(503))
    m = mb.enrich(meta(album=None))
    assert m.mb_recording_id is None and len(http.calls) == 3                                 # three attempts, then give up
    assert mb.cache.conn.execute("SELECT COUNT(*) FROM http_cache").fetchone()[0] == 0        # failures are never cached

def test_global_throttle_holds_across_threads(tmp_path):
    mb, http, clk = make(tmp_path, ok(rec()))
    stamps = []
    orig = http.get
    def get(url, **kw): stamps.append(clk.now()); return orig(url, **kw)
    http.get = get
    ts = [threading.Thread(target=mb.enrich, args=(meta(video_id=str(i), title=f"Foolish Of Me {chr(65+i)}"),)) for i in range(6)]
    [t.start() for t in ts]; [t.join() for t in ts]
    gaps = [b - a for a, b in zip(sorted(stamps), sorted(stamps)[1:])]
    assert len(stamps) == 6 and all(g >= mbmod.MIN_INTERVAL_S - 1e-9 for g in gaps)


# ------------------------------------------------------------------ tags
@pytest.mark.parametrize("ext", ["opus", "m4a", "mp3"])
def test_musicbrainz_tags_roundtrip(tmp_path, ext):
    import subprocess
    from ytmsync.tagging import write_tags
    p = tmp_path / f"a.{ext}"
    codec = {"opus": ["-c:a", "libopus"], "m4a": ["-c:a", "aac"], "mp3": ["-c:a", "libmp3lame"]}[ext]
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1", *codec, str(p)], check=True)
    m = meta(mb_recording_id="rec1", mb_release_id="rel1", mb_release_group_id="rg1", mb_artist_ids=["a1", "a2"],
             mb_album_artist_ids=["a1"], isrc="GBAAA2100001", disc_no=2, disc_total=2)
    write_tags(p, m, None, None)
    r = MediaFile(str(p))
    assert (r.mb_trackid, r.mb_albumid, r.mb_releasegroupid, r.isrc) == ("rec1", "rel1", "rg1", "GBAAA2100001")
    assert r.mb_artistids == ["a1", "a2"] and r.mb_albumartistids == ["a1"] and (r.disc, r.disctotal) == (2, 2)
    assert r.mb_artistid == "a1" and r.mb_albumartistid == "a1"                                # single accessors read the first value
