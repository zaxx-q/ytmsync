from __future__ import annotations

import logging
import re
import shutil
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from .config import Settings
from .sources import Source

log = logging.getLogger("ytmsync.ytdl")

# Format selectors. NOTE: the FFmpegExtractAudio post-processor stream-COPIES when the chosen stream's codec
# already equals the target (verified in yt-dlp source), so opus->.opus and aac->.m4a are lossless remuxes.
FORMAT_SELECTOR = {
    "opus": "bestaudio[acodec=opus]/bestaudio",
    "m4a": "bestaudio[ext=m4a]/bestaudio",
    "mp3": "bestaudio",
}


class _Quiet:  # route yt-dlp's console chatter to our debug log
    def debug(self, m):
        log.debug(m)

    def info(self, m):
        log.debug(m)

    def warning(self, m):
        log.debug(m)

    def error(self, m):
        log.debug(m)


class EnumerationError(Exception): ...


class DownloadFailure(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind  # unavailable | auth | rate_limit | transient | other


def classify_error(msg: str) -> str:
    m = msg.lower()
    if any(
        x in m
        for x in (
            "sign in to confirm your age",
            "confirm your age",
            "members-only",
            "login required",
            "sign in to confirm you",
        )
    ):
        return "auth"
    if any(
        x in m
        for x in (
            "video unavailable",
            "private video",
            "has been removed",
            "not available in your country",
            "this video is not available",
            "copyright",
            "account associated with this video has been terminated",
        )
    ):
        return "unavailable"
    if "429" in m or "too many requests" in m:
        return "rate_limit"
    if any(
        x in m
        for x in (
            "timed out",
            "connection",
            "http error 5",
            "temporary failure",
            "unable to download",
        )
    ):
        return "transient"
    return "other"


def detect_js_runtimes() -> dict[str, dict]:
    """yt-dlp enables ONLY deno unless told otherwise, so enable every supported runtime we can find."""
    try:
        from yt_dlp.utils._jsruntime import (  # private API
            BunJsRuntime,
            DenoJsRuntime,
            NodeJsRuntime,
            QuickJsRuntime,
        )

        out = {}
        for name, cls in (
            ("deno", DenoJsRuntime),
            ("node", NodeJsRuntime),
            ("bun", BunJsRuntime),
            ("quickjs", QuickJsRuntime),
        ):
            info = cls().info  # None when not installed
            if info and info.supported:
                out[name] = {}
        return out
    except Exception:  # noqa: BLE001 -- private module moved -> plain PATH lookup
        return {n: {} for n in ("deno", "node", "bun", "quickjs") if shutil.which(n)}


def base_opts(s: Settings) -> dict:
    o: dict = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _Quiet(),
        "retries": 5,
        "fragment_retries": 5,
        "extractor_retries": 3,
        "socket_timeout": 30,
    }
    if s.js_runtime:
        name, _, path = s.js_runtime.partition("=")
        o["js_runtimes"] = {name: ({"path": path} if path else {})}
    else:
        rt = detect_js_runtimes()
        if rt:
            o["js_runtimes"] = rt
    if s.cookies:
        o["cookiefile"] = str(s.cookies)
    if s.cookies_from_browser:
        o["cookiesfrombrowser"] = tuple(
            s.cookies_from_browser.split(":")
        )  # ("firefox",) or ("chrome","Profile 1")
    if s.sleep_interval:
        o["sleep_interval"] = s.sleep_interval
        o["max_sleep_interval"] = s.sleep_interval * 2
    return o


@dataclass
class PlaylistEntry:
    video_id: str
    title: str
    position: int  # 1-based
    available: bool = True


@dataclass
class PlaylistInfo:
    playlist_id: str
    title: str
    entries: list[PlaylistEntry]


_UNAVAILABLE_TITLE = re.compile(r"^\[(private|deleted) video\]$", re.IGNORECASE)


def enumerate_playlist(s: Settings, src: Source) -> PlaylistInfo:
    """Flat listing = fast, membership only. Rich metadata comes later, per new track only."""
    opts = {
        **base_opts(s),
        "extract_flat": "in_playlist",
        "skip_download": True,
        "ignoreerrors": False,
    }
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(src.url, download=False)
    except DownloadError as e:
        raise EnumerationError(
            f"{e}. If this playlist is private, pass --cookies / --cookies-from-browser."
        ) from e
    if not info:
        raise EnumerationError("yt-dlp returned no data")
    entries: list[PlaylistEntry] = []
    for i, e in enumerate(info.get("entries") or [], 1):
        if not e or not e.get("id"):
            continue
        title = e.get("title") or ""
        entries.append(
            PlaylistEntry(
                e["id"], title, i, available=not _UNAVAILABLE_TITLE.match(title)
            )
        )
    return PlaylistInfo(src.id, info.get("title") or src.id, entries)


