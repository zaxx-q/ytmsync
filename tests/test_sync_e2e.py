import json

import pytest
from conftest import plain, synced
from mediafile import MediaFile

from ytmsync.sources import Source
from ytmsync.syncer import SyncOptions
from ytmsync.tagging import read_video_id
from ytmsync.ytdl import DownloadFailure, EnumerationError


def seed(env, *vids, album="Album"):
    for i, v in enumerate(vids, 1):
        env.add(v, album=album, n=i)


def test_first_sync_creates_library_tags_sidecar_and_m3u(env):
    seed(env, "a", "b")
    env.playlists["P1"] = ["a", "b"]
    env.titles["P1"] = "Road Trip"
    env.lyrics["a"] = synced("hello", "world")
    env.lyrics["b"] = plain("just text")
    rep = env.sync("P1")
    assert rep.ok and rep.added == 2
    assert env.rel("") == [
        "Artist/Album/01 Song a.lrc",
        "Artist/Album/01 Song a.opus",
        "Artist/Album/02 Song b.opus",
        "_Playlists/Road Trip.m3u8",
    ]
    a = MediaFile(str(env.s.library_dir / "Artist/Album/01 Song a.opus"))
    assert (
        a.title == "Song a"
        and a.album == "Album"
        and a.track == 1
        and len(a.images) == 1
        and a.lyrics.startswith("[00:01.00]hello")
    )
    assert read_video_id(env.s.library_dir / "Artist/Album/02 Song b.opus") == "b"
    assert (
        MediaFile(str(env.s.library_dir / "Artist/Album/02 Song b.opus")).lyrics
        == "just text"
    )
    assert "[00:01.00]hello" in (
        env.s.library_dir / "Artist/Album/01 Song a.lrc"
    ).read_text("utf-8")
    m3u = (
        (env.s.library_dir / "_Playlists/Road Trip.m3u8")
        .read_text("utf-8")
        .splitlines()
    )
    assert (
        m3u[0] == "#EXTM3U"
        and m3u[2] == "../Artist/Album/01 Song a.opus"
        and m3u[1] == "#EXTINF:200,Artist - Song a"
    )
    assert (
        env.db.get_track("a")["lyrics_kind"] == "synced"
        and env.db.get_track("b")["lyrics_kind"] == "plain"
    )
    assert not (env.s.tmp_dir).exists()  # temp dir cleaned up


def test_rerun_is_a_noop(env):
    seed(env, "a", "b")
    env.playlists["P1"] = ["a", "b"]
    env.sync("P1")
    env.download_calls.clear()
    env.enrich_calls.clear()
    rep = env.sync("P1")
    assert (
        (rep.added, rep.removed, rep.moved, rep.copied) == (0, 0, 0, 0)
        and env.download_calls == []
        and env.enrich_calls == []
    )


def test_playlist_changes_add_and_remove_and_prune_dirs(env):
    env.add("a", album="Solo A", n=1)
    env.add("b", album="Album", n=1)
    env.add("c", album="Album", n=2)
    env.lyrics["a"] = synced("x", "y")
    env.playlists["P1"] = ["a", "b"]
    env.sync("P1")
    assert "Artist/Solo A/01 Song a.opus" in env.files()
    env.playlists["P1"] = ["b", "c"]
    rep = env.sync("P1")
    assert (rep.added, rep.removed) == (1, 1)
    assert env.files() == ["Artist/Album/01 Song b.opus", "Artist/Album/02 Song c.opus"]
    assert not (
        env.s.library_dir / "Artist/Solo A"
    ).exists()  # empty album dir pruned, sidecar removed too
    assert env.db.get_track("a") is None


def test_song_shared_by_two_playlists_survives_until_last_reference_goes(env):
    seed(env, "a", "b")
    env.playlists.update(P1=["a", "b"], P2=["b"])
    env.sync("P1", "P2")
    assert len(env.files()) == 2 and env.download_calls.count("b") == 1
    env.playlists["P1"] = ["a"]
    env.sync("P1")
    assert len(env.files()) == 2  # P2 still wants b
    env.playlists["P2"] = []
    env.sync("P2", allow_empty=True)
    assert env.files() == ["Artist/Album/01 Song a.opus"]


