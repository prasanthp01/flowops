import copy
import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import func, select

from flowops import wire
from flowops._vendor.ecosystem_contracts import ContractError, validate_handoff
from flowops._vendor.ecosystem_contracts.signatures import sign_event, verify_event
from flowops.agents import handoff
from flowops.db import Run, ServiceInbox, ServiceOutbox, now
from flowops.delivery import FailureDelivery
from flowops.interop import PeerClient, PeerGrant, handoff_document, no_usage_payload
from flowops.service_events import ServiceReceiver, logical_digest, reserve_failure
from tests.test_contracts_v1 import FIXTURES, PREFIX, check_error, drain, start

KEY = b"test-signing-key-not-for-real-use-123456"
TOKEN = "test-bearer-not-for-real-use-123456789"


def credentials(settings):
    return {
        "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
        "application_id": "resolveai",
        "environment": "test",
        "tenant_id": settings.contracts_tenant_id,
        "scopes": ["workflow:read"],
        "keys": {"test-key": KEY.hex()},
    }


def completion(canonical, run_id):
    with canonical[1]() as db:
        run = db.get(Run, run_id)
        meta = wire.metadata(run)
        event = {
            "schema_version": "1.0",
            "event_id": str(uuid.uuid4()),
            "source": {
                "application_id": "resolveai",
                "environment": "test",
                "tenant_id": meta["tenant_id"],
            },
            "audience": "flowops",
            "occurred_at": wire.utc(now()),
            "correlation_id": meta["client_correlation_id"],
            "trace_id": run.id,
            "sensitivity": "PUBLIC",
            "idempotency_key": "completion:" + run.id,
            "kind": "incident.investigation.completed.v1",
            "mode": wire.MODE,
            "provenance": meta["provenance"],
            "payload": {
                "incident_id": "incident-1",
                "investigation_id": "investigation-1",
                "outcome": "EXPLAINED",
                "evidence": [],
            },
            "integrity": {"algorithm": "HMAC-SHA256", "key_id": "test-key"},
        }
    return sign_event(event, KEY)


def send(canonical, event, token=TOKEN):
    return canonical[2].post(
        PREFIX + "/events", headers={"Authorization": "Bearer " + token}, json=event
    )


def test_signed_completion_duplicate_conflict_and_no_approval_authority(canonical):
    canonical[4].state.service_receiver.credentials = [credentials(canonical[0])]
    run_id = start(canonical, 1).json()["run_id"]
    before = drain(canonical, run_id)
    event = completion(canonical, run_id)
    check_error(send(canonical, event, "wrong"), 401, "UNAUTHENTICATED")
    first = send(canonical, event)
    assert first.status_code == 202 and first.json()["status"] == "ACCEPTED"
    repeated = sign_event(
        {**event, "event_id": str(uuid.uuid4()), "occurred_at": wire.utc(now())}, KEY
    )
    assert send(canonical, repeated).json()["status"] == "DUPLICATE"
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: send(canonical, repeated), range(4)))
    assert all(r.status_code == 202 and r.json()["status"] == "DUPLICATE" for r in results)
    conflicting = copy.deepcopy(repeated)
    conflicting["payload"]["outcome"] = "ABSTAINED"
    check_error(send(canonical, sign_event(conflicting, KEY)), 409, "CONFLICT")
    receiver = ServiceReceiver(canonical[0], canonical[1])
    receiver.credentials = [credentials(canonical[0])]
    assert receiver.receive(repeated, "Bearer " + TOKEN)["status"] == "DUPLICATE"
    assert canonical[2].get(PREFIX + f"/simulations/{run_id}").json() == before
    findings = canonical[2].get(PREFIX + f"/simulations/{run_id}/investigations").json()
    assert findings["business_authority"] == "NONE" and len(findings["findings"]) == 1
    with canonical[1]() as db:
        assert db.scalar(select(func.count()).select_from(ServiceInbox)) == 1


