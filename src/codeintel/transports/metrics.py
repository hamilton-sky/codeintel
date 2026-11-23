from __future__ import annotations

import threading

# Path labels are restricted to known routes so a caller cannot explode Prometheus label
# cardinality (a memory-exhaustion vector) by hammering random URLs — everything else folds
# into path="other". Method and status are already low-cardinality; version is our own string.
_KNOWN_PATHS = frozenset(
    {"/code/query", "/code/doctor", "/code/status", "/healthz", "/readyz", "/metrics"}
)


class Metrics:
    """Thread-safe, dependency-free request metrics rendered in Prometheus text exposition
    format. Shared across the threaded HTTP server's worker threads via one instance on the
    server object; every mutation is lock-guarded."""

    def __init__(self, version: str = "") -> None:
        self._lock = threading.Lock()
        self._version = version
        self._count: dict[tuple[str, str, int], int] = {}  # (method, path, status) -> n
        self._lat_sum: dict[str, float] = {}               # path -> total seconds
        self._lat_count: dict[str, int] = {}               # path -> n
        self._in_flight = 0
        self._overload = 0                                 # requests refused at the concurrency cap

    @staticmethod
    def _norm(path: str) -> str:
        return path if path in _KNOWN_PATHS else "other"

    def inc_in_flight(self) -> None:
        with self._lock:
            self._in_flight += 1

    def dec_in_flight(self) -> None:
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)

    def in_flight(self) -> int:
        with self._lock:
            return self._in_flight

    def inc_overload(self) -> None:
        with self._lock:
            self._overload += 1

    def record(self, method: str, path: str, status: int, duration_s: float) -> None:
        p = self._norm(str(path))
        key = (str(method or "?"), p, int(status))
        with self._lock:
            self._count[key] = self._count.get(key, 0) + 1
            self._lat_sum[p] = self._lat_sum.get(p, 0.0) + max(0.0, float(duration_s))
            self._lat_count[p] = self._lat_count.get(p, 0) + 1

    def render(self) -> str:
        with self._lock:
            count = dict(self._count)
            lat_sum = dict(self._lat_sum)
            lat_count = dict(self._lat_count)
            in_flight = self._in_flight
            overload = self._overload
            version = self._version

        out: list[str] = []
        out.append("# HELP codeintel_requests_total Total HTTP requests handled.")
        out.append("# TYPE codeintel_requests_total counter")
        for (method, path, status), n in sorted(count.items()):
            out.append(
                f'codeintel_requests_total{{method="{method}",path="{path}",status="{status}"}} {n}'
            )
        out.append("# HELP codeintel_request_duration_seconds Request handling duration.")
        out.append("# TYPE codeintel_request_duration_seconds summary")
        for path in sorted(lat_count):
            out.append(f'codeintel_request_duration_seconds_sum{{path="{path}"}} {lat_sum[path]:.6f}')
            out.append(f'codeintel_request_duration_seconds_count{{path="{path}"}} {lat_count[path]}')
        out.append("# HELP codeintel_requests_in_flight Requests currently being handled.")
        out.append("# TYPE codeintel_requests_in_flight gauge")
        out.append(f"codeintel_requests_in_flight {in_flight}")
        out.append("# HELP codeintel_requests_rejected_total Requests refused at the concurrency cap.")
        out.append("# TYPE codeintel_requests_rejected_total counter")
        out.append(f"codeintel_requests_rejected_total {overload}")
        if version:
            out.append("# HELP codeintel_build_info Build metadata (constant 1).")
            out.append("# TYPE codeintel_build_info gauge")
            out.append(f'codeintel_build_info{{version="{version}"}} 1')
        return "\n".join(out) + "\n"