def test_template_change_moves_files_without_redownload(make_env):
    e = make_env()
    seed(e, "a")
    e.playlists["P1"] = ["a"]
    e.lyrics["a"] = synced("x", "y")
    e.sync("P1")
    e.download_calls.clear()
    e.new_syncer(template="{year}/{artist} - {title}")
    rep = e.sync("P1")
    assert rep.moved == 1 and e.download_calls == []
    assert e.rel("") == [
        "2020/Artist - Song a.lrc",
        "2020/Artist - Song a.opus",
        "_Playlists/P1.m3u8",
    ]
    assert (
        e.db.path_owner("2020/Artist - Song a.opus") == ("a", "")
        and not (e.s.library_dir / "Artist").exists()
    )


def test_missing_file_is_redownloaded(env):
    seed(env, "a")
    env.playlists["P1"] = ["a"]
    env.sync("P1")
    (env.s.library_dir / "Artist/Album/01 Song a.opus").unlink()
    env.download_calls.clear()
    assert (
        env.sync("P1").added == 1
        and env.download_calls == ["a"]
        and len(env.files()) == 1
    )


def test_dry_run_changes_nothing(env):
    seed(env, "a", "b")
    env.playlists["P1"] = ["a"]
    env.sync("P1")
    before, dbfiles = env.rel(""), env.db.files_all()
    env.playlists["P1"] = ["b"]
    rep = env.sync("P1", dry_run=True)
    assert rep.plan.download == ["b"] and [d[0] for d in rep.plan.delete] == ["a"]
    assert (
        env.rel("") == before
        and env.db.files_all() == dbfiles
        and env.download_calls == ["a"]
    )
    assert env.db.members_all()["P1"] == [(1, "a")]  # memberships untouched


def test_safety_rails_protect_library_from_bad_enumeration(env):
    ids = [f"v{i:02d}" for i in range(20)]
    seed(env, *ids)
    env.playlists["P1"] = ids
    env.sync("P1")
    env.playlists["P1"] = []
    rep = env.sync("P1")
    assert "P1" in rep.playlist_errors and not rep.ok and len(env.files()) == 20
    env.playlists["P1"] = ids[:5]
    rep = env.sync("P1")
    assert "would be removed" in rep.playlist_errors["P1"] and len(env.files()) == 20
    env.playlists["P1"] = EnumerationError("login required")
    rep = env.sync("P1")
    assert "login" in rep.playlist_errors["P1"] and len(env.files()) == 20
    env.playlists["P1"] = ids[:5]
    rep = env.sync("P1", yes=True)
    assert rep.removed == 15 and len(env.files()) == 5


def test_no_delete_flag(env):
    seed(env, "a", "b")
    env.playlists["P1"] = ["a", "b"]
    env.sync("P1")
    env.playlists["P1"] = ["a"]
    rep = env.sync("P1", delete=False)
    assert rep.removed == 0 and len(env.files()) == 2


def test_playlist_layout_is_scoped_and_copies_instead_of_redownloading(make_env):
    e = make_env(layout="playlist")
    seed(e, "a", "b")
    e.titles.update(P1="Mix One", P2="Mix Two")
    e.playlists.update(P1=["a", "b"], P2=["b"])
    e.sync("P1", "P2")
    assert e.files() == [
        "Mix One/001 Artist - Song a.opus",
        "Mix One/002 Artist - Song b.opus",
        "Mix Two/001 Artist - Song b.opus",
    ]
    assert e.download_calls.count("b") == 1  # second scope was a file copy
    e.playlists["P2"] = ["a", "b"]
    e.download_calls.clear()
    rep = e.sync("P2")
    assert (rep.copied, rep.moved, rep.added) == (
        1,
        1,
        0,
    ) and e.download_calls == []  # a copied in, b renamed 001 -> 002
    assert [f for f in e.files() if f.startswith("Mix Two")] == [
        "Mix Two/001 Artist - Song a.opus",
        "Mix Two/002 Artist - Song b.opus",
    ]
    e.playlists["P1"] = ["b", "a"]
    e.sync("P1")  # reorder => rename only
    assert "Mix One/001 Artist - Song b.opus" in e.files() and e.download_calls == []


def test_playlist_index_uses_playlist_id_when_metadata_has_redirected_id(make_env):
    e = make_env(layout="playlist")
    e.add("playlist-id")
    # YTM can redirect an unplayable playlist video to a different, playable ID.
    e.metas["playlist-id"].video_id = "playable-id"
    e.playlists["P1"] = ["playlist-id"]
    e.titles["P1"] = "Mix"

    e.sync("P1")

    assert e.files() == ["Mix/001 Artist - Song playlist-id.opus"]