def download_audio(
    s: Settings,
    video_id: str,
    workdir: Path,
    on_progress: Callable[[int, int | None], None] | None = None,
) -> tuple[Path, dict]:
    """Download + convert one track into `workdir`. Returns (audio_path, yt-dlp info dict). Raises DownloadFailure."""

    def hook(d: dict):
        if on_progress and d.get("status") == "downloading":
            on_progress(
                d.get("downloaded_bytes") or 0,
                d.get("total_bytes") or d.get("total_bytes_estimate"),
            )

    pp = {"key": "FFmpegExtractAudio", "preferredcodec": s.format}
    if s.format == "mp3":
        pp["preferredquality"] = s.mp3_quality
    opts = {
        **base_opts(s),
        "format": FORMAT_SELECTOR[s.format],
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "overwrites": True,
        "postprocessors": [pp],
        "progress_hooks": [hook],
    }
    url = f"https://music.youtube.com/watch?v={video_id}"

    last: Exception | None = None
    for attempt in range(1, 4):
        try:
            with YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
            reqs = info.get("requested_downloads") or []
            path = Path(reqs[0]["filepath"]) if reqs else None
            if not path or not path.exists():  # defensive fallback
                found = sorted(workdir.glob(f"{video_id}.*"))
                path = found[0] if found else None
            if not path or path.suffix != f".{s.format}":
                raise DownloadFailure("other", f"unexpected output file: {path}")
            return path, info
        except DownloadError as e:
            kind = classify_error(str(e))
            last = DownloadFailure(kind, str(e))
            if kind in ("unavailable", "auth", "other"):
                # YouTube Music auto-generated "Topic" tracks (Art Track Videos) are
                # often UNPLAYABLE via yt-dlp but YTM silently redirects to a free
                # counterpart (official upload / different distributor).  Try that
                # before giving up.  See https://github.com/yt-dlp/yt-dlp/issues/14066
                if kind == "unavailable":
                    alt = _resolve_ytm_redirect(video_id, s.cookies)
                    if alt:
                        log.info(
                            "%s: unavailable, trying YTM redirect -> %s", video_id, alt
                        )
                        return _download_redirected(
                            opts, alt, video_id, workdir, s.format
                        )
                raise last from e
            time.sleep(
                30 * attempt if kind == "rate_limit" else 3 * attempt
            )  # backoff, then retry
    raise last or DownloadFailure("other", "download failed")


def _resolve_ytm_redirect(video_id: str, cookies: Path | None = None) -> str | None:
    """Ask music.youtube.com for the canonical URL of a video.

    YouTube Music silently redirects unavailable Art Track Videos (distributor
    changes, geo-blocks, Premium-only) to a free counterpart.  The redirect
    target is embedded in the HTML as ``<link rel="canonical" …>``.

    Returns the *different* video ID if a redirect exists, else ``None``.
    See https://github.com/yt-dlp/yt-dlp/issues/14066
    """
    import http.cookiejar

    url = f"https://music.youtube.com/watch?v={video_id}"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }
    # Use cookies if available — avoids consent-wall redirects and 429s
    opener: urllib.request.OpenerDirector | None = None
    if cookies and cookies.exists():
        try:
            jar = http.cookiejar.MozillaCookieJar(str(cookies))
            jar.load(ignore_discard=True, ignore_expires=True)
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(jar)
            )
        except Exception:
            log.debug("Failed to load cookies for YTM redirect", exc_info=True)
    fetch = opener.open if opener else urllib.request.urlopen
    for attempt in range(1, 4):
        req = urllib.request.Request(url, headers=headers)
        try:
            with fetch(req, timeout=15) as resp:
                html = resp.read().decode("utf-8", errors="replace")
            m = re.search(r'<link rel="canonical" href="[^"]*[?&]v=([^"&]+)"', html)
            if m and m.group(1) != video_id:
                return m.group(1)
            return None  # canonical points to self — no redirect available
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                wait = 2 * attempt
                log.debug("%s: YTM redirect 429, retrying in %ds", video_id, wait)
                time.sleep(wait)
                continue
            log.debug("YTM redirect lookup failed for %s (HTTP %d)", video_id, e.code)
            return None
        except Exception:
            log.debug("YTM redirect lookup failed for %s", video_id, exc_info=True)
            return None
    return None


def _download_redirected(
    opts: dict,
    alt_id: str,
    original_id: str,
    workdir: Path,
    fmt: str,
) -> tuple[Path, dict]:
    """Download the redirected video and rename the output to the *original* ID.

    The rest of the pipeline (state DB, tagging) keys on the original video_id
    that appears in the playlist, so the file on disk must keep that name.
    """
    alt_url = f"https://music.youtube.com/watch?v={alt_id}"
    # Override outtmpl so the file is saved under the *alternative* ID first
    redirect_opts = {**opts, "outtmpl": str(workdir / "%(id)s.%(ext)s")}
    try:
        with YoutubeDL(redirect_opts) as ydl:
            info = ydl.extract_info(alt_url, download=True)
    except DownloadError as e:
        raise DownloadFailure(classify_error(str(e)), str(e)) from e
    reqs = info.get("requested_downloads") or []
    path = Path(reqs[0]["filepath"]) if reqs else None
    if not path or not path.exists():
        found = sorted(workdir.glob(f"{alt_id}.*"))
        path = found[0] if found else None
    if not path or path.suffix != f".{fmt}":
        raise DownloadFailure("other", f"unexpected output file after redirect: {path}")
    # Rename to the original video_id so the pipeline stays consistent
    dest = workdir / f"{original_id}{path.suffix}"
    if path != dest:
        path.rename(dest)
    return dest, info
