# Oracle fixture corpus

A checked-in micro-repository with a **known** answer for every site, so `oracle_py.py` can be tested
without a live backend, a network clone, or either of the private repositories `run.py` points at.

It is not a substitute for running the benchmark on real code — real repos are where the mess lives,
and `bench/run.py` still targets them. It is the floor: it pins the oracle's labelling, including the
cases that produced silent zero-result runs (a `src/` source root, a transitive re-export) and the
case that made fabricated callers free (a bare name the file binds to something else).

Laid out under `src/` on purpose: `src/corpuspkg/sse.py` is imported as `corpuspkg.sse`, never
`src.corpuspkg.sse`, and getting that wrong means no import statement ever matches.

`tests/test_bench_oracle.py` asserts the expected label of every file below.
