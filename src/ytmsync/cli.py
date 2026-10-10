from __future__ import annotations

import dataclasses
import logging
import sys

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.table import Table

from . import __version__
from .config import DEFAULT_CONFIG, Settings, cache_dir, config_path, load_settings
from .doctor import run_checks
from .httpcache import HttpCache
from .musicbrainz import MusicBrainz
from .pipeline import Ctx
from .sources import Source, parse_source
from .state import Db
from .syncer import Report, Syncer, SyncOptions
from .ytdl import EnumerationError, enumerate_playlist
from .ytm import YTMeta

console = Console()


def common_options(f):
    opts = [
        click.option(
            "-L",
            "--library",
            "library_dir",
            type=click.Path(file_okay=False),
            default=None,
            help="Music library folder.",
        ),
        click.option(
            "-f",
            "--format",
            "format",
            type=click.Choice(["opus", "m4a", "mp3"]),
            default=None,
        ),
        click.option(
            "--layout", type=click.Choice(["library", "playlist"]), default=None
        ),
        click.option(
            "--template",
            default=None,
            help="Path template, e.g. '{albumartist}/{album}/{track:02d} {title}'.",
        ),
        click.option("-j", "--threads", type=int, default=None),
        click.option(
            "--cookies",
            type=click.Path(dir_okay=False),
            default=None,
            help="Netscape cookies.txt (private playlists).",
        ),
        click.option(
            "--cookies-from-browser",
            "cookies_from_browser",
            default=None,
            help="e.g. firefox or chrome:Profile 1",
        ),
        click.option(
            "--js-runtime",
            "js_runtime",
            default=None,
            help="deno | node | bun | quickjs, or NAME=/path",
        ),
        click.option(
            "--sleep",
            "sleep_interval",
            type=float,
            default=None,
            help="Seconds between yt-dlp requests.",
        ),
        click.option("--lyrics/--no-lyrics", default=None),
        click.option("--lrc/--no-lrc", default=None, help="Write sidecar .lrc files."),
        click.option("--embed-lyrics/--no-embed-lyrics", "embed_lyrics", default=None),
        click.option("--m3u/--no-m3u", default=None),
        click.option(
            "--musicbrainz/--no-musicbrainz",
            default=None,
            help="Add MusicBrainz ids, ISRC, disc numbers (needs --contact).",
        ),
        click.option(
            "--contact",
            default=None,
            help="Your e-mail or URL, sent to MusicBrainz in the User-Agent.",
        ),
    ]
    for o in reversed(opts):
        f = o(f)
    return f


def _open(kw: dict) -> tuple[Settings, Db, Ctx, Syncer]:
    try:
        s = load_settings(kw)
    except (ValueError, TypeError) as e:
        raise click.UsageError(str(e))
    s.library_dir.mkdir(parents=True, exist_ok=True)
    db = Db(s.state_dir / "state.db")
    cache = HttpCache(cache_dir() / "http_cache.sqlite")
    ctx = Ctx(
        s, YTMeta(), cache, mb=MusicBrainz(s.contact, cache) if s.musicbrainz else None
    )
    return (
        s,
        db,
        ctx,
        Syncer(s, db, ctx, console=console, enumerate_fn=enumerate_playlist),
    )


def _sources(args, ytm: YTMeta, allow_video: bool) -> list[Source]:
    out = []
    for a in args:
        try:
            src = parse_source(a)
        except ValueError as e:
            raise click.BadParameter(str(e))
        if src.kind == "album_browse":
            pid = ytm.resolve_album_browse(src.id)
            if not pid:
                raise click.ClickException(f"Could not resolve album {src.id}")
            src = Source("playlist", pid)
        if src.kind == "video" and not allow_video:
            raise click.UsageError("single-video URLs are only valid for `download`")
        out.append(src)
    return out