@pytest.mark.parametrize(
    "change",
    [
        "signature",
        "key",
        "age",
        "audience",
        "source",
        "tenant",
        "environment",
        "correlation",
        "trace",
        "scope",
    ],
)
def test_receiver_rejects_invalid_trust_context(canonical, change):
    credential = credentials(canonical[0])
    canonical[4].state.service_receiver.credentials = [credential]
    event = completion(canonical, start(canonical).json()["run_id"])
    if change == "signature":
        event["integrity"]["digest"] = "0" * 64
    elif change == "key":
        event["integrity"]["key_id"] = "unknown-key"
    elif change == "age":
        event["occurred_at"] = wire.utc(now() - timedelta(minutes=6))
    elif change == "audience":
        event["audience"] = "agentops"
    elif change == "source":
        event["source"]["application_id"] = "knowledgeops"
    elif change in {"tenant", "environment"}:
        event["source"]["tenant_id" if change == "tenant" else change] = (
            "wrong" if change == "tenant" else "production"
        )
    elif change == "correlation":
        event["correlation_id"] = "unrelated"
    elif change == "trace":
        event["trace_id"] = str(uuid.uuid4())
    elif change == "scope":
        credential["scopes"] = []
    if change != "signature":
        event = sign_event(event, KEY)
    status = 404 if change in {"correlation", "trace"} else 403
    # Canonical semantic validation can reject impossible producer/kind pairs first.
    response = send(canonical, event)
    if response.status_code == 422:
        assert change in {"source", "audience"}
        check_error(response, 422, "VALIDATION_ERROR")
    else:
        check_error(response, status, "NOT_FOUND" if status == 404 else "FORBIDDEN")


def test_key_rotation_is_allowlisted_and_revocation_is_immediate(canonical, tmp_path):
    entry = credentials(canonical[0])
    entry["keys"]["rotated-key"] = (b"k" * 32).hex()
    path = tmp_path / "private-services.json"
    path.write_text(json.dumps([entry]))
    receiver = ServiceReceiver(
        replace(canonical[0], service_credentials_file=str(path)), canonical[1]
    )
    event = completion(canonical, start(canonical).json()["run_id"])
    event["integrity"]["key_id"] = "rotated-key"
    signed = sign_event(event, b"k" * 32)
    assert receiver.receive(signed, "Bearer " + TOKEN)["status"] == "ACCEPTED"
    del receiver.credentials[0]["keys"]["rotated-key"]
    with pytest.raises(Exception) as exc:
        receiver.receive(signed, "Bearer " + TOKEN)
    assert exc.value.status_code == 403


def test_handoffs_and_deterministic_operator_approval(canonical):
    run_id = start(canonical, 1).json()["run_id"]
    drain(canonical, run_id)
    client, settings = canonical[2], canonical[0]
    items = client.get(PREFIX + f"/simulations/{run_id}/handoffs").json()["handoffs"]
    assert {item["agent_role"] for item in items} == {
        "SUPERVISOR",
        "RETRIEVAL",
        "EXECUTION",
        "REVIEW",
    }
    for item in items:
        validate_handoff(
            item,
            granted_scopes={"workflow:read", "workflow:propose"},
            tenant_id=settings.contracts_tenant_id,
        )
    output = client.get(PREFIX + f"/simulations/{run_id}/proposal").json()
    assert not output["final"] and not output["trusted_approval_from_client"]
    approval = output["approval"]
    body = {
        "request_id": approval["request_id"],
        "action_digest": approval["action_digest"],
        "decision": "approve",
    }
    path = PREFIX + f"/operator/simulations/{run_id}/approval"
    check_error(client.post(path, json=body), 401, "UNAUTHENTICATED")
    headers = {"Authorization": "Bearer " + settings.operator_token}
    check_error(
        client.post(path, headers=headers, json={**body, "action_digest": "0" * 64}),
        409,
        "CONFLICT",
    )
    assert client.post(path, headers=headers, json=body).status_code == 200
    assert client.post(path, headers=headers, json=body).status_code == 200
    assert drain(canonical, run_id)["extensions"]["x_business_outcome"] == "SIMULATED_APPLIED"
    with canonical[1]() as db:
        run = db.get(Run, run_id)
        native = handoff(run.id, "operations", run.environment, now() + timedelta(seconds=10), [])
        for changes in [
            {"workflow_id": "wrong"},
            {"delegated_scopes": ["policy:read"]},
            {"deadline": now() - timedelta(seconds=1)},
        ]:
            with pytest.raises(ContractError):
                handoff_document(
                    native.model_copy(update=changes),
                    run,
                    granted_scopes={"workflow:propose"},
                    tenant_id=settings.contracts_tenant_id,
                )
        with pytest.raises(ContractError):
            handoff_document(
                native, run, granted_scopes=set(), tenant_id=settings.contracts_tenant_id
            )


