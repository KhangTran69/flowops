"""Offline tests for the protected release boundary."""
import pytest

from scripts import release_preflight


@pytest.fixture
def release_env():
    return {
        "FLOWOPS_ENVIRONMENT": "production",
        "FLOWOPS_RUN_MIGRATIONS": "false",
        "FLOWOPS_RATE_LIMIT_PER_MINUTE": "120",
        "FLOWOPS_ACTION_ADAPTER": "simulated",
        "FLOWOPS_AUTOMATION_ADAPTER": "simulated",
        "FLOWOPS_AI_ADAPTER": "simulated",
        "FLOWOPS_DATABASE_URL": "postgresql+psycopg://test:example@db.invalid/flowops",
        "FLOWOPS_AUTH_REQUIRED": "true",
        "FLOWOPS_UI_AUTH_REQUIRED": "true",
        "FLOWOPS_SESSION_COOKIE_SECURE": "true",
        "FLOWOPS_SESSION_SECRET": "s" * 32,
        "FLOWOPS_WEBHOOK_API_KEY": "w" * 32,
        "FLOWOPS_ENTRA_ISSUER": "https://issuer.invalid",
        "FLOWOPS_ENTRA_AUDIENCE": "test",
        "FLOWOPS_ENTRA_JWKS_URL": "https://issuer.invalid/keys",
    }


def test_release_configuration(release_env):
    release_preflight.validate_release(release_env)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("FLOWOPS_ENVIRONMENT", "development"),
        ("FLOWOPS_RUN_MIGRATIONS", "true"),
        ("FLOWOPS_RATE_LIMIT_PER_MINUTE", "0"),
        ("FLOWOPS_RATE_LIMIT_PER_MINUTE", "-1"),
        ("FLOWOPS_RATE_LIMIT_PER_MINUTE", "100001"),
        ("FLOWOPS_RATE_LIMIT_PER_MINUTE", "bad"),
        ("FLOWOPS_DEV_BEARER_TOKEN", "not-for-production"),
        ("FLOWOPS_DEMO_USERS", "{}"),
        ("FLOWOPS_DATABASE_URL", "sqlite:///local.db"),
        ("FLOWOPS_DATABASE_URL", "invalid"),
        ("FLOWOPS_AUTH_REQUIRED", "false"),
        ("FLOWOPS_UI_AUTH_REQUIRED", "false"),
        ("FLOWOPS_SESSION_COOKIE_SECURE", "false"),
        ("FLOWOPS_WEBHOOK_API_KEY", "short"),
        ("FLOWOPS_ENTRA_JWKS_URL", ""),
        ("FLOWOPS_ACTION_ADAPTER", "live"),
        ("FLOWOPS_NOTIFICATION_WEBHOOK_URL", "https://example.invalid"),
        ("FLOWOPS_ACTION_FAILURE_WEBHOOK_URL", "https://example.invalid"),
        ("FLOWOPS_N8N_WEBHOOK_URL", "https://example.invalid"),
        ("FLOWOPS_AUTOMATION_CALLBACK_SECRET", "c" * 32),
        ("FLOWOPS_ENTRA_JWKS_URL", "http://issuer.invalid/keys"),
        ("FLOWOPS_WEBHOOK_API_KEY", "s" * 32),
    ],
)
def test_invalid_release_configuration(release_env, key, value):
    with pytest.raises(ValueError):
        release_preflight.validate_release({**release_env, key: value})


def test_preflight_failure_never_runs_migration(monkeypatch):
    def reject(_):
        raise ValueError("invalid configuration")

    def unexpected(*args, **kwargs):
        pytest.fail("migration started before configuration validation")

    monkeypatch.setattr(release_preflight, "validate_release", reject)
    monkeypatch.setattr(release_preflight, "migrate_once", unexpected)
    with pytest.raises(ValueError, match="invalid configuration"):
        release_preflight.main()


def test_validated_release_runs_single_migration(monkeypatch, release_env):
    calls = []
    monkeypatch.setenv("FLOWOPS_DATABASE_URL", release_env["FLOWOPS_DATABASE_URL"])
    monkeypatch.setattr(release_preflight, "validate_release", lambda _: None)
    monkeypatch.setattr(release_preflight, "migrate_once", calls.append)
    release_preflight.main()
    assert calls == [release_env["FLOWOPS_DATABASE_URL"]]


@pytest.mark.parametrize("acquired", [True, False])
def test_release_lock_prevents_concurrent_migration(monkeypatch, acquired):
    from unittest.mock import MagicMock

    engine = MagicMock()
    connection = engine.connect.return_value.__enter__.return_value
    connection.execute.return_value.scalar_one.return_value = acquired
    monkeypatch.setattr(release_preflight, "create_engine", lambda *args, **kwargs: engine)
    run = MagicMock()
    monkeypatch.setattr(release_preflight.subprocess, "run", run)
    if acquired:
        release_preflight.migrate_once("unused")
        run.assert_called_once_with(["alembic", "upgrade", "head"], check=True, timeout=600)
        assert "pg_advisory_unlock" in str(connection.execute.call_args.args[0])
    else:
        with pytest.raises(RuntimeError, match="migration lock"):
            release_preflight.migrate_once("unused")
        run.assert_not_called()
    engine.dispose.assert_called_once()
