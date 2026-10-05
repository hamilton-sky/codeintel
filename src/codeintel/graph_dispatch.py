"""Callers that reach a method through the type it is CALLED against, not the type that defines it.

THE GAP. A call site is bound to a method on the type its receiver is DECLARED as. In
`provider.build_result(...)`, where `provider: CodeProvider`, that is the Protocol's declaration —
`CodeProvider.build_result` — and not `GraphProvider.build_result`, the method that actually runs. No
provider inherits the Protocol (it is structural typing), so the graph has no INHERITS edge to join
the two, and `callers GraphProvider.build_result` listed the tests that construct a `GraphProvider`
and none of the three production call sites. It answered `confidence: complete` and
`safe_for_destructive: true` while every production caller of the method was on the OTHER node —
the dangerous direction, because the next action is a delete or a rename.

WHAT THIS DOES. When the target is a method `C.m`, it finds the methods a call site may have been
written against, asks `callers` about each of them with the SAME machinery (`_fetch_edges`, the
collision filter, the collapse and the cap — there is no second labeller), and lists those callers
apart, under a heading that names the base. Two kinds of base:

* NOMINAL — an ancestor of `C` that also defines `m`. Found through INHERITS edges and, where the
  index has none, through the `base_classes` names the class statement wrote, resolved to an indexed
  class when exactly one candidate carries the name. The answer says which it used.
* STRUCTURAL (Python) — a Protocol that declares `m` where `C`, with its nominal ancestors, defines
  EVERY member the Protocol declares (its own, and those of the Protocols it inherits). That is a
  check on member NAMES, not on signatures, and the answer says so. A member `C` lacks as a method
  but that the Protocol declares as a property could be satisfied by an attribute, and the graph
  records no class-level attribute at all — so that case is UNDECIDED, not "does not conform": the
  Protocol's callers are listed as `protocol-undecided` and the answer is `partial`. Only a Protocol
  METHOD the class provably lacks makes it contribute nothing.

WHAT THE ROWS MAY NEVER CLAIM. A call to the base is not a binding to the override: whether it
reaches `C.m` depends on the object's class at run time, which no static graph knows. So these rows
are never `verified` for the target, they keep their own edge strategy in `why` as the account of how
they reached the BASE, and the envelope goes `partial` with a named gap (`callers-via-base`) whenever
any exist. Ranked and labelled, never filtered — with one exception, which is a PROOF and not a
doubt: a `super()` call can only reach the override of the class it is written in, so the target's
own call to its base, and a `super()` call from a class that does not descend from the target's, can
never reach the target. Those are left out, and COUNTED in a line under the section.

The same hierarchy data answers a smaller question that has the same root cause: a `self.m()` call
inside a class whose base defines `m` is recorded by the backend as a name match, because it cannot
see across the class hierarchy. `_upgrade_self_calls` re-derives that binding from the hierarchy —
under a rule strict enough to refuse whenever it cannot be sure, and which verifies a row ONLY when
every link of the route it found is an INHERITS edge — and relabels such a row `self_mro`.

A MIXIN, for the reason given at the top of `graph_answer.py`: it reads `self._query_rows` and
`self._fetch_edges`, the seams the test stubs replace by assignment.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Any

from codeintel.graph_answer import AnswerRendering
from codeintel.graph_confidence import _NAME_MATCHED, _OWN_RECEIVERS, _edge_confidence, _evidence_class
from codeintel.graph_edges import _CALLER_KINDS, _DIRECT_KIND, _EdgeFetch, _EdgeGroup, _group_edges
from codeintel.graph_render import _cypher_literal, _in_list, _lang_family, _node_labels, _strip_project_prefix
from codeintel.graph_targets import _qualified_name_matches, _SymbolTarget
from codeintel.per_thread import PerThread
from codeintel.provider import log_swallowed

# How far up the class hierarchy to walk. Deep enough for any hierarchy a person wrote by hand (this
# repository's own deepest is four), shallow enough that a pathological or cyclic one costs a bounded
# number of backend calls; reaching it with classes still to expand is a disclosed gap, not silence.
_MAX_DEPTH = 6

# How many base methods one answer asks callers of. Each costs up to three backend calls, and a
# method declared on more than ten ancestors is not a shape a reader can act on row by row.
_MAX_BASES = 10

# Row limits on the lookups below. A lookup that comes back AT its limit may be missing rows, and is
# reported as such rather than read as the whole answer.
_LOOKUP_ROWS = 200
_MEMBER_ROWS = 2000

# All the lookups one op makes beyond the direct query, together, may take this many times the
# per-call budget of the request. Each call is also capped to what is left, so a backend that has
# stopped answering costs a bounded number of timeouts however deep the hierarchy is — before this,
# every base in turn could spend its own three.
_LOOKUP_TIMEOUTS = 6

# Bases that define nothing a `self.m()` or a Protocol check could be waiting on, and that no index
# holds as a class: leaving them in would make every Protocol and every `Generic` subclass look as if
# it had an ancestor the index could not resolve.
_INERT_BASES = frozenset({"object", "Protocol", "Generic", "ABC", "ABCMeta"})

# A class with one of these among its bases is a metaclass: `self` in its methods is a CLASS, whose
# attribute lookup is not the instance MRO the `self.m()` rule reasons about.
_METACLASS_BASES = frozenset({"type", "ABCMeta", "EnumMeta", "EnumType"})

# Decorators that make a method node stand for a member read like an attribute. A class can satisfy
# such a member with a plain attribute, which the graph does not record, so a Protocol member carrying
# one cannot be said to be missing.
_ATTRIBUTE_DECORATORS = frozenset({
    "property", "cached_property", "abstractproperty", "getter", "setter", "deleter"})

# What `_base_names` returns for a `base_classes` value it cannot read. Surfaced as an unresolved base
# rather than as "no bases": a dialect this release cannot parse must not be read as an empty hierarchy.
_UNREADABLE = "<unreadable base_classes>"

# A constructor is chosen by NAMING the class, not by a receiver's declared type, so a call to a base
# class's `__init__` is never a call that could have reached the subclass's.
_CONSTRUCTORS = frozenset({"__init__", "__new__"})

# At most this many distinct methods are looked up for the `self.m()` rule in one answer; a row past
# it simply stays name-matched, which is exactly what it was before the rule existed.
_SELF_CALL_CAP = 200

# The clock the overall deadline reads. A name of its own so a test can drive it without patching
# `time.monotonic` for everything else that runs in the process.
_clock = time.monotonic


def _base_names(value: Any) -> tuple[str, ...]:
    """The names a list-valued cell holds, from `base_classes` (or `decorators`) as the backend stores
    it — a JSON list inside a quoted cell over the 0.10 text dialect, a list over 0.9."""
    if isinstance(value, list | tuple):
        items: list[Any] = list(value)
    else:
        text = str(value if value is not None else "").strip()
        if not text or text == "-":
            return ()
        try:
            parsed = json.loads(text)
        except ValueError:
            return (_UNREADABLE,)
        if not isinstance(parsed, list):
            return (_UNREADABLE,)
        items = parsed
    return tuple(s for s in (str(x).strip() for x in items) if s)


def _leaf(written: str) -> str:
    """The class name a base expression denotes: `typing.Protocol[T]` is `Protocol`."""
    return written.split("[", 1)[0].strip().rsplit(".", 1)[-1]


def _decorator_leaves(value: Any) -> frozenset[str]:
    """The last name of each decorator on a method: `@size.setter(...)` is `setter`."""
    return frozenset(
        d if d == _UNREADABLE else d.lstrip("@").split("(", 1)[0].strip().rsplit(".", 1)[-1]
        for d in _base_names(value))


def _attribute_capable(decorators: frozenset[str]) -> bool:
    """Whether a member carrying these decorators could be satisfied by an attribute. An unreadable
    decorator column counts: "cannot tell" is not "it is a plain method"."""
    return bool(decorators & (_ATTRIBUTE_DECORATORS | {_UNREADABLE}))


@dataclass(frozen=True)
class _ClassNode:
    """A class as the index holds it: where it is, and the bases its statement wrote."""

    qualified: str            # exactly as the backend stores it, project prefix included
    name: str
    file: str
    bases: tuple[str, ...] = ()

    @property
    def is_protocol(self) -> bool:
        return any(_leaf(b) == "Protocol" for b in self.bases)

    @property
    def is_metaclass(self) -> bool:
        return any(_leaf(b) in _METACLASS_BASES for b in self.bases)


def _class_from(row: dict, prefix: str) -> _ClassNode | None:
    qualified = str(row.get(f"{prefix}.qualified_name") or "")
    if not qualified:
        return None
    return _ClassNode(
        qualified, str(row.get(f"{prefix}.name") or qualified.rsplit(".", 1)[-1]),
        str(row.get(f"{prefix}.file_path") or ""), _base_names(row.get(f"{prefix}.base_classes")))


@dataclass
class _Hierarchy:
    """The classes around some roots, grown level by level, and how each link between them was found.

    `how` is `inherits` when the backend recorded an INHERITS edge and `base_classes` when the index
    had none and the link was made by resolving the NAME the class statement wrote — a weaker claim,
    and one the answer states. `unresolved` holds the base names that resolved to no single indexed
    class (a library base, an ambiguous name): they are what makes "no ancestor defines `m`" a
    statement about the INDEX and not about the program.

    One of these lives for one op (`_Scope`) and is grown by whoever needs a class's ancestry, so
    `impact` — which asks for the same classes from its callers half and its callees half — reads the
    backend once. `expanded` is what has been asked, `cut_at` the classes the depth cap stopped at, and
    `failure` a lookup that did not complete: after it, everything not yet linked is unknown."""

    nodes: dict[str, _ClassNode]
    parents: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    unresolved: dict[str, list[str]] = field(default_factory=dict)
    expanded: set[str] = field(default_factory=set)
    cut_at: set[str] = field(default_factory=set)
    failure: str = ""

    def link(self, child: str, parent: _ClassNode, how: str) -> None:
        self.nodes.setdefault(parent.qualified, parent)
        edges = self.parents.setdefault(child, [])
        if all(qn != parent.qualified for qn, _ in edges):
            edges.append((parent.qualified, how))

    def ancestors(self, qualified: str) -> list[str]:
        """Every ancestor, nearest first, each once."""
        out: list[str] = []
        seen = {qualified}
        level = [qualified]
        while level:
            nxt: list[str] = []
            for c in level:
                for parent, _ in self.parents.get(c, []):
                    if parent not in seen:
                        seen.add(parent)
                        out.append(parent)
                        nxt.append(parent)
            level = nxt
        return out

    def cut_above(self, qualified: str) -> bool:
        """Whether the walk stopped at the depth cap somewhere in this class's ancestry."""
        return any(q in self.cut_at for q in (qualified, *self.ancestors(qualified)))

    def route(self, child: str, ancestor: str, *, only: str = "") -> list[tuple[str, str]]:
        """The shortest chain of `(class, how it was reached)` from `child` up to `ancestor` — over
        the links found `only` that way when that is given."""
        came: dict[str, tuple[str, str]] = {}
        seen = {child}
        level = [child]
        while level and ancestor not in came:
            nxt: list[str] = []
            for c in level:
                for parent, how in self.parents.get(c, []):
                    if parent not in seen and (not only or how == only):
                        seen.add(parent)
                        came[parent] = (c, how)
                        nxt.append(parent)
            level = nxt
        chain: list[tuple[str, str]] = []
        at = ancestor
        while at in came:
            below, how = came[at]
            chain.append((at, how))
            at = below
        chain.reverse()
        return chain

    def how(self, child: str, ancestor: str) -> str:
        """`inherits` when INHERITS edges alone lead from `child` up to `ancestor`."""
        return "inherits" if self.route(child, ancestor, only="inherits") else "base_classes"