def sender(canonical, transport=None):
    return FailureDelivery(
        canonical[1],
        url="https://resolveai.test/events",
        bearer=TOKEN,
        signing_key=KEY,
        key_id="test-key",
        environment="test",
        tenant_id=canonical[0].contracts_tenant_id,
        transport=transport,
    )


def failed(canonical):
    run_id = start(canonical, changes={"api_response_mode": "timeout"}).json()["run_id"]
    assert drain(canonical, run_id)["status"] == "FAILED"
    return "workflow-failed:" + run_id


def due(canonical, operation):
    with canonical[1].begin() as db:
        db.get(ServiceOutbox, operation).next_attempt_at = now() - timedelta(seconds=1)


def test_outbox_lost_response_then_duplicate_receipt(canonical):
    operation = failed(canonical)
    seen, events = {}, []

    def receive(request):
        event = json.loads(request.content)
        wire.validated("cross-project-event", event)
        verify_event(
            event,
            KEY,
            audience="resolveai",
            source_app="flowops",
            environment="test",
            tenant_id=canonical[0].contracts_tenant_id,
        )
        events.append(event)
        key = event["idempotency_key"]
        checksum = logical_digest(event)
        if key not in seen:
            seen[key] = checksum
            raise httpx.ReadTimeout("Response lost after commit")
        assert seen[key] == checksum
        return httpx.Response(
            202,
            json={
                "schema_version": "1.0",
                "event_id": event["event_id"],
                "correlation_id": event["correlation_id"],
                "status": "DUPLICATE",
            },
        )

    transport = httpx.MockTransport(receive)
    assert sender(canonical, transport).deliver_once()
    with canonical[1]() as db:
        assert db.get(ServiceOutbox, operation).last_code == "UNCERTAIN"
    due(canonical, operation)
    assert sender(
        canonical, transport
    ).deliver_once()  # New sender process/object, same durable journal.
    with canonical[1]() as db:
        row = db.get(ServiceOutbox, operation)
        assert row.status == "DELIVERED" and row.attempts == 2
    assert events[0]["event_id"] != events[1]["event_id"]
    assert len(seen) == 1


def test_outbox_fencing_bounded_retries_and_atomic_rollback(canonical):
    operation = failed(canonical)
    a = sender(canonical)
    first = a.claim()
    assert first and a.claim() is None
    with canonical[1].begin() as db:
        db.get(ServiceOutbox, operation).lease_until = now() - timedelta(seconds=1)
    b = sender(canonical)
    second = b.claim()
    assert second[1] > first[1]
    assert not a.settle(first[0], first[1], "ACKNOWLEDGED")
    assert b.settle(second[0], second[1], "RETRYABLE", retry_after=999999)
    with canonical[1]() as db:
        row = db.get(ServiceOutbox, operation)
        assert 3598 <= (row.next_attempt_at - now()).total_seconds() <= 3600
    transport = httpx.MockTransport(lambda _: httpx.Response(503))
    for _ in range(3):
        due(canonical, operation)
        sender(canonical, transport).deliver_once()
    with canonical[1]() as db:
        row = db.get(ServiceOutbox, operation)
        assert row.status == "DEAD_LETTER" and row.attempts == 5
    run_id = start(canonical).json()["run_id"]
    with pytest.raises(RuntimeError):
        with canonical[1].begin() as db:
            reserve_failure(db, db.get(Run, run_id), "TEST")
            db.flush()
            raise RuntimeError("Domain transaction rolled back")
    with canonical[1]() as db:
        assert not db.get(ServiceOutbox, "workflow-failed:" + run_id)


