# ytmsync

Download and **sync** YouTube Music playlists to a local music library, using [yt-dlp](https://github.com/yt-dlp/yt-dlp) as the engine.

- **Sync, not just download** – re-running adds new tracks, removes deleted ones, and _moves_ files if you change the naming template (no re-download).
- **Rich tags** – title, all artists, album, track/disc numbers, year, cover art (squared), YouTube Music ID; optional [MusicBrainz](https://musicbrainz.org) IDs + ISRC.
- **Lyrics** – time-synced `.lrc` (embedded _and_ as a sidecar file) with plain-text fallback, from YouTube Music, LRCLIB, Musixmatch/NetEase and creator-uploaded captions.
- **Formats** – `opus` (default, no re-encode), `m4a` (no re-encode when the source is AAC), `mp3`.
- **Safe** – dry-run, refuses to delete your library because of a transient error, never overwrites files it doesn't own.

## Install

### Prerequisites

1. **Python ≥ 3.11**
2. **ffmpeg & ffprobe** with Opus support (`libopus`).
   - Ubuntu / Debian: `sudo apt install ffmpeg`
   - Fedora: `sudo dnf install ffmpeg` (from RPM Fusion)
   - Arch Linux: `sudo pacman -S ffmpeg`
   - macOS: `brew install ffmpeg`
   - Windows: `winget install Gyan.FFmpeg` or `choco install ffmpeg`
3. **JavaScript runtime**: Current `yt-dlp` requires a JS runtime for YouTube's challenge solving. The installation automatically includes Deno via `yt-dlp[default,deno]`. System-installed Deno (≥ 2.3), Bun (≥ 1.2), or Node (≥ 22) are also detected automatically.

### Recommended: standalone CLI tool (`pipx` or `uv`)

Because `ytmsync` is an executable CLI, install it in an isolated environment so its dependencies don't clash with system packages:

```bash
# Using pipx (standard)
pipx install .

# Or using uv
uv tool install .
```

Once installed, `ytmsync` is directly available on your `PATH`. Verify your environment:

```bash
ytmsync doctor
```

### Standard virtual environment

```bash
python -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install .
ytmsync doctor
```

### From Git repository

```bash
pipx install git+https://github.com/your-name/ytmsync.git
# or with uv
uv tool install git+https://github.com/your-name/ytmsync.git
```

## Quick start

```bash
ytmsync sync "https://music.youtube.com/playlist?list=PLxxxxxxxx" -o ~/Music/ytmsync   # first time: subscribes + downloads
ytmsync sync                                                                           # later: sync everything you subscribed to
ytmsync sync --dry-run                                                                 # show what would change, touch nothing
```

Result:

```
~/Music/ytmsync/
├── Artist/Album/01 Title.opus
├── Artist/Album/01 Title.lrc          # synced lyrics (only when synced lyrics exist)
├── _Playlists/My Playlist.m3u8        # one playlist file per subscription, relative paths
└── .ytmsync/state.db                  # sync state (safe to keep with the library)
```

## Commands

| Command                                  | What it does                                                                                           |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------ |
| `ytmsync add SOURCE…`                    | Subscribe to playlists/albums (downloads nothing).                                                     |
| `ytmsync list`                           | Show subscriptions.                                                                                    |
| `ytmsync remove SOURCE [--delete-files]` | Unsubscribe. Files are kept unless `--delete-files`. `SOURCE` can also be the playlist's title.        |
| `ytmsync sync [SOURCE…]`                 | Sync subscriptions, or the given sources (they get subscribed).                                        |
| `ytmsync download SOURCE…`               | One-shot download (also accepts single-video URLs). Files are _pinned_: never deleted by a later sync. |
| `ytmsync lyrics [--force]`               | Only fetch/upgrade lyrics for files already in the library.                                            |
| `ytmsync doctor [--offline]`             | Diagnose your environment.                                                                             |
| `ytmsync config path\|init\|show`        | Locate / create / print the configuration.                                                             |

`SOURCE` = playlist URL, `watch?v=…&list=…` URL, bare playlist id (`PL…`, `OLAK5uy_…`, `LM`), or an album `…/browse/MPREb_…` URL.

**`sync` options:** `--dry-run`, `--no-delete`, `--yes` (confirm large removals), `--allow-empty` (accept a playlist that is now really empty),
`--refresh-lyrics`, `--refresh-tags` (re-fetch metadata/cover, keeps lyrics), `--retry-unavailable`, `--no-retry-failed`.

**Options shared by most commands:** `-o/--output-dir`, `-f/--format [opus|m4a|mp3]`, `--layout [library|playlist]`, `--template`, `-j/--threads`,
`--cookies FILE`, `--cookies-from-browser NAME[:PROFILE]`, `--js-runtime`, `--sleep SECONDS`, `--lyrics/--no-lyrics`, `--lrc/--no-lrc`,
`--embed-lyrics/--no-embed-lyrics`, `--m3u/--no-m3u`, `--musicbrainz/--no-musicbrainz`, `--contact`. `-v`/`-vv` (before the command) increase logging.

Exit codes: `0` ok · `1` finished but something failed · `2` usage/config error · `130` interrupted.

## Private playlists and "Liked Music"

Pass a cookies file or read cookies from your browser:

```bash
ytmsync sync LM --cookies ~/cookies.txt                     # Netscape-format cookies.txt exported from music.youtube.com
ytmsync sync LM --cookies-from-browser firefox              # or "chrome:Profile 1"
```

Tip: YouTube rotates cookies of _open_ sessions. Export from a private/incognito window, then close it. `ytmsync doctor` checks the file.
A cookies file is a credential: keep it private (`chmod 600`), never commit it. A playlist that can't be read is **skipped and reported — nothing is deleted because of it.**

## Layout and naming

`-o` / `--output-dir` selects the root destination directory. `--layout` or `--template` selects the paths below it.

Two presets, or your own template:

| `--layout`          | Template                                             | Duplicates                                               |
| ------------------- | ---------------------------------------------------- | -------------------------------------------------------- |
| `library` (default) | `{albumartist}/{album}/{track:02d} {title}`          | a song in two playlists is stored **once**               |
| `playlist`          | `{playlist}/{playlist_index:03d} {artist} - {title}` | stored once **per playlist** (copied, not re-downloaded) |

Variables: `{artist} {artists} {albumartist} {album} {title} {track} {tracktotal} {disc} {disctotal} {year} {video_id} {playlist} {playlist_index} {playlist_id}`; integers accept format specs like `{track:02d}`.
Changing the template later **moves** existing files on the next `sync`. `{playlist_index}` changes when you reorder a playlist (files get renamed). `--format` applies to _new_ downloads only.

## Lyrics

Default order (`lyrics_providers` in the config): **creator-uploaded YouTube captions** → **LRCLIB** → **Musixmatch/NetEase** (via `syncedlyrics`) → **YouTube Music**.
The first plausible _synced_ result wins; otherwise plain text is embedded. Candidates that don't resemble YouTube's own text, or that don't fit the track length, are rejected.
Tracks that only got plain/no lyrics are retried after 14 days and upgraded to synced when possible. Opus/M4A store LRC text in the lyrics tag; MP3 gets plain `USLT` + timed `SYLT`.
Musixmatch/NetEase are unofficial endpoints and may stop working — remove `syncedlyrics` from `lyrics_providers` if they misbehave.

## MusicBrainz (optional)

```bash
ytmsync sync --musicbrainz --contact you@example.com
```

Adds MusicBrainz IDs, ISRC, disc numbers and a precise release date, and fixes per-disc track numbers on multi-disc albums. It never rewrites title/artist/album text, matches strictly, and is limited to one request per second (so it adds ≈1 s per _new_ track). `--contact` (your e-mail or URL) is mandatory: MusicBrainz requires it in the User-Agent. Apply it to an existing library with `ytmsync sync --refresh-tags --musicbrainz --contact …`.

## Configuration

`ytmsync config init` writes a commented `config.toml` (`ytmsync config path` shows where; override with `$YTMSYNC_CONFIG`). Precedence: command-line flag > config file > default. Keys match the option names (`library_dir`, `format`, `layout`, `template`, `threads`, `cookies`, `lyrics_providers`, `contact`, …). `ytmsync config show` prints the effective values.

## Safety rules

- `sync` refuses to apply a playlist that suddenly returns 0 tracks, or would remove > 50 % (and > 10) of its known tracks, unless you pass `--allow-empty` / `--yes`.
- Files are built in `.ytmsync/tmp` and moved into the library only when complete; Ctrl-C leaves no half-written files.
- An existing file that ytmsync didn't create is never overwritten (the new one gets ` [video_id]` appended).
- Private/deleted/region-locked tracks are skipped and reported; `--retry-unavailable` tries them again.

## Troubleshooting

| Symptom                                                | Try                                                                         |
| ------------------------------------------------------ | --------------------------------------------------------------------------- |
| `Sign in to confirm you're not a bot` / age-restricted | pass `--cookies` or `--cookies-from-browser`                                |
| Nothing downloads, JS-related error                    | `ytmsync doctor`; `pip install -U "yt-dlp[default,deno]"`                   |
| HTTP 429 / throttling                                  | `-j 1 --sleep 2`                                                            |
| Playlist "skipped"                                     | private, or login expired → refresh cookies. Nothing was deleted.           |
| Suddenly failing after weeks                           | `pip install -U "yt-dlp[default,deno]"` (YouTube changes often)             |
| Wrong/missing lyrics for a song                        | `-vv` shows which providers were tried; `ytmsync lyrics --force` re-fetches |

## Legal

Licensed under the [MIT License](LICENSE). Parts of the code are adapted from [Shira](https://github.com/KraXen72/shira) (MIT) and [spotDL](https://github.com/spotDL/spotify-downloader) (MIT); see [`NOTICE`](NOTICE).

Downloading from YouTube may violate its Terms of Service. Use this only for personal backups of content you have the right to keep. Keep cookie files private (`chmod 600`) and never commit them.
