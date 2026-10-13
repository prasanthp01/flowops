import asyncio
import hashlib
import json
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from flowops import wire
from flowops._vendor.ecosystem_contracts import ContractError, validate_stream
from flowops.contracts import PRESETS
from flowops.db import Run, ServiceOutbox, now

FIXTURES = Path(__file__).parent / "fixtures/contracts-v1"
PREFIX = "/api/projects/flowops"


@pytest.mark.parametrize("path", sorted((FIXTURES / "valid").glob("*.json")), ids=lambda p: p.stem)
def test_pinned_canonical_valid_invalid_and_required_fields(path):
    value = json.loads(path.read_text())
    wire.REGISTRY.validate(path.stem, value)
    with pytest.raises(ContractError):
        wire.REGISTRY.validate(
            path.stem, json.loads((FIXTURES / "invalid" / path.name).read_text())
        )
    for field in wire.REGISTRY.schemas[path.stem]["required"]:
        with pytest.raises(ContractError):
            wire.REGISTRY.validate(path.stem, {k: v for k, v in value.items() if k != field})


def test_vendor_release_and_hashes():
    root = Path(__file__).parents[1]
    lock = json.loads((root / "contracts.lock.json").read_text())
    assert lock["release_commit"] == "b55d1a9dc5d2bab23817b984b9801ccff7304f04"
    for name, expected in lock["files"].items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected, name


def start(canonical, preset=0, *, changes=None, seed=7, key=None, correlation="contract-check-1"):
    client = canonical[2]
    inputs = {k: v for k, v in PRESETS[preset]["input"].items() if k != "seed"}
    inputs.update(changes or {})
    return client.post(
        PREFIX + "/simulations",
        headers={"Idempotency-Key": key or str(uuid.uuid4())},
        json={
            "schema_version": "1.0",
            "project_id": "flowops",
            "mode": "SANDBOX_DETERMINISTIC",
            "preset_id": PRESETS[preset]["id"],
            "input": inputs,
            "seed": seed,
            "client_correlation_id": correlation,
        },
    )


def drain(canonical, run_id):
    worker = canonical[3]

    async def work():
        for _ in range(300):
            await worker.run_once()
            state = canonical[2].get(PREFIX + "/simulations/" + run_id)
            assert state.status_code == 200, state.text
            value = state.json()
            wire.REGISTRY.validate("simulation-run", value)
            if (
                value["status"] in wire.TERMINAL
                or value["extensions"]["x_business_outcome"] == "APPROVAL_PENDING"
            ):
                return value
            await asyncio.sleep(0.01)
        raise AssertionError("Workflow failed to settle")

    return asyncio.run(work())


def check_error(response, status, code):
    assert response.status_code == status, response.text
    wire.REGISTRY.validate("error", response.json())
    assert response.json()["code"] == code


def test_schema_presets_and_direct_prefix_alias(canonical):
    client = canonical[2]
    for path, name in [
        ("/project", "project-identity"),
        ("/simulation/schema", "simulation-schema"),
        ("/simulation/presets", "simulation-presets"),
    ]:
        a, b = client.get(path), client.get(PREFIX + path)
        assert a.status_code == b.status_code == 200
        assert a.json() == b.json()
        wire.REGISTRY.validate(name, a.json())
    value = client.get("/simulation/schema").json()
    assert (
        hashlib.sha256(wire.compact(value["input_schema"])).hexdigest()
        == value["input_schema_sha256"]
    )
    assert "seed" not in value["input_schema"]["properties"]
    assert value["limits"]["max_concurrent_global"] == 16


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
def test_real_workflow_projection_and_replay(canonical, preset, outcome):
    client = canonical[2]
    created = start(canonical, preset)
    assert created.status_code == 202, created.text
    wire.REGISTRY.validate("simulation-run", created.json())
    run_id = created.json()["run_id"]
    check_error(client.get(PREFIX + f"/simulations/{run_id}/result"), 409, "CONFLICT")
    state = drain(canonical, run_id)
    assert state["extensions"]["x_business_outcome"] == outcome
    if outcome == "APPROVAL_PENDING":
        assert state["completed_at"] is None and state["result_ref"] is None
        check_error(client.get(PREFIX + f"/simulations/{run_id}/result"), 409, "CONFLICT")
        client.post(PREFIX + f"/simulations/{run_id}/cancel")
    response = client.get(PREFIX + f"/simulations/{run_id}/result")
    assert response.status_code == 200, response.text
    result = response.json()
    wire.REGISTRY.validate("simulation-result", result)
    assert result["cost"]["kind"] == result["usage"]["kind"] == "NONE"
    assert result["usage"]["provider_calls"] == 0
    if outcome == "BLOCKED":
        assert result["safety_status"] == "BLOCKED" and not result["errors"] and result["warnings"]
    response = client.get(
        PREFIX + f"/simulations/{run_id}/events", headers={"Accept": "application/json"}
    )
    wire.REGISTRY.validate("simulation-events", response.json())
    all_events = response.json()["events"]
    validate_stream(all_events, run_id=run_id, project_id="flowops")
    repeat = client.get(PREFIX + f"/simulations/{run_id}/events").json()
    assert response.json() == repeat
    replay = client.get(
        PREFIX + f"/simulations/{run_id}/events?after=2", headers={"Last-Event-ID": "2"}
    ).json()
    assert replay["events"] == all_events[2:]
    sse = client.get(
        PREFIX + f"/simulations/{run_id}/events",
        headers={"Accept": "text/event-stream", "Last-Event-ID": "2"},
    )
    assert sse.status_code == 200
    assert f'event: end\ndata: {{"last_sequence":{len(all_events)}}}' in sse.text
    assert [
        json.loads(line[6:])
        for line in sse.text.splitlines()
        if line.startswith('data: {"component"')
    ] == all_events[2:]
    for ev in result["evidence"]:
        content = client.get(PREFIX + "/" + ev["reference"]).json()
        assert hashlib.sha256(wire.compact(content["content"])).hexdigest() == ev["sha256"]
    prior = client.get(PREFIX + f"/simulations/{run_id}").json()
    assert client.post(PREFIX + f"/simulations/{run_id}/cancel").json() == prior


