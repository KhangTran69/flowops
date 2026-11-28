"""Rate limit configuration and failure behavior without live infrastructure."""
import pytest
from fastapi import HTTPException
from sqlalchemy.exc import OperationalError

from src import db, deps, rate_limit


def test_production_rate_limit_cannot_be_disabled(monkeypatch):
    monkeypatch.setenv("FLOWOPS_ENVIRONMENT", "production")
    monkeypatch.setenv("FLOWOPS_RATE_LIMIT_PER_MINUTE", "0")
    with pytest.raises(ValueError, match="positive"):
        rate_limit.configured_limit()
    monkeypatch.setenv("FLOWOPS_RATE_LIMIT_PER_MINUTE", "120")
    db.use_database("sqlite:///:memory:")
    with pytest.raises(ValueError, match="PostgreSQL"):
        rate_limit.validate_configuration()


def test_rate_limiter_database_error_fails_closed(monkeypatch):
    def unavailable():
        raise OperationalError("synthetic", {}, RuntimeError("private database detail"))

    monkeypatch.setattr(db.engine, "begin", unavailable)
    with pytest.raises(rate_limit.Unavailable, match="Shared admission unavailable"):
        rate_limit.consume("synthetic", 2)
    assert any('outcome="unavailable"' in line for line in rate_limit.metric_lines())


def test_invalid_configuration_fails_closed(monkeypatch):
    monkeypatch.setenv("FLOWOPS_RATE_LIMIT_PER_MINUTE", "bad")
    with pytest.raises(HTTPException) as error:
        deps._enforce_rate_limit("synthetic")
    assert error.value.status_code == 503


def test_identity_framing_separates_tenants():
    assert rate_limit.actor_key("a:b", "c") != rate_limit.actor_key("b", "c:a")
    assert rate_limit.actor_key("user", None) != rate_limit.actor_key("user", "global")
