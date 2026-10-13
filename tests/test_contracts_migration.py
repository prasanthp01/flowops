"""Migration preservation and rollback guard, restricted by the system fixture to flowops_test."""

import asyncio

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from flowops.db import Event, Run
from tests.conftest import start


def test_additive_migration_preserves_legacy_rows_and_blocks_loss(system):
    _, sessions, client, worker, _ = system
    run_id = start(client).json()["run_id"]
    asyncio.run(worker.run_once())
    with sessions() as db:
        before = db.get(Run, run_id).input
        event = db.query(Event).filter_by(run_id=run_id).first()
        event_id, payload = event.id, event.payload
    cfg = Config("alembic.ini")
    command.downgrade(cfg, "0001")
    try:
        with sessions() as db:
            assert (
                db.execute(text("SELECT input FROM runs WHERE id=:id"), {"id": run_id}).scalar()
                == before
            )
            assert (
                db.execute(
                    text("SELECT payload FROM events WHERE id=:id"), {"id": event_id}
                ).scalar()
                == payload
            )
    finally:
        command.upgrade(cfg, "head")
    with sessions.begin() as db:
        assert db.get(Event, event_id).payload == payload
        db.get(Event, event_id).wire_payload = {"preserve": "sentinel"}
    with pytest.raises(RuntimeError, match="Contracts data exists"):
        command.downgrade(cfg, "0001")
    with sessions() as db:
        assert db.get(Event, event_id).wire_payload == {"preserve": "sentinel"}