def print_report(rep: Report, dry_run: bool) -> int:
    if dry_run and rep.plan:
        p = rep.plan
        console.print("[bold]Dry run — nothing was changed.[/bold]")
        console.print(
            f"would download {len(p.download)}, copy {len(p.copy)}, move {len(rep.planned_moves)}, delete {len(p.delete)}"
        )
        for v in p.download[:20]:
            console.print(f"  [green]+[/green] {v}")
        for v, _, rel in p.delete[:20]:
            console.print(f"  [red]-[/red] {rel}")
        for _, _, old, new in rep.planned_moves[:20]:
            console.print(f"  [yellow]→[/yellow] {old}  ⇒  {new}")
    else:
        t = Table(show_header=False, box=None)
        for label, n in (
            ("added", rep.added),
            ("copied", rep.copied),
            ("moved", rep.moved),
            ("removed", rep.removed),
            ("lyrics upgraded", rep.lyrics_upgraded),
            ("re-tagged", rep.retagged),
        ):
            if n:
                t.add_row(label, str(n))
        console.print(t if t.row_count else "Everything is up to date.")
    if rep.unavailable:
        console.print(
            f"[yellow]{len(rep.unavailable)} unavailable (private/deleted/region-locked) — skipped.[/yellow]"
        )
    for vid, err in rep.failed[:20]:
        console.print(f"[red]failed[/red] {vid}: {err}")
    for pid, err in rep.playlist_errors.items():
        console.print(f"[red]playlist {pid} skipped:[/red] {err}")
    return 0 if rep.ok else 1


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__)
@click.option("-v", "--verbose", count=True, help="-v info, -vv debug")
@click.option("-q", "--quiet", is_flag=True)
def cli(verbose: int, quiet: bool):
    """Download and sync YouTube Music playlists (yt-dlp engine)."""
    level = (
        logging.ERROR
        if quiet
        else [logging.WARNING, logging.INFO, logging.DEBUG][min(verbose, 2)]
    )
    logging.basicConfig(
        level=level,
        handlers=[RichHandler(console=Console(stderr=True), show_path=False)],
        format="%(message)s",
        force=True,
    )


@cli.command()
@click.argument("sources", nargs=-1, required=True)
@common_options
def add(sources, **kw):
    """Subscribe to playlists/albums (downloads nothing)."""
    s, db, ctx, _ = _open(kw)
    for src in _sources(sources, ctx.ytm, allow_video=False):
        try:
            info = enumerate_playlist(s, src)
        except EnumerationError as e:
            raise click.ClickException(str(e))
        db.upsert_playlist(src.id, info.title, src.url, subscribed=True)
        console.print(
            f"subscribed: [bold]{info.title}[/bold] ({len(info.entries)} tracks) [{src.id}]"
        )


@cli.command("list")
@common_options
def list_(**kw):
    """Show subscriptions."""
    _, db, _, _ = _open(kw)
    members = db.members_all()
    t = Table("Title", "ID", "Tracks", "Last synced")
    for r in db.playlists():
        t.add_row(
            r["title"],
            r["playlist_id"],
            str(len(members.get(r["playlist_id"], []))),
            r["last_synced_at"] or "never",
        )
    console.print(
        t
        if t.row_count
        else "No subscriptions yet. Use `ytmsync add URL` or `ytmsync sync URL`."
    )


@cli.command()
@click.argument("source")
@click.option(
    "--delete-files", is_flag=True, help="Also delete files no other playlist needs."
)
@common_options
def remove(source, delete_files, **kw):
    """Unsubscribe. Files are kept (detached) unless --delete-files."""
    _, db, _, syncer = _open(kw)
    try:
        pid = parse_source(source).id
    except ValueError:
        pid = next(
            (
                r["playlist_id"]
                for r in db.playlists()
                if r["title"].casefold() == source.casefold()
            ),
            None,
        )
    if pid is None or pid not in {r["playlist_id"] for r in db.playlists()}:
        raise click.ClickException(f"Not subscribed: {source}")
    db.unsubscribe(pid, pin_files=not delete_files)
    n = syncer.prune().removed if delete_files else 0
    console.print(
        f"unsubscribed {pid}"
        + (f"; deleted {n} file(s)" if delete_files else "; files kept")
    )


