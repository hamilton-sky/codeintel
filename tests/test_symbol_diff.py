"""Which definitions an edit removed, re-signed or rewrote — pinned as pure functions over literals.

`code.query op=changed` used to say which FILES a change touched, so appending one function to an
80-symbol file reported "80 symbols impacted" and a DELETED function could not be asked about at
all. `symbol_diff` is the half of the fix that says which definitions moved; it takes two strings
and returns a classification, so every behaviour here is a unit test with no repository, no
subprocess and no backend.

Each test is named for the sentence it asserts, and every one of them fails against a module that
classifies at file granularity — which is the module this one replaces.
"""
from __future__ import annotations

import pytest

from codeintel.symbol_diff import (
    ADDED,
    BODY,
    REMOVED,
    SIGNATURE,
    STATUS_OK,
    STATUS_UNPARSABLE,
    STATUS_UNSUPPORTED,
    classify_mentions,
    diff_file,
    language_of,
)


def _changes(old: str | None, new: str | None, path: str = "pkg/mod.py") -> dict[str, tuple]:
    """`{qualified_name: (change, facets)}` — the whole classification, comparable as a dict."""
    out = diff_file(path, old, new)
    assert out.status == STATUS_OK, out
    return {c.qualified_name: (c.change, c.facets) for c in out.changes}


# ------------------------------------------------------------------------------------- python

def test_a_removed_function_is_removed():
    got = _changes("def keep(): pass\ndef gone(): pass\n", "def keep(): pass\n")
    assert got == {"gone": (REMOVED, ())}


def test_a_changed_parameter_list_is_a_signature_change_and_says_which_parameter():
    got = _changes("def f(a, verified_only=False): pass\n", "def f(a, keep=False): pass\n")
    change, facets = got["f"]
    assert change == SIGNATURE
    assert facets == ("parameters (-verified_only, +keep)",)


def test_a_new_optional_parameter_is_a_signature_change():
    """A caller still fits — but it is the reviewer's call to make, and only a SIGNATURE entry
    tells them there is one. The qualifier says the parameter was added rather than renamed."""
    got = _changes("def f(a): pass\n", "def f(a, extra=None): pass\n")
    assert got["f"] == (SIGNATURE, ("parameters (+extra)",))


def test_a_changed_default_is_a_signature_change_without_inventing_a_parameter_delta():
    got = _changes("def f(a, b=1): pass\n", "def f(a, b=2): pass\n")
    assert got["f"] == (SIGNATURE, ("parameters (defaults or annotations)",))


def test_a_changed_return_annotation_is_a_signature_change():
    """`graph_answer` went from returning two values to three, and the only thing in its header
    that said so was the annotation."""
    got = _changes("def f() -> tuple[int, int]: pass\n", "def f() -> tuple[int, int, int]: pass\n")
    assert got["f"] == (SIGNATURE, ("return annotation",))


def test_a_changed_decorator_is_a_signature_change():
    got = _changes("def f(): pass\n", "@cache\ndef f(): pass\n")
    assert got["f"] == (SIGNATURE, ("decorators",))


def test_turning_a_function_async_is_a_signature_change():
    """Every caller now has to await it. The body is untouched, which is exactly why a diff that
    only compared bodies would call this a non-event."""
    got = _changes("def f(): return 1\n", "async def f(): return 1\n")
    assert got["f"] == (SIGNATURE, ("async",))


def test_an_equal_header_with_a_different_implementation_is_a_body_change():
    got = _changes("def f(a):\n    return a + 1\n", "def f(a):\n    return a + 2\n")
    assert got["f"] == (BODY, ())


def test_a_new_function_is_added_and_is_not_a_removal_or_a_rewrite():
    got = _changes("def a(): pass\n", "def a(): pass\ndef b(): pass\n")
    assert got == {"b": (ADDED, ())}


def test_a_reformat_and_a_comment_are_not_a_rewrite():
    """Python's own parser never sees either, so a function that was only re-spaced or annotated
    with a comment must not appear under 'rewritten' — the noise that would bury the real ones."""
    old = "def f(a, b):\n    return a+b\n"
    new = "def f(a,\n      b):\n    # add them\n    return a + b\n"
    assert _changes(old, new) == {}


