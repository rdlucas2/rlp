from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from rlp_platform import ui
from rlp_platform.app import create_app
from rlp_platform.config import Settings

INDEX = "<!doctype html><title>rlp inventory</title>"


@pytest.fixture
def ui_dir(tmp_path):
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX, encoding="utf-8")
    (root / "assets" / "index-abc.js").write_text("console.log('ui')", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside", encoding="utf-8")
    return root


@pytest.fixture
def client(settings, ui_dir) -> TestClient:
    return TestClient(create_app(dataclasses.replace(settings, ui_dir=str(ui_dir))))


def test_settings_read_rlp_ui_dir():
    assert Settings.from_env({"RLP_UI_DIR": "/app/ui"}).ui_dir == "/app/ui"
    assert Settings.from_env({}).ui_dir is None


def test_root_serves_index(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.text == INDEX
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["cache-control"] == ui.REVALIDATE


@pytest.mark.parametrize("path", ["/projects/orders", "/projects/orders?x=1", "/anything/else", "/assets/missing.js"])
def test_client_routes_fall_back_to_index(client, path):
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.text == INDEX


def test_assets_are_served_and_cached(client):
    resp = client.get("/assets/index-abc.js")
    assert resp.status_code == 200
    assert resp.text == "console.log('ui')"
    assert resp.headers["cache-control"] == ui.IMMUTABLE
    other = client.get("/favicon.svg")
    assert other.text == "<svg/>"
    assert other.headers["cache-control"] == ui.REVALIDATE


@pytest.mark.parametrize("path", ["/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt"])
def test_files_outside_the_build_are_not_served(client, path):
    resp = client.get(path)
    assert "outside" not in resp.text


@pytest.mark.parametrize("path", ["/api", "/api/v2/projects", "/api/v1/nope"])
def test_api_paths_never_get_the_page(client, path):
    resp = client.get(path)
    assert resp.status_code in (401, 404)
    assert INDEX not in resp.text


def test_api_and_health_still_answer(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/api/v1/projects").status_code == 401


def test_the_ui_is_left_out_of_the_openapi_spec(client, settings):
    assert client.app.openapi()["paths"] == create_app(settings).openapi()["paths"]


def test_no_index_means_no_ui(settings, tmp_path):
    app = create_app(dataclasses.replace(settings, ui_dir=str(tmp_path)))
    assert TestClient(app).get("/").status_code == 404


def test_without_rlp_ui_dir_root_is_404(settings):
    assert TestClient(create_app(settings)).get("/").status_code == 404
