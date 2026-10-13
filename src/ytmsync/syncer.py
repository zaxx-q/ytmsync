"""Sync orchestration. Rules: workers only build files in a temp dir; THIS module (main thread) is the only writer of
the library and of tracks/files/memberships rows."""

from __future__ import annotations

import json
import logging
import os
import shutil
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
)

from .config import Settings
from .lyrics.chain import RANK, fetch_lyrics, lyrics_kind
from .m3u import write_m3u
from .metadata import fetch_cover
from .models import LyricsResult, TrackMeta
from .paths import disambiguate, is_playlist_scoped, render_path
from .pipeline import Ctx, JobResult, process_track, write_sidecar
from .plan import FileRow, Plan, check_removals, make_plan, needs_move
from .sources import Source
from .state import Db
from .tagging import existing_lyrics, write_lyrics_only, write_tags
from .ytdl import (
    _UNAVAILABLE_TITLE,
    EnumerationError,
    PlaylistEntry,
    PlaylistInfo,
    enumerate_playlist,
    fetch_unavailable_title_artist,
)

log = logging.getLogger("ytmsync.sync")


@dataclass
class SyncOptions:
    dry_run: bool = False
    delete: bool = True
    yes: bool = False
    allow_empty: bool = False
    refresh_lyrics: bool = False
    refresh_tags: bool = False
    retry_unavailable: bool = False
    retry_failed: bool = True
    pin: bool = False  # `download`: pin files + do not subscribe


