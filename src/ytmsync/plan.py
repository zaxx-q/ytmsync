"""Pure sync planning: no I/O, no network, no DB. Everything is passed in, so it is trivially unit-testable."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .paths import disambiguate


@dataclass(frozen=True)
class FileRow:
    path: str  # POSIX path relative to the library root
    pinned: bool = False  # pinned files (from `download`, or detached playlists) are never auto-deleted


@dataclass
class Plan:
    download: list[str] = field(
        default_factory=list
    )  # video_ids that need audio fetched
    scopes: dict[str, list[str]] = field(
        default_factory=dict
    )  # for each download: scopes to create afterwards
    copy: list[tuple[str, str, str]] = field(
        default_factory=list
    )  # (video_id, new_scope, donor_scope)
    rescope: list[tuple[str, str, str]] = field(
        default_factory=list
    )  # (video_id, new_scope, donor_scope): doomed donor re-keyed (moved), not copy+delete
    delete: list[tuple[str, str, str]] = field(
        default_factory=list
    )  # (video_id, scope, rel_path)
    skipped_unavailable: list[str] = field(default_factory=list)


def make_plan(
    members: dict[
        str, list[tuple[int, str]]
    ],  # playlist_id -> [(position, video_id)]  (merged: DB overlaid with this run's fresh enumerations)
    files: dict[tuple[str, str], FileRow],  # (video_id, scope) -> row, from the DB
    track_status: dict[str, str],  # video_id -> 'ok' | 'failed' | 'unavailable'
    scoped: bool,  # paths.is_playlist_scoped(template)
    file_exists: Callable[[str], bool],
    *,
    unavailable_now: frozenset[
        str
    ] = frozenset(),  # entries flagged [Private video]/[Deleted video] by the enumerator
    retry_failed: bool = True,
    retry_unavailable: bool = False,
    delete: bool = True,
) -> Plan:
    want: set[tuple[str, str]] = {
        (vid, pid if scoped else "")
        for pid, items in members.items()
        for _, vid in items
    }
    want_by_vid: dict[str, set[str]] = {}
    for v, s in want:
        want_by_vid.setdefault(v, set()).add(s)
    have = {k for k, f in files.items() if file_exists(f.path)}
    plan = Plan()

    for vid, scope in sorted(want):
        if (vid, scope) in have:
            continue
        status = track_status.get(vid)
        if vid in unavailable_now or (
            status == "unavailable" and not retry_unavailable
        ):
            if vid not in plan.skipped_unavailable:
                plan.skipped_unavailable.append(vid)
            continue
        if status == "failed" and not retry_failed:
            continue
        donor = next(
            (k for k in sorted(have) if k[0] == vid), None
        )  # the same song already on disk under another scope
        if donor:
            plan.copy.append((vid, scope, donor[1]))
        else:
            if vid not in plan.download:
                plan.download.append(vid)
            plan.scopes.setdefault(vid, []).append(scope)

    if delete:
        doomed = {
            k: f for k, f in sorted(files.items()) if k not in want and not f.pinned
        }
        rehomed: dict[tuple[str, str], str] = {}

        # A copy whose donor is doomed (layout flip, song switching playlists) is really a move: re-key the
        # donor row instead of deleting the file out from under the copy.
        copies: list[tuple[str, str, str]] = []
        for vid, scope, donor in plan.copy:
            if (vid, donor) in doomed and (vid, donor) not in rehomed:
                plan.rescope.append((vid, scope, donor))
                rehomed[(vid, donor)] = scope
            else:
                copies.append((vid, scope, rehomed.get((vid, donor), donor)))
        plan.copy = copies

        # Wanted-but-skipped tracks (unavailable, failed without retry) cannot be fetched again; re-key a
        # doomed row to a wanted scope so their files survive the re-keying instead of being deleted.
        coming: dict[str, set[str]] = {}
        for v, s in have:
            if (v, s) not in doomed:
                coming.setdefault(v, set()).add(s)
        for v, ss in plan.scopes.items():
            coming.setdefault(v, set()).update(ss)
        for v, s in {(v, s) for v, s, _ in plan.rescope} | {
            (v, s) for v, s, _ in plan.copy
        }:
            coming.setdefault(v, set()).add(s)
        for key, f in doomed.items():
            if not file_exists(f.path):
                continue  # stale row (file already gone): plain delete cleans up the DB
            missing = sorted(want_by_vid.get(key[0], set()) - coming.get(key[0], set()))
            if missing:
                plan.rescope.append((key[0], missing[0], key[1]))
                rehomed[key] = missing[0]
                coming.setdefault(key[0], set()).add(missing[0])

        plan.delete = [
            (vid, scope, f.path)
            for (vid, scope), f in sorted(doomed.items())
            if (vid, scope) not in rehomed
        ]
    return plan


def check_removals(
    old_ids: set[str],
    new_ids: set[str],
    *,
    allow_empty: bool = False,
    yes: bool = False,
) -> str | None:
    """Return a human-readable refusal reason if applying this playlist update looks dangerous, else None."""
    if old_ids and not new_ids and not allow_empty:
        return f"playlist now returns 0 tracks but {len(old_ids)} are known — expired login or a transient error? (--allow-empty to override)"
    if old_ids and not new_ids:
        return None  # emptied on purpose: --allow-empty is explicit consent, don't also demand --yes
    removed = old_ids - new_ids
    if len(removed) > 10 and len(removed) > 0.5 * len(old_ids) and not yes:
        return f"{len(removed)} of {len(old_ids)} tracks would be removed — re-run with --yes to confirm"
    return None


def needs_move(recorded_rel: str, expected_rel: str, video_id: str) -> bool:
    """A file sitting at 'expected' or at its collision-disambiguated twin is already where it belongs."""
    return recorded_rel not in (expected_rel, disambiguate(expected_rel, video_id))