def test_failures_are_isolated_recorded_and_retried(env):
    seed(env, "a", "b", "c")
    env.playlists["P1"] = ["a", "b", "c"]
    env.fail["b"] = DownloadFailure("transient", "boom")
    env.fail["c"] = DownloadFailure("unavailable", "Video unavailable")
    rep = env.sync("P1")
    assert (
        rep.added == 1
        and [v for v, _ in rep.failed] == ["b"]
        and rep.unavailable == ["c"]
        and not rep.ok
    )
    assert env.db.track_status_all() == {"a": "ok", "b": "failed", "c": "unavailable"}
    assert not (env.s.tmp_dir / "b").exists()
    del env.fail["b"]
    env.download_calls.clear()
    rep = env.sync("P1")
    assert (
        env.download_calls == ["b"] and rep.added == 1 and rep.unavailable == ["c"]
    )  # failed retried, unavailable not
    env.fail.pop("c")
    env.download_calls.clear()
    env.sync("P1", retry_unavailable=True)
    assert env.download_calls == ["c"]


def test_unavailable_entries_from_enumeration_are_skipped(env):
    from ytmsync.ytdl import PlaylistEntry, PlaylistInfo

    seed(env, "a")
    env.syncer.enumerate_fn = lambda s, src: PlaylistInfo(
        "P1",
        "P1",
        [
            PlaylistEntry("a", "", 1),
            PlaylistEntry("gone", "[Deleted video]", 2, available=False),
        ],
    )
    rep = env.syncer.run([Source("playlist", "P1")], SyncOptions())
    assert (
        rep.added == 1
        and rep.unavailable == ["gone"]
        and "gone" not in env.download_calls
        and env.db.track_status_all()["gone"] == "unavailable"
    )


def test_unavailable_entries_details_populated(env):
    from ytmsync.ytdl import PlaylistEntry, PlaylistInfo

    seed(env, "a")
    env.syncer.enumerate_fn = lambda s, src: PlaylistInfo(
        "P1",
        "P1",
        [
            PlaylistEntry("a", "", 1),
            PlaylistEntry("gone", "Song A", 2, available=False, artist="Artist A"),
        ],
    )
    rep = env.syncer.run([Source("playlist", "P1")], SyncOptions())
    assert rep.unavailable == ["gone"]
    assert rep.unavailable_details["gone"] == ("Song A", "Artist A")


def test_unavailable_download_failure_details_populated(env):
    seed(env, "a")
    env.playlists["P1"] = ["a"]
    env.fail["a"] = DownloadFailure("unavailable", "Video unavailable")
    rep = env.sync("P1")
    assert rep.unavailable == ["a"]
    assert rep.unavailable_details["a"] == ("Song a", "Artist")


def test_unavailable_from_db_meta_on_resync(env):
    seed(env, "a")
    env.playlists["P1"] = ["a"]
    env.fail["a"] = DownloadFailure("unavailable", "Video unavailable")
    env.sync("P1")  # sets status='unavailable' with meta
    # Next sync without retry_unavailable
    rep = env.sync("P1")
    assert rep.unavailable == ["a"]
    assert rep.unavailable_details["a"] == ("Song a", "Artist")


def test_lyrics_upgrade_plain_to_synced_on_later_sync(env):
    seed(env, "a", "b")
    env.playlists["P1"] = ["a", "b"]
    env.lyrics["a"] = plain("old text")
    env.lyrics["b"] = synced("keep", "me")
    env.sync("P1")
    assert not (env.s.library_dir / "Artist/Album/01 Song a.lrc").exists()
    env.lyrics["a"] = synced("new", "synced")
    assert (
        env.sync("P1").lyrics_upgraded == 0
    )  # checked <14 days ago => not retried yet
    env.db.conn.execute(
        "UPDATE tracks SET lyrics_checked_at = lyrics_checked_at - 15*86400"
    )
    env.db.conn.commit()
    rep = env.sync("P1")
    assert rep.lyrics_upgraded == 1 and sorted(env.download_calls) == [
        "a",
        "b",
    ]  # no re-download
    assert MediaFile(
        str(env.s.library_dir / "Artist/Album/01 Song a.opus")
    ).lyrics.startswith("[00:01.00]new")
    assert (
        env.s.library_dir / "Artist/Album/01 Song a.lrc"
    ).exists() and env.db.get_track("a")["lyrics_kind"] == "synced"
    assert (
        read_video_id(env.s.library_dir / "Artist/Album/01 Song a.opus") == "a"
    )  # other tags untouched


