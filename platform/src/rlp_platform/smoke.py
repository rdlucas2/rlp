"""Smoke-test a running instance: python -m rlp_platform.smoke [BASE_URL].

Polls the liveness and readiness endpoints until both answer 200 or the
deadline passes. BASE_URL defaults to $SMOKE_URL, else http://127.0.0.1:$PORT.
"""

from __future__ import annotations

import http.client
import os
import sys
import threading
import time
from urllib.parse import urlsplit

PATHS = ("/healthz", "/readyz")


def check(url: str, timeout: float = 2.0) -> int | None:
    """HTTP status of a GET, or None when nothing answers."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"smoke URL must be an http(s) URL, got {url!r}")
    conn_class = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
    conn = conn_class(parts.hostname, parts.port, timeout=timeout)
    try:
        conn.request("GET", parts.path or "/")
        return conn.getresponse().status
    except OSError:
        return None
    finally:
        conn.close()


def wait(base: str, deadline_seconds: float, interval: float = 0.5) -> dict[str, int | None]:
    deadline = time.monotonic() + deadline_seconds
    pause = threading.Event()  # never set; wait() is the delay between polls
    while True:
        results = {path: check(base.rstrip("/") + path) for path in PATHS}
        if all(code == 200 for code in results.values()) or time.monotonic() >= deadline:
            return results
        pause.wait(interval)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    base = argv[0] if argv else os.environ.get("SMOKE_URL") or f"http://127.0.0.1:{os.environ.get('PORT', '8000')}"
    deadline = float(os.environ.get("SMOKE_TIMEOUT", "30"))
    results = wait(base, deadline)
    for path, code in results.items():
        print(f"{base.rstrip('/')}{path}: {code if code is not None else 'no answer'}")
    return 0 if all(code == 200 for code in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