def test_a_docstring_only_edit_is_a_body_change_marked_as_such():
    """It IS a change to the function's source, so it is not hidden — but it is flagged so a
    reader can discount it, rather than looking at a 'rewrite' that rewrote nothing."""
    got = _changes('def f():\n    """old"""\n    return 1\n',
                   'def f():\n    """new"""\n    return 1\n')
    assert got["f"] == (BODY, ("docstring only",))


def test_a_method_is_keyed_by_its_class_so_two_classes_with_a_run_do_not_collide():
    old = "class A:\n    def run(self): pass\nclass B:\n    def run(self): pass\n"
    new = "class A:\n    def run(self): pass\nclass B:\n    def run(self, x): pass\n"
    assert _changes(old, new) == {"B.run": (SIGNATURE, ("parameters (+x)",))}


def test_a_nested_class_method_is_keyed_by_the_whole_chain():
    old = "class Outer:\n    class Inner:\n        def deep(self): return 1\n"
    new = "class Outer:\n    class Inner:\n        def deep(self): return 2\n"
    assert _changes(old, new) == {"Outer.Inner.deep": (BODY, ())}


def test_an_async_method_is_classified_like_any_other_definition():
    old = "class S:\n    async def go(self): return 1\n"
    new = "class S:\n    async def go(self): return 2\n"
    assert _changes(old, new) == {"S.go": (BODY, ())}


def test_a_class_is_removed_along_with_every_method_it_held():
    """A deleted class is one symbol callers instantiate and N methods they call; reporting only
    the class would hide the methods, and reporting only the methods would hide the class."""
    got = _changes("class Gone:\n    def m(self): pass\n", "")
    assert got == {"Gone": (REMOVED, ()), "Gone.m": (REMOVED, ())}


def test_a_class_whose_bases_changed_has_a_signature_change_and_one_whose_methods_changed_does_not():
    """The class's own header is its signature; its methods are their own definitions. Counting a
    method edit against the class as a body change would put the class in every diff of the file's
    biggest class."""
    sig = _changes("class C(A):\n    pass\n", "class C(B):\n    pass\n")
    assert sig["C"] == (SIGNATURE, ("bases",))
    only_method = _changes("class C:\n    def m(self): return 1\n",
                           "class C:\n    def m(self): return 2\n")
    assert "C" not in only_method and "C.m" in only_method


def test_a_changed_class_attribute_is_a_body_change_of_the_class():
    """A dataclass field IS the constructor's signature, so a class whose own statements changed is
    not silent."""
    got = _changes("class C:\n    x: int = 1\n", "class C:\n    x: int = 1\n    y: int = 2\n")
    assert got["C"] == (BODY, ("class-level statements",))


def test_a_definition_under_a_module_level_if_or_try_is_still_found():
    old = "try:\n    import x\nexcept ImportError:\n    def fallback(): return 1\n"
    new = "try:\n    import x\nexcept ImportError:\n    def fallback(): return 2\n"
    assert _changes(old, new) == {"fallback": (BODY, ())}


def test_a_property_and_its_setter_share_a_name_and_a_change_to_either_is_seen():
    old = ("class C:\n    @property\n    def x(self): return 1\n"
           "    @x.setter\n    def x(self, v): pass\n")
    new = ("class C:\n    @property\n    def x(self): return 1\n"
           "    @x.setter\n    def x(self, v): self._v = v\n")
    assert _changes(old, new) == {"C.x": (BODY, ())}


def test_dropping_one_of_two_definitions_under_a_name_is_a_signature_change():
    old = "def f(): pass\nif True:\n    def f(a): pass\n"
    new = "def f(): pass\n"
    assert _changes(old, new)["f"][0] == SIGNATURE