@cli.command()
@click.argument("sources", nargs=-1)
@click.option("--dry-run", is_flag=True)
@click.option("--no-delete", is_flag=True, help="Never delete files.")
@click.option("--yes", is_flag=True, help="Confirm large removals.")
@click.option(
    "--allow-empty", is_flag=True, help="Accept a playlist that is now empty."
)
@click.option("--refresh-lyrics", is_flag=True)
@click.option("--refresh-tags", is_flag=True)
@click.option("--retry-unavailable", is_flag=True)
@click.option("--no-retry-failed", is_flag=True)
@common_options
def sync(
    sources,
    dry_run,
    no_delete,
    yes,
    allow_empty,
    refresh_lyrics,
    refresh_tags,
    retry_unavailable,
    no_retry_failed,
    **kw,
):
    """Sync subscribed playlists (or the given ones, which get subscribed)."""
    _, db, ctx, syncer = _open(kw)
    srcs = (
        _sources(sources, ctx.ytm, allow_video=False)
        if sources
        else [Source("playlist", r["playlist_id"]) for r in db.playlists()]
    )
    if not srcs:
        raise click.ClickException(
            "No subscriptions. Pass a playlist URL, or use `ytmsync add`."
        )
    opts = SyncOptions(
        dry_run=dry_run,
        delete=not no_delete,
        yes=yes,
        allow_empty=allow_empty,
        refresh_lyrics=refresh_lyrics,
        refresh_tags=refresh_tags,
        retry_unavailable=retry_unavailable,
        retry_failed=not no_retry_failed,
    )
    rc = print_report(syncer.run(srcs, opts), dry_run)
    if rc:
        click.get_current_context().exit(rc)


@cli.command()
@click.argument("sources", nargs=-1, required=True)
@common_options
def download(sources, **kw):
    """One-shot download; files are pinned and nothing is ever deleted."""
    _, _, ctx, syncer = _open(kw)
    rc = print_report(
        syncer.run(
            _sources(sources, ctx.ytm, allow_video=True),
            SyncOptions(pin=True, delete=False),
        ),
        False,
    )
    if rc:
        click.get_current_context().exit(rc)


@cli.command()
@click.option("--force", is_flag=True, help="Re-fetch lyrics for every track.")
@common_options
def lyrics(force, **kw):
    """Fetch/upgrade lyrics for the existing library only."""
    *_, syncer = _open({**kw, "lyrics": True})
    rep = syncer.lyrics_only(SyncOptions(refresh_lyrics=force))
    console.print(f"lyrics upgraded: {rep.lyrics_upgraded}")


@cli.command()
@click.option("--offline", is_flag=True, help="Skip network checks.")
@common_options
def doctor(offline, **kw):
    """Check the environment."""
    try:
        s = load_settings(kw)
    except (ValueError, TypeError) as e:
        raise click.UsageError(str(e))
    t, bad = Table("Check", "", "Detail"), False
    for c in run_checks(s, offline):
        t.add_row(
            c.name,
            {
                "ok": "[green]✓[/green]",
                "warn": "[yellow]![/yellow]",
                "fail": "[red]✗[/red]",
            }[c.status],
            c.detail + (f"\n[dim]{c.hint}[/dim]" if c.hint else ""),
        )
        bad |= c.status == "fail"
    console.print(t)
    if bad:
        click.get_current_context().exit(1)


@cli.group()
def config():
    """Configuration file helpers."""


@config.command("path")
def config_path_cmd():
    click.echo(config_path())


@config.command("init")
@click.option("--force", is_flag=True)
def config_init(force):
    p = config_path()
    if p.exists() and not force:
        raise click.ClickException(f"{p} already exists (use --force to overwrite)")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(DEFAULT_CONFIG, encoding="utf-8")
    click.echo(f"wrote {p}")


@config.command("show")
@common_options
def config_show(**kw):
    try:
        s = load_settings(kw)
    except (ValueError, TypeError) as e:
        raise click.UsageError(str(e))
    for f in dataclasses.fields(s):
        click.echo(f"{f.name} = {getattr(s, f.name)!r}")
    click.echo(f"path_template = {s.path_template!r}")


def main() -> None:
    try:
        rc = cli.main(standalone_mode=False)
    except click.exceptions.Abort:
        click.echo("Interrupted.", err=True)
        rc = 130
    except click.ClickException as e:
        e.show()
        rc = e.exit_code
    sys.exit(rc or 0)
