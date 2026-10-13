import json
from pathlib import Path
from typing import ClassVar

import pytest

from ytmsync import ytdl
from ytmsync.config import Settings, load_settings
from ytmsync.sources import Source
from ytmsync.state import Db


# ------------------------------------------------------------------ config
def test_defaults_and_derived_paths(tmp_path):
    s = Settings(library_dir=tmp_path)
    assert (
        s.format == "opus"
        and s.path_template == "{albumartist}/{album}/{track:02d} {title}"
    )
    assert s.tmp_dir == tmp_path / ".ytmsync" / "tmp"


def test_precedence_cli_over_file_over_default(tmp_path, monkeypatch):
    f = tmp_path / "c.toml"
    f.write_text('[defaults]\nformat="m4a"\nthreads=7\nlyrics=false\n')
    monkeypatch.setenv("YTMSYNC_CONFIG", str(f))
    s = load_settings(
        {"format": None, "threads": 2, "lyrics": None, "library_dir": str(tmp_path)}
    )
    assert (s.format, s.threads, s.lyrics) == (
        "m4a",
        2,
        False,
    )  # None = flag not passed


def test_validation(tmp_path):
    for bad in (
        {"format": "flac"},
        {"layout": "nope"},
        {"threads": 0},
        {"template": "{album}/{track}"},
        {"template": "{bogus}/{title}"},
    ):
        with pytest.raises(ValueError):
            Settings(library_dir=tmp_path, **bad)
    with pytest.raises(ValueError):
        load_settings({"not_a_setting": 1})


def test_playlist_preset_and_tilde():
    s = Settings(library_dir="~/x", cookies="~/c.txt", layout="playlist")
    assert (
        "~" not in str(s.library_dir)
        and "~" not in str(s.cookies)
        and "{playlist}" in s.path_template
    )


# ------------------------------------------------------------------ state
@pytest.fixture
def db(tmp_path):
    return Db(tmp_path / ".ytmsync" / "state.db")


def test_memberships_orphans_and_scopes(db):
    db.upsert_playlist("P1", "One", "u1")
    db.upsert_playlist("P2", "Two", "u2")
    db.replace_memberships(
        "P1", [(1, "a"), (2, "b"), (3, "a")]
    )  # duplicate video at two positions
    db.replace_memberships("P2", [(1, "b")])
    assert db.members_all() == {"P1": [(1, "a"), (2, "b"), (3, "a")], "P2": [(1, "b")]}
    db.upsert_file("a", "", "A/a.opus", "opus", 10, False)
    db.upsert_file("b", "", "B/b.opus", "opus", 10, False)
    assert set(db.files_all()) == {("a", ""), ("b", "")}
    assert db.path_owner("A/a.opus") == ("a", "") and db.path_owner("nope") is None
    assert sorted((r["playlist_id"], r["position"]) for r in db.playlists_of("a")) == [
        ("P1", 1)
    ]  # MIN(position)


def test_upsert_file_replaces_stale_row_owning_same_path(db):
    db.upsert_file("a", "", "X/1.opus", "opus", 1, False)
    db.upsert_file(
        "b", "", "X/1.opus", "opus", 1, False
    )  # b takes the path (a's row was stale)
    assert set(db.files_all()) == {("b", "")}


def test_unsubscribe_pins_only_that_playlists_files(db):
    db.upsert_playlist("P1", "One", "u")
    db.upsert_playlist("P2", "Two", "u")
    db.replace_memberships("P1", [(1, "a")])
    db.replace_memberships("P2", [(1, "b")])
    db.upsert_file("a", "", "a.opus", "opus", 1, False)
    db.upsert_file("b", "", "b.opus", "opus", 1, False)
    db.unsubscribe("P1", pin_files=True)
    files = db.files_all()
    assert files[("a", "")].pinned and not files[("b", "")].pinned
    assert [r["playlist_id"] for r in db.playlists()] == ["P2"]
    assert "P1" not in db.members_all()  # unsubscribed => ignored


def test_scoped_unsubscribe(db):
    db.upsert_playlist("P1", "One", "u")
    db.upsert_playlist("P2", "Two", "u")
    db.replace_memberships("P1", [(1, "a")])
    db.replace_memberships("P2", [(1, "a")])
    db.upsert_file("a", "P1", "One/a.opus", "opus", 1, False)
    db.upsert_file("a", "P2", "Two/a.opus", "opus", 1, False)
    db.unsubscribe("P1", pin_files=True)
    f = db.files_all()
    assert f[("a", "P1")].pinned and not f[("a", "P2")].pinned


def test_resubscribe_via_upsert_and_oneshot_never_downgrades(db):
    db.upsert_playlist("P1", "One", "u")
    db.upsert_playlist("P1", "One (renamed)", "u", subscribed=False)
    assert db.playlists()[0]["title"] == "One (renamed)"  # still subscribed
    db.unsubscribe("P1", pin_files=False)
    assert db.playlists() == []
    db.upsert_playlist("P1", "One", "u")
    assert len(db.playlists()) == 1  # resubscribe


