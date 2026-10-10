import io, subprocess, sys
from pathlib import Path
import pytest
from PIL import Image
from mediafile import MediaFile
from mutagen.id3 import ID3
from ytmsync.models import TrackMeta, LyricsResult
from ytmsync.tagging import write_tags, write_lyrics_only, read_video_id
from ytmsync.lyrics.lrc import parse_lrc, to_plain

CODEC = {"opus": ["-c:a", "libopus"], "m4a": ["-c:a", "aac"], "mp3": ["-c:a", "libmp3lame"]}

def _stub(path: Path, ext: str):
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1",
                    *CODEC[ext], "-metadata", "title=JUNK", str(path)], check=True)

def _lyrics(kind):
    lines, _ = parse_lrc("[00:01.00]hello\n[00:02.50]world\n[00:04.00]\n[00:05.00]bye")
    if kind == "synced": return LyricsResult("synced", "lrclib", lines, to_plain(lines))
    if kind == "plain":  return LyricsResult("plain", "ytmusic", [], "hello\nworld")
    return None

META = TrackMeta("dQw4w9WgXcQ", "Never Gonna Give You Up", ["Rick Astley", "Someone Else"], "Whenever You Need Somebody",
                 "MPREb_x", ["Rick Astley"], 1, 10, 1, 1, 1987, "1987-11-12", 213, None, False, True)

@pytest.mark.parametrize("ext", ["opus", "m4a", "mp3"])
@pytest.mark.parametrize("kind", ["synced", "plain", "none"])
def test_roundtrip(tmp_path, ext, kind):
    p = tmp_path / f"a.{ext}"; _stub(p, ext)
    buf = io.BytesIO(); Image.new("RGB", (600, 600), (200, 30, 30)).save(buf, "JPEG")
    write_tags(p, META, buf.getvalue(), _lyrics(kind))
    r = MediaFile(str(p))
    assert r.title == META.title and r.artist == "Rick Astley & Someone Else" and r.artists == META.artists
    assert r.albumartist == "Rick Astley" and r.album == META.album
    assert (r.track, r.tracktotal, r.disc, r.disctotal) == (1, 10, 1, 1)
    assert str(r.date) == "1987-11-12" and len(r.images) == 1
    assert read_video_id(p) == META.video_id
    if kind == "none": assert not r.lyrics
    if kind == "plain": assert r.lyrics == "hello\nworld"
    if kind == "synced" and ext != "mp3": assert r.lyrics.splitlines()[0] == "[00:01.00]hello"
    if kind == "synced" and ext == "mp3":
        assert r.lyrics.startswith("hello") and ID3(str(p)).getall("SYLT")[0].text[0] == ("hello", 1000)

@pytest.mark.parametrize("ext", ["opus", "m4a", "mp3"])
def test_lyrics_upgrade_keeps_other_tags(tmp_path, ext):
    p = tmp_path / f"a.{ext}"; _stub(p, ext)
    write_tags(p, META, None, _lyrics("plain"))
    write_lyrics_only(p, _lyrics("synced"))
    r = MediaFile(str(p))
    assert r.title == META.title and r.album == META.album and read_video_id(p) == META.video_id   # untouched
    if ext != "mp3": assert r.lyrics.startswith("[00:01.00]hello")
    else: assert ID3(str(p)).getall("SYLT")
    write_lyrics_only(p, None)                                  # removal path
    assert not MediaFile(str(p)).lyrics
