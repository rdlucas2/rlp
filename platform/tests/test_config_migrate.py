from __future__ import annotations

import pytest

from rlp_platform.config import ConfigError, Settings


def test_migrate_on_start_is_off_by_default():
    assert Settings.from_env({}).migrate_on_start is False


@pytest.mark.parametrize(("raw", "expected"), [("true", True), ("TRUE", True), ("false", False), (" False ", False)])
def test_migrate_on_start_reads_true_or_false(raw, expected):
    env = {"RLP_MIGRATE_ON_START": raw, "DATABASE_URL": "postgresql://db/app"}
    assert Settings.from_env(env).migrate_on_start is expected


def test_migrate_on_start_rejects_other_values():
    with pytest.raises(ConfigError, match="RLP_MIGRATE_ON_START must be true or false"):
        Settings.from_env({"RLP_MIGRATE_ON_START": "yes"})


def test_migrate_on_start_needs_a_database():
    with pytest.raises(ConfigError, match="needs DATABASE_URL"):
        Settings.from_env({"RLP_MIGRATE_ON_START": "true"})
