"""`qualifier_scan.files_naming`: which of a list of files write a token — and nothing it cannot back.

The module reads source files on behalf of a `callers` answer, so the properties worth pinning are the
ones that bound the cost and the exposure: it never leaves the root, never reads more than the shared
per-file limit, never spends more than its total budget, never raises, and never records a file it did
not read as one that does not write the token. "Unknown" and "no" are different facts; the whole
module exists to keep them apart.
"""
from __future__ import annotations

import os
import sys
import threading
import types

import pytest

from codeintel import qualifier_scan
from codeintel.changed_range import MAX_SOURCE_BYTES
from codeintel.qualifier_scan import files_naming

TOKEN = "StrategyChain"


def _write(root, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_it_reports_per_file_whether_the_token_is_written(tmp_path):
    _write(tmp_path, "a.ts", f"import {{ {TOKEN} }} from './chain';\n")
    _write(tmp_path, "b.ts", "export const x = 1;\n")

    assert files_naming(str(tmp_path), TOKEN, ["a.ts", "b.ts"]) == {"a.ts": True, "b.ts": False}


def test_the_match_is_the_substring_the_printed_command_would_find(tmp_path):
    """`rg --fixed-strings` without `-w`: the note that states this result and the command that
    reproduces it must not disagree. A longer identifier containing the token matches, which is the
    lenient direction — it can only keep a row, never refute one."""
    _write(tmp_path, "a.ts", "class FallbackStrategyChainFactory {}\n")

    assert files_naming(str(tmp_path), TOKEN, ["a.ts"]) == {"a.ts": True}


def test_a_file_that_cannot_be_read_is_unknown_and_never_recorded_as_absent(tmp_path):
    _write(tmp_path, "there.ts", "export const x = 1;\n")

    seen = files_naming(str(tmp_path), TOKEN, ["there.ts", "gone.ts"])

    assert seen == {"there.ts": False}, "a file nobody opened was recorded as one that lacks the token"


def test_nothing_judged_is_none_and_not_an_empty_mapping(tmp_path):
    """An empty mapping would say every file was checked and none matched."""
    assert files_naming(str(tmp_path), TOKEN, ["gone.ts"]) is None
    assert files_naming(str(tmp_path), TOKEN, []) is None
    assert files_naming(str(tmp_path), TOKEN, ["", ""]) is None


@pytest.mark.parametrize("root,token", [("", TOKEN), ("/no/such/root/anywhere", TOKEN), (".", "")])
def test_no_root_no_token_or_a_root_that_is_not_a_directory_is_not_a_scan(root, token):
    assert files_naming(root, token, ["a.ts"]) is None


def test_a_path_the_operating_system_refuses_is_unknown_and_never_raises(tmp_path):
    _write(tmp_path, "a.ts", f"{TOKEN}\n")

    assert files_naming(str(tmp_path), TOKEN, ["bad\0name.ts"]) is None
    assert files_naming(str(tmp_path), TOKEN, ["bad\0name.ts", "a.ts"]) is None, (
        "an unexpected failure abandons the scan rather than half-answering it")


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_a_symlink_that_leaves_the_root_is_never_read(tmp_path):
    """Containment is the one definition every reader of the tree goes through. A link committed to
    point outside the repository must not have its target's bytes read into an answer — and the
    target here DOES write the token, so a reader that followed it would say `true`."""
    root = tmp_path / "repo"
    outside = tmp_path / "outside.ts"
    outside.write_text(f"{TOKEN}\n")
    _write(root, "inside.ts", "export const x = 1;\n")
    (root / "escape.ts").symlink_to(outside)

    assert files_naming(str(root), TOKEN, ["escape.ts", "inside.ts"]) == {"inside.ts": False}


@pytest.mark.skipif(sys.platform == "win32", reason="hard links need privileges on Windows")
def test_a_hard_linked_file_is_unknown_for_the_same_reason_a_symlink_is(tmp_path):
    """`contained_path` refuses an inode with a second directory entry: it may also live outside."""
    root = tmp_path / "repo"
    outside = tmp_path / "outside.ts"
    outside.write_text(f"{TOKEN}\n")
    root.mkdir()
    os.link(outside, root / "linked.ts")
    _write(root, "plain.ts", "export const x = 1;\n")

    assert files_naming(str(root), TOKEN, ["linked.ts", "plain.ts"]) == {"plain.ts": False}


def test_a_file_over_the_shared_size_limit_is_unknown_not_clean(tmp_path, monkeypatch):
    """The limit is `changed_range`'s own, imported rather than restated, so the two readers cannot
    disagree about what is too large. The token sits past the limit in the big file: a reader that
    truncated and searched would say `false` for a file it never finished."""
    assert qualifier_scan.MAX_SOURCE_BYTES == MAX_SOURCE_BYTES
    monkeypatch.setattr(qualifier_scan, "MAX_SOURCE_BYTES", 100)
    _write(tmp_path, "big.ts", "x" * 200 + TOKEN)
    _write(tmp_path, "small.ts", TOKEN)

    assert files_naming(str(tmp_path), TOKEN, ["big.ts", "small.ts"]) == {"small.ts": True}


def test_the_total_budget_leaves_the_rest_unjudged_rather_than_reading_on(tmp_path, monkeypatch):
    """The cost of an answer is bounded by construction. Past the budget the remaining files are
    unknown — never `false`, which would claim they were looked at."""
    monkeypatch.setattr(qualifier_scan, "_MAX_TOTAL_BYTES", 25)
    for i in range(5):
        _write(tmp_path, f"f{i}.ts", "y" * 10)

    seen = files_naming(str(tmp_path), TOKEN, [f"f{i}.ts" for i in range(5)])

    # A hard ceiling, not a stopping threshold: each read reserves its size (+1 byte to see growth)
    # first, so two 10-byte files fit in 25 bytes and a third does not. This test used to accept three
    # — 30 bytes read against a 25-byte ceiling.
    assert seen == {"f0.ts": False, "f1.ts": False}, seen


def test_the_file_ceiling_leaves_the_rest_unjudged(tmp_path, monkeypatch):
    monkeypatch.setattr(qualifier_scan, "_MAX_FILES", 2)
    for i in range(5):
        _write(tmp_path, f"f{i}.ts", TOKEN if i == 4 else "z")

    assert files_naming(str(tmp_path), TOKEN, [f"f{i}.ts" for i in range(5)]) == {
        "f0.ts": False, "f1.ts": False}


def test_a_file_listed_twice_is_read_once(tmp_path, monkeypatch):
    _write(tmp_path, "a.ts", "q" * 10)
    monkeypatch.setattr(qualifier_scan, "_MAX_TOTAL_BYTES", 15)

    assert files_naming(str(tmp_path), TOKEN, ["a.ts", "a.ts", "a.ts"]) == {"a.ts": False}


def test_the_limits_the_graph_doc_states_are_the_limits_the_scan_enforces():
    """`docs/graph.md` quotes the ceilings, and a doc that quotes a number is a claim about the code
    that has to follow it. A file over the size limit is not partly read — it is not read — and the
    doc says so in those words."""
    import pathlib

    text = " ".join((pathlib.Path(__file__).resolve().parent.parent / "docs" / "graph.md")
                    .read_text(encoding="utf-8").split())

    assert f"larger than {MAX_SOURCE_BYTES // 1_000_000} MB is not read" in text
    assert "at most 1 MB of any one file" not in text, "the scan skips a larger file, it does not truncate"
    assert (f"{qualifier_scan._MAX_TOTAL_BYTES // 1_000_000} MB or {qualifier_scan._MAX_FILES} files "
            f"in total or after {qualifier_scan._MAX_SECONDS:g} seconds") in text


def _returns_promptly(fn, *args, seconds: float = 5.0, **kwargs):
    """`(finished, value)` for `fn(*args)` run on a thread — so a call that blocks forever is a failed
    assertion and not a hung test run. The thread is a daemon: a blocked one cannot hold the process."""
    box: dict = {}

    def run():
        box["value"] = fn(*args, **kwargs)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    return not thread.is_alive(), box.get("value")


def _release(fifo) -> None:
    """Unblock a reader still waiting in `open` on *fifo*, so a failing run leaves no thread behind."""
    try:
        fd = os.open(fifo, os.O_WRONLY | os.O_NONBLOCK)
    except OSError:
        return
    os.close(fd)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="needs POSIX FIFOs")
