# Classify a failed embedding-model download as a download failure, not a config error

Type: task
Status: open
Blocked by: —
Test first: yes
Review: auto

In `src/codeintel/semantic_db.py`, `load_embedder` maps every `ValueError`
from fastembed's `TextEmbedding(model_name=...)` to the "that is not a model
fastembed ships — check the `model` key in your codeintel config" remedy.
That classification was written for the case where fastembed rejects an
unknown name before any network call. fastembed 0.9.0 also raises
`ValueError("Could not load model <name> from any source.")` after its
download retries are exhausted — for a name it *does* ship
(`BAAI/bge-small-en-v1.5` is in `TextEmbedding.list_supported_models()`).
The user is then sent to debug config for a network problem.

Change the classifier: before emitting the unknown-model remedy, check the
name against `TextEmbedding.list_supported_models()`. If the name is known,
emit the download/network remedy (the one used for the other download
failures — huggingface.co, proxy, `FASTEMBED_CACHE_PATH`). If it is unknown,
keep the config remedy exactly as today. Cache the supported-model list so a
failed load does not pay for the listing twice; never let the listing itself
raise out of `load_embedder` (if it cannot be obtained, fall back to today's
behavior).

## Not in this ticket

- The cold-start warming work — tickets 02 and 03.
- Any change to the `OSError`/errno classification beside this branch; it
  stays as is.

## Done when

- A new test fails before the fix and passes after: constructing the
  embedder for a shipped model name while fastembed raises
  `ValueError("Could not load model ... from any source.")` produces a
  message naming huggingface.co / the download remedy and *not* "not a model
  fastembed ships"; an unshipped name with `ValueError` still produces the
  config remedy. Extend `tests/test_index_failure_reason.py` (it already
  covers this classifier) or add a focused test module next to it.
- The full suite still passes: `pytest tests/ -q` exits 0.

```check
# The conductor runs this block before review.
$ pytest tests/test_index_failure_reason.py -q
exit 0
prints passed
$ pytest tests/ -q
exit 0
prints passed
```

## Reference

- spec.md § Edge cases (rows 1–2), § Decisions (third bullet)
- `src/codeintel/semantic_db.py` — `load_embedder`, the `except ValueError` branch and its comment
- `tests/test_index_failure_reason.py` — existing classifier tests (read-only)

## Answer

