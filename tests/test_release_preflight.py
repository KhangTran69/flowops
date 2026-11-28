"""Offline tests for the protected release boundary."""
import pytest

from scripts import release_preflight


@pytest.fixture
def release_env():
    return {
        "FLOWOPS_ENVIRONMENT": "production",
        "FLOWOPS_RUN_MIGRATIONS": "false",
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
    monkeypatch.setattr(release_preflight.subprocess, "run", unexpected)
    with pytest.raises(ValueError, match="invalid configuration"):
        release_preflight.main()


def test_validated_release_runs_single_migration(monkeypatch):
    calls = []
    monkeypatch.setattr(release_preflight, "validate_release", lambda _: None)
    monkeypatch.setattr(release_preflight.subprocess, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    release_preflight.main()
    assert calls == [((["alembic", "upgrade", "head"],), {"check": True})]
