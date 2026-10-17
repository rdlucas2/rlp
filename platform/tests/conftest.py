from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from rlp_platform.app import create_app
from rlp_platform.config import Settings


@pytest.fixture(autouse=True)
def quiet_telemetry(monkeypatch):
    """Tests never export spans, whatever the environment says."""
    monkeypatch.setenv("OTEL_TRACES_EXPORTER", "none")


@pytest.fixture
def settings() -> Settings:
    return Settings.from_env({})


@pytest.fixture
def client(settings) -> TestClient:
    return TestClient(create_app(settings))
