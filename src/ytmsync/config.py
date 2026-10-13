from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

from platformdirs import user_cache_dir, user_config_dir

from .paths import PRESETS, validate_template

APP = "ytmsync"
FORMATS = ("opus", "m4a", "mp3")


def config_path() -> Path:
    return Path(
        os.environ.get("YTMSYNC_CONFIG") or Path(user_config_dir(APP)) / "config.toml"
    )


def cache_dir() -> Path:
    p = Path(user_cache_dir(APP))
    p.mkdir(parents=True, exist_ok=True)
    return p


@dataclass
class Settings:
    library_dir: Path = Path("~/Music/ytmsync")
    format: str = "opus"
    layout: str = "library"  # key of paths.PRESETS
    template: str | None = None  # overrides layout when set
    threads: int = 3
    sleep_interval: float = 0.0  # seconds between yt-dlp requests (0 = off)
    cookies: Path | None = None  # Netscape cookies.txt
    cookies_from_browser: str | None = None  # "firefox" | "chrome:Profile 1"
    js_runtime: str | None = None  # None = auto-detect; "node" or "node=/usr/bin/node"
    mp3_quality: str = "0"  # ffmpeg VBR scale 0 (best) .. 9
    lyrics: bool = True
    lyrics_providers: list[str] = field(
        default_factory=lambda: ["ytcaptions", "lrclib", "syncedlyrics", "ytmusic"]
    )
    embed_lyrics: bool = True
    lrc: bool = True  # write sidecar .lrc when lyrics are synced
    lyrics_retry_days: int = 14  # re-try plain/none lyrics after this many days
    cover_method: str = "auto"  # auto | crop | pad
    cover_size: int = 1200
    m3u: bool = True
    musicbrainz: bool = False
    contact: str | None = (
        None  # e-mail or URL; MusicBrainz REQUIRES it in the User-Agent
    )
    artist_join: str = " & "

    def __post_init__(self):
        self.library_dir = Path(self.library_dir).expanduser()
        if self.cookies:
            self.cookies = Path(self.cookies).expanduser()
        if self.format not in FORMATS:
            raise ValueError(f"format must be one of {FORMATS}")
        if self.template is None and self.layout not in PRESETS:
            raise ValueError(f"layout must be one of {tuple(PRESETS)}")
        if self.threads < 1:
            raise ValueError("threads must be >= 1")
        validate_template(self.path_template)
        if self.musicbrainz and not self.contact:
            raise ValueError(
                "MusicBrainz lookups need --contact (your e-mail or project URL); MusicBrainz requires it in the User-Agent"
            )

    @property
    def path_template(self) -> str:
        return self.template or PRESETS[self.layout]

    @property
    def state_dir(self) -> Path:
        return self.library_dir / ".ytmsync"

    @property
    def tmp_dir(
        self,
    ) -> Path:  # MUST be inside the library so os.replace() stays on one filesystem
        return self.state_dir / "tmp"


def load_settings(cli_overrides: dict) -> Settings:
    data: dict = {}
    p = config_path()
    if p.exists():
        data = dict(tomllib.loads(p.read_text("utf-8")).get("defaults", {}))
    data.update(
        {k: v for k, v in cli_overrides.items() if v is not None}
    )  # only flags actually passed
    unknown = set(data) - {f.name for f in fields(Settings)}
    if unknown:
        raise ValueError(
            f"Unknown setting(s) in config/CLI: {', '.join(sorted(unknown))}"
        )
    return Settings(**data)


DEFAULT_CONFIG = """\
# ytmsync configuration. CLI flags override these values.
[defaults]
library_dir = "~/Music/ytmsync"
format = "opus"            # opus | m4a | mp3
layout = "library"         # library | playlist
# template = "{albumartist}/{album}/{track:02d} {title}"   # overrides layout
threads = 3
# cookies = "~/cookies.txt"            # needed for private playlists / Liked Music
# cookies_from_browser = "firefox"
lyrics = true
lyrics_providers = ["ytcaptions", "lrclib", "syncedlyrics", "ytmusic"]   # order = priority; unknown names are ignored
# musicbrainz = true                   # MBIDs, ISRC, disc numbers (1 request/second, so it slows big first syncs)
# contact = "you@example.com"          # required when musicbrainz = true
lrc = true                 # sidecar .lrc files
"""
