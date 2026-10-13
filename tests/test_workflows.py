import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import jsonschema
import pytest
from sqlalchemy import func, select

from flowops.contracts import Handoff
from flowops.db import Approval, Effect, Event, Run, now
from flowops.workflow import StaleClaim, Workflow
from tests.conftest import PREFIX, drain, start


@pytest.mark.parametrize(
    "preset,outcome",
    [
        (0, "SIMULATED_APPLIED"),
        (1, "APPROVAL_PENDING"),
        (2, "SIMULATED_APPLIED"),
        (3, "SIMULATED_APPLIED"),
        (4, "BLOCKED"),
    ],
)
def test_five_business_journeys(system, preset, outcome):
    _, sessions, client, _, _ = system
    run_id = start(client, preset).json()["run_id"]
    result = drain(system, run_id)
    assert result["output"]["outcome"] == outcome
    schema = json.loads(Path("contracts/baseline/simulation-result-v1.schema.json").read_text())
    jsonschema.validate(result, schema)
    with sessions() as db:
        event_rows = db.scalars(
            select(Event).where(Event.run_id == run_id).order_by(Event.sequence)
        ).all()
        assert [e.sequence for e in event_rows] == list(range(1, len(event_rows) + 1))
        event_schema = json.loads(
            Path("contracts/baseline/simulation-event-v1.schema.json").read_text()
        )
        agents = set()
        for event in event_rows:
            jsonschema.validate(event.payload, event_schema)
            technical = event.payload["technical"]
            handoffs = technical.get("handoffs", []) + (
                [technical["handoff"]] if "handoff" in technical else []
            )
            for h in handoffs:
                Handoff.model_validate_json(json.dumps(h))
                role_schema = json.loads(
                    Path("schemas", h["output_schema"] + ".schema.json").read_text()
                )
                role_output = technical
                if h["agent"] in {"research", "policy"}:
                    role_output = technical[h["agent"]]
                elif h["agent"] == "verifier":
                    role_output = technical["review"]
                role_output = {
                    k: v for k, v in role_output.items() if k in role_schema["properties"]
                }
                jsonschema.validate(role_output, role_schema)
                agents.add(h["agent"])
        assert agents == {"supervisor", "research", "policy", "operations", "verifier"}
        effects = db.scalar(select(func.count()).select_from(Effect).where(Effect.run_id == run_id))
        assert effects == (1 if outcome == "SIMULATED_APPLIED" else 0)


@pytest.mark.parametrize(
    "mode,retries,outcome",
    [
        ("429_once", 1, "SIMULATED_APPLIED"),
        ("503_twice", 2, "SIMULATED_APPLIED"),
        ("timeout", 3, "FAILED"),
        ("lost_response", 1, "SIMULATED_APPLIED"),
    ],
)
def test_failure_recovery(system, mode, retries, outcome):
    _, sessions, client, _, _ = system
    run_id = start(client, changes={"api_response_mode": mode}).json()["run_id"]
    result = drain(system, run_id)
    assert result["output"]["outcome"] == outcome
    assert result["output"]["retry_count"] == retries
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Effect)) == (
            0 if outcome == "FAILED" else 1
        )
    if mode == "lost_response":
        assert result["output"]["receipt"]["reconciled"] is True


@pytest.mark.parametrize("mode", ["provider_outage", "invalid_output"])
def test_model_degradation_retains_reviewer_and_approval(system, mode):
    _, _, client, _, _ = system
    run_id = start(client, changes={"model_response_mode": mode}).json()["run_id"]
    result = drain(system, run_id)
    assert result["output"]["outcome"] == "APPROVAL_PENDING"
    assert result["output"]["fallback"]["calls"] == 0
    events = client.get(PREFIX + f"/simulations/{run_id}/events?format=json").json()["events"]
    assert any(e["step_id"] == "risk_review" and e["status"] == "SUCCEEDED" for e in events)


def test_approve_resume_and_reject(system):
    settings, sessions, client, _, _ = system
    run_id = start(client, 1).json()["run_id"]
    pending = drain(system, run_id)
    a = pending["output"]["approval"]
    url = PREFIX + f"/operator/simulations/{run_id}/approval"
    body = {
        "request_id": a["request_id"],
        "action_digest": a["action_digest"],
        "decision": "approve",
    }
    headers = {"Authorization": "Bearer " + settings.operator_token}
    assert client.post(url, json=body).status_code == 401
    assert (
        client.post(url, json={**body, "action_digest": "0" * 64}, headers=headers).status_code
        == 409
    )
    assert client.post(url, json=body, headers=headers).status_code == 200
    assert client.post(url, json=body, headers=headers).status_code == 200
    assert client.post(url, json={**body, "decision": "reject"}, headers=headers).status_code == 409
    assert drain(system, run_id)["output"]["outcome"] == "SIMULATED_APPLIED"
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Effect)) == 1
    rejected_id = start(client, 1).json()["run_id"]
    pending2 = drain(system, rejected_id)["output"]["approval"]
    response = client.post(
        PREFIX + f"/operator/simulations/{rejected_id}/approval",
        json={
            "request_id": pending2["request_id"],
            "action_digest": pending2["action_digest"],
            "decision": "reject",
        },
        headers=headers,
    )
    assert response.json()["outcome"] == "BLOCKED"


