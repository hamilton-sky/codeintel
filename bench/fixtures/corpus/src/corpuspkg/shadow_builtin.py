"""NOT-TARGET — the builtin. Nothing here imports `corpuspkg.sse.filter`."""


def evens(items):
    return filter(lambda i: i % 2 == 0, items)
