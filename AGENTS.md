# YTMSYNC

**Generated:** 2026-10-10T10:02:53Z
**Commit:** uncommitted

CLI tool to mirror and sync YouTube Music playlists to a local library with embedded/sidecar synced lyrics and rich metadata.
Stack: Python ≥3.11, `yt-dlp[default,deno]`, `ytmusicapi`, `mediafile`, `mutagen`, `pillow`, `click`, `rich`, `sqlite3` (WAL).

## STRUCTURE

```
ytmsync/
├── src/ytmsync/
│   ├── cli.py             # Click command groups, CLI options, report rendering
│   ├── config.py          # Settings dataclass, TOML loader, precedence resolution
│   ├── plan.py            # Pure sync planner (no I/O); removal safety rails
│   ├── syncer.py          # Main thread sync engine (moves, copies, DB writes, deletes)
│   ├── pipeline.py        # Worker thread task (process_track: enrich → dl → tag in tmp/)
│   ├── state.py           # SQLite state manager (<library>/.ytmsync/state.db)
│   ├── tagging.py         # MediaFile + Mutagen tags (YTM_VIDEO_ID, SYLT, lyrics)
│   ├── ytdl.py            # yt-dlp flat enumerator, audio downloader, JS runtime probe
│   ├── ytm.py             # ytmusicapi unauthenticated metadata & lyrics fetcher
│   ├── musicbrainz.py     # Optional strict MBID/ISRC enrichment (1.1s rate limit)
│   ├── doctor.py          # Diagnostic suite (ffmpeg/libopus, JS, cookies, endpoints)
│   ├── cover.py           # Cover upscaling, flat-border crop vs dominant pad
│   └── lyrics/
│       ├── chain.py       # Priority orchestrator (ytmusic → lrclib → synced → captions)
│       ├── lrc.py         # Line-level LRC parser and centisecond formatter
│       ├── match.py       # Bigram-Dice similarity cross-validation against YTM text
│       └── providers/     # Base, lrclib, synced (Musixmatch/NetEase), ytcaptions, ytmusic
└── tests/                 # 165 offline tests; conftest.py provides mock network & stubs
```

## WHERE TO LOOK

| Task                                        | Location                                                     |
| ------------------------------------------- | ------------------------------------------------------------ |
| CLI commands and argument flags             | `src/ytmsync/cli.py`                                         |
| Defaults and TOML config merging            | `src/ytmsync/config.py:load_settings`                        |
| Sync plan calculations & safety thresholds  | `src/ytmsync/plan.py:make_plan`, `check_removals`            |
| File moves, copies, and pruning             | `src/ytmsync/syncer.py:Syncer`                               |
| Audio download & worker temp staging        | `src/ytmsync/pipeline.py:process_track`                      |
| yt-dlp options, format selectors & retries  | `src/ytmsync/ytdl.py:download_audio`, `base_opts`            |
| YTM album track indexing & cover resolution | `src/ytmsync/ytm.py:YTMeta`                                  |
| Custom tag frames (`YTM_VIDEO_ID`, `SYLT`)  | `src/ytmsync/tagging.py:write_tags`                          |
| LRC parser, offsets, and word tag stripping | `src/ytmsync/lyrics/lrc.py:parse_lrc`                        |
| Lyrics cross-validation & coverage rules    | `src/ytmsync/lyrics/match.py`, `src/ytmsync/lyrics/chain.py` |
| MusicBrainz query construction & caching    | `src/ytmsync/musicbrainz.py:MusicBrainz`                     |
| Diagnostics & cookie health checks          | `src/ytmsync/doctor.py:run_checks`                           |
| End-to-end test scenarios & mocks           | `tests/conftest.py`, `tests/test_sync_e2e.py`                |

## CONVENTIONS

- `lyrics/__init__.py` and `lyrics/providers/__init__.py` MUST remain empty to avoid circular imports (`models.py` imports `lyrics.lrc`).
- Artist tagging: `ARTIST` / `ALBUMARTIST` joined by `" & "`; `ARTISTS` / `ALBUMARTISTS` store raw multi-value lists (Picard convention).
- Album track numbering: derived from 1-based index in `get_album` track list, NEVER `trackNumber` field (`0` bug in YTM API).
- Singles/no album: album name is `"<Title> - Single"`, `is_single=True`, track `1/1`.
- File scoping: `(video_id, scope)` in SQLite; `scope=""` for shared library layout, `scope=playlist_id` when template uses `{playlist*}` variables.
- Path state: SQLite stores real relative POSIX paths; never derive filenames from metadata to locate existing files on disk.
- MusicBrainz enrichment is strictly additive: adds MBIDs, ISRC, dates, disc numbers; never alters user-visible title/artist/album text.
- Mediafile quirk: assign `mb_artistids` list only; assigning `mb_artistid` scalar alongside it clobbers the list.

## ANTI-PATTERNS

- NEVER write to library files or execute DB mutations from worker threads; workers build in `.ytmsync/tmp/<video_id>/` and return `JobResult`. Only `Syncer._finalize` writes destination files.
- NEVER raise exceptions from `process_track`; all errors map to `JobResult(ok=False, status=...)` to prevent aborting a sync.
- NEVER overwrite untracked files or files owned by another track; call `disambiguate()` to append ` [<video_id>]`.
- NEVER use YouTube auto-generated captions for lyrics (`subtitles[lang]` with `ext=json3` only; `automatic_captions` forbidden).
- NEVER log raw cookie file paths or cookie contents.

## COMMANDS

```bash
uv run --extra dev pytest -q                  # Run all 165 unit/integration tests offline
uv run ytmsync doctor                         # Inspect ffmpeg, JS runtimes, cookies, endpoints
uv run ytmsync sync <SOURCE> -o <DIR> --dry-run # Preview downloads, copies, moves, and deletions
uv run ruff check                             # Lint codebase
uv build                                      # Build sdist and wheel distributions via Hatchling
```

## KEY CONFIGS

| Tool           | Entry                                | Notes                                                                  |
| -------------- | ------------------------------------ | ---------------------------------------------------------------------- |
| Hatchling      | `pyproject.toml`                     | Build backend, package definition (`src/ytmsync`), dependencies        |
| Runtime Config | `~/.config/ytmsync/config.toml`      | Defaults overridden by `$YTMSYNC_CONFIG` and CLI flags                 |
| Sync DB        | `<library>/.ytmsync/state.db`        | SQLite WAL mode, foreign keys ON; tracks real paths and TrackMeta JSON |
| HTTP Cache     | `~/.cache/ytmsync/http_cache.sqlite` | SQLite WAL cache for LRCLIB and MusicBrainz (30d hit / 1d miss)        |

## NOTES

- YouTube requires a JavaScript runtime in current `yt-dlp` for challenge solving (`deno`, `bun`, or `node ≥22`).
- YouTube rotates open-session browser cookies; advise exporting from private window and closing it immediately.
- Opus streams (`bestaudio[acodec=opus]`) are stream-copied (`-acodec copy`) without re-encoding.
- MusicBrainz requires a contact email/URL in User-Agent (`--contact`); rate-limited to 1 request / 1.1s.
