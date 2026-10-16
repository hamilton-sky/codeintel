# Graph answers say when the index is older than the code they describe

Type: task
Status: open
Blocked by: —
Test first: yes
Review: full

The README names this project's characteristic failure mode: **answering
confidently from the wrong index rather than failing loudly**, where a wrong
answer and a right one are the same shape. The same outside review that
prompted this plan picked it out as the one thing an agent consumer should
keep worrying about.

The machinery to see it already exists, but only `changed` uses it.
`changed_range.graph_index_age` compares the graph index file's mtime with the
files in question and with the last time HEAD moved by something other than a
commit (checkout, merge, reset, rebase), and `graph_changed.py` turns `stale`
into a `stale-index` gap and `unknown` into `index-age-unknown`. A plain
`callers` or `impact` answer runs no such check, so a caller list read from an
index built before last week's branch switch looks exactly as current as one
built a minute ago.

Run the same check on every graph-answered `callers` and `impact` answer, with
the target's file and every file that appears in a result row as the paths. Add
the same two gaps, with the same wording rules as `changed`: `stale` names what
changed after the index was written and says callers added since then are
missing, and `unknown` never reads as `fresh`. A `stale-index` gap makes the
answer `confidence: partial` and `safe_for_destructive: false`. A fresh index
changes nothing in the answer.

Read the age **before** the backend lookups, for the reason the comment in
`graph_changed.py` gives: the lookups open the same db file, and a stamp taken
afterwards would always read as "just now".

## Not in this ticket

- Reindexing automatically. The answer reports the age; `codeintel index` stays
  the remedy, named in the gap.
- The semantic engine, which already has its own `index-stale` reason.
- New envelope fields. Both gap kinds already exist and docs/trust.md documents
  how to read `gaps`.

## Done when

- New tests fail before and pass after: with the graph db's mtime set older than
  a file in the answer, `callers` and `impact` carry a `stale-index` gap, are
  `partial`, and are not `safe_for_destructive`; with it newer, the answer is
  unchanged from today; with no db file, the gap is `index-age-unknown`.
- A HEAD reflog entry for a checkout newer than the index produces
  `stale-index` even when every file's mtime is older than the index (the
  branch-switch case).
- docs/trust.md lists `stale-index` and `index-age-unknown` for `callers` and
  `impact`, and `tests/test_docs_trust_claims.py` still passes (it checks every
  gap kind the guide names is raised by some provider).
- New summary or tally sites are registered in `_VERIFIED_BY`
  (`tests/test_summary_integrity.py`).
- The full suite still passes: `pytest tests/ -q` exits 0.

```check
# The conductor runs this block before review.
$ pytest tests/test_graph_provider.py tests/test_docs_trust_claims.py -q
exit 0
prints passed
$ pytest tests/ -q
exit 0
prints passed
```

## Reference

- README.md § Project status, "The honest one-paragraph version"
- `src/codeintel/changed_range.py`: `graph_index_age`, `_last_tree_move`
- `src/codeintel/graph_changed.py`: where `changed` raises the two gaps
- `src/codeintel/graph_dispatch.py`: the `callers` and `impact` dispatch
- docs/trust.md: the `gaps` contract

## Answer
