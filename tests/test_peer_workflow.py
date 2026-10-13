import copy
import json
import uuid
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient

from flowops import wire
from flowops._vendor.ecosystem_contracts import ContractError
from flowops.canonical_api import create_app
from flowops.contracts import PRESETS
from flowops.db import Run
from flowops.interop import PeerClient, PeerResponseError
from flowops.peer_workflow import Connection
from flowops.workflow import Workflow
from tests.test_contracts_v1 import PREFIX, drain


@pytest.fixture
def connected(system, tmp_path, monkeypatch):
    settings, sessions = system[:2]
    profile = {
        "profile_id": "local-review",
        "environment": "test",
        "tenant_id": settings.contracts_tenant_id,
        "synthetic_only": True,
        "inference": {"base_url": "https://agent.test", "bearer": "a" * 40},
        "knowledge": {
            "base_url": "https://knowledge.test",
            "bearer": "k" * 40,
            "collection_ids": ["operations"],
            "query": "answer citation original source passage document version",
        },
    }
    path = tmp_path / "profiles.json"
    path.write_text(json.dumps([profile]))
    path.chmod(0o600)
    settings = replace(settings, source_revision="1" * 40, peer_profiles_file=str(path))
    calls = []

    def respond(request):
        body = json.loads(request.content)
        calls.append((request.url.host, body))
        if request.url.host == "agent.test":
            return httpx.Response(403, json=wire.error("POLICY_BLOCKED", 403, "peer-correlation-1"))
        return httpx.Response(
            200,
            json={
                "schema_version": "1.0",
                "status": "OK",
                "answer": "Inspect the original passage.",
                "citations": [
                    {
                        "source_id": "search-guide",
                        "title": "Synthetic citation guide",
                        "passage": "Inspect the original passage.",
                        "location": "section-1",
                        "evidence": {
                            "evidence_id": "search-guide",
                            "origin": "FIXTURE",
                            "reference": "synthetic/search-guide.md",
                            "sha256": "1" * 64,
                        },
                    }
                ],
                "correlation_id": body["correlation_id"],
                "mode": wire.MODE,
                "limitations": ["Synthetic only."],
            },
        )

    monkeypatch.setattr(
        Connection,
        "client",
        lambda self: PeerClient(self.base_url, self.bearer, httpx.MockTransport(respond)),
    )
    app = create_app(settings)
    with TestClient(app) as client:
        client.headers["Origin"] = settings.origin
        token = client.post(PREFIX + "/simulation/session", json={}).json()["session_token"]
        client.headers["Authorization"] = "Bearer " + token
        yield (settings, sessions, client, Workflow(sessions, settings), app), path, calls, profile
    app.state.engine.dispose()


def admitted(connected, *, authorized=True, profile="local-review"):
    canonical = connected[0]
    body = {
        "schema_version": "1.0",
        "project_id": "flowops",
        "mode": wire.MODE,
        "seed": 7,
        "client_correlation_id": "opt-in-workflow",
        "input": {k: v for k, v in PRESETS[0]["input"].items() if k != "seed"},
        "extensions": {"x_peer_profile": profile},
    }
    headers = {"Idempotency-Key": str(uuid.uuid4())}
    if authorized:
        headers["X-FlowOps-Operator-Token"] = canonical[0].operator_token
    return canonical[2].post(PREFIX + "/simulations", headers=headers, json=body)


def test_trusted_opt_in_positive_knowledge_and_policy_refusal(connected):
    canonical, path, calls, profile = connected
    assert admitted(connected, authorized=False).status_code == 403
    assert not calls
    response = admitted(connected)
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]
    assert drain(canonical, run_id)["extensions"]["x_business_outcome"] == "APPROVAL_PENDING"
    peer = canonical[2].get(PREFIX + f"/simulations/{run_id}/peers").json()
    assert peer["inference"]["route"]["outcome"] == "POLICY_BLOCKED"
    assert peer["inference"]["error"]["code"] == "POLICY_BLOCKED"
    assert peer["inference"]["route"]["fallback_kind"] == "DETERMINISTIC"
    wire.validated("route-decision", peer["inference"]["route"])
    assert peer["knowledge"]["response"]["status"] == "OK"
    assert peer["knowledge"]["response"]["citations"][0]["source_id"] == "search-guide"
    assert all(
        value["business_authority"] == "NONE"
        for value in (peer, peer["inference"], peer["knowledge"])
    )
    assert len(calls) == 2
    body = calls[0][1]
    assert body["risk_class"] == "HIGH" and body["currency"] == "EUR"
    assert body["allowed_regions"] == ["eu-test"] and body["context_refs"] == [run_id]
    assert body["max_cost"] == 0 and body["routing_policy_version"] == "flowops-sandbox-no-spend-1"
    assert "a" * 40 not in json.dumps(peer) and "k" * 40 not in json.dumps(peer)
    # Checkpoint recovery does not repeat either completed peer phase.
    canonical[3].maintain()
    assert len(calls) == 2
    with canonical[1]() as db:
        run = db.get(Run, run_id)
        assert run.context["decision"]["status"] == "ALLOW"
        assert run.context["runtime"]["status"] == "AI_UNAVAILABLE"


@pytest.mark.parametrize("change", ["deleted", "query", "tenant"])
def test_profile_revocation_or_changed_scope_stops_outbound_calls(connected, change):
    canonical, path, calls, profile = connected
    run_id = admitted(connected).json()["run_id"]
    if change == "deleted":
        path.write_text("[]")
    else:
        profile = copy.deepcopy(profile)
        if change == "query":
            profile["knowledge"]["query"] = "changed query"
        else:
            profile["tenant_id"] = "foreign"
        path.write_text(json.dumps([profile]))
    assert drain(canonical, run_id)["extensions"]["x_business_outcome"] == "APPROVAL_PENDING"
    assert not calls
    peers = canonical[2].get(PREFIX + f"/simulations/{run_id}/peers").json()
    assert peers["inference"]["error"]["code"] == "FORBIDDEN"
    assert peers["knowledge"]["error"]["code"] == "FORBIDDEN"


@pytest.mark.parametrize("kind", ["canonical", "oversize", "invalid", "wrong-status"])
def test_peer_errors_are_bounded_and_read_before_stream_closes(kind):
    closed = []
    body = wire.error("POLICY_BLOCKED", 403, "correlation-peer")
    content = wire.compact(body)
    if kind == "oversize":
        content = b"x" * 262145
    if kind == "invalid":
        content = b"private server exception"
    if kind == "wrong-status":
        content = wire.compact({**body, "http_status": 401})

    class Body(httpx.SyncByteStream):
        def __iter__(self):
            for start in range(0, len(content), 1000):
                yield content[start : start + 1000]

        def close(self):
            closed.append(True)

    client = PeerClient(
        "https://peer.test",
        "t" * 40,
        httpx.MockTransport(lambda _: httpx.Response(403, stream=Body())),
    )
    if kind == "canonical":
        with pytest.raises(PeerResponseError) as caught:
            client._post("/inference", {}, "route-decision")
        assert caught.value.error == body and str(caught.value) == "POLICY_BLOCKED"
        assert not hasattr(caught.value, "response")
    else:
        with pytest.raises(ContractError):
            client._post("/inference", {}, "route-decision")
    assert closed == [True]
