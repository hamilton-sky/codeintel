"""State that belongs to one request, on an object that serves many at once.

A provider is built once and shared: `serve-http` is a `ThreadingHTTPServer`, so two requests can be
inside the same provider's `build_result` at the same time. Anything the provider accumulates while
answering — the gaps it found, the rows it rendered, why the last backend call failed — is a fact
about ONE answer, and a plain instance attribute makes it a fact about whichever thread wrote last.
Two concurrent `callers` queries could then return each other's gaps, or one's `safe_for_destructive`
computed over the other's rows: the envelope contradicting itself in the direction that ends in a
deletion, with nothing in either answer to show it.

`PerThread` keeps the attribute syntax every call site already uses (`self._pending_rows += ...`)
and gives each thread its own value. The LSP provider solved the same problem earlier with
hand-written `threading.local` properties; this is that pattern as one descriptor, so a new
per-request field is one line and cannot be declared shared by accident of how it was written.

Per INSTANCE as well as per thread: two providers used from one thread keep separate state. The
`threading.local` is created lazily, so objects built with `__new__` — as many test stubs do — work
without `__init__` having run.
"""
from __future__ import annotations

import threading
from typing import Any, Generic, TypeVar, overload

T = TypeVar("T")

_SLOT = "_per_thread_state"


class PerThread(Generic[T]):
    """A class attribute whose value is separate for every (instance, thread) pair.

    The default must be immutable (None, a tuple, a number, a bool): it is shared by every thread
    that has not yet written its own value.
    """

    def __init__(self, default: T) -> None:
        self.default = default
        self.name = ""

    def __set_name__(self, owner: type, name: str) -> None:
        self.name = name

    @staticmethod
    def _local(obj: Any) -> threading.local:
        state = obj.__dict__.get(_SLOT)
        if state is None:
            # `setdefault` so two threads racing to create it end up sharing one — each thread
            # still sees only its own attributes on it.
            state = obj.__dict__.setdefault(_SLOT, threading.local())
        return state

    @overload
    def __get__(self, obj: None, owner: type) -> PerThread[T]: ...
    @overload
    def __get__(self, obj: object, owner: type) -> T: ...

    def __get__(self, obj: object | None, owner: type) -> PerThread[T] | T:
        if obj is None:
            return self
        return getattr(self._local(obj), self.name, self.default)

    def __set__(self, obj: object, value: T) -> None:
        setattr(self._local(obj), self.name, value)