def test_event_projection_is_frozen_before_later_steps(canonical):
    client, worker = canonical[2:4]
    run_id = start(canonical).json()["run_id"]
    asyncio.run(worker.run_once())
    first = client.get(PREFIX + f"/simulations/{run_id}/events").json()["events"]
    drain(canonical, run_id)
    later = client.get(PREFIX + f"/simulations/{run_id}/events").json()["events"]
    assert first == later[: len(first)]


@pytest.mark.parametrize(
    "query,header", [("-1", None), ("abc", None), ("1025", None), ("1", "2"), (None, "bad")]
)
def test_invalid_cursors(canonical, query, header):
    run_id = start(canonical).json()["run_id"]
    path = (
        PREFIX + f"/simulations/{run_id}/events" + ("?after=" + query if query is not None else "")
    )
    check_error(
        canonical[2].get(path, headers={"Last-Event-ID": header} if header else {}),
        409,
        "INVALID_CURSOR",
    )


def test_strict_inputs_ownership_expiry_and_idempotency(canonical):
    client, app = canonical[2], canonical[4]
    key = str(uuid.uuid4())
    first = start(canonical, key=key, seed=4294967295)
    assert first.status_code == 202, first.text
    run_id = first.json()["run_id"]
    assert start(canonical, key=key, seed=4294967295).json()["run_id"] == run_id
    check_error(start(canonical, key=key, seed=1), 409, "CONFLICT")
    check_error(start(canonical, changes={"seed": 9}), 422, "VALIDATION_ERROR")
    check_error(
        start(canonical, changes={"refund_requested_minor": 999999}), 422, "VALIDATION_ERROR"
    )
    with TestClient(app) as other:
        check_error(other.get(PREFIX + f"/simulations/{run_id}"), 401, "UNAUTHENTICATED")
        token = other.post(
            PREFIX + "/simulation/session", json={}, headers={"Origin": canonical[0].origin}
        ).json()["session_token"]
        other.headers["Authorization"] = "Bearer " + token
        a = other.get(PREFIX + f"/simulations/{run_id}")
        b = other.get(PREFIX + f"/simulations/{uuid.uuid4()}")
        check_error(a, 404, "NOT_FOUND")
        check_error(b, 404, "NOT_FOUND")
    with canonical[1].begin() as db:
        db.get(Run, run_id).expires_at = now() - timedelta(seconds=1)
    check_error(client.get(PREFIX + f"/simulations/{run_id}/events"), 410, "RUN_EXPIRED")


def test_body_authentication_and_error_bounds(canonical):
    client = canonical[2]
    check_error(
        client.post(
            PREFIX + "/simulations",
            content='{"x":"' + "x" * 17000 + '"}',
            headers={"Content-Type": "application/json"},
        ),
        413,
        "PAYLOAD_TOO_LARGE",
    )
    for body in ["{bad", '{"x":NaN}', '{"x":1,"x":2}', "[" * 20 + "0" + "]" * 20]:
        check_error(
            client.post(
                PREFIX + "/simulations", content=body, headers={"Content-Type": "application/json"}
            ),
            422,
            "VALIDATION_ERROR",
        )
    check_error(
        client.post(
            PREFIX + "/simulation/session", json={}, headers={"Origin": "https://wrong.example"}
        ),
        403,
        "FORBIDDEN",
    )
    check_error(
        client.get(PREFIX + "/simulation/schema", headers={"Host": "untrusted.example"}),
        403,
        "FORBIDDEN",
    )


def test_failure_outbox_and_late_timeout_observation(canonical):
    run_id = start(canonical, changes={"api_response_mode": "timeout"}).json()["run_id"]
    assert drain(canonical, run_id)["status"] == "FAILED"
    with canonical[1]() as db:
        outbox = db.scalar(select(ServiceOutbox))
        assert outbox.logical_event["payload"]["workflow_id"] == run_id
    created = start(canonical).json()["run_id"]
    with canonical[1].begin() as db:
        run = db.get(Run, created)
        run.created_at = now() - timedelta(minutes=5)
        run.deadline_at = run.created_at + timedelta(seconds=60)
    canonical[3].maintain()
    result = canonical[2].get(PREFIX + f"/simulations/{created}/result")
    assert result.status_code == 200, result.text
    wire.REGISTRY.validate("simulation-result", result.json())
    assert result.json()["duration_ms"] == 60000
    assert result.json()["extensions"]["x_timeout_observed_at"]


def test_published_openapi_and_late_cancel_remain_canonical(canonical):
    client = canonical[2]
    document = client.get("/openapi.json").json()
    assert "/inference" not in document["paths"] and "/knowledge/query" not in document["paths"]
    response = document["paths"]["/simulations"]["post"]["responses"]["202"]
    assert response["content"]["application/json"]["schema"]["$ref"].endswith("/simulation-run")
    run_id = start(canonical).json()["run_id"]
    with canonical[1].begin() as db:
        run = db.get(Run, run_id)
        run.created_at = now() - timedelta(minutes=5)
        run.deadline_at = run.created_at + timedelta(seconds=60)
    cancelled = client.post(PREFIX + f"/simulations/{run_id}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "FAILED"
    result = client.get(PREFIX + f"/simulations/{run_id}/result")
    assert result.status_code == 200 and result.json()["duration_ms"] == 60000