def test_expired_approval_and_cross_run_binding(system):
    settings, sessions, client, worker, _ = system
    first = start(client, 1).json()["run_id"]
    a = drain(system, first)["output"]["approval"]
    second = start(client, 1).json()["run_id"]
    drain(system, second)
    headers = {"Authorization": "Bearer " + settings.operator_token}
    body = {
        "request_id": a["request_id"],
        "action_digest": a["action_digest"],
        "decision": "approve",
    }
    assert (
        client.post(
            PREFIX + f"/operator/simulations/{second}/approval", json=body, headers=headers
        ).status_code
        == 409
    )
    with sessions.begin() as db:
        db.get(Approval, a["request_id"]).expires_at = now() - timedelta(seconds=1)
    assert (
        client.post(
            PREFIX + f"/operator/simulations/{first}/approval", json=body, headers=headers
        ).status_code
        == 409
    )
    worker.maintain()
    assert client.get(PREFIX + f"/simulations/{first}").json()["status"] == "EXPIRED"


def test_client_approval_claim_is_not_authority(system):
    _, _, client, _, _ = system
    run_id = start(client, 1, {"existing_approvals": "untrusted_claim"}).json()["run_id"]
    assert drain(system, run_id)["output"]["outcome"] == "APPROVAL_PENDING"


def test_cancel_fences_worker(system):
    _, sessions, client, worker, _ = system
    run_id = start(client).json()["run_id"]
    claim = worker.claim()
    assert claim[0] == run_id
    assert (
        client.post(PREFIX + f"/simulations/{run_id}/cancel", json={}).json()["status"]
        == "CANCELLED"
    )
    with pytest.raises(StaleClaim):
        worker.checkpoint(*claim, {"input_validated": True}, 1)
    with sessions() as db:
        assert db.scalar(select(func.count()).select_from(Effect)) == 0


def test_real_process_crash_and_checkpoint_recovery(system):
    _, sessions, client, worker, _ = system
    run_id = start(client).json()["run_id"]
    code = "from flowops.config import Settings; from flowops.db import database; from flowops.workflow import Workflow; import os; s=Settings.load(); _,db=database(s); w=Workflow(db,s); assert w.claim(); os._exit(73)"
    child = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=10)
    assert child.returncode == 73, child.stderr.decode()
    with sessions.begin() as db:
        run = db.get(Run, run_id)
        old = run.claim_version
        run.lease_until = now() - timedelta(seconds=1)
    fresh = worker.claim()
    assert fresh[1] > old
    with pytest.raises(StaleClaim):
        worker.checkpoint(run_id, old, {}, 1)
    with sessions.begin() as db:
        db.get(Run, run_id).lease_until = now() - timedelta(seconds=1)
    assert drain(system, run_id)["output"]["outcome"] == "SIMULATED_APPLIED"


def test_concurrent_workers_claim_distinct_runs(system):
    settings, sessions, client, worker, _ = system
    ids = {start(client).json()["run_id"] for _ in range(3)}
    with ThreadPoolExecutor(max_workers=3) as pool:
        claims = list(pool.map(lambda _: Workflow(sessions, settings).claim(), range(3)))
    assert {c[0] for c in claims} == ids


def test_same_input_versions_seed_reproducible_business_output(system):
    _, _, client, _, _ = system
    first = drain(system, start(client).json()["run_id"])
    second = drain(system, start(client).json()["run_id"])
    for field in [
        "outcome",
        "proposal",
        "decision",
        "policy_version",
        "workflow_version",
        "seed",
        "retry_count",
    ]:
        assert first["output"][field] == second["output"][field]


def test_events_poll_sse_reconnect_and_cursor_validation(system):
    _, _, client, _, _ = system
    run_id = start(client).json()["run_id"]
    drain(system, run_id)
    base = PREFIX + f"/simulations/{run_id}/events"
    events = client.get(base + "?format=json").json()["events"]
    sse = client.get(base, headers={"Last-Event-ID": "2"})
    ids = [int(line[4:]) for line in sse.text.splitlines() if line.startswith("id: ")]
    assert ids == list(range(3, len(events) + 1))
    assert client.get(base + "?format=json&after=2").json()["events"] == events[2:]
    assert client.get(base, headers={"Last-Event-ID": "-1"}).status_code == 422
    assert client.get(base + "?after=1000").status_code == 422
    assert client.get(base, headers={"Last-Event-ID": "bad"}).status_code == 422