def test_track_lifecycle_and_lyrics_retry(db):
    db.set_track_ok("a", {"title": "T"}, "plain", "ytmusic")
    db.set_track_ok("b", {"title": "T"}, "synced", "lrclib")
    db.set_track_failed("c", "failed", "boom")
    db.set_track_failed("c", "failed", "boom again")
    assert db.get_track("c")["attempts"] == 2 and db.track_status_all() == {
        "a": "ok",
        "b": "ok",
        "c": "failed",
    }
    assert json.loads(db.get_track("a")["meta_json"]) == {"title": "T"}
    assert db.tracks_needing_lyrics(14, False) == []  # just checked
    db.conn.execute(
        "UPDATE tracks SET lyrics_checked_at = lyrics_checked_at - 15*86400"
    )
    db.conn.commit()
    assert [r["video_id"] for r in db.tracks_needing_lyrics(14, False)] == [
        "a"
    ]  # plain is retried, synced is final
    assert (
        len(db.tracks_needing_lyrics(14, True)) == 2
    )  # --refresh-lyrics: all ok tracks
    db.set_lyrics_state("a", "synced", "lrclib")
    assert db.tracks_needing_lyrics(14, False) == []


def test_delete_file_row_forgets_track_when_last_file_goes(db):
    db.set_track_ok("a", {}, None, None)
    db.upsert_file("a", "P1", "1/a.opus", "opus", 1, False)
    db.upsert_file("a", "P2", "2/a.opus", "opus", 1, False)
    db.delete_file_row("a", "P1")
    assert db.get_track("a") is not None
    db.delete_file_row("a", "P2")
    assert db.get_track("a") is None


def test_state_survives_reopen_and_threads(tmp_path):
    import threading

    p = tmp_path / "s.db"
    d = Db(p)
    d.upsert_file("a", "", "a.opus", "opus", 1, False)
    seen = {}
    t = threading.Thread(target=lambda: seen.update(Db(p).files_all()))
    t.start()
    t.join()
    assert ("a", "") in seen


# ------------------------------------------------------------------ ytdl
@pytest.mark.parametrize(
    "msg,kind",
    [
        (
            "ERROR: [youtube] x: Video unavailable. This video is not available",
            "unavailable",
        ),
        ("ERROR: Private video. Sign in if you've been granted access", "unavailable"),
        ("Sign in to confirm your age. This video may be inappropriate", "auth"),
        ("HTTP Error 429: Too Many Requests", "rate_limit"),
        ("Unable to download webpage: <urlopen error timed out>", "transient"),
        ("something weird", "other"),
    ],
)
def test_classify_error(msg, kind):
    assert ytdl.classify_error(msg) == kind


def test_detect_js_runtimes_shape():
    rt = ytdl.detect_js_runtimes()
    assert (
        isinstance(rt, dict)
        and set(rt) <= {"deno", "node", "bun", "quickjs"}
        and all(v == {} for v in rt.values())
    )


def test_base_opts(tmp_path):
    s = Settings(
        library_dir=tmp_path,
        cookies=tmp_path / "c.txt",
        cookies_from_browser="chrome:Profile 1",
        sleep_interval=2,
        js_runtime="node=/opt/node",
    )
    o = ytdl.base_opts(s)
    assert o["cookiefile"].endswith("c.txt") and o["cookiesfrombrowser"] == (
        "chrome",
        "Profile 1",
    )
    assert (o["sleep_interval"], o["max_sleep_interval"]) == (2, 4) and o[
        "js_runtimes"
    ] == {"node": {"path": "/opt/node"}}


class FakeYDL:
    """Stands in for yt_dlp.YoutubeDL; behaviour is scripted per test through class attributes."""

    script: ClassVar[list] = []
    last_opts: ClassVar[dict] = {}

    def __init__(self, opts):
        FakeYDL.last_opts = opts
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=True):
        step = FakeYDL.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(self, url) if callable(step) else step


def test_enumerate_playlist_parses_flat_entries(monkeypatch, tmp_path):
    monkeypatch.setattr(ytdl, "YoutubeDL", FakeYDL)
    FakeYDL.script = [
        {
            "title": "Road Trip",
            "entries": [
                {"id": "aaaaaaaaaaa", "title": "Song A"},
                None,
                {"id": "bbbbbbbbbbb", "title": "[Private video]"},
                {"id": "ccccccccccc", "title": "[Deleted video]"},
                {"title": "no id"},
            ],
        }
    ]
    info = ytdl.enumerate_playlist(
        Settings(library_dir=tmp_path), Source("playlist", "PL123456789A")
    )
    assert (
        info.title == "Road Trip" and FakeYDL.last_opts["extract_flat"] == "in_playlist"
    )
    assert [(e.video_id, e.position, e.available) for e in info.entries] == [
        ("aaaaaaaaaaa", 1, True),
        ("bbbbbbbbbbb", 3, False),
        ("ccccccccccc", 4, False),
    ]  # positions keep gaps: index in source list


