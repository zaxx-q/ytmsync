import datetime as dt
import shutil
import time

import pytest
from click.testing import CliRunner

from ytmsync import cli as climod
from ytmsync import doctor, pipeline
from ytmsync.cli import cli
from ytmsync.config import Settings
from ytmsync.ytdl import DownloadFailure, EnumerationError, PlaylistEntry, PlaylistInfo


# ------------------------------------------------------------------ doctor
def test_ytdlp_age():
    assert doctor.ytdlp_age_days("2026.08.19", dt.date(2026, 9, 30)) == 42
    assert (
        doctor.ytdlp_age_days("2026.08.19.1", dt.date(2026, 8, 20)) == 1
        and doctor.ytdlp_age_days("weird") is None
    )


def test_check_cookies_variants(tmp_path):
    def s(**kw):
        return Settings(library_dir=tmp_path, **kw)

    assert doctor.check_cookies(s()).status == "ok"
    assert doctor.check_cookies(s(cookies=tmp_path / "nope.txt")).status == "fail"
    bad = tmp_path / "bad.txt"
    bad.write_text("this is not a cookie file")
    assert doctor.check_cookies(s(cookies=bad)).status == "fail"
    future = int(time.time()) + 86400 * 30

    def jar(*rows):
        f = tmp_path / "c.txt"
        f.write_text(
            "# Netscape HTTP Cookie File\n"
            + "".join(
                f".youtube.com\tTRUE\t/\tTRUE\t{exp}\t{name}\tv\n" for name, exp in rows
            )
        )
        return f

    assert (
        doctor.check_cookies(s(cookies=jar(("PREF", future)))).status == "warn"
    )  # no login cookie
    assert (
        doctor.check_cookies(
            s(cookies=jar(("SAPISID", future), ("PREF", future)))
        ).status
        == "ok"
    )
    assert (
        doctor.check_cookies(s(cookies=jar(("SAPISID", 1000)))).status == "warn"
    )  # expired
    assert doctor.check_cookies(s(cookies_from_browser="firefox")).status == "ok"


def test_run_checks_offline_shape_and_failure_hints(tmp_path, monkeypatch):
    res = doctor.run_checks(Settings(library_dir=tmp_path), offline=True)
    names = [c.name for c in res]
    assert {
        "Python",
        "yt-dlp",
        "ffmpeg",
        "ffprobe",
        "JS runtime",
        "Library dir",
        "Cookies",
    } <= set(names) and not any("Network" in n for n in names)
    monkeypatch.setattr(doctor, "detect_js_runtimes", dict)
    js = doctor.check_js()
    assert js.status == "fail" and "yt-dlp[deno]" in js.hint
    monkeypatch.setattr(doctor.shutil, "which", lambda x: None)
    assert [c.status for c in doctor.check_ffmpeg(Settings(library_dir=tmp_path))] == [
        "fail",
        "fail",
    ]


# ------------------------------------------------------------------ CLI
class StubYTM:
    def enrich(self, vid):
        return None

    def resolve_album_browse(self, b):
        return "OLAK5uy_resolved" if b == "MPREb_ok" else None


@pytest.fixture
def app(tmp_path, stub_opus, monkeypatch):
    monkeypatch.setenv("YTMSYNC_CONFIG", str(tmp_path / "cfg" / "config.toml"))
    monkeypatch.setattr(
        climod, "cache_dir", lambda: tmp_path / "cache"
    )  # cross-platform (XDG vars only work on Linux)
    lists = {"PLaaaaaaaaaa": ["v1", "v2"], "OLAK5uy_resolved": ["v3"]}
    calls = []

    def enum(s, src):
        if src.id not in lists:
            raise EnumerationError("does not exist")
        return PlaylistInfo(
            src.id,
            f"List {src.id[:4]}",
            [PlaylistEntry(v, "", i) for i, v in enumerate(lists[src.id], 1)],
        )

    def dl(s, vid, workdir, on_progress=None):
        calls.append(vid)
        if vid == "bad":
            raise DownloadFailure("other", "nope")
        out = workdir / f"{vid}.{s.format}"
        shutil.copy(stub_opus, out)
        return out, {
            "id": vid,
            "title": f"Title {vid}",
            "artists": ["Art"],
            "album": "Alb",
            "duration": 100,
        }

    monkeypatch.setattr(climod, "enumerate_playlist", enum)
    monkeypatch.setattr(climod, "YTMeta", StubYTM)
    monkeypatch.setattr(pipeline, "download_audio", dl)
    monkeypatch.setattr(pipeline, "fetch_lyrics", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "fetch_cover", lambda *a, **k: None)
    lib = tmp_path / "lib"

    def run(*args, **kw):
        return CliRunner().invoke(
            cli,
            [*args, "-o", str(lib)] if args[0] not in ("config",) else list(args),
            catch_exceptions=False,
            **kw,
        )

    run.lib, run.calls, run.lists = lib, calls, lists
    return run


def test_help_and_version():
    r = CliRunner().invoke(cli, ["--help"])
    assert r.exit_code == 0 and "sync" in r.output and "doctor" in r.output
    assert CliRunner().invoke(cli, ["--version"]).exit_code == 0