@dataclass
class Report:
    added: int = 0
    copied: int = 0
    moved: int = 0
    removed: int = 0
    lyrics_upgraded: int = 0
    retagged: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    unavailable_details: dict[str, tuple[str, str]] = field(default_factory=dict)
    playlist_errors: dict[str, str] = field(default_factory=dict)
    plan: Plan | None = None
    planned_moves: list[tuple[str, str, str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed and not self.playlist_errors


def prune_empty_dirs(start: Path, stop: Path) -> None:
    d = start
    while d != stop and stop in d.parents:
        try:
            d.rmdir()  # only succeeds when empty
        except OSError:
            break
        d = d.parent


class Syncer:
    def __init__(
        self,
        s: Settings,
        db: Db,
        ctx: Ctx,
        *,
        console: Console | None = None,
        enumerate_fn: Callable = enumerate_playlist,
        process_fn: Callable = process_track,
        lyrics_fn: Callable = fetch_lyrics,
    ):
        self.s, self.db, self.ctx = s, db, ctx
        self.console = console or Console()
        self.enumerate_fn, self.process_fn, self.lyrics_fn = (
            enumerate_fn,
            process_fn,
            lyrics_fn,
        )
        self.lib = s.library_dir
        self.scoped = is_playlist_scoped(s.path_template)
        self._titles: dict[str, str] = {}
        self._pos: dict[tuple[str, str], int] = {}
        self._entry_meta: dict[str, tuple[str, str]] = {}

    def _resolve_track_info(
        self, vid: str, meta: TrackMeta | None = None
    ) -> tuple[str, str]:
        if meta and meta.video_id == vid:
            title = meta.title or ""
            artist = ", ".join(meta.artists) if meta.artists else ""
            if title and not _UNAVAILABLE_TITLE.match(title):
                return title, artist
        entry_title, entry_artist = self._entry_meta.get(vid, ("", ""))
        if entry_title and not _UNAVAILABLE_TITLE.match(entry_title):
            return entry_title, entry_artist
        fetched_title, fetched_artist = fetch_unavailable_title_artist(
            vid, self.s, getattr(self.ctx, "http", None)
        )
        if fetched_title and not _UNAVAILABLE_TITLE.match(fetched_title):
            self.db.set_track_failed(
                vid,
                "unavailable",
                "unavailable",
                meta_json={
                    "video_id": vid,
                    "title": fetched_title,
                    "artists": [fetched_artist] if fetched_artist else [],
                },
            )
            return fetched_title, fetched_artist
        row = self.db.get_track(vid)
        if row and row["meta_json"]:
            try:
                mj = json.loads(row["meta_json"])
                title = mj.get("title") or ""
                artists = mj.get("artists") or []
                artist = (
                    ", ".join(artists)
                    if isinstance(artists, list)
                    else str(artists or "")
                )
                if title and not _UNAVAILABLE_TITLE.match(title):
                    return title, artist
            except Exception:
                pass
        return entry_title, entry_artist

    # ------------------------------------------------------------------ path helpers
    def _base_rel(self, meta: TrackMeta, scope: str, ext: str) -> str:
        artists = meta.artists or ["Unknown Artist"]
        aa = meta.album_artists or artists
        v = {
            "artist": artists[0],
            "artists": ", ".join(artists),
            "albumartist": aa[0],
            "album": meta.album or "Unknown Album",
            "title": meta.title,
            "track": meta.track_no or 0,
            "tracktotal": meta.track_total or 0,
            "disc": meta.disc_no or 0,
            "disctotal": meta.disc_total or 0,
            "year": meta.year or 0,
            "video_id": meta.video_id,
            "playlist": self._titles.get(scope, "Singles"),
            "playlist_index": self._pos.get((scope, meta.video_id), 0),
            "playlist_id": scope,
        }
        return render_path(self.s.path_template, v, ext)

    def _resolve(self, rel: str, vid: str, scope: str) -> str:
        """Never overwrite another track's file, nor a file we do not own."""
        owner = self.db.path_owner(rel)
        if owner == (vid, scope):
            return rel
        if owner is not None or (self.lib / rel).exists():
            return disambiguate(rel, vid)
        return rel

    def _meta(self, vid: str) -> TrackMeta | None:
        row = self.db.get_track(vid)
        return (
            TrackMeta.from_json(json.loads(row["meta_json"]))
            if row and row["meta_json"]
            else None
        )

    # ------------------------------------------------------------------ enumeration + safety rails
    def _enumerate(
        self, sources: list[Source], opts: SyncOptions, rep: Report
    ) -> dict[str, PlaylistInfo]:
        infos: dict[str, PlaylistInfo] = {}
        old = self.db.members_all(subscribed_only=False)
        for src in sources:
            try:
                if src.kind == "video":
                    info = PlaylistInfo(
                        "singles", "Singles", [PlaylistEntry(src.id, "", 1)]
                    )
                else:
                    info = self.enumerate_fn(self.s, src)
            except EnumerationError as e:
                rep.playlist_errors[src.id] = str(e)
                continue
            if src.kind == "playlist" and opts.delete:
                msg = check_removals(
                    {v for _, v in old.get(info.playlist_id, [])},
                    {e.video_id for e in info.entries},
                    allow_empty=opts.allow_empty,
                    yes=opts.yes,
                )
                if msg:
                    rep.playlist_errors[info.playlist_id] = msg
                    continue  # keep the old membership => nothing gets deleted because of this playlist
            infos[info.playlist_id] = info
        return infos

    # ------------------------------------------------------------------ main entry
    def run(self, sources: list[Source], opts: SyncOptions) -> Report:
        s, db, rep = self.s, self.db, Report()
        s.tmp_dir.mkdir(parents=True, exist_ok=True)
        for stale in s.tmp_dir.iterdir():  # leftovers of an interrupted run
            shutil.rmtree(stale, ignore_errors=True)

        infos = self._enumerate(sources, opts, rep)
        self._entry_meta = {
            e.video_id: (e.title, getattr(e, "artist", ""))
            for i in infos.values()
            for e in i.entries
        }
        fresh = {
            pid: [(e.position, e.video_id) for e in i.entries]
            for pid, i in infos.items()
        }
        merged = {
            **db.members_all(),
            **fresh,
        }  # DB memberships overlaid with this run's fresh ones
        self._titles = {
            **{r["playlist_id"]: r["title"] for r in db.playlists(False)},
            **{p: i.title for p, i in infos.items()},
        }
        self._pos = {}
        for pid, items in merged.items():
            for pos, vid in items:
                self._pos.setdefault(
                    (pid, vid), pos
                )  # first position wins for {playlist_index}
        unavailable_now = frozenset(
            e.video_id for i in infos.values() for e in i.entries if not e.available
        )

        plan = make_plan(
            merged,
            db.files_all(),
            db.track_status_all(),
            self.scoped,
            lambda rel: (self.lib / rel).exists(),
            unavailable_now=unavailable_now,
            retry_failed=opts.retry_failed,
            retry_unavailable=opts.retry_unavailable,
            delete=opts.delete,
        )
        rep.plan = plan
        rep.unavailable = list(plan.skipped_unavailable)
        for vid in rep.unavailable:
            rep.unavailable_details[vid] = self._resolve_track_info(vid)
        rep.planned_moves = self._plan_moves(
            db.files_all(), {d[:2] for d in plan.delete}
        )

        if opts.dry_run:
            return rep

        try:
            # ---- apply -------------------------------------------------------------------------------------
            for pid, info in infos.items():
                db.upsert_playlist(
                    pid,
                    info.title,
                    Source("playlist", pid).url,
                    subscribed=not opts.pin,
                )
                db.replace_memberships(pid, fresh[pid])
            for vid in unavailable_now:
                if db.track_status_all().get(vid) != "ok":
                    db.set_track_failed(
                        vid, "unavailable", "listed as private/deleted in the playlist"
                    )

            for vid, scope, rel in plan.delete:
                self._remove_file(vid, scope, rel)
                rep.removed += 1
            if opts.refresh_tags:
                self._refresh_tags(rep)
            for vid, scope, old_rel, new_rel in self._plan_moves(db.files_all(), set()):
                self._move(vid, scope, old_rel, new_rel)
                rep.moved += 1
            for vid, scope, donor in plan.copy:
                if self._copy(vid, scope, donor):
                    rep.copied += 1

            just_done = self._download_all(plan, opts, rep)
            if s.lyrics:
                self._lyrics_pass(opts, rep, skip=just_done)
            if s.m3u:
                self._write_playlists(merged)
        finally:
            self._entry_meta.clear()
            shutil.rmtree(
                s.tmp_dir, ignore_errors=True
            )  # also on Ctrl-C: no half-built files linger
        return rep

    # ------------------------------------------------------------------ file operations
    def _abs(self, rel: str) -> Path:
        return self.lib / rel

    def _remove_file(self, vid: str, scope: str, rel: str) -> None:
        p = self._abs(rel)
        p.unlink(missing_ok=True)
        p.with_suffix(".lrc").unlink(missing_ok=True)
        prune_empty_dirs(p.parent, self.lib)
        self.db.delete_file_row(vid, scope)

    def _plan_moves(
        self, files: dict[tuple[str, str], FileRow], skip: set[tuple[str, str]]
    ) -> list[tuple[str, str, str, str]]:
        out = []
        for (vid, scope), f in sorted(files.items()):
            if (vid, scope) in skip or not self._abs(f.path).exists():
                continue
            meta = self._meta(vid)
            if meta is None:
                continue
            base = self._base_rel(meta, scope, Path(f.path).suffix.lstrip("."))
            if needs_move(f.path, base, vid):
                out.append((vid, scope, f.path, self._resolve(base, vid, scope)))
        return out

    def _move(self, vid: str, scope: str, old_rel: str, new_rel: str) -> None:
        src, dst = self._abs(old_rel), self._abs(new_rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
        if src.with_suffix(".lrc").exists():
            os.replace(src.with_suffix(".lrc"), dst.with_suffix(".lrc"))
        prune_empty_dirs(src.parent, self.lib)
        f = self.db.files_all()[(vid, scope)]
        self.db.upsert_file(
            vid, scope, new_rel, dst.suffix.lstrip("."), dst.stat().st_size, f.pinned
        )

    def _copy(self, vid: str, scope: str, donor_scope: str) -> bool:
        files = self.db.files_all()
        donor, meta = files.get((vid, donor_scope)), self._meta(vid)
        if not donor or not meta:
            return False
        src = self._abs(donor.path)
        rel = self._resolve(
            self._base_rel(meta, scope, src.suffix.lstrip(".")), vid, scope
        )
        dst = self._abs(rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        if src.with_suffix(".lrc").exists():
            shutil.copy2(src.with_suffix(".lrc"), dst.with_suffix(".lrc"))
        self.db.upsert_file(
            vid, scope, rel, dst.suffix.lstrip("."), dst.stat().st_size, donor.pinned
        )
        return True

    # ------------------------------------------------------------------ downloads
    def _download_all(self, plan: Plan, opts: SyncOptions, rep: Report) -> set[str]:
        done: set[str] = set()
        if not plan.download:
            return done
        with (
            Progress(
                TextColumn("{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                console=self.console,
                transient=True,
            ) as prog,
            ThreadPoolExecutor(self.s.threads) as pool,
        ):
            task = prog.add_task("Downloading", total=len(plan.download))
            futs = {
                pool.submit(self.process_fn, vid, self.ctx): vid
                for vid in plan.download
            }
            try:
                for fut in as_completed(futs):
                    res: JobResult = fut.result()  # process_fn never raises
                    self._finalize(res, plan.scopes[res.video_id], opts, rep)
                    done.add(res.video_id)
                    prog.advance(task)
            except KeyboardInterrupt:
                pool.shutdown(wait=False, cancel_futures=True)
                raise
        return done

    def _finalize(
        self, res: JobResult, scopes: list[str], opts: SyncOptions, rep: Report
    ) -> None:
        """Main thread only: move the finished file(s) into the library and record them."""
        if not res.ok:
            self.db.set_track_failed(
                res.video_id,
                res.status,
                res.error or "",
                meta_json=res.meta.to_json() if res.meta else None,
            )
            if res.status == "failed":
                rep.failed.append((res.video_id, res.error or ""))
            else:
                if res.video_id not in rep.unavailable:
                    rep.unavailable.append(res.video_id)
                rep.unavailable_details[res.video_id] = self._resolve_track_info(
                    res.video_id, res.meta
                )
            log.warning("%s: %s", res.video_id, res.error)
            return
        meta, audio = res.meta, res.audio
        ext = audio.suffix.lstrip(".")
        for i, scope in enumerate(scopes):
            rel = self._resolve(self._base_rel(meta, scope, ext), res.video_id, scope)
            dst = self._abs(rel)
            dst.parent.mkdir(parents=True, exist_ok=True)
            last = i == len(scopes) - 1
            (os.replace if last else shutil.copy2)(audio, dst)
            if res.sidecar:
                (os.replace if last else shutil.copy2)(
                    res.sidecar, dst.with_suffix(".lrc")
                )
            self.db.upsert_file(
                res.video_id, scope, rel, ext, dst.stat().st_size, opts.pin
            )
        kind = lyrics_kind(res.lyrics) if self.s.lyrics else None
        self.db.set_track_ok(
            res.video_id,
            meta.to_json(),
            kind,
            res.lyrics.source if res.lyrics else None,
        )
        shutil.rmtree(audio.parent, ignore_errors=True)
        rep.added += 1

    # ------------------------------------------------------------------ lyrics pass (upgrade plain/none -> synced later)
    def _files_of(self, vid: str) -> list[tuple[str, Path]]:
        return [
            (sc, self._abs(f.path))
            for (v, sc), f in self.db.files_all().items()
            if v == vid and self._abs(f.path).exists()
        ]

    def _apply_lyrics(self, vid: str, meta: TrackMeta, res: LyricsResult) -> None:
        for _, path in self._files_of(vid):
            write_lyrics_only(path, res)
            side = path.with_suffix(".lrc")
            if self.s.lrc and res.kind == "synced" and not res.instrumental:
                write_sidecar(side, meta, res)
            else:
                side.unlink(missing_ok=True)

    def _lyrics_pass(self, opts: SyncOptions, rep: Report, skip: set[str]) -> None:
        rows = [
            r
            for r in self.db.tracks_needing_lyrics(
                self.s.lyrics_retry_days, opts.refresh_lyrics
            )
            if r["video_id"] not in skip
        ]
        if not rows:
            return

        def work(row):
            meta = TrackMeta.from_json(json.loads(row["meta_json"]))
            return (
                row,
                meta,
                self.lyrics_fn(meta, self.ctx.lyrics_ctx(), self.s.lyrics_providers),
            )

        with ThreadPoolExecutor(self.s.threads) as pool:
            for fut in [pool.submit(work, r) for r in rows]:
                try:
                    row, meta, res = fut.result()
                except Exception as e:  # noqa: BLE001 -- one lyrics task must not abort the pass
                    log.debug("lyrics pass error: %s", e)
                    continue
                old = row["lyrics_kind"] or "none"
                new_rank = 0 if res is None or res.instrumental else RANK[res.kind]
                if res is not None and (
                    new_rank > RANK.get(old, 0)
                    or opts.refresh_lyrics
                    or res.instrumental
                ):
                    self._apply_lyrics(row["video_id"], meta, res)
                    rep.lyrics_upgraded += int(new_rank > RANK.get(old, 0))
                kind = (
                    lyrics_kind(res)
                    if (res is not None or old not in ("synced", "plain"))
                    else old
                )  # never forget lyrics we have
                self.db.set_lyrics_state(
                    row["video_id"], kind, res.source if res else row["lyrics_source"]
                )

    def lyrics_only(self, opts: SyncOptions) -> Report:
        rep = Report()
        self._lyrics_pass(opts, rep, skip=set())
        return rep

    # ------------------------------------------------------------------ refresh tags (re-enrich metadata + cover, keep lyrics)
    def _refresh_tags(self, rep: Report) -> None:
        for vid in sorted({v for (v, _) in self.db.files_all()}):
            old = self._meta(vid)
            new = self.ctx.ytm.enrich(vid)
            if new is None or old is None:
                continue
            if self.ctx.mb:
                new = self.ctx.mb.enrich(new)
            cover = fetch_cover(new, None, self.s, self.ctx.http)
            for _, path in self._files_of(vid):
                lyr = (
                    self.lyrics_fn(new, self.ctx.lyrics_ctx(), self.s.lyrics_providers)
                    if self.s.lyrics
                    else None
                ) or existing_lyrics(path)
                write_tags(
                    path, new, cover, lyr, self.s.artist_join, self.s.embed_lyrics
                )
            row = self.db.get_track(vid)
            self.db.set_track_ok(
                vid, new.to_json(), row["lyrics_kind"], row["lyrics_source"]
            )
            rep.retagged += 1

    # ------------------------------------------------------------------ playlists on disk
    def _write_playlists(self, members: dict[str, list[tuple[int, str]]]) -> None:
        files, taken = self.db.files_all(), set()
        for pid, items in sorted(members.items()):
            entries = []
            for _, vid in sorted(items):
                f = files.get((vid, pid if self.scoped else ""))
                meta = self._meta(vid)
                if f and meta and self._abs(f.path).exists():
                    entries.append(
                        (
                            f.path,
                            f"{', '.join(meta.artists)} - {meta.title}",
                            int(meta.duration_s) if meta.duration_s else None,
                        )
                    )
            write_m3u(self.lib, self._titles.get(pid, pid), entries, taken, pid)

    def prune(self) -> Report:
        """Delete every file no subscribed playlist references (used by `remove --delete-files`)."""
        rep = Report()
        plan = make_plan(
            self.db.members_all(),
            self.db.files_all(),
            {},
            self.scoped,
            lambda rel: True,
        )
        for vid, scope, rel in plan.delete:
            self._remove_file(vid, scope, rel)
            rep.removed += 1
        return rep
