"""`ytmsync doctor`: fast, actionable environment checks."""

from __future__ import annotations

import datetime as dt
import http.cookiejar
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass

import requests

from .config import Settings
from .ytdl import detect_js_runtimes

ENCODER_FOR = {"opus": "libopus", "m4a": "aac", "mp3": "libmp3lame"}
AUTH_COOKIES = {"SAPISID", "__Secure-3PAPISID", "LOGIN_INFO", "__Secure-1PSID", "SID"}


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail
    detail: str
    hint: str = ""


def check_python() -> Check:
    v = sys.version_info
    return Check(
        "Python",
        "ok" if v >= (3, 11) else "fail",
        f"{v.major}.{v.minor}.{v.micro}",
        "" if v >= (3, 11) else "Python 3.11+ required",
    )


def ytdlp_age_days(version: str, today: dt.date | None = None) -> int | None:
    m = re.match(r"(\d{4})\.(\d{2})\.(\d{2})", version)
    if not m:
        return None
    return ((today or dt.date.today()) - dt.date(*map(int, m.groups()))).days


def check_ytdlp() -> Check:
    try:
        from yt_dlp.version import __version__ as v
    except Exception as e:
        return Check("yt-dlp", "fail", f"not importable: {e}", "pip install -U yt-dlp")
    age = ytdlp_age_days(v)
    if age is not None and age > 45:
        return Check(
            "yt-dlp",
            "warn",
            f"{v} ({age} days old)",
            "YouTube changes often: pip install -U 'yt-dlp[default,deno]'",
        )
    return Check("yt-dlp", "ok", v)


def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout


def check_ffmpeg(s: Settings) -> list[Check]:
    out = []
    for exe in ("ffmpeg", "ffprobe"):
        p = shutil.which(exe)
        out.append(
            Check(exe, "ok", p)
            if p
            else Check(
                exe,
                "fail",
                "not found on PATH",
                "install ffmpeg (apt/brew/winget install ffmpeg)",
            )
        )
    if shutil.which("ffmpeg"):
        enc = ENCODER_FOR[s.format]
        has = enc in _run(["ffmpeg", "-hide_banner", "-encoders"])
        out.append(
            Check(
                f"ffmpeg encoder {enc}",
                "ok" if has else "warn",
                "available" if has else "missing",
                ""
                if has
                else f"needed only if YouTube offers no native {s.format} stream (then a transcode is required)",
            )
        )
    return out


def check_js() -> Check:
    rt = detect_js_runtimes()
    if rt:
        return Check("JS runtime", "ok", ", ".join(rt))
    return Check(
        "JS runtime",
        "fail",
        "none found (YouTube downloads will fail)",
        "pip install 'yt-dlp[deno]'  — or install Deno >=2.3, Node >=22, or Bun >=1.2.11",
    )


def check_library(s: Settings) -> Check:
    try:
        s.tmp_dir.mkdir(parents=True, exist_ok=True)
        probe = s.tmp_dir / ".write-test"
        probe.write_text("x")
        probe.unlink()
        return Check("Library dir", "ok", str(s.library_dir))
    except OSError as e:
        return Check(
            "Library dir",
            "fail",
            f"{s.library_dir}: {e}",
            "check the path and permissions",
        )


def check_cookies(s: Settings) -> Check | None:
    if s.cookies_from_browser:
        return Check("Cookies", "ok", f"from browser: {s.cookies_from_browser}")
    if not s.cookies:
        return Check("Cookies", "ok", "none configured (public playlists only)")
    if not s.cookies.exists():
        return Check("Cookies", "fail", f"{s.cookies} not found")
    jar = http.cookiejar.MozillaCookieJar(str(s.cookies))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (http.cookiejar.LoadError, OSError) as e:
        return Check(
            "Cookies",
            "fail",
            f"not a valid Netscape cookies.txt: {e}",
            "export in Netscape format",
        )
    yt = [c for c in jar if "youtube.com" in c.domain]
    names = {c.name for c in yt}
    if not yt:
        return Check(
            "Cookies",
            "fail",
            "no youtube.com cookies in file",
            "export cookies while on music.youtube.com",
        )
    if not names & AUTH_COOKIES:
        return Check(
            "Cookies",
            "warn",
            "no login cookies found",
            "you were probably logged out when exporting",
        )
    if all(c.is_expired() for c in yt if c.name in AUTH_COOKIES):
        return Check(
            "Cookies",
            "warn",
            "login cookies expired",
            "re-export (tip: export from a private window, then close it — YouTube rotates cookies of open sessions)",
        )
    return Check("Cookies", "ok", f"{len(yt)} youtube.com cookies, login present")


def check_musicbrainz(s: Settings) -> Check | None:
    if not s.musicbrainz:
        return None
    if not s.contact:
        return Check(
            "MusicBrainz",
            "fail",
            "enabled but no contact set",
            "pass --contact you@example.com (required by MusicBrainz)",
        )
    return Check(
        "MusicBrainz", "ok", f"enabled, contact {s.contact} (1 request/second)"
    )


def check_network(s: Settings | None = None) -> list[Check]:
    out = []
    targets = [
        ("music.youtube.com", "https://music.youtube.com"),
        ("lrclib.net", "https://lrclib.net"),
    ]
    if s and s.musicbrainz:
        targets.append(("musicbrainz.org", "https://musicbrainz.org/ws/2"))
    for name, url in targets:
        try:
            r = requests.head(url, timeout=8, allow_redirects=True)
            out.append(Check(f"Network: {name}", "ok", f"HTTP {r.status_code}"))
        except requests.RequestException as e:
            out.append(
                Check(
                    f"Network: {name}",
                    "fail",
                    type(e).__name__,
                    "check connectivity / proxy / firewall",
                )
            )
    return out


def run_checks(s: Settings, offline: bool = False) -> list[Check]:
    checks = [
        check_python(),
        check_ytdlp(),
        *check_ffmpeg(s),
        check_js(),
        check_library(s),
        check_cookies(s),
        check_musicbrainz(s),
    ]
    if not offline:
        checks += check_network(s)
    return [c for c in checks if c]
