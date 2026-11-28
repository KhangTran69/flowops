"""Shared rate limiter: disposable PostgreSQL and independent processes."""
import os
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from multiprocessing import get_context

import pytest
from sqlalchemy import select

from src import db, rate_limit

POSTGRES_URL = os.environ.get("FLOWOPS_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not POSTGRES_URL, reason="FLOWOPS_TEST_POSTGRES_URL is not set")


@pytest.fixture
def database():
    db.use_database(POSTGRES_URL)
    db.Base.metadata.drop_all(bind=db.engine)
    db.Base.metadata.create_all(bind=db.engine)
    yield
    db.Base.metadata.drop_all(bind=db.engine)


def _process_consume(args):
    url, key = args
    db.use_database(url)
    return rate_limit.consume(key, 7)[0]


def test_postgres_shared_rate_limit_across_processes(database):
    key = rate_limit.actor_key("same-actor", "tenant-a")
    # Avoid minute rollover in the test by waiting only if within the final
    # five seconds of a window. The limiter itself always uses database time.
    with db.engine.connect() as connection:
        from sqlalchemy import text

        remaining = connection.execute(text("SELECT 60 - mod(floor(extract(epoch from clock_timestamp())), 60)")).scalar_one()
    if remaining < 5:
        import time

        time.sleep(float(remaining) + 0.1)
    with ProcessPoolExecutor(max_workers=4, mp_context=get_context("spawn")) as pool:
        outcomes = list(pool.map(_process_consume, [(POSTGRES_URL, key)] * 24))
    assert sum(outcomes) == 7
    with db.SessionLocal() as session:
        row = session.execute(select(db.RateLimitBucketRecord)).scalar_one()
        assert row.count == 7 and row.digest != key


def test_postgres_rate_limit_is_tenant_scoped_and_recovers(database):
    a, b = (rate_limit.actor_key("shared", tenant) for tenant in ("a", "b"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(lambda _: rate_limit.consume(a, 4)[0], range(16)))
    assert sum(outcomes) == 4
    assert rate_limit.consume(b, 4)[0]
    with db.SessionLocal() as session:
        for row in session.execute(select(db.RateLimitBucketRecord)).scalars():
            row.window_id -= 10
        session.commit()
    assert rate_limit.sweep_expired() == 2
    assert rate_limit.consume(a, 4)[0]


def test_authenticated_api_quota_shared_across_clients(database, monkeypatch):
    from fastapi.testclient import TestClient

    from src import api
    from src.auth import Actor
    from src.deps import _actor_from_header

    monkeypatch.setenv("FLOWOPS_RATE_LIMIT_PER_MINUTE", "5")
    actor = Actor("synthetic-api-principal", ("automation.admin",), source="bearer")
    api.app.dependency_overrides[_actor_from_header] = lambda: actor
    try:
        # Separate clients contend for the same authenticated quota.
        def request(_):
            with TestClient(api.app) as client:
                return client.get("/tickets").status_code

        with ThreadPoolExecutor(max_workers=8) as pool:
            statuses = list(pool.map(request, range(16)))
        assert statuses.count(200) == 5
        assert statuses.count(429) == 11
    finally:
        api.app.dependency_overrides.pop(_actor_from_header, None)
