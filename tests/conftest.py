import asyncio
import uuid
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from flowops.api import create_app
from flowops.config import Settings
from flowops.contracts import PRESETS
from flowops.db import database
from flowops.workflow import Workflow

PREFIX = "/api/projects/flowops"


@pytest.fixture
def system():
    settings = replace(Settings.load(), retry_base=0.001, run_seconds=60)
    assert settings.environment == "test"
    assert settings.database_url.rsplit("/", 1)[-1] == "flowops_test", (
        "Tests require the dedicated flowops_test database"
    )
    engine, sessions = database(settings)
    with engine.begin() as c:
        c.execute(
            text(
                "TRUNCATE sessions, runs, events, approvals, approval_records, effects, rate_buckets, stream_slots, service_inbox, service_outbox CASCADE"
            )
        )
    app = create_app(settings)
    client = TestClient(app)
    client.headers["Origin"] = settings.origin
    response = client.post(PREFIX + "/session", json={})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    yield settings, sessions, client, Workflow(sessions, settings), app
    client.close()
    app.state.engine.dispose()
    engine.dispose()


def start(client, preset=0, changes=None, key=None):
    data = {**PRESETS[preset]["input"], **(changes or {})}
    return client.post(
        PREFIX + "/simulations",
        json={"input": data, "preset_id": PRESETS[preset]["id"]},
        headers={"Idempotency-Key": key or str(uuid.uuid4())},
    )


def drain(system, run_id):
    _, _, client, worker, _ = system

    async def loop():
        for _ in range(300):
            await worker.run_once()
            response = client.get(PREFIX + f"/simulations/{run_id}")
            assert response.status_code == 200, response.text
            state = response.json()
            if (
                state["status"] in {"SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"}
                or state["outcome"] == "APPROVAL_PENDING"
            ):
                return client.get(PREFIX + f"/simulations/{run_id}/result").json()
            await asyncio.sleep(0.02)
        raise AssertionError("Workflow did not settle")

    return asyncio.run(loop())


@pytest.fixture
def canonical(system):
    settings, sessions, _, _, _ = system
    settings = replace(settings, source_revision="1" * 40)
    from flowops.canonical_api import create_app as canonical_app

    app = canonical_app(settings)
    with TestClient(app) as client:
        client.headers["Origin"] = settings.origin
        response = client.post(PREFIX + "/simulation/session", json={})
        assert response.status_code == 200, response.text
        client.headers["Authorization"] = "Bearer " + response.json()["session_token"]
        yield settings, sessions, client, Workflow(sessions, settings), app
    app.state.engine.dispose()
