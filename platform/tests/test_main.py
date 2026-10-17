from __future__ import annotations

import rlp_platform.__main__ as entrypoint


def test_main_starts_uvicorn_with_settings(monkeypatch):
    calls = {}
    monkeypatch.setenv("PORT", "9002")
    monkeypatch.setenv("LOG_LEVEL", "warning")
    monkeypatch.setattr(entrypoint.uvicorn, "run", lambda app, **kwargs: calls.update(app=app, **kwargs))
    entrypoint.main()
    assert calls["port"] == 9002
    assert calls["log_level"] == "warning"
    assert calls["log_config"] is None
    assert calls["app"].title == "rlp-platform"
