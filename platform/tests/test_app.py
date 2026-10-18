from __future__ import annotations

import dataclasses
import socket

from fastapi.testclient import TestClient

from rlp_platform.app import create_app, database_reachable


def test_healthz(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_readyz_without_dependencies(client):
    resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "checks": {}}


def test_hello(client):
    resp = client.get("/hello/rlp")
    assert resp.status_code == 200
    assert resp.json() == {"message": "hello, rlp"}


def listening_socket() -> socket.socket:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    return sock


def closed_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_readyz_with_reachable_database(settings):
    with listening_socket() as sock:
        port = sock.getsockname()[1]
        url = f"postgresql://app@127.0.0.1:{port}/app"
        client = TestClient(create_app(dataclasses.replace(settings, database_url=url)))
        resp = client.get("/readyz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "checks": {"database": "ok"}}


def test_readyz_with_unreachable_database(settings):
    url = f"postgresql://app@127.0.0.1:{closed_port()}/app"
    client = TestClient(create_app(dataclasses.replace(settings, database_url=url)))
    resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json() == {"status": "unavailable", "checks": {"database": "unreachable"}}


def test_database_reachable_rejects_bad_urls():
    assert not database_reachable("not a url")
    assert not database_reachable("postgresql://app@127.0.0.1:99999/app")


def test_database_reachable_uses_scheme_default_port(monkeypatch):
    seen = {}

    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_connect(address, timeout):
        seen["address"] = address
        return Conn()

    monkeypatch.setattr(socket, "create_connection", fake_connect)
    assert database_reachable("postgresql+psycopg://app@db/app")
    assert seen["address"] == ("db", 5432)