def test_lyrics_never_downgraded_or_forgotten(env):
    seed(env, "a")
    env.playlists["P1"] = ["a"]
    env.lyrics["a"] = synced("x", "y")
    env.sync("P1")
    env.lyrics["a"] = None
    env.sync("P1", refresh_lyrics=True)
    assert (
        env.db.get_track("a")["lyrics_kind"] == "synced"
        and MediaFile(str(env.s.library_dir / "Artist/Album/01 Song a.opus")).lyrics
    )


def test_lyrics_disabled_records_nothing_then_backfills_when_enabled(make_env):
    e = make_env(lyrics=False)
    seed(e, "a")
    e.playlists["P1"] = ["a"]
    e.lyrics["a"] = synced("x", "y")
    e.sync("P1")
    assert (
        e.db.get_track("a")["lyrics_kind"] is None
        and not MediaFile(str(e.s.library_dir / "Artist/Album/01 Song a.opus")).lyrics
    )
    e.new_syncer(lyrics=True)
    rep = e.syncer.lyrics_only(SyncOptions())
    assert (
        rep.lyrics_upgraded == 1
        and MediaFile(str(e.s.library_dir / "Artist/Album/01 Song a.opus")).lyrics
    )


def test_path_collisions_get_video_id_suffix(env):
    env.add("a", title="Same", n=1)
    env.add("b", title="Same", n=1)
    env.playlists["P1"] = ["a", "b"]
    env.sync("P1")
    f = env.files()
    assert (
        len(f) == 2
        and "Artist/Album/01 Same.opus" in f
        and any("[a]" in x or "[b]" in x for x in f)
    )
    env.download_calls.clear()
    rep = env.sync("P1")
    assert rep.moved == 0 and env.download_calls == []  # stable on re-run


def test_foreign_file_in_library_is_never_overwritten(env):
    seed(env, "a")
    env.playlists["P1"] = ["a"]
    mine = env.s.library_dir / "Artist/Album/01 Song a.opus"
    mine.parent.mkdir(parents=True)
    mine.write_bytes(b"user's own file")
    env.sync("P1")
    assert (
        mine.read_bytes() == b"user's own file"
        and "Artist/Album/01 Song a [a].opus" in env.files()
    )


def test_download_pins_files_and_never_deletes(env):
    seed(env, "a", "b")
    env.playlists["P1"] = ["a"]
    env.playlists["P2"] = ["b"]
    env.sync("P1", pin=True, delete=False)
    assert env.db.playlists() == [] and all(
        f.pinned for f in env.db.files_all().values()
    )  # not subscribed
    env.sync("P2")  # normal sync of another playlist
    assert len(env.files()) == 2
    env.playlists["P2"] = []
    env.sync("P2", allow_empty=True)
    assert env.files() == [
        "Artist/Album/01 Song a.opus"
    ]  # pinned a survives, b deleted


def test_single_video_source_as_singles(env):
    seed(env, "a")
    rep = env.syncer.run([Source("video", "a")], SyncOptions(pin=True, delete=False))
    assert rep.added == 1 and env.files() == ["Artist/Album/01 Song a.opus"]


def test_unsubscribe_detach_then_prune(env):
    seed(env, "a", "b")
    env.playlists.update(P1=["a"], P2=["b"])
    env.sync("P1", "P2")
    env.db.unsubscribe("P1", pin_files=True)
    env.db.unsubscribe("P2", pin_files=False)
    rep = env.syncer.prune()
    assert rep.removed == 1 and env.files() == ["Artist/Album/01 Song a.opus"]


def test_refresh_tags_updates_metadata_moves_file_keeps_lyrics(env):
    seed(env, "a")
    env.playlists["P1"] = ["a"]
    env.lyrics["a"] = synced("keep", "me")
    env.sync("P1")
    env.metas["a"].album = "Album (Remastered)"
    env.metas["a"].year = 2024
    env.lyrics["a"] = None  # provider now returns nothing
    rep = env.sync("P1", refresh_tags=True)
    assert rep.retagged == 1 and rep.moved == 1
    p = env.s.library_dir / "Artist/Album (Remastered)/01 Song a.opus"
    m = MediaFile(str(p))
    assert (
        m.album == "Album (Remastered)"
        and m.year == 2024
        and m.lyrics.startswith("[00:01.00]keep")
    )
    assert (
        p.with_suffix(".lrc").exists()
        and not (env.s.library_dir / "Artist/Album").exists()
    )


