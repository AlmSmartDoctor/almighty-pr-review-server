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


def test_benchmark_expected_identity_requires_complete_explicit_values(monkeypatch):
    fields = config._BENCHMARK_IDENTITY_FIELDS
    value = {
        field: ("a" * 64 if field.endswith("_sha256") else "value")
        for field in fields
    }
    value["implementation_commit_sha"] = "b" * 40
    value["chunk_budget"] = 100000
    import json
    monkeypatch.setenv("ALMIGHTY_TEST_BENCHMARK_IDENTITY", json.dumps(value))
    assert config._env_optional_benchmark_expected_identity(
        "ALMIGHTY_TEST_BENCHMARK_IDENTITY"
    ) == value

    value.pop("schema_sha256")
    monkeypatch.setenv("ALMIGHTY_TEST_BENCHMARK_IDENTITY", json.dumps(value))
    with pytest.raises(RuntimeError, match="every benchmark identity"):
        config._env_optional_benchmark_expected_identity(
            "ALMIGHTY_TEST_BENCHMARK_IDENTITY"
        )


def test_webhook_ingress_requires_new_0700_temp_workspace(tmp_path):
    workspace = tmp_path / "almighty-ingress-test"
    workspace.mkdir(mode=0o700)
    # pytest tmp roots are under the OS temp root and the target is new.
    config._validate_webhook_ingress_db(workspace / "replay.db")
    existing = workspace / "existing.db"
    existing.write_bytes(b"existing")
    with pytest.raises(RuntimeError, match="new disposable"):
        config._validate_webhook_ingress_db(existing)
    workspace.chmod(0o755)
    with pytest.raises(RuntimeError, match="0700"):
        config._validate_webhook_ingress_db(workspace / "other.db")


def test_benchmark_report_path_must_be_absolute(monkeypatch):
    monkeypatch.setenv("ALMIGHTY_TEST_REPORT_PATH", "relative/report.json")
    with pytest.raises(RuntimeError, match="absolute local path"):
        config._env_optional_absolute_path("ALMIGHTY_TEST_REPORT_PATH")


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