def test_a_fifo_at_an_indexed_path_is_unknown_and_never_blocks_the_scan(tmp_path):
    """`contained_path` checks where a file is and how many links it has, not what it is. A FIFO planted
    at a path the index names has one link and a size of zero, passes containment, and `open` on it
    blocks until a writer appears — which would hang the `callers` thread, and with it every
    `changed` worker. The tenant who can plant it is the one `containment.py` names. Only a regular
    file is opened; a FIFO is unknown, never `false`."""
    _write(tmp_path, "plain.ts", "export const x = 1;\n")
    fifo = tmp_path / "pipe.ts"
    os.mkfifo(fifo)
    try:
        finished, seen = _returns_promptly(
            files_naming, str(tmp_path), TOKEN, ["pipe.ts", "plain.ts"], seconds=3.0)
    finally:
        _release(fifo)

    assert finished, "the scan blocked on a FIFO"
    assert seen == {"plain.ts": False}, seen


def test_a_file_with_a_wide_byte_order_mark_is_unknown_not_refuted(tmp_path):
    """The printed `rg` transcodes a UTF-16 or UTF-32 file by its byte-order mark and finds the token;
    this scan matches raw UTF-8 bytes and would read the same file as `false`, so the note and its own
    command would disagree about it. A file that is not plain text is not judged."""
    for name, encoding in (("utf16.ts", "utf-16"), ("utf16le.ts", "utf-16-le"),
                           ("utf32.ts", "utf-32")):
        text = f"import {{ {TOKEN} }} from './chain';\n"
        data = text.encode(encoding)
        if encoding == "utf-16-le":
            data = b"\xff\xfe" + data                      # the BOM `utf-16-le` itself omits
        (tmp_path / name).write_bytes(data)
    _write(tmp_path, "plain.ts", "export const x = 1;\n")

    seen = files_naming(str(tmp_path), TOKEN, ["utf16.ts", "utf16le.ts", "utf32.ts", "plain.ts"])

    assert seen == {"plain.ts": False}, seen