def test_config_init_path_show(app, tmp_path):
    assert app("config", "path").output.strip().endswith("config.toml")
    assert (
        app("config", "init").exit_code == 0
        and (tmp_path / "cfg" / "config.toml").exists()
    )
    assert app("config", "init").exit_code == 1  # refuses to overwrite
    out = app("config", "show", "-f", "m4a", "-o", str(tmp_path / "x")).output
    assert (
        "format = 'm4a'" in out
        and "path_template = '{albumartist}/{album}/{track:02d} {title}'" in out
    )


def test_add_list_sync_remove_flow(app):
    r = app("add", "https://music.youtube.com/playlist?list=PLaaaaaaaaaa")
    assert r.exit_code == 0 and "subscribed" in r.output and "2 tracks" in r.output
    assert "PLaaaaaaaaaa" in app("list").output
    r = app("sync")
    assert (
        r.exit_code == 0 and "added" in r.output and sorted(app.calls) == ["v1", "v2"]
    )
    assert sorted(p.name for p in (app.lib / "Art" / "Alb").glob("*.opus")) == [
        "00 Title v1.opus",
        "00 Title v2.opus",
    ]
    assert "up to date" in app("sync").output
    app.lists["PLaaaaaaaaaa"] = ["v1"]
    r = app("sync", "--dry-run")
    assert (
        r.exit_code == 0
        and "Dry run" in r.output
        and "would download 0" in r.output
        and "delete 1" in r.output
    )
    assert (
        len(list((app.lib / "Art" / "Alb").glob("*.opus"))) == 2
    )  # dry-run deleted nothing
    r = app("remove", "List PLaa", "--delete-files")
    assert r.exit_code == 0 and "deleted 2" in r.output
    assert list(app.lib.rglob("*.opus")) == []


def test_sync_with_url_auto_subscribes_and_album_url_resolves(app):
    assert (
        app("sync", "PLaaaaaaaaaa").exit_code == 0
        and "PLaaaaaaaaaa" in app("list").output
    )
    r = app("sync", "https://music.youtube.com/browse/MPREb_ok")
    assert r.exit_code == 0 and "OLAK5uy_resolved" in app("list").output
    r = app("sync", "https://music.youtube.com/browse/MPREb_missing")
    assert r.exit_code == 1 and "resolve" in r.output


def test_exit_codes_and_errors(app):
    r = app("sync")
    assert r.exit_code == 1 and "No subscriptions" in r.output
    r = app("sync", "https://example.com/x")
    assert r.exit_code == 2
    r = app("sync", "https://youtu.be/dQw4w9WgXcQ")
    assert r.exit_code == 2 and "download" in r.output  # video only for download
    r = app("sync", "PLaaaaaaaaaa", "--template", "{nope}/{title}")
    assert r.exit_code == 2
    r = app("sync", "PLmissingxxxx")
    assert r.exit_code == 1 and "skipped" in r.output
    app.lists["PLbadbadbadb"] = ["v1", "bad"]
    r = app("sync", "PLbadbadbadb")
    assert r.exit_code == 1 and "failed" in r.output and "bad" in r.output
    assert app("remove", "nothing-here").exit_code == 1


def test_download_command_pins_and_accepts_video_url(app):
    r = app("download", "https://youtu.be/dQw4w9WgXcQ")
    assert r.exit_code == 0 and "added" in r.output
    assert app.calls == ["dQw4w9WgXcQ"] and "No subscriptions yet" in app("list").output


def test_doctor_command_offline(app):
    r = app("doctor", "--offline")
    assert "JS runtime" in r.output and "ffmpeg" in r.output


def test_check_musicbrainz(tmp_path):
    assert (
        doctor.check_musicbrainz(Settings(library_dir=tmp_path)) is None
    )  # silent when disabled
    s = Settings(library_dir=tmp_path, musicbrainz=True, contact="me@example.com")
    assert doctor.check_musicbrainz(s).status == "ok"
    s.contact = None
    c = doctor.check_musicbrainz(s)
    assert c.status == "fail" and "--contact" in c.hint
    names = [
        c.name
        for c in doctor.run_checks(
            Settings(library_dir=tmp_path, musicbrainz=True, contact="x"), offline=True
        )
    ]
    assert "MusicBrainz" in names


def test_cli_musicbrainz_flags(app, monkeypatch):
    r = app("sync", "PLaaaaaaaaaa", "--musicbrainz")
    assert r.exit_code == 2 and "contact" in r.output
    seen = []

    class StubMB:
        def __init__(self, contact, cache):
            seen.append(("init", contact))

        def enrich(self, meta):
            seen.append(("enrich", meta.video_id))
            return meta

    monkeypatch.setattr(climod, "MusicBrainz", StubMB)
    r = app("sync", "PLaaaaaaaaaa", "--musicbrainz", "--contact", "me@example.com")
    assert (
        r.exit_code == 0
        and seen[0] == ("init", "me@example.com")
        and {x for x in seen if x[0] == "enrich"}
        == {("enrich", "v1"), ("enrich", "v2")}
    )
    seen.clear()
    app("sync", "PLaaaaaaaaaa")
    assert seen == []  # not constructed unless enabled
    assert (
        "contact = 'me@example.com'"
        in app(
            "config",
            "show",
            "-o",
            str(app.lib),
            "--musicbrainz",
            "--contact",
            "me@example.com",
        ).output
    )
