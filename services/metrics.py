"""In-process HTTP / protocol metrics.

No external metrics backend required; exposed via Admin ``GET /admin/metrics``.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class _LatencyStats:
    count: int = 0
    total_ms: float = 0.0
    max_ms: float = 0.0

    def observe(self, duration_ms: float) -> None:
        self.count += 1
        self.total_ms += duration_ms
        if duration_ms > self.max_ms:
            self.max_ms = duration_ms

    def as_dict(self) -> dict[str, Any]:
        avg = (self.total_ms / self.count) if self.count else 0.0
        return {
            "count": self.count,
            "totalMs": round(self.total_ms, 3),
            "avgMs": round(avg, 3),
            "maxMs": round(self.max_ms, 3),
        }


@dataclass
class MetricsRegistry:
    _lock: threading.Lock = field(default_factory=threading.Lock)
    http_requests: dict[tuple[str, str, int], int] = field(
        default_factory=lambda: defaultdict(int)
    )
    http_latency: dict[str, _LatencyStats] = field(
        default_factory=lambda: defaultdict(_LatencyStats)
    )
    protocol_codes: dict[tuple[str, int], int] = field(
        default_factory=lambda: defaultdict(int)
    )
    errors: int = 0

    def record_http(
        self, *, method: str, path: str, status: int, duration_ms: float
    ) -> None:
 # Collapse high-cardinality query strings; keep path template-ish.
        route = path.split("?", 1)[0]
        with self._lock:
            self.http_requests[(method.upper(), route, int(status))] += 1
            self.http_latency[route].observe(duration_ms)
            if int(status) >= 500:
                self.errors += 1

    def record_protocol(self, *, procedure: str, response_code: int) -> None:
        with self._lock:
            self.protocol_codes[(procedure, int(response_code))] += 1

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "httpRequests": [
                    {
                        "method": method,
                        "path": path,
                        "status": status,
                        "count": count,
                    }
                    for (method, path, status), count in sorted(
                        self.http_requests.items()
                    )
                ],
                "httpLatencyMs": {
                    path: stats.as_dict()
                    for path, stats in sorted(self.http_latency.items())
                },
                "protocolResponseCodes": [
                    {
                        "procedure": proc,
                        "responseCode": code,
                        "count": count,
                    }
                    for (proc, code), count in sorted(self.protocol_codes.items())
                ],
                "http5xx": self.errors,
                "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }

    def reset(self) -> None:
        with self._lock:
            self.http_requests.clear()
            self.http_latency.clear()
            self.protocol_codes.clear()
            self.errors = 0


_REGISTRY = MetricsRegistry()


def get_metrics() -> MetricsRegistry:
    return _REGISTRY
