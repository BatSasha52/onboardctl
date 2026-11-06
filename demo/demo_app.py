"""Stand-in for a team's service, for trying onboardctl locally and in CI.

It is not part of onboardctl. It simulates its own traffic and serves the
resulting Prometheus metrics, so the generated ServiceMonitor has a target
and the SLO rules have data. Standard library only, so it runs on a stock
python image from a ConfigMap without building anything.

Environment:
  PORT                  listen port (8080)
  METRICS_PATH          path of the metrics endpoint (/metrics)
  REQUESTS_PER_SECOND   simulated request rate (20)
  ERROR_RATIO           share of simulated requests that return 500 (0.0005)
  SLOW_RATIO            share that take 1-3 seconds (0.002)
"""

from __future__ import annotations

import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Same bounds as the Go client's default buckets, written the same way.
BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)

PORT = int(os.environ.get("PORT", "8080"))
METRICS_PATH = os.environ.get("METRICS_PATH", "/metrics")
RPS = float(os.environ.get("REQUESTS_PER_SECOND", "20"))
ERROR_RATIO = float(os.environ.get("ERROR_RATIO", "0.0005"))
SLOW_RATIO = float(os.environ.get("SLOW_RATIO", "0.002"))

_lock = threading.Lock()
_requests: dict[str, int] = {"200": 0, "500": 0}
_buckets = [0] * len(BUCKETS)
_count = 0
_sum = 0.0


def _observe(code: str, seconds: float) -> None:
    global _count, _sum
    with _lock:
        _requests[code] += 1
        _count += 1
        _sum += seconds
        for i, bound in enumerate(BUCKETS):
            if seconds <= bound:
                _buckets[i] += 1


def _simulate() -> None:
    tick = 0.1
    while True:
        time.sleep(tick)
        expected = RPS * tick
        n = int(expected) + (random.random() < expected - int(expected))
        for _ in range(n):
            code = "500" if random.random() < ERROR_RATIO else "200"
            if random.random() < SLOW_RATIO:
                seconds = random.uniform(1, 3)
            else:
                seconds = min(random.expovariate(1 / 0.04), 0.9)
            _observe(code, seconds)


def _exposition() -> str:
    with _lock:
        lines = [
            "# HELP http_requests_total Simulated HTTP requests.",
            "# TYPE http_requests_total counter",
        ]
        lines += [f'http_requests_total{{code="{c}"}} {n}' for c, n in sorted(_requests.items())]
        lines += [
            "# HELP http_request_duration_seconds Simulated request latency.",
            "# TYPE http_request_duration_seconds histogram",
        ]
        lines += [
            f'http_request_duration_seconds_bucket{{le="{bound:g}"}} {n}'
            for bound, n in zip(BUCKETS, _buckets, strict=True)
        ]
        lines += [
            f'http_request_duration_seconds_bucket{{le="+Inf"}} {_count}',
            f"http_request_duration_seconds_sum {_sum:.6f}",
            f"http_request_duration_seconds_count {_count}",
        ]
    return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (name required by BaseHTTPRequestHandler)
        if self.path == METRICS_PATH:
            body, content_type = _exposition(), "text/plain; version=0.0.4"
        elif self.path in ("/", "/healthz"):
            body, content_type = "ok\n", "text/plain"
        else:
            self.send_error(404)
            return
        data = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        pass  # scrapes every 15s would flood the log


if __name__ == "__main__":
    threading.Thread(target=_simulate, daemon=True).start()
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