def test_enumerate_error_mentions_cookies(monkeypatch, tmp_path):
    monkeypatch.setattr(ytdl, "YoutubeDL", FakeYDL)
    FakeYDL.script = [ytdl.DownloadError("The playlist does not exist")]
    with pytest.raises(ytdl.EnumerationError, match="cookies"):
        ytdl.enumerate_playlist(
            Settings(library_dir=tmp_path), Source("playlist", "PLx")
        )


def _ok_download(fmt):
    def step(ydl, url):
        out = Path(ydl.opts["outtmpl"]).parent / f"vid12345678.{fmt}"
        out.write_bytes(b"x")
        return {"id": "vid12345678", "requested_downloads": [{"filepath": str(out)}]}

    return step


def test_download_success_and_postprocessor_config(monkeypatch, tmp_path):
    monkeypatch.setattr(ytdl, "YoutubeDL", FakeYDL)
    for fmt, has_q in (("opus", False), ("mp3", True)):
        FakeYDL.script = [_ok_download(fmt)]
        path, _info = ytdl.download_audio(
            Settings(library_dir=tmp_path, format=fmt), "vid12345678", tmp_path
        )
        pp = FakeYDL.last_opts["postprocessors"][0]
        assert (
            path.suffix == f".{fmt}"
            and pp["preferredcodec"] == fmt
            and ("preferredquality" in pp) == has_q
        )


def test_download_retries_transient_then_succeeds(monkeypatch, tmp_path):
    monkeypatch.setattr(ytdl, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(ytdl.time, "sleep", lambda s: None)
    FakeYDL.script = [
        ytdl.DownloadError("connection reset"),
        ytdl.DownloadError("HTTP Error 429"),
        _ok_download("opus"),
    ]
    path, _ = ytdl.download_audio(
        Settings(library_dir=tmp_path), "vid12345678", tmp_path
    )
    assert path.exists() and FakeYDL.script == []


@pytest.mark.parametrize(
    "msg,kind",
    [("Video unavailable", "unavailable"), ("Sign in to confirm your age", "auth")],
)
def test_download_does_not_retry_permanent_errors(monkeypatch, tmp_path, msg, kind):
    monkeypatch.setattr(ytdl, "YoutubeDL", FakeYDL)
    FakeYDL.script = [ytdl.DownloadError(msg), _ok_download("opus")]
    with pytest.raises(ytdl.DownloadFailure) as e:
        ytdl.download_audio(Settings(library_dir=tmp_path), "vid12345678", tmp_path)
    assert (
        e.value.kind == kind and len(FakeYDL.script) == 1
    )  # second scripted step never consumed


def test_download_gives_up_after_three_transient_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(ytdl, "YoutubeDL", FakeYDL)
    monkeypatch.setattr(ytdl.time, "sleep", lambda s: None)
    FakeYDL.script = [ytdl.DownloadError("timed out")] * 3
    with pytest.raises(ytdl.DownloadFailure) as e:
        ytdl.download_audio(Settings(library_dir=tmp_path), "vid12345678", tmp_path)
    assert e.value.kind == "transient"


def test_download_rejects_wrong_extension(monkeypatch, tmp_path):
    monkeypatch.setattr(ytdl, "YoutubeDL", FakeYDL)
    FakeYDL.script = [_ok_download("webm")]  # post-processing silently didn't happen
    with pytest.raises(ytdl.DownloadFailure):
        ytdl.download_audio(
            Settings(library_dir=tmp_path, format="opus"), "vid12345678", tmp_path
        )


def test_musicbrainz_requires_contact(tmp_path):
    with pytest.raises(ValueError, match="contact"):
        Settings(library_dir=tmp_path, musicbrainz=True)
    assert Settings(
        library_dir=tmp_path, musicbrainz=True, contact="me@example.com"
    ).musicbrainz
    assert Settings(library_dir=tmp_path).musicbrainz is False  # off by default
    with pytest.raises(ValueError, match="contact"):
        load_settings({"library_dir": str(tmp_path), "musicbrainz": True})


def test_contact_from_config_file(tmp_path, monkeypatch):
    f = tmp_path / "c.toml"
    f.write_text('[defaults]\nmusicbrainz = true\ncontact = "me@example.com"\n')
    monkeypatch.setenv("YTMSYNC_CONFIG", str(f))
    s = load_settings({"library_dir": str(tmp_path)})
    assert s.musicbrainz and s.contact == "me@example.com"
    assert (
        load_settings({"library_dir": str(tmp_path), "musicbrainz": False}).musicbrainz
        is False
    )  # CLI wins over file