@dataclass
class _Scope:
    """What one op has already asked the backend, and how long it may keep asking.

    The hierarchy and the method listings are facts about the INDEX, so they are shared by everything
    the op does with them — the dispatch lookup and the `self.m()` rule, and for `impact` both of its
    halves. The clock starts at the first extra lookup, not at the op: the direct query is the
    answer, and these are an addition to it."""

    timeout_ms: int
    hierarchy: _Hierarchy = field(default_factory=lambda: _Hierarchy({}))
    members: dict[str, dict[str, frozenset[str]]] = field(default_factory=dict)
    deadline: float | None = None

    def left_ms(self) -> int:
        now = _clock()
        if self.deadline is None:
            self.deadline = now + _LOOKUP_TIMEOUTS * self.timeout_ms / 1000
        return int((self.deadline - now) * 1000)


@dataclass(frozen=True)
class _Definer:
    """A class, and the method of that name it defines."""

    cls: _ClassNode
    method: str               # the method's qualified name, as stored
    method_file: str

    @property
    def short(self) -> str:
        """`Class.method`: what a sentence calls it."""
        return f"{self.cls.name}.{self.method.rsplit('.', 1)[-1]}"


@dataclass(frozen=True)
class _DispatchBase:
    """A method a call site may have been written against instead of the target."""

    owner: _ClassNode
    method: str
    method_file: str
    kind: str                 # "protocol" | "protocol-undecided" | "base"
    how: str                  # "inherits" | "base_classes" | "structure" | "undecided"

    @property
    def label(self) -> str:
        return _strip_project_prefix(self.method, may_be_filename=False)

    @property
    def short(self) -> str:
        return f"{self.owner.name}.{self.method.rsplit('.', 1)[-1]}"


@dataclass
class _Dispatch:
    """What was found about the target's bases, and every way the finding falls short.

    `unknown` is set when a shortfall leaves callers through a base UNCOUNTED (a lookup failed, was
    cut or was capped), as opposed to a shortfall that only leaves their relevance undecided: the
    first makes the envelope's total unknown, the second does not."""

    target: _Definer | None = None
    bases: list[_DispatchBase] = field(default_factory=list)
    shortfalls: list[str] = field(default_factory=list)
    unknown: bool = False