def test_a_file_with_a_nul_byte_anywhere_is_unknown_not_judged(tmp_path):
    """`rg` stops searching a file at the first NUL it meets, so a file that writes the token only
    AFTER a NUL — even one past the first 8 KB — is "no match" to the printed command. Judging it here
    would print a verdict the command beside it cannot reproduce, so such a file is not judged at all.
    CONTROL: a UTF-8 byte-order mark is plain text, and a file with no NUL is judged."""
    (tmp_path / "binary.ts").write_bytes(b"x" * 100 + b"\0" + TOKEN.encode())
    (tmp_path / "late_nul.ts").write_bytes(b"x" * 9000 + b"\0" + TOKEN.encode())
    (tmp_path / "utf8_bom.ts").write_bytes(b"\xef\xbb\xbf" + TOKEN.encode())
    (tmp_path / "plain.ts").write_bytes(b"x" * 9000 + TOKEN.encode())

    seen = files_naming(str(tmp_path), TOKEN, ["binary.ts", "late_nul.ts", "utf8_bom.ts", "plain.ts"])

    assert seen == {"utf8_bom.ts": True, "plain.ts": True}, seen


def test_a_read_stalled_on_a_slow_mount_is_abandoned_at_the_deadline_not_waited_out(
        tmp_path, monkeypatch):
    """The budget is a deadline, not a tally. Charging a read only after it returns bounds nothing
    when one read does not return — a regular file on a stalled NFS or FUSE mount — and `callers`, or a
    `changed` worker, would wait on it indefinitely. The stalled read is left on its own thread; it and
    every file after it are unknown, and the answer comes back on time."""
    import time

    _write(tmp_path, "stalled.ts", TOKEN)
    _write(tmp_path, "after.ts", TOKEN)
    release = threading.Event()
    real_read = qualifier_scan._read

    def read(root_real, path, *rest):
        if path.endswith("stalled.ts"):
            release.wait(8)                              # a read that does not come back in time
        return real_read(root_real, path, *rest)

    monkeypatch.setattr(qualifier_scan, "_read", read)
    monkeypatch.setattr(qualifier_scan, "_stalled", set())   # this test's abandoned read stays its own
    monkeypatch.setattr(qualifier_scan, "_slots", threading.BoundedSemaphore(4))
    started = time.monotonic()
    try:
        seen = files_naming(str(tmp_path), TOKEN, ["stalled.ts", "after.ts"],
                            budget=qualifier_scan.Budget(0.3))
    finally:
        release.set()

    assert time.monotonic() - started < 4, "a stalled read held the answer past its deadline"
    assert seen is None, f"a file at or after the stall was judged: {seen}"


