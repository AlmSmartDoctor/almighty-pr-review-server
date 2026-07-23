import pytest

from server import config


def test_irreversible_diagnostic_cleanup_is_disabled_by_default():
    assert config.DIAGNOSTIC_CLEANUP_ENABLED is False


def test_env_int_uses_default_and_accepts_minimum(monkeypatch):
    monkeypatch.delenv("ALMIGHTY_TEST_INT", raising=False)
    assert config._env_int("ALMIGHTY_TEST_INT", 7, minimum=1) == 7

    monkeypatch.setenv("ALMIGHTY_TEST_INT", " 9 ")
    assert config._env_int("ALMIGHTY_TEST_INT", 7, minimum=1) == 9


def test_env_int_rejects_invalid_or_out_of_range_values(monkeypatch):
    monkeypatch.setenv("ALMIGHTY_TEST_INT", "many")
    with pytest.raises(RuntimeError, match="ALMIGHTY_TEST_INT must be an integer"):
        config._env_int("ALMIGHTY_TEST_INT", 7, minimum=1)

    monkeypatch.setenv("ALMIGHTY_TEST_INT", "0")
    with pytest.raises(RuntimeError, match="must be >= 1"):
        config._env_int("ALMIGHTY_TEST_INT", 7, minimum=1)


def test_optional_sha256_accepts_empty_or_canonical_digest(monkeypatch):
    monkeypatch.delenv("ALMIGHTY_TEST_HASH", raising=False)
    assert config._env_optional_sha256("ALMIGHTY_TEST_HASH") == ""
    monkeypatch.setenv("ALMIGHTY_TEST_HASH", " A" + "B" * 63 + " ")
    assert config._env_optional_sha256("ALMIGHTY_TEST_HASH") == "a" + "b" * 63

    monkeypatch.setenv("ALMIGHTY_TEST_HASH", "not-a-hash")
    with pytest.raises(RuntimeError, match="SHA-256 hex digest"):
        config._env_optional_sha256("ALMIGHTY_TEST_HASH")


def test_env_float_rejects_invalid_or_out_of_range_values(monkeypatch):
    monkeypatch.setenv("ALMIGHTY_TEST_FLOAT", "later")
    with pytest.raises(RuntimeError, match="ALMIGHTY_TEST_FLOAT must be a number"):
        config._env_float("ALMIGHTY_TEST_FLOAT", 1.5, minimum=0.1)

    monkeypatch.setenv("ALMIGHTY_TEST_FLOAT", "0")
    with pytest.raises(RuntimeError, match="must be >= 0.1"):
        config._env_float("ALMIGHTY_TEST_FLOAT", 1.5, minimum=0.1)

    for non_finite in ("nan", "inf", "-inf"):
        monkeypatch.setenv("ALMIGHTY_TEST_FLOAT", non_finite)
        with pytest.raises(RuntimeError, match="must be finite"):
            config._env_float("ALMIGHTY_TEST_FLOAT", 1.5, minimum=0.1)
