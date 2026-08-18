"""One answer to "is this hand-written source?", shared by everything that needs it.

Before this module the question was answered by three separate frozensets of directory NAMES —
``_SKIP_DIRS``/``_DEFAULT_IGNORES`` in the indexer, ``_VERIFY_SKIP_DIRS``/``_ARCHIVE_DIRS`` in the
graph provider — each entry added by an incident, and the tests parametrized over those same sets.
That is a change-detector for a constant, not a specification: it proves the list contains what the
list contains. Every name on it was added *after* a real repository shipped a wrong answer, so the
next tree named ``bazel-bin/``, ``_generated/``, ``.output/`` or ``Pods/`` reproduces the bug
exactly as ``out/`` and ``dist/`` did before 0.15.1.

The property actually wanted is a *shape*, not a name. A minified bundle's real property is that it
is one 400KB line; a protobuf stub's is that it says so in a header. So this module answers the
question three ways, cheapest first:

* ``looks_generated_path`` — directory segments and filename patterns. A fast pre-filter that needs
  no I/O. Still a list, but a much larger one, and no longer the whole answer.
* ``looks_generated_text`` — content shape: an ``@generated``-style marker near the top, or line
  geometry no human writes.
* ``is_vendored_by_gitattributes`` — the repository's own declaration, via
  ``linguist-generated`` / ``linguist-vendored``. This is the only *authoritative* signal: the
  project telling you directly which of its files are not hand-written.

Thresholds deliberately err toward *under*-hiding, matching the same judgement the deadcode
verifier makes: wrongly hiding real source is a silent loss of coverage, which is worse than
occasionally ranking a generated file.
"""
from __future__ import annotations

import os
import re

# Directory segments that are generated or vendored AT ANY DEPTH.
#
# This list is deliberately conservative, and adds to the existing per-context lists rather than
# overriding them. The callers already encode a judgement worth keeping: the indexer treats
# ambiguous names (`out`, `dist`, `build`, `target`, `vendor`, `generated`, `coverage`) as output
# only at the REPO ROOT, because one level down they are somebody's package — matching a basename
# at any depth previously indexed 0 files of pypa/build, hid coverage.py's collector, and deleted a
# NestJS vendor module from the corpus. The graph provider makes the opposite call for a few of
# them, which is right for a ranking op and wrong for a corpus.
#
# So nothing ambiguous belongs here. `packages/` is source in every monorepo; `output/` is not
# `out/`; `gen/`, `obj/`, `release/` and `debug/` are all real module names somewhere. What is left
# is names that are never hand-written source wherever they appear.
GENERATED_DIRS = frozenset({
    # package managers / vendored dependencies — unambiguous wherever they appear
    "node_modules", "bower_components", "jspm_packages", "site-packages", "pods",
    "venv", ".venv", "__pycache__",
    # framework build output and tooling caches — all dot-prefixed or otherwise unmistakable
    ".next", ".nuxt", ".svelte-kit", ".turbo", ".parcel-cache", ".gradle", ".terraform",
    ".angular", ".docusaurus", ".vercel", ".serverless", ".webpack", ".output", "_build",
    "deriveddata", "htmlcov", ".nyc_output",
    # explicitly-marked generated trees (the underscores are the signal; bare `generated` is not)
    "_generated", "__generated__",
    # retired trees
    ".archive", ".archived", "_archive", "_archived", ".backup", ".backups", ".bak", ".old",
    ".deprecated", ".trash",
})

# Directory-name PREFIXES — Bazel writes `bazel-bin`, `bazel-out`, `bazel-testlogs` and a
# `bazel-<workspace-name>` symlink whose name is unknowable in advance, which is precisely the kind
# of thing an exact-name list can never cover.
GENERATED_DIR_PREFIXES = ("bazel-", "cmake-build-")

# Filename patterns for generated files that live BESIDE hand-written source, where no directory
# rule can help: a `*_pb2.py` sits in the same package as the code using it.
_GENERATED_FILE_RE = re.compile(
    r"""(?ix)
    ^( .*\.min\.(js|css|mjs|cjs)                 # minified assets
     | .*[.\-_]bundle\.(js|css|mjs|cjs)          # webpack/rollup bundles
     | .*\.pb\.(go|cc|h|py)                      # protobuf output
     | .*_pb2(_grpc)?\.pyi?                      # python protobuf/grpc stubs
     | .*_pb\.(js|ts)
     | .*\.generated\.[a-z0-9]+                  # explicit .generated. infix
     | .*\.g\.(dart|cs|kt)                       # codegen conventions
     | .*\.designer\.cs
     | .*\.d\.ts                                 # emitted type declarations
     | .*\.(map|lock)                            # sourcemaps, lockfiles
     | (package-lock|yarn|pnpm-lock|composer|poetry|cargo|gemfile)\.(json|lock|yaml)
     )$
    """,
)

# Markers a generator writes into its own output. `@generated` is the widely-adopted convention;
# the rest cover tools that predate or ignore it. Matched case-insensitively near the top only —
# the phrase "do not edit" appears in plenty of hand-written files further down.
_GENERATED_MARKER_RE = re.compile(
    r"(?i)(@generated\b"
    r"|\bcode generated by\b"
    r"|\bgenerated by\b.{0,40}\bdo not edit\b"
    r"|\bautogenerated\b|\bauto-generated\b"
    r"|\bdo not edit\b.{0,40}\bgenerated\b"
    r"|\bthis file (was|is) automatically generated\b)"
)