def test_concurrent_downloads_with_many_tracks(make_env):
    e = make_env(threads=4)
    ids = [f"t{i:03d}" for i in range(25)]
    for i, v in enumerate(ids, 1):
        e.add(v, n=i)
    e.playlists["P1"] = ids
    rep = e.sync("P1")
    assert rep.added == 25 and len(e.files()) == 25 and rep.ok
    assert (
        sorted(e.db.track_status_all()) == sorted(ids) and len(e.db.files_all()) == 25
    )


def test_interrupted_run_leaves_no_partial_files(env, monkeypatch):
    seed(env, "a")
    env.playlists["P1"] = ["a"]
    from ytmsync import syncer

    with monkeypatch.context() as m:  # scoped: do NOT undo the fixture's fake network
        m.setattr(
            syncer.Syncer,
            "_finalize",
            lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt()),
        )
        with pytest.raises(KeyboardInterrupt):
            env.sync("P1")
    assert env.files() == [] and not env.s.tmp_dir.exists()
    assert env.sync("P1").added == 1 and len(env.files()) == 1  # next run succeeds


def test_musicbrainz_enrichment_in_new_downloads(env):
    env.new_syncer(mb=True)
    seed(env, "a", "b")
    env.mb_data["a"] = {
        "mb_recording_id": "rec-a",
        "isrc": "GBAAA2100001",
        "mb_artist_ids": ["art1"],
    }
    env.mb_data["b"] = {
        "disc_no": 2,
        "disc_total": 2,
        "track_no": 3,
        "track_total": 10,
    }  # multi-disc fix changes the file name
    env.playlists["P1"] = ["a", "b"]
    env.sync("P1")
    a = MediaFile(str(env.s.library_dir / "Artist/Album/01 Song a.opus"))
    assert (
        a.mb_trackid == "rec-a"
        and a.isrc == "GBAAA2100001"
        and a.mb_artistids == ["art1"]
    )
    b = MediaFile(
        str(env.s.library_dir / "Artist/Album/03 Song b.opus")
    )  # track 2 -> per-disc track 3
    assert (b.disc, b.disctotal, b.track, b.tracktotal) == (2, 2, 3, 10)
    assert json.loads(env.db.get_track("a")["meta_json"])["mb_recording_id"] == "rec-a"
    env.mb_calls.clear()
    env.sync("P1")
    assert env.mb_calls == []  # nothing new => no MusicBrainz traffic


def test_refresh_tags_applies_musicbrainz_to_existing_library(env):
    seed(env, "a")
    env.playlists["P1"] = ["a"]
    env.lyrics["a"] = synced("keep", "me")
    env.sync("P1")
    assert (
        MediaFile(str(env.s.library_dir / "Artist/Album/01 Song a.opus")).mb_trackid
        is None
    )
    env.new_syncer(mb=True)
    env.mb_data["a"] = {"mb_recording_id": "rec-a", "isrc": "GBAAA2100001"}
    rep = env.sync("P1", refresh_tags=True)
    m = MediaFile(str(env.s.library_dir / "Artist/Album/01 Song a.opus"))
    assert (
        rep.retagged == 1
        and m.mb_trackid == "rec-a"
        and m.isrc == "GBAAA2100001"
        and m.lyrics.startswith("[00:01.00]keep")
    )


def test_layout_flip_playlist_to_library_moves_files_instead_of_wiping(make_env):
    e = make_env(layout="playlist")
    seed(e, "a", "b")
    e.titles["P1"] = "Mix"
    e.playlists["P1"] = ["a", "b"]
    e.sync("P1")
    assert e.files() == [
        "Mix/001 Artist - Song a.opus",
        "Mix/002 Artist - Song b.opus",
    ]
    e.download_calls.clear()
    e.new_syncer(
        layout="library"
    )  # back to default layout (the "forgot --layout" footgun)
    rep = e.sync("P1")
    assert (rep.added, rep.copied, rep.moved, rep.removed) == (0, 0, 2, 0)
    assert e.download_calls == []
    assert e.files() == [
        "Artist/Album/01 Song a.opus",
        "Artist/Album/02 Song b.opus",
    ]
    assert e.db.path_owner("Artist/Album/01 Song a.opus") == ("a", "")
    assert not (e.s.library_dir / "Mix").exists()


