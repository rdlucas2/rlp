from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest

from rlp_platform import smoke


class Handler(BaseHTTPRequestHandler):
    statuses: ClassVar[dict[str, int]] = {}

    def do_GET(self):
        self.send_response(self.statuses.get(self.path, 404))
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def base(httpd) -> str:
    return f"http://127.0.0.1:{httpd.server_address[1]}"


def test_passes_when_both_endpoints_answer_200(server, capsys):
    Handler.statuses = {"/healthz": 200, "/readyz": 200}
    assert smoke.main([base(server)]) == 0
    assert "/readyz: 200" in capsys.readouterr().out


def test_fails_when_not_ready(server, monkeypatch, capsys):
    Handler.statuses = {"/healthz": 200, "/readyz": 503}
    monkeypatch.setenv("SMOKE_TIMEOUT", "0")
    assert smoke.main([base(server)]) == 1
    assert "/readyz: 503" in capsys.readouterr().out


def test_fails_when_nothing_answers(monkeypatch, capsys):
    monkeypatch.setenv("SMOKE_TIMEOUT", "0")
    monkeypatch.setenv("SMOKE_URL", "http://127.0.0.1:1")
    assert smoke.main([]) == 1
    assert "no answer" in capsys.readouterr().out


def test_default_url_uses_port(monkeypatch):
    seen = []
    monkeypatch.delenv("SMOKE_URL", raising=False)
    monkeypatch.setenv("PORT", "9003")
    monkeypatch.setattr(smoke, "wait", lambda base, deadline: seen.append(base) or {"/healthz": 200, "/readyz": 200})
    assert smoke.main([]) == 0
    assert seen == ["http://127.0.0.1:9003"]


def test_https_and_rejects_other_schemes():
    assert smoke.check("https://127.0.0.1:1/healthz", timeout=0.2) is None
    with pytest.raises(ValueError, match="http"):
        smoke.check("file:///etc/passwd")


def test_wait_retries_until_deadline(monkeypatch):
    answers = iter([None, None, 200, 200])
    monkeypatch.setattr(smoke, "check", lambda url: next(answers))
    assert smoke.wait("http://x", deadline_seconds=5, interval=0) == {"/healthz": 200, "/readyz": 200}