def test_a_function_that_becomes_a_class_is_a_signature_change_and_does_not_hide_the_file():
    """`def Foo` becoming `class Foo` used to compare a five-facet function header with a three-facet
    class header, raise, and mark the WHOLE file `unparsable` — every other change in it hidden
    behind a gap that blamed the file's syntax. The change is a signature change, and the rest of
    the file is still read."""
    old = "def Foo(x):\n    return x\n\n\ndef other():\n    return 1\n\n\ndef gone():\n    pass\n"
    new = "class Foo:\n    x = 1\n\n\ndef other():\n    return 2\n"
    out = diff_file("pkg/mod.py", old, new)

    assert out.status == STATUS_OK, out
    got = {c.qualified_name: (c.change, c.facets) for c in out.changes}
    assert got == {
        "Foo": (SIGNATURE, ("kind changed (function → class)",)),
        "other": (BODY, ()),
        "gone": (REMOVED, ()),
    }


def test_a_class_that_becomes_a_function_is_the_same_signature_change_the_other_way():
    out = diff_file("pkg/mod.py", "class Foo:\n    pass\n", "def Foo():\n    pass\n")
    assert out.status == STATUS_OK, out
    assert [(c.change, c.facets) for c in out.changes] == [
        (SIGNATURE, ("kind changed (class → function)",))]


def test_a_nested_function_is_part_of_its_parents_body_and_is_not_its_own_symbol():
    old = "def outer():\n    def inner(): return 1\n    return inner\n"
    new = "def outer():\n    def inner(): return 2\n    return inner\n"
    assert _changes(old, new) == {"outer": (BODY, ())}


def test_a_new_file_is_all_added_and_a_deleted_file_is_all_removed():
    assert _changes(None, "def a(): pass\ndef b(): pass\n") == {
        "a": (ADDED, ()), "b": (ADDED, ())}
    assert _changes("def a(): pass\n", None) == {"a": (REMOVED, ())}


def test_a_renamed_file_with_identical_definitions_reports_nothing():
    """`git diff -M` hands over the old text and the new text of one renamed file. Names are
    module-relative, so a pure move is the same set of definitions on both sides — not a removal
    plus an addition of everything in it."""
    body = "class C:\n    def m(self): return 1\ndef f(): pass\n"
    assert _changes(body, body, path="pkg/new_name.py") == {}


def test_a_renamed_file_that_also_edited_one_function_reports_only_that_function():
    old = "def stay(): return 1\ndef edit(): return 1\n"
    new = "def stay(): return 1\ndef edit(): return 2\n"
    assert _changes(old, new, path="pkg/new_name.py") == {"edit": (BODY, ())}


def test_a_file_that_does_not_parse_is_unparsable_and_says_which_side_and_why():
    """The new side is a syntax error mid-edit more often than the old one is. 'We could not look'
    must not come back as an empty change list, which reads as 'nothing changed'."""
    out = diff_file("pkg/mod.py", "def ok(): pass\n", "def broken(:\n")
    assert out.status == STATUS_UNPARSABLE
    assert out.changes == ()
    assert "new version" in out.detail and "does not parse as Python" in out.detail
    assert diff_file("pkg/mod.py", "def (:\n", "def ok(): pass\n").detail.startswith("the old version")


@pytest.mark.parametrize("hostile", ["\x00", "def f(:\x00", "(" * 5000, "\ufeffdef f(): pass\n" * 3])
def test_hostile_source_never_raises(hostile):
    out = diff_file("pkg/mod.py", hostile, hostile + "\n")
    assert out.status in (STATUS_OK, STATUS_UNPARSABLE)


# ------------------------------------------------------------------------------ tree-sitter

def test_typescript_methods_and_functions_are_classified_with_their_class_prefix():
    old = ("export class Svc {\n  run(a: number): string { return 'x' }\n"
           "  stop() { return 1 }\n}\nexport function top(a) { return a }\n"
           "export const arrow = (a: number) => a + 1\n")
    new = ("export class Svc {\n  run(a: number, b: string): string { return 'x' }\n"
           "  stop() { return 2 }\n}\nexport const arrow = (a: number) => a + 2\n"
           "export function added() {}\n")
    got = _changes(old, new, path="src/svc.ts")
    assert got == {
        "top": (REMOVED, ()),
        "Svc.run": (SIGNATURE, ("declaration header",)),
        "Svc.stop": (BODY, ()),
        "arrow": (BODY, ()),
        "added": (ADDED, ()),
    }