_MARKER_SCAN_LINES = 40           # a generator's banner is at the top or it is not a banner
_MINIFIED_LINE_CHARS = 2000       # no human writes a 2000-character line
_LARGE_FILE_BYTES = 50_000        # below this, a long mean line is more likely data than minified
_LARGE_FILE_MEAN_LINE = 200


def looks_generated_path(file_path: str) -> bool:
    """Whether *file_path* is generated/vendored judged by its path alone. No I/O."""
    norm = str(file_path).replace("\\", "/")
    segments = [p.lower() for p in norm.split("/") if p]
    for seg in segments[:-1]:
        if seg in GENERATED_DIRS or seg.startswith(GENERATED_DIR_PREFIXES):
            return True
    basename = segments[-1] if segments else ""
    return bool(basename) and _GENERATED_FILE_RE.match(basename) is not None


def looks_generated_text(text: str) -> bool:
    """Whether *text* has the shape of generated output.

    Two independent signals: a generator's own banner near the top, and line geometry. The geometry
    check is what catches a minified bundle whose directory and filename both look ordinary — the
    case a name list structurally cannot reach.
    """
    if not text:
        return False
    head = text[:8192].splitlines()[:_MARKER_SCAN_LINES]
    if any(_GENERATED_MARKER_RE.search(line) for line in head):
        return True
    lines = text.splitlines() or [text]
    if any(len(line) > _MINIFIED_LINE_CHARS for line in lines):
        return True
    return len(text) > _LARGE_FILE_BYTES and (len(text) / max(len(lines), 1)) > _LARGE_FILE_MEAN_LINE


_SAMPLE_BYTES = 8192


def looks_generated_file(path: str | os.PathLike) -> bool:
    """``looks_generated_text`` over a bounded head of the file, for callers walking a tree.

    Reads at most 8KB — enough for a generator banner, and enough to spot minification: a minified
    bundle is one enormous line, so a sample that large containing no newline at all is decisive.
    Bounded deliberately, because this runs once per candidate file during a walk and the whole
    point is to be cheaper than the wrong answer it prevents. Unreadable means no claim.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            sample = fh.read(_SAMPLE_BYTES)
    except OSError:
        return False
    if not sample:
        return False
    if "\n" not in sample:
        return True                       # 8KB without a line break is not something a human typed
    return looks_generated_text(sample)


def load_gitattributes_globs(root: str) -> list[str]:
    """Globs the repository itself declares as generated or vendored, via `.gitattributes`.

    This is the one authoritative signal available — the project stating which of its own files are
    not hand-written — and nothing consulted it before. Missing or unreadable file means no claim,
    which is the safe direction.
    """
    globs: list[str] = []
    try:
        with open(os.path.join(root, ".gitattributes"), encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                attrs = [a.lower() for a in parts[1:]]
                if any(a in ("linguist-generated", "linguist-vendored",
                             "linguist-generated=true", "linguist-vendored=true") for a in attrs):
                    globs.append(parts[0])
    except OSError:
        return []
    return globs


def matches_any_glob(file_path: str, globs: list[str]) -> bool:
    """Whether *file_path* (repo-relative) matches any `.gitattributes` pattern."""
    if not globs:
        return False
    import fnmatch
    norm = str(file_path).replace("\\", "/").lstrip("/")
    for pattern in globs:
        pat = pattern.lstrip("/")
        if fnmatch.fnmatch(norm, pat) or fnmatch.fnmatch(norm, f"*/{pat}"):
            return True
        # A bare directory pattern (`docs/api`) covers everything beneath it.
        if not pat.endswith("*") and (norm == pat or norm.startswith(pat.rstrip("/") + "/")):
            return True
    return False


# --------------------------------------------------------------------------- code vs prose

# Extensions whose contents are PROSE ABOUT code rather than code. Semantic search embeds both into
# one vector space and ranks by cosine similarity — and prose about a subject is written in the
# language of a question about that subject, so it systematically outranks the implementation. On a
# repository carrying a large markdown corpus this made `search` useless: a query for "where does
# the FSM decide the next stage transition" returned ten markdown files and zero Python, while the
# function literally named `evaluate_transition_rules` sat unreturned. This is not a tuning problem;
# the embedding is doing exactly what it was asked. The corpora have to be separated.
PROSE_EXTS = frozenset({
    ".md", ".mdx", ".markdown", ".rst", ".txt", ".adoc", ".org",
})

# Trees that are prose or fixtures regardless of extension.
PROSE_DIR_PARTS = ("/docs/", "/doc/", "/snapshots/", "/__snapshots__/", "/.archive/",
                   "/adr/", "/rfc/", "/changelog/")


def is_prose(file_path: str) -> bool:
    """Whether a path holds prose about code rather than code itself.

    Deliberately conservative: an unknown extension counts as CODE, because demoting a real
    implementation is a worse failure than admitting one more doc — the bug being fixed here is
    prose crowding code out, and an over-eager rule would just invert it."""
    if not file_path:
        return False
    p = "/" + str(file_path).replace(os.sep, "/").lstrip("/")
    if os.path.splitext(p)[1].lower() in PROSE_EXTS:
        return True
    return any(part in p.lower() for part in PROSE_DIR_PARTS)


def partition_by_corpus(items: list, path_of=lambda m: m.get("path", "")) -> tuple[list, list]:
    """Split ranked hits into (code, prose), preserving each list's existing order."""
    code, prose = [], []
    for m in items:
        (prose if is_prose(str(path_of(m) or "")) else code).append(m)
    return code, prose
