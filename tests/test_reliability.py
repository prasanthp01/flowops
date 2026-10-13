import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text

from flowops.db import Run, Session, now
from flowops.workflow import StaleClaim
from tests.conftest import PREFIX, start


def test_heartbeat_is_fenced(system):
    _, sessions, client, worker, _ = system
    run_id = start(client).json()["run_id"]
    claim = worker.claim()
    worker.heartbeat(*claim)
    with sessions.begin() as db:
        run = db.get(Run, run_id)
        assert run.lease_until > now()
        run.claim_version += 1
    with pytest.raises(StaleClaim):
        worker.heartbeat(*claim)


def test_run_deadline_and_ttl_cleanup(system):
    _, sessions, client, worker, _ = system
    run_id = start(client).json()["run_id"]
    with sessions.begin() as db:
        db.get(Run, run_id).deadline_at = now() - timedelta(seconds=1)
    worker.maintain()
    assert client.get(PREFIX + f"/simulations/{run_id}").json()["status"] == "EXPIRED"
    with sessions.begin() as db:
        r = db.get(Run, run_id)
        r.expires_at = now() - timedelta(seconds=1)
        db.get(Session, r.owner_id).expires_at = now() - timedelta(seconds=1)
    worker.maintain()
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Run)) == 0
        assert db.scalar(select(func.count()).select_from(Session)) == 0


def test_stale_worker_cannot_apply_action(system):
    _, sessions, client, worker, _ = system
    run_id = start(client).json()["run_id"]
    for _ in range(6):
        asyncio.run(worker.run_once())
    claim = worker.claim()
    with sessions.begin() as db:
        db.get(Run, run_id).lease_until = now() - timedelta(seconds=1)
    with pytest.raises(StaleClaim):
        worker.apply_effect(*claim)


def test_database_lock_timeout_leaves_no_partial_admission(system):
    _, sessions, client, _, app = system
    contender = TestClient(app, raise_server_exceptions=False)
    contender.cookies.update(client.cookies)
    contender.headers.update(client.headers)
    with sessions.begin() as db:
        db.execute(text("SELECT id FROM guards WHERE id='admission' FOR UPDATE"))
        original_count = db.scalar(select(func.count()).select_from(Run))
        with ThreadPoolExecutor(max_workers=1) as pool:
            response = pool.submit(start, contender).result(timeout=8)
        assert response.status_code == 500
        assert response.json() == {"detail": "INTERNAL_ERROR"}
    with sessions() as db:
        assert db.execute(text("SELECT id FROM guards")).scalar() == "admission"
        assert db.scalar(select(func.count()).select_from(Run)) == original_count