def test_reads_left_blocked_by_earlier_answers_are_bounded_across_the_process(tmp_path, monkeypatch):
    """Each answer leaves at most one stalled read behind, but a server asked again and again about a
    tree on a stalled mount would leave one per answer until it ran out of threads. While one is still
    blocked, later scans start no read at all — they judge nothing and print the command — and reading
    resumes by itself once the stalled read returns."""
    import time

    _write(tmp_path, "stalled.ts", TOKEN)
    _write(tmp_path, "fine.ts", TOKEN)
    release = threading.Event()
    real_read = qualifier_scan._read
    began: list[str] = []

    def read(root_real, path, *rest):
        began.append(os.path.basename(path))
        if path.endswith("stalled.ts"):
            release.wait(8)
        return real_read(root_real, path, *rest)

    monkeypatch.setattr(qualifier_scan, "_read", read)
    monkeypatch.setattr(qualifier_scan, "_stalled", set())
    monkeypatch.setattr(qualifier_scan, "_slots", threading.BoundedSemaphore(4))
    try:
        first = files_naming(str(tmp_path), TOKEN, ["stalled.ts"], budget=qualifier_scan.Budget(0.2))
        second = files_naming(str(tmp_path), TOKEN, ["stalled.ts"], budget=qualifier_scan.Budget(0.2))
        third = files_naming(str(tmp_path), TOKEN, ["fine.ts"], budget=qualifier_scan.Budget(5.0))
    finally:
        release.set()

    assert (first, second, third) == (None, None, None)
    assert began == ["stalled.ts"], f"a later answer started another read behind a stalled one: {began}"
    deadline = time.monotonic() + 5
    while qualifier_scan._stalled_reads() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert files_naming(str(tmp_path), TOKEN, ["fine.ts"], budget=qualifier_scan.Budget(5.0)) == {
        "fine.ts": True}, "reading did not resume once the stalled read returned"


def test_an_exhausted_time_budget_leaves_every_file_unjudged(tmp_path):
    """A bound on the time one answer spends reading: `changed` can look up forty symbols of two
    hundred files, and a stalled mount makes each read slow rather than failed. Files not read once
    the allowance is gone are unknown — the scan judged nothing, so it says `None`, not an empty
    mapping."""
    _write(tmp_path, "a.ts", TOKEN)
    spent = qualifier_scan.Budget(1.0)
    spent.spend(2.0)

    assert files_naming(str(tmp_path), TOKEN, ["a.ts"], budget=spent) is None
    assert files_naming(str(tmp_path), TOKEN, ["a.ts"], budget=qualifier_scan.Budget(60.0)) == {
        "a.ts": True}


