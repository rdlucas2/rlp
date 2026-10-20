from __future__ import annotations

import json

import pytest

import rlp_platform.__main__ as entrypoint


def test_main_starts_uvicorn_with_settings(monkeypatch):
    calls = {}
    monkeypatch.setenv("PORT", "9002")
    monkeypatch.setenv("LOG_LEVEL", "warning")
    monkeypatch.setenv("RLP_API_TOKEN", "a-key")
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda app, **kwargs: calls.update(app=app, **kwargs))
    entrypoint.main()
    assert calls["port"] == 9002
    assert calls["log_level"] == "warning"
    assert calls["log_config"] is None
    assert calls["app"].title == "rlp-platform"


def test_main_refuses_to_start_without_an_api_token(monkeypatch, capsys):
    monkeypatch.delenv("RLP_API_TOKEN", raising=False)
    monkeypatch.delenv("RLP_API_TOKEN_DISABLED", raising=False)
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda app, **kwargs: pytest.fail("started without a token"))
    assert entrypoint.main([]) == 2
    assert "RLP_API_TOKEN is not set" in capsys.readouterr().out


def test_main_starts_open_when_the_token_is_disabled(monkeypatch, capsys):
    calls = {}
    monkeypatch.delenv("RLP_API_TOKEN", raising=False)
    monkeypatch.setenv("RLP_API_TOKEN_DISABLED", "true")
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda app, **kwargs: calls.update(app=app))
    assert entrypoint.main([]) == 0
    assert "app" in calls
    assert "the API is open" in capsys.readouterr().out


def test_main_prints_the_openapi_spec(capsys):
    assert entrypoint.main(["openapi"]) == 0
    spec = json.loads(capsys.readouterr().out)
    assert "/api/v1/projects" in spec["paths"]