@dataclass
class _Skipped:
    """The callers of one base that provably cannot reach the target, and so are not listed."""

    itself: bool = False
    others: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class _DirectTarget:
    """What the direct answer already established about the target symbol, before any new lookup.

    `count` is how many distinct symbols the target matched: the lookup runs for exactly one, because
    an answer over several same-named symbols is already `target-ambiguous` and is narrowed by asking
    again. `qn` is that one symbol's qualified name, `""` when the direct answer had no rows to name
    it. `is_method` is read from the row labels; an answer with no rows cannot say, and is asked."""

    count: int = 0
    qn: str = ""
    is_method: bool = True


def _direct_target(selected: list[_EdgeGroup]) -> _DirectTarget:
    if not selected:
        return _DirectTarget()
    group = selected[0]
    labels = next((r.get("labels(b)") for r in group.rows if r.get("labels(b)") is not None), None)
    return _DirectTarget(len(selected), group.qn_raw, "Method" in _node_labels(labels))


def _self_call_of(row: dict) -> str:
    """`m` when this row is a name-matched call written EXACTLY `self.m` or `cls.m` from a method to a
    method of the same name in Python, else `""`.

    Narrow on purpose. The recorded call text is the only thing that says the receiver is the
    enclosing object, so any other spelling (`gp.m`, `self.helper.m`, `super().m`) is some other
    value and is left exactly as the backend labelled it."""
    if _evidence_class(str(row.get("strategy") or "")) != "name-guess":
        return ""
    receiver, dot, leaf = str(row.get("callee") or "").strip().rpartition(".")
    if not dot or receiver not in _OWN_RECEIVERS["python"] or leaf != str(row.get("b.name") or ""):
        return ""
    if _lang_family(str(row.get("a.file_path") or "")) != "python":
        return ""
    if "Method" not in _node_labels(row.get("labels(a)")) or "Method" not in _node_labels(
            row.get("labels(b)")):
        return ""
    return leaf


def _super_call(row: dict, name: str) -> bool:
    """Whether the recorded call text is `super().name` or `super(C, self).name`."""
    callee = str(row.get("callee") or "").strip()
    return re.fullmatch(r"super\s*\([^()]*\)\s*\.\s*" + re.escape(name), callee) is not None


def _names_the_base(row: dict, base: _DispatchBase) -> bool:
    """Whether the recorded call text is the unbound `Base.m` form, as in `Base.m(self)`."""
    callee = str(row.get("callee") or "").strip()
    return callee == base.short or callee.endswith("." + base.short)


def _pick_class(candidates: list[_ClassNode], written: str, child: _ClassNode) -> _ClassNode | None:
    """The one indexed class a base NAME denotes, or `None` when the name does not settle it.

    A dotted spelling (`pkg.Base`) must match an indexed class by its path: `torch.nn.Module` names a
    class this index does not hold, however many project classes are called `Module`, and falling back
    to the bare name would hand the child a hierarchy it never had. For a bare name, a class defined
    in the child's OWN file wins, because a bare name in a class statement is looked up in the module's
    scope first; failing that, the name must be unique in the index. An ambiguous name is unresolved,
    not guessed — binding a base to the wrong class would hand its methods to the wrong hierarchy."""
    pool = [c for c in candidates if c.qualified != child.qualified]
    dotted = written.split("[", 1)[0].strip()
    if "." in dotted:
        pool = [c for c in pool if _qualified_name_matches(c.qualified, dotted)]
    if len(pool) == 1:
        return pool[0]
    same_file = [c for c in pool if c.file == child.file]
    return same_file[0] if len(same_file) == 1 else None


@dataclass
class _Via:
    """The rows that reach the target through one base method."""

    base: _DispatchBase
    group: _EdgeGroup