def test_peer_authorization_redaction_and_no_provider_spend(canonical):
    run_id = start(canonical).json()["run_id"]
    with canonical[1]() as db:
        run = db.get(Run, run_id)
        grant = PeerGrant(
            run.id,
            canonical[0].contracts_tenant_id,
            frozenset({"knowledge:read", "inference:request"}),
            frozenset({"synthetic-policies"}),
        )
        calls = []

        def receive(request):
            calls.append(request)
            data = json.loads(request.content)
            if request.url.path == "/knowledge/query":
                wire.validated("knowledge-query", data)
                return httpx.Response(
                    200,
                    json={
                        "schema_version": "1.0",
                        "status": "DENIED",
                        "answer": None,
                        "citations": [],
                        "correlation_id": data["correlation_id"],
                        "mode": wire.MODE,
                        "limitations": ["Access denied"],
                    },
                )
            wire.validated("inference-request", data)
            value = json.loads((FIXTURES / "valid/route-decision.json").read_text())
            value.update(
                {
                    k: data[k]
                    for k in (
                        "operation_id",
                        "correlation_id",
                        "trace_id",
                        "mode",
                        "routing_policy_version",
                    )
                }
            )
            value["usage"]["cost"]["currency"] = data["currency"]
            return httpx.Response(200, json=value)

        client = PeerClient("https://peer.test", TOKEN, httpx.MockTransport(receive))
        denied = client.knowledge(run, grant, ["synthetic-policies"])
        assert denied["answer"] is None and denied["citations"] == []
        assert client.inference(run, grant)["outcome"] == "AI_UNAVAILABLE"
        assert no_usage_payload(run)["usage"]["provider_calls"] == 0
        for denied_grant in [
            replace(grant, workflow_id="foreign"),
            replace(grant, tenant_id="foreign"),
            replace(grant, scopes=frozenset()),
        ]:
            with pytest.raises(PermissionError):
                client.knowledge(run, denied_grant, ["synthetic-policies"])
        with pytest.raises(PermissionError):
            client.knowledge(run, grant, ["secret-collection"])
        assert len(calls) == 2
        leaked = {**denied, "answer": "private", "citations": []}
        client.transport = httpx.MockTransport(lambda _: httpx.Response(200, json=leaked))
        with pytest.raises(ContractError):
            client.knowledge(run, grant, ["synthetic-policies"])
        client.transport = httpx.MockTransport(lambda _: httpx.Response(503))
        with pytest.raises(ContractError):
            client.inference(run, grant)


@pytest.mark.parametrize("bad", ["provider_calls", "cost", "currency", "correlation", "region"])
def test_inference_rejects_untrusted_accounting_and_bindings(canonical, bad):
    run_id = start(canonical).json()["run_id"]
    with canonical[1]() as db:
        run = db.get(Run, run_id)
        grant = PeerGrant(
            run.id, canonical[0].contracts_tenant_id, frozenset({"inference:request"})
        )

        def receive(request):
            body = json.loads(request.content)
            value = json.loads((FIXTURES / "valid/route-decision.json").read_text())
            value.update(
                {
                    k: body[k]
                    for k in (
                        "operation_id",
                        "correlation_id",
                        "trace_id",
                        "mode",
                        "routing_policy_version",
                    )
                }
            )
            value["usage"]["cost"]["currency"] = body["currency"]
            if bad == "provider_calls":
                value["usage"]["provider_calls"] = 1
            elif bad == "cost":
                value["usage"]["cost"]["value"] = 1
            elif bad == "currency":
                value["usage"]["cost"]["currency"] = "USD"
            elif bad == "correlation":
                value["correlation_id"] = "unrelated"
            else:
                value["outcome"] = "SUCCEEDED"
                value["selected_deployment"] = {
                    "deployment_id": "wrong-region",
                    "model_version": "fixture-1",
                    "provider": "mock",
                    "region": "us-east",
                    "capabilities": ["STRUCTURED_OUTPUT"],
                }
            return httpx.Response(200, json=value)

        client = PeerClient("https://peer.test", TOKEN, httpx.MockTransport(receive))
        with pytest.raises(ContractError):
            client.inference(run, grant)