def test_layout_flip_preserves_unavailable_tracks_file(make_env):
    from ytmsync.ytdl import PlaylistEntry, PlaylistInfo

    e = make_env(layout="playlist")
    seed(e, "a", "u")
    e.titles["P1"] = "Mix"
    e.playlists["P1"] = ["a", "u"]
    e.sync("P1")
    assert e.files() == [
        "Mix/001 Artist - Song a.opus",
        "Mix/002 Artist - Song u.opus",
    ]
    e.download_calls.clear()
    e.new_syncer(layout="library")
    e.syncer.enumerate_fn = lambda s, src: PlaylistInfo(  # u went private upstream
        "P1",
        "Mix",
        [
            PlaylistEntry("a", "", 1),
            PlaylistEntry("u", "[Deleted video]", 2, available=False),
        ],
    )
    rep = e.sync("P1")
    assert rep.unavailable == ["u"] and e.download_calls == []
    assert (
        rep.removed == 0
        and "Artist/Album/02 Song u.opus" in e.files()
        and e.db.files_all()[("u", "")].path == "Artist/Album/02 Song u.opus"
    )


def test_layout_flip_library_to_playlist_moves_into_playlist_scope(make_env):
    e = make_env()  # library layout
    seed(e, "a", "b")
    e.titles["P1"] = "Mix"
    e.playlists["P1"] = ["a", "b"]
    e.sync("P1")
    e.download_calls.clear()
    e.new_syncer(layout="playlist")
    rep = e.sync("P1")
    assert (rep.added, rep.copied, rep.moved, rep.removed) == (0, 0, 2, 0)
    assert e.files() == [
        "Mix/001 Artist - Song a.opus",
        "Mix/052 Artist - Song b.opus",  # playlist_index from merged DB membership
    ] or e.files() == [
        "Mix/001 Artist - Song a.opus",
        "Mix/002 Artist - Song b.opus",
    ]
    assert e.db.path_owner("Mix/001 Artist - Song a.opus") == ("a", "P1")
    assert not (e.s.library_dir / "Artist").exists()


def test_layout_flip_dry_run_shows_moves_and_no_deletes(make_env):
    e = make_env(layout="playlist")
    seed(e, "a")
    e.titles["P1"] = "Mix"
    e.playlists["P1"] = ["a"]
    e.sync("P1")
    before = e.files()
    e.new_syncer(layout="library")
    rep = e.sync("P1", dry_run=True)
    assert rep.plan.delete == [] and rep.plan.copy == []
    assert len(rep.planned_moves) == 1 and rep.plan.rescope == [("a", "", "P1")]
    assert e.files() == before  # nothing was touched


def test_unsubscribe_prune_leaves_rekeyed_donors_alone(make_env):
    e = make_env(layout="playlist")
    seed(e, "a")
    e.titles.update(P1="Mix One", P2="Mix Two")
    e.playlists.update(P1=["a"], P2=["a"])
    e.sync("P1", "P2")
    e.playlists["P1"] = []
    e.sync("P1", allow_empty=True)
    assert e.files() == ["Mix Two/001 Artist - Song a.opus"]


def test_rerun_after_layout_flip_is_stable(make_env):
    e = make_env(layout="playlist")
    seed(e, "a")
    e.titles["P1"] = "Mix"
    e.playlists["P1"] = ["a"]
    e.sync("P1")
    e.new_syncer(layout="library")
    e.sync("P1")
    e.download_calls.clear()
    rep = e.sync("P1")
    assert (rep.added, rep.copied, (rep.moved), rep.removed) == (0, 0, 0, 0)
    assert e.download_calls == []
    assert e.db.path_owner("Artist/Album/01 Song a.opus") == ("a", "")


def test_song_moving_between_playlists_is_rekeyed_without_redownload(make_env):
    from ytmsync.plan import FileRow

    e = make_env(layout="playlist")
    seed(e, "a")
    e.titles.update(P1="Mix One", P2="Mix Two")
    e.playlists.update(P1=["a"], P2=[])
    e.sync("P1")
    assert e.db.files_all() == {
        ("a", "P1"): FileRow("Mix One/001 Artist - Song a.opus", False)
    }
    e.download_calls.clear()
    e.playlists.update(P1=[], P2=["a"])  # removed from P1, added to P2
    rep = e.sync("P1", "P2", allow_empty=True)
    assert (rep.added, rep.copied, rep.moved, rep.removed) == (0, 0, 1, 0)
    assert e.download_calls == []
    assert e.files() == ["Mix Two/001 Artist - Song a.opus"]
    assert e.db.files_all() == {
        ("a", "P2"): FileRow("Mix Two/001 Artist - Song a.opus", False)
    }
