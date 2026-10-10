from ytmsync.plan import FileRow, check_removals, make_plan, needs_move

def F(*items, pinned=()):
    return {(v, s): FileRow(f"{v}.opus", (v, s) in pinned) for v, s in items}

ALWAYS = lambda p: True

def test_fresh_library_downloads_everything():
    p = make_plan({"P1": [(1, "a"), (2, "b")]}, {}, {}, False, ALWAYS)
    assert p.download == ["a", "b"] and not p.delete and not p.copy

def test_shared_song_is_downloaded_once_and_only_new_ones_fetched():
    files = F(("a", ""), ("b", ""))
    p = make_plan({"P1": [(1, "a"), (2, "b")], "P2": [(1, "b"), (2, "c")]}, files, {"a": "ok", "b": "ok"}, False, ALWAYS)
    assert p.download == ["c"] and not p.delete

def test_removed_from_one_playlist_but_still_in_another_is_kept():
    files = F(("a", ""), ("b", ""))
    p = make_plan({"P1": [(1, "a")], "P2": [(1, "b")]}, files, {}, False, ALWAYS)
    assert not p.delete
    p = make_plan({"P1": [(1, "a")], "P2": []}, files, {}, False, ALWAYS)
    assert p.delete == [("b", "", "b.opus")]

def test_scoped_layout_copies_from_donor_instead_of_redownloading():
    files = F(("a", "P1"))
    p = make_plan({"P1": [(1, "a")], "P2": [(1, "a")]}, files, {"a": "ok"}, True, ALWAYS)
    assert p.download == [] and p.copy == [("a", "P2", "P1")]

def test_scoped_layout_new_song_in_two_playlists_downloads_once_two_scopes():
    p = make_plan({"P1": [(1, "a")], "P2": [(1, "a")]}, {}, {}, True, ALWAYS)
    assert p.download == ["a"] and p.scopes["a"] == ["P1", "P2"]

def test_file_missing_on_disk_is_redownloaded():
    p = make_plan({"P1": [(1, "a")]}, F(("a", "")), {"a": "ok"}, False, lambda path: False)
    assert p.download == ["a"]

def test_pinned_never_deleted_and_delete_flag_respected():
    files = F(("a", ""), ("z", ""), pinned={("z", "")})
    assert make_plan({"P1": [(1, "a")]}, files, {}, False, ALWAYS).delete == []
    assert make_plan({"P1": []}, files, {}, False, ALWAYS).delete == [("a", "", "a.opus")]
    assert make_plan({"P1": []}, files, {}, False, ALWAYS, delete=False).delete == []

def test_unavailable_and_failed_policies():
    m = {"P1": [(1, "a"), (2, "b"), (3, "c")]}
    st = {"a": "unavailable", "b": "failed"}
    p = make_plan(m, {}, st, False, ALWAYS, unavailable_now=frozenset({"c"}))
    assert p.download == ["b"] and set(p.skipped_unavailable) == {"a", "c"}
    assert make_plan(m, {}, st, False, ALWAYS, retry_failed=False, unavailable_now=frozenset({"c"})).download == []
    assert "a" in make_plan(m, {}, st, False, ALWAYS, retry_unavailable=True).download

def test_check_removals_guards():
    ids = {f"v{i}" for i in range(20)}
    assert "0 tracks" in check_removals(ids, set())
    assert check_removals(ids, set(), allow_empty=True) is None
    assert "would be removed" in check_removals(ids, set(list(ids)[:5]))
    assert check_removals(ids, set(list(ids)[:5]), yes=True) is None
    assert check_removals(ids, ids - {"v1", "v2"}) is None            # small change: fine
    assert check_removals(set(), {"x"}) is None                       # first sync

def test_needs_move():
    assert not needs_move("a/b/01 T.opus", "a/b/01 T.opus", "vid")
    assert not needs_move("a/b/01 T [vid].opus", "a/b/01 T.opus", "vid")
    assert needs_move("Old/01 T.opus", "a/b/01 T.opus", "vid")
