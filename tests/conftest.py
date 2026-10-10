"""End-to-end harness: real syncer / pipeline / tagging / DB / lyrics chain; fake network (yt-dlp, ytmusicapi, HTTP)."""
import io, shutil, subprocess
from pathlib import Path

import pytest
from PIL import Image
from rich.console import Console

from ytmsync import pipeline
from ytmsync.config import Settings
from ytmsync.httpcache import HttpCache
from ytmsync.lyrics.lrc import LyricLine, to_plain
from ytmsync.models import LyricsResult, TrackMeta
from ytmsync.pipeline import Ctx
from ytmsync.state import Db
from ytmsync.syncer import SyncOptions, Syncer
from ytmsync.ytdl import DownloadFailure, EnumerationError, PlaylistEntry, PlaylistInfo
from ytmsync.ytm import YTMeta


@pytest.fixture(scope="session")
def stub_opus(tmp_path_factory) -> Path:
    p = tmp_path_factory.mktemp("stub") / "stub.opus"
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1", "-c:a", "libopus", str(p)], check=True)
    return p

def synced(*texts, source="lrclib"):
    lines = [LyricLine(1000 * (i + 1), t) for i, t in enumerate(texts)]
    return LyricsResult("synced", source, lines, to_plain(lines))

def plain(text="la la la", source="ytmusic"): return LyricsResult("plain", source, [], text)

class FakeYTM(YTMeta):
    def __init__(self, env): super().__init__(lambda: None); self.env = env
    def enrich(self, vid):
        self.env.enrich_calls.append(vid)
        return self.env.metas[vid].__class__(**self.env.metas[vid].to_json()) if vid in self.env.metas else None

class FakeMB:
    """Stands in for MusicBrainz: env.mb_data[video_id] = fields to apply."""
    def __init__(self, env): self.env = env
    def enrich(self, meta):
        self.env.mb_calls.append(meta.video_id)
        for k, v in self.env.mb_data.get(meta.video_id, {}).items(): setattr(meta, k, v)
        return meta

class Env:
    def __init__(self, tmp_path, stub_opus, monkeypatch, **settings):
        self.tmp, self.stub = tmp_path, stub_opus
        self.settings_kw = {"library_dir": tmp_path / "lib", "threads": 2, **settings}
        self.metas: dict[str, TrackMeta] = {}
        self.playlists: dict[str, list[str] | Exception] = {}
        self.titles: dict[str, str] = {}
        self.lyrics: dict[str, LyricsResult | None] = {}
        self.fail: dict[str, Exception] = {}
        self.mb_data: dict[str, dict] = {}
        self.mb_calls: list[str] = []
        self.download_calls: list[str] = []
        self.enrich_calls: list[str] = []
        self.console = Console(quiet=True)
        jpeg = io.BytesIO(); Image.new("RGB", (500, 500), (10, 200, 30)).save(jpeg, "JPEG"); self.jpeg = jpeg.getvalue()
        env = self

        def fake_download(s, vid, workdir, on_progress=None):
            env.download_calls.append(vid)
            if vid in env.fail: raise env.fail[vid]
            out = workdir / f"{vid}.{s.format}"; shutil.copy(env.stub, out)
            return out, {"id": vid, "duration": 200}
        monkeypatch.setattr(pipeline, "download_audio", fake_download)
        monkeypatch.setattr(pipeline, "fetch_lyrics", lambda meta, ctx, names, registry=None: env.lyrics.get(meta.video_id))

        class Resp:  # every cover URL works
            status_code, content = 200, env.jpeg
        class Http:
            def get(self, url, **kw): return Resp()
        self.http = Http()
        self.new_syncer()

    def new_syncer(self, mb=False, **override):
        self.s = Settings(**{**self.settings_kw, **override})
        self.db = Db(self.s.state_dir / "state.db")
        ctx = Ctx(self.s, FakeYTM(self), HttpCache(self.tmp / "cache.sqlite"), http_factory=lambda: self.http, mb=FakeMB(self) if mb else None)
        def enum(s, src):
            v = self.playlists[src.id]
            if isinstance(v, Exception): raise v
            return PlaylistInfo(src.id, self.titles.get(src.id, src.id), [PlaylistEntry(x, "", i) for i, x in enumerate(v, 1)])
        self.syncer = Syncer(self.s, self.db, ctx, console=self.console, enumerate_fn=enum,
                             lyrics_fn=lambda meta, lctx, names: self.lyrics.get(meta.video_id))
        return self.syncer

    def add(self, vid, title=None, artist="Artist", album="Album", n=1, **kw):
        self.metas[vid] = TrackMeta(vid, title or f"Song {vid}", [artist], album, album_artists=[artist], track_no=n, track_total=10,
                                    duration_s=200, year=2020, cover_url="https://c/x", **kw)
    def sync(self, *pids, **opts):
        from ytmsync.sources import Source
        return self.syncer.run([Source("playlist", p) for p in pids], SyncOptions(**opts))
    def files(self):  # relative POSIX paths of audio files
        lib = self.s.library_dir
        return sorted(p.relative_to(lib).as_posix() for p in lib.rglob("*.opus") if ".ytmsync" not in p.parts)
    def rel(self, pattern):  # all non-hidden files
        lib = self.s.library_dir
        return sorted(p.relative_to(lib).as_posix() for p in lib.rglob("*") if p.is_file() and ".ytmsync" not in p.parts)

@pytest.fixture
def env(tmp_path, stub_opus, monkeypatch):
    return Env(tmp_path, stub_opus, monkeypatch)

@pytest.fixture
def make_env(tmp_path, stub_opus, monkeypatch):
    return lambda **kw: Env(tmp_path, stub_opus, monkeypatch, **kw)