def test_a_typescript_reformat_and_a_comment_are_not_a_rewrite():
    old = "function f(a: number) { return a+1 }\n"
    new = "function f(a: number) {\n  // one more\n  return a + 1\n}\n"
    assert _changes(old, new, path="src/f.ts") == {}


def test_a_typescript_file_with_syntax_errors_is_unparsable_not_half_diffed():
    """tree-sitter tolerates errors and returns a partial tree; diffing whatever survived the
    error would classify a guess."""
    out = diff_file("src/f.ts", "function f( {", "function f() {}\n")
    assert out.status == STATUS_UNPARSABLE and out.changes == ()


def test_go_methods_are_qualified_by_their_receiver_type():
    old = "package p\nfunc (s *Svc) Run(a int) int { return a }\nfunc Top() {}\n"
    new = "package p\nfunc (s *Svc) Run(a int, b int) int { return a }\nfunc Top() { _ = 1 }\n"
    assert _changes(old, new, path="p/svc.go") == {
        "Svc.Run": (SIGNATURE, ("declaration header",)), "Top": (BODY, ())}


def test_rust_methods_are_qualified_by_the_type_they_are_implemented_for():
    old = "impl Foo { fn a(&self) -> i32 { 1 } fn b(&self) {} }\nfn free() {}\n"
    new = "impl Foo { fn a(&self) -> i64 { 1 } fn b(&self) { let _x = 1; } }\n"
    assert _changes(old, new, path="src/foo.rs") == {
        "free": (REMOVED, ()), "Foo.a": (SIGNATURE, ("declaration header",)), "Foo.b": (BODY, ())}


def test_java_methods_are_qualified_by_their_class():
    old = "class A { int f(int a) { return a; } }"
    new = "class A { int f(int a, int b) { return a; } }"
    assert _changes(old, new, path="src/A.java") == {"A.f": (SIGNATURE, ("declaration header",))}


# --------------------------------------------------------------------------- degrading honestly

@pytest.mark.parametrize("path", ["src/native.cpp", "src/native.c", "include/x.h", "app/main.rb",
                                  "app/Main.kt"])
def test_a_language_with_no_definition_level_reading_degrades_to_unsupported(path):
    """C and C++ are chunked by the indexer but their definitions are NOT named by its name rule
    (the name lives in a declarator), so diffing them would report 'no symbols changed' for a file
    whose functions were all rewritten. They say 'unsupported' instead — and carry no changes,
    because a classification nobody checked is a made-up one."""
    out = diff_file(path, "int f(void) { return 1; }\n", "int f(void) { return 2; }\n")
    assert out.status == STATUS_UNSUPPORTED
    assert out.changes == ()
    assert "no definition-level reading" in out.detail


def test_language_of_reads_the_extension_and_knows_python():
    assert language_of("a/b.py") == "python"
    assert language_of("a/b.pyi") == "python"
    assert language_of("a/b.tsx") == "tsx"
    assert language_of("a/b.unknown") == ""


# ------------------------------------------------------------------------------------ mentions

def test_a_python_mention_is_labelled_code_string_comment_or_definition():
    src = ('x = foo(1)\n'              # 1 code
           '# foo was here\n'          # 2 comment
           's = "call foo"\n'          # 3 string
           'from a import foo\n'       # 4 code (an import is a use)
           'def foo(): pass\n')        # 5 definition
    assert classify_mentions(src, "foo", [1, 2, 3, 4, 5]) == {
        1: "code", 2: "comment", 3: "string", 4: "code", 5: "definition"}


def test_a_name_inside_an_f_string_expression_is_code_and_not_a_string():
    """A real call written as `f"{foo()}"` is a use that breaks when `foo` goes. Telling it apart
    from the prose around it needs the parser; a text filter would have thrown it away."""
    assert classify_mentions('s = f"value: {foo()}"\n', "foo", [1]) == {1: "code"}


def test_code_wins_over_a_string_on_the_same_line():
    assert classify_mentions('foo("foo")\n', "foo", [1]) == {1: "code"}


def test_an_unparsable_python_file_gets_no_labels_rather_than_a_guess():
    assert classify_mentions("def (:\n  foo\n", "foo", [2]) == {}