class DispatchCallers(AnswerRendering):
    """Callers through a base, and the `self.m()` rule. Mixed into `GraphOps`.

    Requires `self._query_rows` and `self._fetch_edges` from the class it joins, and every gap it
    raises is also stated in the body, because `gaps` is what an integration branches on and the
    body is what an agent reads."""

    if TYPE_CHECKING:
        # Annotations and not `def` stubs, which is how `graph_changed.py` declares its own seams: the
        # source-reading tests key every function by NAME across the provider's whole MRO.
        _last_failure: Any
        _clear_failure: Callable[[], None]
        _query_rows: Callable[[str, str, int], list[dict]]
        _fetch_edges: Callable[..., _EdgeFetch]

    # The scope of the op in progress on this thread, if any. Per thread because the provider is
    # shared by concurrent requests, and `changed <ref>` runs its lookups on a pool.
    _lookup_state: PerThread[_Scope | None] = PerThread(None)

    # ------------------------------------------------------------------------- guarded lookups

    @contextmanager
    def _lookup_scope(self, timeout_ms: int) -> Iterator[_Scope]:
        """The scope of one op. Re-entrant: `impact` opens it, and the callers and callees it runs
        inside join it rather than starting their own."""
        active = self._lookup_state
        if active is not None:
            yield active
            return
        scope = self._lookup_state = _Scope(timeout_ms)
        try:
            yield scope
        finally:
            self._lookup_state = None

    def _active_scope(self, timeout_ms: int) -> _Scope:
        return self._lookup_state or _Scope(timeout_ms)

    def _guarded(
        self, ask: Callable[[int], Any], timeout_ms: int, *, surface: bool = True
    ) -> tuple[Any, str]:
        """One backend lookup, and WHY it failed when it did — `""` when it did not. Never raises.

        `ask` receives the time it may take: the per-call budget, or what is left of the op's overall
        allowance when that is less. Past the allowance nothing is asked and the lookup is reported
        as the shortfall it is.

        The backend records a failure on `_last_failure` and returns an empty reply, which the ops
        read as "no rows". That is the very misreading a lookup that decides whether a method has
        callers cannot afford, so the failure state is cleared before the call and read after it.

        `surface` says whether the failure belongs to the ANSWER. A failure the answer depends on is
        left on `_last_failure`, where `build_result` turns it into a gap and a body note. A failure
        in an optional improvement (the `self.m()` rule) changes nothing the answer claims, so the
        earlier state is restored and the failure is only returned."""
        budget = min(timeout_ms, self._active_scope(timeout_ms).left_ms())
        if budget <= 0:
            return None, (f"the time allowed for these lookups ({_LOOKUP_TIMEOUTS} times the "
                          f"{timeout_ms / 1000:g}s budget of one) ran out")
        prior = self._last_failure
        self._clear_failure()
        value: Any = None
        why = ""
        try:
            value = ask(budget)
        except Exception as exc:
            log_swallowed("DispatchCallers._guarded", exc)
            why = "the lookup raised unexpectedly"
        miss = self._last_failure
        if miss is not None:
            why = miss.describe()
        if miss is None or not surface:
            self._last_failure = prior
        return value, why

    def _lookup(self, cypher: str, project: str, timeout_ms: int, *,
                surface: bool = True) -> tuple[list[dict], str]:
        rows, why = self._guarded(
            lambda budget: self._query_rows(cypher, project, budget), timeout_ms, surface=surface)
        return (rows if isinstance(rows, list) else []), why

    def _classes_defining(
        self, name: str, project: str, timeout_ms: int
    ) -> tuple[list[_Definer], str]:
        """Every class that defines a method called `name`, with the bases its statement wrote.

        One query serves three questions: which class is the target's, which of its ancestors define
        the method, and which Protocols declare it — all three are "a class defining a method of this
        name". A reply at its limit is reported, because the target may be past it."""
        rows, why = self._lookup(
            "MATCH (c:Class)-[:DEFINES_METHOD]->(m) "
            f'WHERE m.name="{_cypher_literal(name)}" '
            "RETURN c.qualified_name, c.name, c.file_path, c.base_classes, m.qualified_name, "
            f"m.file_path LIMIT {_LOOKUP_ROWS}", project, timeout_ms)
        if why:
            return [], f"the lookup of the classes defining `{name}` did not complete ({why})"
        out: list[_Definer] = []
        for r in rows:
            cls = _class_from(r, "c")
            method = str(r.get("m.qualified_name") or "")
            if cls is not None and method:
                out.append(_Definer(cls, method, str(r.get("m.file_path") or "")))
        short = (f"{_LOOKUP_ROWS} or more classes define a method called `{name}`, so the lookup of "
                 "its bases was cut and the target's bases may be among those not seen"
                 if len(rows) >= _LOOKUP_ROWS else "")
        return out, short

    def _members_of(
        self, classes: list[str], project: str, timeout_ms: int, *, surface: bool = True
    ) -> tuple[dict[str, dict[str, frozenset[str]]], str]:
        """The methods each class defines, with the decorators each carries — what a name-level
        conformance check compares. A class already listed in this op is not listed again."""
        scope = self._active_scope(timeout_ms)
        wanted = list(dict.fromkeys(classes))
        missing = [c for c in wanted if c not in scope.members]
        if missing:
            rows, why = self._lookup(
                "MATCH (c:Class)-[:DEFINES_METHOD]->(m) "
                f"WHERE c.qualified_name IN [{_in_list(missing)}] "
                f"RETURN c.qualified_name, m.name, m.decorators LIMIT {_MEMBER_ROWS}", project,
                timeout_ms, surface=surface)
            if why:
                return {}, why
            if len(rows) >= _MEMBER_ROWS:
                return {}, f"the method listing reached its limit of {_MEMBER_ROWS} rows"
            listed: dict[str, dict[str, frozenset[str]]] = {c: {} for c in missing}
            for r in rows:
                owner, name = str(r.get("c.qualified_name") or ""), str(r.get("m.name") or "")
                if owner in listed and name:
                    listed[owner][name] = (listed[owner].get(name, frozenset())
                                           | _decorator_leaves(r.get("m.decorators")))
            scope.members.update(listed)
        return {c: scope.members[c] for c in wanted}, ""

    def _method_owners(
        self, methods: list[str], project: str, timeout_ms: int, *, surface: bool = True
    ) -> tuple[dict[str, _ClassNode], str]:
        """The class that defines each of these methods, by the method's qualified name."""
        rows, why = self._lookup(
            "MATCH (c:Class)-[:DEFINES_METHOD]->(m) "
            f"WHERE m.qualified_name IN [{_in_list(methods)}] "
            "RETURN c.qualified_name, c.name, c.file_path, c.base_classes, m.qualified_name "
            f"LIMIT {_MEMBER_ROWS}", project, timeout_ms, surface=surface)
        if why:
            return {}, why
        owner_of: dict[str, _ClassNode] = {}
        for r in rows:
            cls, method = _class_from(r, "c"), str(r.get("m.qualified_name") or "")
            if cls is not None and method:
                owner_of[method] = cls
        return owner_of, ""

    def _grow_hierarchy(
        self, roots: list[_ClassNode], project: str, timeout_ms: int, *, surface: bool = True
    ) -> _Hierarchy:
        """The ancestry of `roots`, one level per round trip, to `_MAX_DEPTH` levels — added to the
        hierarchy this op already holds, so a class's ancestry is read once however many ask.

        Each level asks the backend for the INHERITS edges of the classes it is expanding and then,
        for any base NAME those edges do not account for, which indexed class carries it. Only
        constructs this module already relies on are used (`IN [...]`, a label, a relationship type);
        a variable-length path would be quicker and is a part of the dialect nothing here has pinned.

        A failed or capped lookup stops the walk and says so on the result: what was linked before it
        is still true, and `failure` makes everything past it unknown rather than absent. Once a walk
        has failed, nothing more is asked — a backend that did not answer once is not asked again."""
        h = self._active_scope(timeout_ms).hierarchy
        for root in roots:
            h.nodes.setdefault(root.qualified, root)
        frontier = [root.qualified for root in roots]
        for _ in range(_MAX_DEPTH):
            if h.failure:
                return h
            frontier = [q for q in dict.fromkeys(frontier) if q not in h.expanded]
            if not frontier:
                return h
            h.expanded.update(frontier)
            h.cut_at.difference_update(frontier)       # an earlier walk stopped here; this one goes on

            # A Python class with no bases has no INHERITS edge to find; any other language's
            # `extends` may be recorded as one with no `base_classes` beside it, so those are asked.
            asked = [q for q in frontier
                     if h.nodes[q].bases or _lang_family(h.nodes[q].file) != "python"]
            inherits: dict[str, list[_ClassNode]] = {}
            if asked:
                rows, why = self._lookup(
                    "MATCH (c:Class)-[:INHERITS]->(p:Class) "
                    f"WHERE c.qualified_name IN [{_in_list(asked)}] "
                    "RETURN c.qualified_name, p.qualified_name, p.name, p.file_path, p.base_classes "
                    f"LIMIT {_LOOKUP_ROWS}", project, timeout_ms, surface=surface)
                if why or len(rows) >= _LOOKUP_ROWS:
                    h.failure = (f"the class hierarchy could not be read completely ({why})" if why
                                 else "the INHERITS lookup returned its maximum of "
                                      f"{_LOOKUP_ROWS} rows, so the hierarchy may be missing links")
                    return h
                for r in rows:
                    child, parent = str(r.get("c.qualified_name") or ""), _class_from(r, "p")
                    if child in h.nodes and parent is not None and parent.qualified != child:
                        inherits.setdefault(child, []).append(parent)

            # Base names the INHERITS edges do not account for — an import the backend could not
            # follow, a class it never linked. These are resolved by NAME, and recorded as such.
            needed: dict[str, list[tuple[str, str]]] = {}
            for child in frontier:
                accounted = {p.name for p in inherits.get(child, [])}
                for written in h.nodes[child].bases:
                    leaf = _leaf(written)
                    if written == _UNREADABLE:
                        h.unresolved.setdefault(child, []).append(written)
                    elif leaf and leaf not in _INERT_BASES and leaf not in accounted:
                        needed.setdefault(leaf, []).append((child, written))
            named: dict[str, list[_ClassNode]] = {}
            if needed:
                rows, why = self._lookup(
                    f"MATCH (p:Class) WHERE p.name IN [{_in_list(list(needed))}] "
                    "RETURN p.qualified_name, p.name, p.file_path, p.base_classes "
                    f"LIMIT {_LOOKUP_ROWS}", project, timeout_ms, surface=surface)
                if why or len(rows) >= _LOOKUP_ROWS:
                    h.failure = (f"the base-class names could not be resolved ({why})" if why
                                 else "the lookup of base-class names returned its maximum of "
                                      f"{_LOOKUP_ROWS} rows, so a name may have resolved wrongly")
                    return h
                for r in rows:
                    parent = _class_from(r, "p")
                    if parent is not None:
                        named.setdefault(parent.name, []).append(parent)

            nxt: list[str] = []
            for child in frontier:
                for parent in inherits.get(child, []):
                    h.link(child, parent, "inherits")
                    nxt.append(parent.qualified)
            for leaf, uses in needed.items():
                for child, written in uses:
                    parent_or_none = _pick_class(named.get(leaf, []), written, h.nodes[child])
                    if parent_or_none is None:
                        h.unresolved.setdefault(child, []).append(written)
                    else:
                        h.link(child, parent_or_none, "base_classes")
                        nxt.append(parent_or_none.qualified)
            frontier = nxt
        left = [q for q in dict.fromkeys(frontier) if q not in h.expanded]
        h.cut_at.update(q for q in left
                        if h.nodes[q].bases or _lang_family(h.nodes[q].file) != "python")
        return h

    # ---------------------------------------------------------------- the bases of a target

    def _dispatch_bases(
        self, wanted: _SymbolTarget, direct: _DirectTarget, project: str, timeout_ms: int
    ) -> _Dispatch:
        """The methods the target's callers may have been written against.

        Returns empty without a lookup when the target is not one method (several symbols answered,
        or the rows say it is a function) and with one when it is: a target that is not a method of
        an indexed class has no bases to find, and says nothing about it."""
        out = _Dispatch()
        if not wanted.name or wanted.name in _CONSTRUCTORS:
            return out
        if direct.count > 1 or not direct.is_method:
            return out
        definers, short = self._classes_defining(wanted.name, project, timeout_ms)
        if short:
            out.shortfalls.append(short)
            out.unknown = True
        # By qualified name, because a method with overloads or a property setter can be listed once
        # per definition and is still ONE method.
        mine = {d.method: d for d in definers if wanted.matches(d.method, d.method_file)}
        if direct.qn:
            mine = {qn: d for qn, d in mine.items() if qn == direct.qn}
        if len(mine) > 1:
            out.shortfalls.append(
                f"{len(mine)} methods named `{wanted.name}` match this target "
                f"({', '.join(f'`{_strip_project_prefix(qn, may_be_filename=False)}`' for qn in mine)}), "
                "so which one's bases to look up is not settled")
            out.unknown = True
        if len(mine) != 1:
            return out
        target = out.target = next(iter(mine.values()))
        own = target.cls

        h = self._grow_hierarchy([own], project, timeout_ms)
        ancestors = h.ancestors(own.qualified)
        by_class = {d.cls.qualified: d for d in definers}
        found: list[_DispatchBase] = []
        for qn in ancestors:
            d = by_class.get(qn)
            if d is not None:
                found.append(_DispatchBase(
                    d.cls, d.method, d.method_file, "protocol" if d.cls.is_protocol else "base",
                    h.how(own.qualified, qn)))

        walked = [own]
        if _lang_family(own.file) == "python":
            inherited = set(ancestors)
            protocols = sorted(
                (d for d in definers
                 if d.cls.is_protocol and _lang_family(d.cls.file) == "python"
                 and d.cls.qualified != own.qualified and d.cls.qualified not in inherited),
                key=lambda d: d.cls.qualified)
            if protocols:
                walked += [p.cls for p in protocols]
                found += self._protocol_bases(wanted, own, [own.qualified, *ancestors], protocols,
                                              project, timeout_ms, out)

        if h.failure:
            out.shortfalls.append(f"{h.failure}; bases past the failure are unknown")
            out.unknown = True
        cut = [c for c in walked if h.cut_above(c.qualified)]
        if cut:
            out.shortfalls.append(
                f"the ancestry of {', '.join(f'`{c.name}`' for c in cut)} is deeper than "
                f"{_MAX_DEPTH} levels, and what lies past that was not followed")
            out.unknown = True
        if len(found) > _MAX_BASES:
            out.shortfalls.append(
                f"{len(found) - _MAX_BASES} further base method(s) were not asked about: the lookup "
                f"stops at {_MAX_BASES}, nearest ancestors first")
            out.unknown = True
            found = found[:_MAX_BASES]
        out.bases = found
        return out

    def _protocol_bases(
        self, wanted: _SymbolTarget, own: _ClassNode, chain: list[str], protocols: list[_Definer],
        project: str, timeout_ms: int, out: _Dispatch
    ) -> list[_DispatchBase]:
        """The Protocols that declare the target's method and that `own` satisfies — or may.

        A Protocol requires every member it declares and every member of the Protocols it inherits.
        `own`, with its nominal ancestors (`chain`), satisfies it when it defines a method of each name.
        When it does not, the question is whether the missing member could be an ATTRIBUTE:

        * a member the Protocol declares as a property could be satisfied by one, and the graph records
          no class-level assignment or dataclass field at all;
        * a base this index cannot resolve could supply any member.

        Either makes it UNDECIDED, and an undecided Protocol is listed (its callers are the ones that
        would reach the target if it does) and named as a gap. Only a plain Protocol METHOD that `own`
        provably lacks rules the Protocol out, and then it contributes nothing."""
        # Only a Protocol that names a base other than `Protocol` itself can inherit members.
        h = self._grow_hierarchy(
            [p.cls for p in protocols if any(_leaf(b) not in _INERT_BASES for b in p.cls.bases)],
            project, timeout_ms)
        needs_of = {
            p.cls.qualified: [p.cls.qualified, *(q for q in h.ancestors(p.cls.qualified)
                                                 if h.nodes[q].is_protocol)]
            for p in protocols}
        members, why = self._members_of(
            list(dict.fromkeys([*chain, *(q for qs in needs_of.values() for q in qs)])),
            project, timeout_ms)
        if why:
            out.shortfalls.append(
                f"whether `{own.name}` satisfies the Protocol(s) "
                f"{', '.join(f'`{p.cls.name}`' for p in protocols)} could not be checked ({why})")
            out.unknown = True
            return []
        have: set[str] = set().union(*(members.get(q, {}).keys() for q in chain))
        blind = [n for q in chain for n in h.unresolved.get(q, [])]
        found: list[_DispatchBase] = []
        for p in protocols:
            required: dict[str, frozenset[str]] = {}
            for q in needs_of[p.cls.qualified]:
                for name, decorators in members.get(q, {}).items():
                    required[name] = required.get(name, frozenset()) | decorators
            if not required:
                continue
            lacking = sorted(set(required) - have)
            if not lacking:
                found.append(_DispatchBase(p.cls, p.method, p.method_file, "protocol", "structure"))
                continue
            attribute_like = [n for n in lacking if _attribute_capable(required[n])]
            if len(attribute_like) < len(lacking) and not blind:
                continue
            found.append(_DispatchBase(
                p.cls, p.method, p.method_file, "protocol-undecided", "undecided"))
            reasons = []
            if attribute_like:
                reasons.append(
                    f"an attribute or property could satisfy {', '.join(f'`{n}`' for n in attribute_like)}"
                    " (the index records neither a class-level attribute nor a dataclass field)")
            if blind:
                reasons.append(
                    f"`{own.name}` has base classes this index cannot resolve "
                    f"({', '.join(f'`{b}`' for b in dict.fromkeys(blind))}), which may supply them")
            out.shortfalls.append(
                f"`{p.cls.name}` declares `{wanted.name}`, but `{own.name}` does not define every "
                f"member it declares (missing: {', '.join(lacking)}) and {'; and '.join(reasons)} — "
                f"whether it satisfies the Protocol is undecided, so the callers of "
                f"`{p.cls.name}.{wanted.name}` are listed as undecided")
        return found

    # ---------------------------------------------------------------- callers through a base

    def _callers_through_bases(
        self, target: str, wanted: _SymbolTarget, direct: _DirectTarget, project: str, timeout_ms: int
    ) -> tuple[str, int]:
        """`(text, rows)`: the callers that reach the target through a base, rendered as sections to
        append to the direct answer, and how many rows that is. `("", 0)` when there is nothing to
        say, which is the case for a target with no bases and keeps its answer byte-identical.

        An addition to an answer that is already whole, so a fault in it must not take that answer
        down: an unexpected exception here would otherwise reach `build_result`'s catch-all and turn a
        complete list of the symbol's own callers into `reason: "error"`. It is logged, and — when
        there is a direct answer for it to qualify — named as a gap, because "the bases were not
        looked up" is exactly the silence this module exists to end."""
        try:
            with self._lookup_scope(timeout_ms):
                return self._through_bases(target, wanted, direct, project, timeout_ms)
        except Exception as exc:
            log_swallowed("DispatchCallers._callers_through_bases", exc)
            if not direct.count:
                return "", 0
            detail = ("the lookup of the bases raised unexpectedly (set CODEINTEL_DEBUG=1 for the "
                      "traceback), so callers that reach this symbol through a base are unknown")
            self._add_gap("callers", "dispatch-bases-incomplete", detail)
            self._pending_row_cap = True
            return f"\n\n_{detail}._", 0

    def _through_bases(
        self, target: str, wanted: _SymbolTarget, direct: _DirectTarget, project: str, timeout_ms: int
    ) -> tuple[str, int]:
        dispatch = self._dispatch_bases(wanted, direct, project, timeout_ms)
        shortfalls = list(dispatch.shortfalls)
        unknown = dispatch.unknown
        if not dispatch.bases and (not shortfalls or not direct.count):
            # Nothing to add — and with no direct rows either there is no answer for a gap to qualify:
            # the caller reports the target as having no edges, which already says it is not proof.
            return "", 0

        fetched: list[tuple[_DispatchBase, list[dict]]] = []
        cut: list[tuple[str, int]] = []
        for i, base in enumerate(dispatch.bases):
            asked = _SymbolTarget(name=wanted.name, qualified=base.method, file_hint=base.method_file)
            got, why = self._guarded(
                partial(self._fetch_edges, _CALLER_KINDS, "b", "a", asked, project), timeout_ms)
            if why or not isinstance(got, _EdgeFetch):
                # The first failure ends the loop: a backend that did not answer once is not asked
                # again for each base that is left, and what was not asked is named.
                left = dispatch.bases[i + 1:]
                shortfalls.append(
                    f"the callers of `{base.short}` could not be fetched ({why or 'no answer'}), so "
                    "callers reaching the target through it are unknown"
                    + (f"; the callers of {', '.join(f'`{b.short}`' for b in left)} were not looked up"
                       if left else ""))
                unknown = True
                break
            if got.cut_short:
                cut.append((base.short, got.limit))
                unknown = True
            held = [g for g in _group_edges(got.rows, "b.name", "b.qualified_name", "b.file_path")
                    if asked.matches(g.qn_raw, g.file)]
            fetched.append((base, [r for g in held for r in g.rows]))

        skipped: dict[str, _Skipped] = {}
        if dispatch.target is not None:
            fetched, skipped = self._prune_unreachable(dispatch.target, wanted, fetched, project, timeout_ms)
        vias = [_Via(base, _EdgeGroup(base.label, base.method, base.method_file, rows))
                for base, rows in fetched]

        # The same three steps `callers` applies, in the same order and for the same reasons, over
        # every base at once so the cap bounds the section rather than each base separately. They
        # have to come before any count is printed, so a count describes the rows that are shown.
        groups = [v.group for v in vias]
        dropped = self._drop_edge_collisions(groups, "a.file_path", "labels(a)")
        self._collapse_module_scope(groups, "labels(a)", "a.file_path")
        self._collapse_repeat_edges(groups, "a.name", "a.qualified_name", "a.file_path")
        omitted = self._cap_distinct_edges(
            groups, "a.name", "a.qualified_name", "a.file_path", tests_last=True)
        for v in vias:
            v.group.rows = [self._mark_via(r, v.base) for r in v.group.rows]

        shown = [v for v in vias if v.group.rows]
        total = sum(len(v.group.rows) for v in shown)
        parts: list[str] = []
        if shown and dispatch.target is not None:
            own = dispatch.target.cls
            parts.extend(self._render_via(dispatch.target, own, v) for v in shown)
            self._record_rows(
                [r for v in shown for r in v.group.rows], ("a.name", "a.qualified_name", "a.file_path"),
                "caller", row_cap_hit=bool(cut), withheld=omitted.rows)
            named = "; ".join(f"`{v.base.short}` ({v.base.kind}, {len(v.group.rows)})" for v in shown)
            self._add_gap(
                "callers", "callers-via-base",
                f"callers were written against a base type or Protocol rather than against "
                f"`{dispatch.target.short}`: {named}. They are listed apart and counted as possible, "
                "never as verified callers of this symbol — whether a call reaches it depends on the "
                "object's class at run time, which no static graph knows. A caller that also calls "
                "the symbol directly is in the direct list as well")
        elif dispatch.bases and dispatch.target is not None and not shortfalls:
            idle = [b for b, _ in fetched if b.label not in skipped]
            if idle:
                parts.append(self._no_caller_through_bases(dispatch.target, idle))

        notes = self._collision_note("callers", dropped)
        if omitted.endpoints and not cut:
            notes = self._distinct_cap_note(
                "callers", "caller", ", ".join(v.base.short for v in shown), omitted,
                tests_last=True) + notes
        for label, limit in cut:
            notes = self._row_cap_note("callers", label, limit) + notes
        if dispatch.target is not None:
            for base, _ in fetched:
                if base.label in skipped:
                    notes += self._skipped_note(dispatch.target, base, skipped[base.label])
        if shortfalls:
            detail = "; ".join(shortfalls)
            self._add_gap("callers", "dispatch-bases-incomplete", detail)
            notes += (f"\n\n_The lookup of the bases this symbol's callers may have been written "
                      f"against fell short: {detail}. A caller that reaches it through one of them "
                      f"is unknown here, not absent._")
        if unknown:
            # Said on the envelope too: callers through a base that were never counted make the total
            # unknown, and an exact total beside a gap that says otherwise is the contradiction this
            # module exists to end. Set whether or not any row is shown.
            self._pending_row_cap = True
        if not parts and not shortfalls and not notes:
            return "", 0
        return "".join(parts) + notes, total

    def _prune_unreachable(
        self, target: _Definer, wanted: _SymbolTarget, fetched: list[tuple[_DispatchBase, list[dict]]],
        project: str, timeout_ms: int
    ) -> tuple[list[tuple[_DispatchBase, list[dict]]], dict[str, _Skipped]]:
        """Drop the rows that provably cannot reach the target; count them by base.

        `super()` binds to the class AFTER the one it is written in, so a call to a base method
        through it cannot reach:

        * the target itself — `Child.run` calling `super().run()` calls its base, and a method is not
          its own caller through the method it overrides;
        * an override in a class that does not descend from the target's — `Other.run` calling
          `super().run()` runs `Base.run`, and `Other` is a sibling of the target, not a subclass.

        A `super()` call in a class that DOES descend from the target's class can reach the target, and
        stays, labelled. So does one whose class this lookup cannot place: it is a doubt and not a
        proof, and a doubt is labelled, never filtered. What is dropped is a proof, and is counted."""
        skipped: dict[str, _Skipped] = {}
        own = target.cls
        supers = sorted({str(r.get("a.qualified_name") or "") for _, rows in fetched for r in rows
                         if str(r.get("a.qualified_name") or "") not in ("", target.method)
                         and _super_call(r, wanted.name)})
        owners: dict[str, _ClassNode] = {}
        h: _Hierarchy | None = None
        if supers:
            # An improvement over listing everything, not a claim: when it cannot run the rows stay,
            # and each says its class was not placed.
            owners, why = self._method_owners(supers, project, timeout_ms, surface=False)
            if not why and owners:
                h = self._grow_hierarchy(
                    list({c.qualified: c for c in owners.values()}.values()), project, timeout_ms,
                    surface=False)
        kept: list[tuple[_DispatchBase, list[dict]]] = []
        for base, rows in fetched:
            keep: list[dict] = []
            for r in rows:
                caller = str(r.get("a.qualified_name") or "")
                # The target's call to the method it overrides — `super().run()`, or the unbound
                # `Base.run(self)` — runs the base and cannot come back to the target. Any other call
                # the target makes to the base method (`self.inner.run()` on a `Base`-typed delegate)
                # CAN reach it, when the delegate is itself this class: a doubt, so it stays listed.
                if caller == target.method and (
                        _super_call(r, wanted.name) or _names_the_base(r, base)):
                    skipped.setdefault(base.label, _Skipped()).itself = True
                elif not _super_call(r, wanted.name):
                    keep.append(r)
                else:
                    verdict, via_class = self._super_verdict(owners.get(caller), h, own)
                    if verdict == "cannot":
                        skipped.setdefault(base.label, _Skipped()).others.add(caller)
                    elif verdict == "reaches":
                        keep.append(dict(r, _via_super=via_class))
                    else:
                        keep.append(dict(r, _via_super_unplaced=True))
            kept.append((base, keep))
        return kept, skipped

    @staticmethod
    def _super_verdict(caller_cls: _ClassNode | None, h: _Hierarchy | None, own: _ClassNode) -> tuple[str, str]:
        """`("reaches" | "cannot" | "unplaced", the caller's class name)` for a `super()` call."""
        if caller_cls is None or h is None or h.failure:
            return "unplaced", ""
        if caller_cls.qualified != own.qualified and own.qualified in h.ancestors(caller_cls.qualified):
            return "reaches", caller_cls.name
        # A base this index could not place, spelled like the target's class, may BE the target's class.
        chain = [caller_cls.qualified, *h.ancestors(caller_cls.qualified)]
        if h.cut_above(caller_cls.qualified) or any(
                _leaf(w) == own.name for q in chain for w in h.unresolved.get(q, [])):
            return "unplaced", caller_cls.name
        return "cannot", caller_cls.name

    @staticmethod
    def _skipped_note(target: _Definer, base: _DispatchBase, skipped: _Skipped) -> str:
        """The one line that says which callers of `base` were left out because they cannot reach the
        target, so a row that is not listed has not vanished unexplained."""
        what = []
        if skipped.itself:
            what.append(f"`{target.short}` itself, which calls the method it overrides")
        if skipped.others:
            what.append(f"{len(skipped.others)} `super()` call(s) from classes that do not descend from "
                        f"`{target.cls.name}` (other overrides of `{base.short}`)")
        return (f"\n\n_Not listed, because a call to `{base.short}` from them cannot reach "
                f"`{target.short}`: {' and '.join(what)}._")

    @staticmethod
    def _mark_via(row: dict, base: _DispatchBase) -> dict:
        """A copy of `row` that says it reached the target through `base` — never `verified`, and
        counted with the possible rows because it is a candidate and not a binding."""
        marked = dict(row)
        marked["_via"] = base.label
        marked["_via_kind"] = base.kind
        marked["_via_how"] = base.how
        marked["_bucket"] = _NAME_MATCHED
        marked["_low_confidence"] = _edge_confidence(row)
        return marked

    def _render_via(self, target: _Definer, own: _ClassNode, via: _Via) -> str:
        """One base's callers: a heading that says what they called, one line of how the base was
        found and what that does and does not tie to the target, then the rows."""
        base, rows = via.base, via.group.rows
        rows.sort(key=lambda r: str(r.get("type(c)") or "") != _DIRECT_KIND)
        direct = sum(1 for r in rows if str(r.get("type(c)") or "") == _DIRECT_KIND)
        count = (f"{len(rows)}" if direct == len(rows)
                 else f"{direct} direct, {len(rows) - direct} other reference(s)")
        what = "the base class" if base.kind == "base" else "the Protocol"
        head = (f"\n\n## Callers through `{base.short}` — they call {what}; at run time a call reaches "
                f"this override only when the object is a `{own.name}` ({count})\n")
        if base.how == "structure":
            how = (f"`{own.name}` does not inherit `{base.owner.name}`; it satisfies the Protocol by "
                   f"structure — it defines every method `{base.owner.name}` declares, checked by "
                   "method NAME and not by signature.")
        elif base.how == "undecided":
            how = (f"`{own.name}` does not inherit `{base.owner.name}`, and whether it satisfies the "
                   "Protocol is UNDECIDED: it defines no method for a member the Protocol declares, "
                   "and something the index cannot see could supply it (an attribute or property, or "
                   "a base class it cannot resolve). These are the callers that reach it if it does.")
        elif base.how == "inherits":
            how = f"`{own.name}` inherits `{base.owner.name}` (INHERITS edges in the index)."
        else:
            how = (f"`{own.name}` inherits `{base.owner.name}`, found by resolving the base-class "
                   "NAME its statement wrote — the index holds no INHERITS edge for it, so a class "
                   f"of that name imported from elsewhere would be taken for `{base.owner.name}`.")
        tied = (f"A call written against {what} is bound to its declaration, so none of the rows "
                f"below is a call the index ties to `{target.short}`; they are listed so a change "
                "here is not made blind to them.")
        supered = sum(1 for r in rows if r.get("_via_super"))
        if supered:
            tied += (f" {supered} of them are `super()` calls from a class that descends from "
                     f"`{own.name}`, so they can reach `{target.short}`.")
        lines = "\n".join(self._display(r, "a.name", "a.qualified_name", "a.file_path") for r in rows)
        return f"{head}_{how} {tied}_\n{lines}"

    @staticmethod
    def _no_caller_through_bases(target: _Definer, bases: list[_DispatchBase]) -> str:
        """The one line for a target that has bases and whose bases have no callers either."""
        def kind(b: _DispatchBase) -> str:
            if b.how == "structure":
                return "a Protocol it satisfies by method names"
            if b.how == "undecided":
                return "a Protocol it may satisfy"
            return "a Protocol it inherits" if b.kind == "protocol" else "its base class"

        named = ", ".join(f"`{b.short}` ({kind(b)})" for b in bases)
        return (f"\n\n_`{target.short}` is also declared on {named}; the graph records no caller of "
                f"{'it' if len(bases) == 1 else 'them'} either._")

    # ----------------------------------------------------------------- `self.m()` across classes

    def _upgrade_self_calls(self, groups: list[_EdgeGroup], project: str, timeout_ms: int) -> None:
        """Relabel name-matched `self.m()` / `cls.m()` rows `self_mro` when the class hierarchy binds
        them, in place and without mutating the backend's rows.

        The backend resolves a `self.m()` call by bare name, so a call to a method defined on a base
        or a mixin of the caller's own class — `GraphOps._op_callers` calling
        `self._render_edge_answer`, defined on `AnswerRendering` — arrives as a `unique_name` guess,
        and the answer then says the name belongs to "a library function". The call is not a guess:
        Python resolves `self.m` through the caller's class and then its ancestors, and the index
        holds those. The rule is one sentence and refuses whenever it cannot be sure of it:

            the call text is exactly `self.m` or `cls.m`; the caller is a method of class K, which is
            not a metaclass; the target's class T is K or one of K's ancestors, reached by INHERITS
            EDGES; and no class that could come before T in K's resolution order defines `m` or has
            a base this index could not resolve.

        "Could come before T" is every class in K's ancestry that is not T or one of T's own
        ancestors — those are shadowed by T's definition, and everything else is a candidate for
        winning the lookup, including a class on a side branch of a multiple inheritance. A subclass
        of K that overrides `m` is not considered: this is the binding from K's own hierarchy, which
        is also all `same_module` has ever claimed for a `self.` call.

        A link found by resolving a base NAME is not an edge: a project class that happens to share a
        name with the library class K really inherits would be taken for it, and the call promoted to
        verified is the library-collision failure `_name_resolution_note` warns about. Such a row
        stays name-matched and says why.

        Anything uncertain — a failed lookup, a hierarchy cut at the depth cap, an unresolvable base —
        leaves the row exactly as the backend labelled it. A failed lookup here is not a gap: the
        row stays name-matched and says so, which is no claim at all."""
        try:
            with self._lookup_scope(timeout_ms):
                self._relabel_self_calls(groups, project, timeout_ms)
        except Exception as exc:
            # An improvement that cannot make an answer worse than it was: the rows it did not reach
            # keep the label the backend gave them.
            log_swallowed("DispatchCallers._upgrade_self_calls", exc)

    def _relabel_self_calls(self, groups: list[_EdgeGroup], project: str, timeout_ms: int) -> None:
        pending = [(g, i, m) for g in groups for i, r in enumerate(g.rows) if (m := _self_call_of(r))]
        if not pending:
            return
        methods = sorted({str(g.rows[i].get(k) or "") for g, i, _ in pending
                          for k in ("a.qualified_name", "b.qualified_name")} - {""})[:_SELF_CALL_CAP]
        owner_of, why = self._method_owners(methods, project, timeout_ms, surface=False)
        if why or not owner_of:
            return
        roots = list({c.qualified: c for c in owner_of.values()}.values())
        h = self._grow_hierarchy(roots, project, timeout_ms, surface=False)
        if h.failure:
            return
        universe = sorted({q for c in roots for q in (c.qualified, *h.ancestors(c.qualified))})
        members, why = self._members_of(universe, project, timeout_ms, surface=False)
        if why:
            return
        for g, i, m in pending:
            row = g.rows[i]
            caller = owner_of.get(str(row.get("a.qualified_name") or ""))
            target = owner_of.get(str(row.get("b.qualified_name") or ""))
            if caller is None or target is None:
                continue
            reachable = [caller.qualified, *h.ancestors(caller.qualified)]
            if target.qualified not in reachable or h.cut_above(caller.qualified):
                continue
            if any(h.nodes[q].is_metaclass for q in reachable):
                continue
            shadowed = {target.qualified, *h.ancestors(target.qualified)}
            ahead = [q for q in reachable if q not in shadowed]
            if any(m in members.get(q, {}) or h.unresolved.get(q) for q in ahead):
                continue
            if caller.qualified != target.qualified and not h.route(
                    caller.qualified, target.qualified, only="inherits"):
                noted = dict(row)
                noted["_hierarchy_note"] = self._name_link_note(h, caller, target)
                g.rows[i] = noted
                continue
            upgraded = dict(row)
            upgraded["strategy"] = "self_mro"
            upgraded["_self_mro"] = self._self_mro_why(
                row, m, h, caller, target, str(row.get("strategy") or "").strip())
            g.rows[i] = upgraded

    @staticmethod
    def _name_link_note(h: _Hierarchy, caller: _ClassNode, target: _ClassNode) -> str:
        """Why a row the hierarchy would otherwise bind stays a name match."""
        chain = h.route(caller.qualified, target.qualified)
        names = " → ".join([f"`{caller.name}`", *(f"`{h.nodes[q].name}`" for q, _ in chain)])
        by_name = next((h.nodes[q].name for q, how in chain if how != "inherits"), target.name)
        return (f"the class hierarchy would bind it ({names}), but the link into `{by_name}` was found by "
                "resolving the base-class NAME its statement wrote, not from an INHERITS edge — a "
                f"library class of that name would be taken for `{by_name}` — so it is not counted as "
                "resolved")

    @staticmethod
    def _self_mro_why(
        row: dict, method: str, h: _Hierarchy, caller: _ClassNode, target: _ClassNode, was: str
    ) -> str:
        """The rule, stated on the row it relabelled, with the route it found."""
        receiver = str(row.get("callee") or "").strip().rpartition(".")[0]
        if caller.qualified == target.qualified:
            route = f"`{caller.name}` defines `{method}` itself"
        else:
            chain = h.route(caller.qualified, target.qualified, only="inherits")
            names = " → ".join([f"`{caller.name}`", *(f"`{h.nodes[q].name}`" for q, _ in chain)])
            route = (f"`{caller.name}` reaches `{target.name}` through its ancestry ({names}, from "
                     "INHERITS edges)")
        return (f"the call is written `{receiver}.{method}` inside a method of `{caller.name}`: {route}, "
                f"and no class that could come before `{target.name}` in that resolution order "
                f"defines `{method}` or has a base this index could not resolve. Bound by the class "
                f"hierarchy rather than by name — the backend scored this edge by name"
                f"{f' ({was})' if was else ''} — and a subclass of `{caller.name}` that overrides "
                f"`{method}` is not considered, nor is a class that assigns `{method}` (an alias, "
                "`staticmethod(...)`, a class variable), which the index does not record")