def test_a_budget_used_up_part_way_leaves_the_rest_unjudged_rather_than_clean(tmp_path, monkeypatch):
    """The allowance is checked before each file, so one used up after two of five leaves the other
    three unknown — never `false`, which would claim they were looked at. The fake clock makes every
    file cost one second."""
    for i in range(5):
        _write(tmp_path, f"f{i}.ts", "z" * 10)
    ticks = iter(range(100))
    monkeypatch.setattr(qualifier_scan, "time", types.SimpleNamespace(monotonic=lambda: next(ticks)))

    seen = files_naming(str(tmp_path), TOKEN, [f"f{i}.ts" for i in range(5)],
                        budget=qualifier_scan.Budget(2.0))

    assert seen == {"f0.ts": False, "f1.ts": False}, seen


@pytest.mark.skipif(not __import__("shutil").which("rg"), reason="needs ripgrep on PATH")
def test_the_printed_command_finds_what_the_scan_found_in_hidden_and_ignored_files(tmp_path):
    """The scan reads whatever file a caller row names — a dot-directory, an ignored path, a symlink
    to a file inside the root included. `rg` skips all three by default, so the plain command printed beneath a `true` found no match in the
    very file the scan had matched. The printed command must reproduce every verdict — run it for
    real and see. CONTROL: the plain command really does miss them, which is the defect."""
    import shlex
    import subprocess

    _write(tmp_path, ".internal/chain.ts", f"new {TOKEN}()")
    _write(tmp_path, "ignored/use.ts", f"{TOKEN}.resolve()")
    (tmp_path / ".ignore").write_text("ignored/\n")
    _write(tmp_path, "real/target.ts", f"const c = new {TOKEN}()")
    (tmp_path / "link.ts").symlink_to(tmp_path / "real" / "target.ts")   # an in-root file symlink
    files = [".internal/chain.ts", "ignored/use.ts", "link.ts"]

    seen = files_naming(str(tmp_path), TOKEN, files)
    printed = subprocess.run(shlex.split(qualifier_scan.rerun_command(TOKEN, str(tmp_path))),
                             capture_output=True, text=True, timeout=30).stdout
    plain = subprocess.run(["rg", "-n", "--fixed-strings", TOKEN, str(tmp_path)],
                           capture_output=True, text=True, timeout=30).stdout

    assert seen == {".internal/chain.ts": True, "ignored/use.ts": True, "link.ts": True}, seen
    for rel in files:
        assert rel in printed, f"the printed command does not reproduce the scan's verdict on {rel}"
        assert rel not in plain, f"CONTROL: plain rg was expected to skip {rel}"


