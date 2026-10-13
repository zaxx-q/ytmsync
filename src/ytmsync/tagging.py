"""Write tags to .opus / .m4a / .mp3 with mediafile (common fields) + mutagen (custom / timed frames)."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from mediafile import Image as MFImage
from mediafile import ImageType, MediaFile
from mutagen.id3 import ID3, SYLT, TXXX
from mutagen.mp4 import MP4, MP4FreeForm
from mutagen.oggopus import OggOpus

from .lyrics.lrc import format_lrc
from .models import LyricsResult, TrackMeta

VIDEO_ID_TAG = "YTM_VIDEO_ID"
ARTIST_JOIN = (
    " & "  # Picard convention: ARTIST = display string, ARTISTS = multi-value list
)


def write_tags(
    path: Path,
    meta: TrackMeta,
    cover_jpeg: bytes | None,
    lyrics: LyricsResult | None,
    artist_join: str = ARTIST_JOIN,
    embed_lyrics: bool = True,
) -> None:
    ext = path.suffix.lower()
    mf = MediaFile(str(path))
    mf.delete()  # start clean: drop yt-dlp/ffmpeg leftovers
    mf.title = meta.title
    mf.artist = artist_join.join(meta.artists)
    mf.artists = list(meta.artists)
    aa = meta.album_artists or meta.artists
    mf.albumartist = artist_join.join(aa)
    mf.albumartists = list(aa)
    if meta.album:
        mf.album = meta.album
    if meta.track_no:
        mf.track, mf.tracktotal = meta.track_no, meta.track_total
    if meta.disc_no:
        mf.disc, mf.disctotal = meta.disc_no, meta.disc_total
    if meta.date:
        mf.date = dt.date.fromisoformat(meta.date)
    elif meta.year:
        mf.year = meta.year
    mf.comments = f"https://music.youtube.com/watch?v={meta.video_id}"
    # MusicBrainz ids (D12: recording -> mb_trackid, release -> mb_albumid, release group -> mb_releasegroupid)
    if meta.mb_recording_id:
        mf.mb_trackid = meta.mb_recording_id
    if meta.mb_release_id:
        mf.mb_albumid = meta.mb_release_id
    if meta.mb_release_group_id:
        mf.mb_releasegroupid = meta.mb_release_group_id
    # NB: `mb_artistid` (single) and `mb_artistids` (list) share ONE underlying tag; assigning both would clobber the list.
    if meta.mb_artist_ids:
        mf.mb_artistids = list(meta.mb_artist_ids)
    if meta.mb_album_artist_ids:
        mf.mb_albumartistids = list(meta.mb_album_artist_ids)
    if meta.isrc:
        mf.isrc = meta.isrc
    if cover_jpeg:
        mf.images = [MFImage(data=cover_jpeg, desc="Cover", type=ImageType.front)]
    if embed_lyrics:
        _set_lyrics_field(mf, ext, lyrics)
    mf.save()

    # ---- things mediafile has no field for --------------------------------------------------
    vid = meta.video_id
    if ext == ".m4a":
        t = MP4(str(path))
        t[f"----:com.apple.iTunes:{VIDEO_ID_TAG}"] = [MP4FreeForm(vid.encode())]
        t.save()
    elif ext == ".opus":
        t = OggOpus(str(path))
        t[VIDEO_ID_TAG] = [vid]
        t.save()
    elif ext == ".mp3":
        t = ID3(str(path))
        t.add(TXXX(encoding=3, desc=VIDEO_ID_TAG, text=[vid]))
        _set_sylt(t, lyrics if embed_lyrics else None)
        t.save(v2_version=4)
    else:
        raise ValueError(f"Unsupported extension {ext}")


def read_video_id(path: Path) -> str | None:
    """Used by a future `reindex` command and by tests."""
    ext = path.suffix.lower()
    if ext == ".m4a":
        v = MP4(str(path)).get(f"----:com.apple.iTunes:{VIDEO_ID_TAG}")
        return bytes(v[0]).decode() if v else None
    if ext == ".opus":
        v = OggOpus(str(path)).get(VIDEO_ID_TAG)
        return v[0] if v else None
    if ext == ".mp3":
        fr = ID3(str(path)).getall(f"TXXX:{VIDEO_ID_TAG}")
        return str(fr[0].text[0]) if fr else None
    return None


def _set_lyrics_field(mf: MediaFile, ext: str, lyrics: LyricsResult | None) -> None:
    if not lyrics or lyrics.instrumental:
        mf.lyrics = None
    elif ext == ".mp3":
        mf.lyrics = (
            lyrics.plain
        )  # USLT must stay human-readable; timed data goes in SYLT
    elif lyrics.kind == "synced":
        mf.lyrics = format_lrc(
            lyrics.lines
        )  # LRC text in \u00a9lyr / LYRICS: most players parse it
    else:
        mf.lyrics = lyrics.plain


def _set_sylt(t: ID3, lyrics: LyricsResult | None) -> None:
    t.delall("SYLT")
    if lyrics and lyrics.kind == "synced" and not lyrics.instrumental:
        t.add(
            SYLT(
                encoding=3,
                lang="eng",
                format=2,
                type=1,
                desc="",  # format 2 = milliseconds, type 1 = lyrics
                text=[(l.text, l.start_ms) for l in lyrics.lines],
            )
        )


def write_lyrics_only(path: Path, lyrics: LyricsResult | None) -> None:
    """Used by the 'upgrade plain -> synced lyrics later' pass: touches ONLY lyrics frames."""
    ext = path.suffix.lower()
    mf = MediaFile(str(path))
    _set_lyrics_field(mf, ext, lyrics)
    mf.save()
    if ext == ".mp3":
        t = ID3(str(path))
        _set_sylt(t, lyrics)
        t.save(v2_version=4)


def existing_lyrics(path: Path) -> LyricsResult | None:
    """Recover lyrics already stored next to / inside a file, so `--refresh-tags` never destroys lyrics it cannot re-fetch."""
    from .lyrics.lrc import parse_lrc, to_plain

    side = path.with_suffix(".lrc")
    text = (
        side.read_text("utf-8")
        if side.exists()
        else (MediaFile(str(path)).lyrics or "")
    )
    if not text.strip():
        return None
    lines, _ = parse_lrc(text)
    if lines:
        return LyricsResult("synced", "existing", lines, to_plain(lines))
    return LyricsResult("plain", "existing", [], text.strip())
