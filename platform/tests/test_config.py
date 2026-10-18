from __future__ import annotations

import pytest

from rlp_platform.config import ConfigError, Settings


def test_defaults():
    s = Settings.from_env({})
    assert s.port == 8000
    assert s.host == "127.0.0.1"
    assert s.log_level == "info"
    assert s.database_url is None


def test_reads_environment():
    s = Settings.from_env(
        {"PORT": "9000", "HOST": "10.0.0.5", "LOG_LEVEL": "DEBUG", "DATABASE_URL": "postgresql://db/app"}
    )
    assert (s.port, s.host, s.log_level, s.database_url) == (9000, "10.0.0.5", "debug", "postgresql://db/app")


def test_reads_os_environ(monkeypatch):
    monkeypatch.setenv("PORT", "9001")
    assert Settings.from_env().port == 9001


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"PORT": "http"}, "PORT must be an integer"),
        ({"PORT": "0"}, "PORT must be between"),
        ({"LOG_LEVEL": "loud"}, "LOG_LEVEL must be one of"),
    ],
)
def test_rejects_bad_values(env, message):
    with pytest.raises(ConfigError, match=message):
        Settings.from_env(env)