def test_scans_racing_on_one_budget_cannot_read_past_its_byte_ceiling(tmp_path):
    """The byte ceiling used to be a stopping threshold: a file got through whenever any byte was left,
    and `changed`'s workers, all seeing the same positive balance, each got one — 4 MB over the
    advertised ceiling. Each read now reserves its bytes first, under the budget's lock. Thirty-three
    bytes hold three 10-byte files (each reserves 11, the extra byte that shows a file grew), and four
    scans racing for them get three between them — never more."""
    for i in range(8):
        _write(tmp_path, f"f{i}.ts", "w" * 10)
    shared = qualifier_scan.Budget(60.0, total_bytes=33)
    gate = threading.Barrier(4)
    results: list[dict | None] = []
    lock = threading.Lock()

    def scan(k):
        gate.wait()
        got = files_naming(str(tmp_path), TOKEN, [f"f{2 * k}.ts", f"f{2 * k + 1}.ts"], budget=shared)
        with lock:
            results.append(got)

    workers = [threading.Thread(target=scan, args=(k,)) for k in range(4)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(10)

    judged = sum(len(r) for r in results if r)
    assert judged == 3, f"{judged} files were read on a budget that holds three"


def test_a_file_that_grew_since_its_stat_is_unknown_not_judged_on_part_of_itself(tmp_path, monkeypatch):
    """The read is bounded by the stat — its size plus one byte — so a file that grew between the two
    calls is noticed by that one byte and left unknown, never judged on whatever prefix was read.
    CONTROL: a file whose stat is true is judged."""
    _write(tmp_path, "grew.ts", "x" * 12 + TOKEN)
    _write(tmp_path, "same.ts", "x" * 12 + TOKEN)
    real_stat = os.stat

    def stale_stat(path, *args, **kwargs):
        info = real_stat(path, *args, **kwargs)
        if str(path).endswith("grew.ts"):
            fields = list(info)
            fields[6] = 10                                # st_size as it was before the file grew
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(qualifier_scan.os, "stat", stale_stat)

    assert files_naming(str(tmp_path), TOKEN, ["grew.ts", "same.ts"]) == {"same.ts": True}


def test_a_root_whose_metadata_stalls_is_abandoned_at_the_deadline_too(tmp_path, monkeypatch):
    """Checking that the root is a directory and canonicalising it are filesystem calls: on a stale
    NFS or FUSE root they block exactly as a read does, before any read starts. They run under the
    same deadline, so the answer still comes back on time — with nothing judged."""
    import time

    _write(tmp_path, "a.ts", TOKEN)
    release = threading.Event()
    real = qualifier_scan.real_root

    def stalled_root(root):
        release.wait(8)                                  # a metadata call that does not come back
        return real(root)

    monkeypatch.setattr(qualifier_scan, "real_root", stalled_root)
    monkeypatch.setattr(qualifier_scan, "_stalled", set())
    monkeypatch.setattr(qualifier_scan, "_slots", threading.BoundedSemaphore(4))
    started = time.monotonic()
    try:
        seen = files_naming(str(tmp_path), TOKEN, ["a.ts"], budget=qualifier_scan.Budget(0.3))
    finally:
        release.set()

    assert time.monotonic() - started < 4, "a stalled root probe held the answer past its deadline"
    assert seen is None, seen


def test_scans_that_arrive_together_cannot_leave_more_blocked_reads_than_there_are_slots(
        tmp_path, monkeypatch):
    """The "is anything stalled?" check is not atomic: scans arriving together — concurrent requests,
    `changed`'s pool — all see nothing stalled yet. Each blocking call therefore holds a process-wide
    slot from before its thread starts until it returns, so however many scans race, no more reads
    can be left blocked than there are slots."""
    import time

    _write(tmp_path, "stalled.ts", TOKEN)
    release = threading.Event()
    real_read = qualifier_scan._read
    began: list[str] = []
    lock = threading.Lock()

    def read(root_real, path, *rest):
        with lock:
            began.append(path)
        release.wait(8)
        return real_read(root_real, path, *rest)

    monkeypatch.setattr(qualifier_scan, "_read", read)
    monkeypatch.setattr(qualifier_scan, "_stalled", set())
    monkeypatch.setattr(qualifier_scan, "_slots", threading.BoundedSemaphore(2))
    gate = threading.Barrier(6)
    results: list[object] = []

    def scan():
        gate.wait()
        results.append(files_naming(str(tmp_path), TOKEN, ["stalled.ts"],
                                    budget=qualifier_scan.Budget(0.5)))

    workers = [threading.Thread(target=scan) for _ in range(6)]
    started = time.monotonic()
    try:
        for w in workers:
            w.start()
        for w in workers:
            w.join(6)
        elapsed = time.monotonic() - started
    finally:
        release.set()

    assert len(began) <= 2, f"{len(began)} reads were started behind a stalled mount with 2 slots"
    assert results == [None] * 6, results
    assert elapsed < 5, "a scan waited past its deadline for a slot"


def test_scans_sharing_a_budget_stop_together_when_its_time_is_gone(tmp_path, monkeypatch):
    """`changed`'s workers share one budget. Each wait used to know only the time left when ITS call
    began, and nothing is charged until a call returns — so a second stalled read starting late in the
    allowance waited nearly a whole allowance more, and the answer took close to twice it. Every wait
    now counts what the other calls still running are spending, so they all stop when it is gone."""
    import time

    _write(tmp_path, "a.ts", TOKEN)
    _write(tmp_path, "b.ts", TOKEN)
    release = threading.Event()
    real_read = qualifier_scan._read

    def read(root_real, path, *rest):
        release.wait(8)                                  # both reads stall
        return real_read(root_real, path, *rest)

    monkeypatch.setattr(qualifier_scan, "_read", read)
    monkeypatch.setattr(qualifier_scan, "_stalled", set())
    monkeypatch.setattr(qualifier_scan, "_slots", threading.BoundedSemaphore(4))
    shared = qualifier_scan.Budget(1.0)
    ended: dict[str, float] = {}

    def scan(name, delay):
        time.sleep(delay)
        files_naming(str(tmp_path), TOKEN, [name], budget=shared)
        ended[name] = time.monotonic()

    started = time.monotonic()
    workers = [threading.Thread(target=scan, args=("a.ts", 0.0)),
               threading.Thread(target=scan, args=("b.ts", 0.6))]
    try:
        for w in workers:
            w.start()
        for w in workers:
            w.join(6)
    finally:
        release.set()

    assert set(ended) == {"a.ts", "b.ts"}, ended
    assert max(ended.values()) - started < 1.3, (
        f"the answer spent {max(ended.values()) - started:.2f}s reading on a 1.0s shared budget")


def test_one_budget_also_caps_the_files_and_bytes_of_every_scan_that_shares_it(tmp_path):
    """`changed` runs up to forty scans on one budget. Counted per scan, each would get the full file
    and byte allowance, and a fast local tree would read forty times what the answer was promised
    inside the time limit. The second scan here gets only what the first left."""
    for i in range(4):
        _write(tmp_path, f"f{i}.ts", "z" * 10)

    by_files = qualifier_scan.Budget(60.0, files=3)
    first = files_naming(str(tmp_path), TOKEN, ["f0.ts", "f1.ts"], budget=by_files)
    second = files_naming(str(tmp_path), TOKEN, ["f2.ts", "f3.ts"], budget=by_files)
    assert (first, second) == ({"f0.ts": False, "f1.ts": False}, {"f2.ts": False})

    by_bytes = qualifier_scan.Budget(60.0, total_bytes=25)
    first = files_naming(str(tmp_path), TOKEN, ["f0.ts", "f1.ts"], budget=by_bytes)
    second = files_naming(str(tmp_path), TOKEN, ["f2.ts", "f3.ts"], budget=by_bytes)
    assert (first, second) == ({"f0.ts": False, "f1.ts": False}, None), (
        "the bytes the first scan reserved were not charged to the second, or the ceiling overshot")


def test_one_budget_is_drawn_on_by_every_scan_that_shares_it(tmp_path, monkeypatch):
    """`changed` runs a scan per changed symbol. They share a budget, so the second scan inherits what
    the first left, and a scan with no budget of its own starts a fresh one."""
    for i in range(4):
        _write(tmp_path, f"f{i}.ts", "z" * 10)
    ticks = iter(range(100))
    monkeypatch.setattr(qualifier_scan, "time", types.SimpleNamespace(monotonic=lambda: next(ticks)))
    shared = qualifier_scan.Budget(3.0)

    first = files_naming(str(tmp_path), TOKEN, ["f0.ts", "f1.ts"], budget=shared)
    second = files_naming(str(tmp_path), TOKEN, ["f2.ts", "f3.ts"], budget=shared)
    alone = files_naming(str(tmp_path), TOKEN, ["f2.ts", "f3.ts"])

    assert first == {"f0.ts": False, "f1.ts": False}, first
    assert second == {"f2.ts": False}, "only one second was left for the second scan"
    assert alone == {"f2.ts": False, "f3.ts": False}, "a scan with no budget of its own starts one"
