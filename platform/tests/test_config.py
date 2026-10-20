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
        ({"RLP_API_TOKEN_DISABLED": "yes"}, "RLP_API_TOKEN_DISABLED must be true or false"),
    ],
)
def test_rejects_bad_values(env, message):
    with pytest.raises(ConfigError, match=message):
        Settings.from_env(env)


def test_api_token():
    s = Settings.from_env({"RLP_API_TOKEN": "k3y"})
    assert (s.api_token, s.api_token_disabled) == ("k3y", False)
    assert "k3y" not in repr(s)
    s.check_api_auth()


def test_api_token_disabled():
    s = Settings.from_env({"RLP_API_TOKEN_DISABLED": "TRUE"})
    assert (s.api_token, s.api_token_disabled) == (None, True)
    s.check_api_auth()


@pytest.mark.parametrize("env", [{}, {"RLP_API_TOKEN": ""}, {"RLP_API_TOKEN_DISABLED": "false"}])
def test_api_auth_needs_a_token(env):
    with pytest.raises(ConfigError, match="RLP_API_TOKEN is not set"):
        Settings.from_env(env).check_api_auth()
