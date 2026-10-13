"""Repeatable HTTP and restart smoke for the isolated compose.contracts.yaml stack only."""

import argparse
import hashlib
import json
import subprocess
import time
import uuid
from pathlib import Path

import httpx

from flowops import wire
from flowops._vendor.ecosystem_contracts import validate_stream
from flowops._vendor.ecosystem_contracts.signatures import sign_event
from flowops.db import now

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "http://127.0.0.1:8766"
BASE = ORIGIN + "/api/projects/flowops"
COMPOSE = ["docker", "compose", "-f", "compose.contracts.yaml"]


def command(*args):
    subprocess.run(
        [*COMPOSE, *args], cwd=ROOT, check=True, capture_output=True, text=True, timeout=60
    )


def run(*, restart=False):
    with httpx.Client(
        base_url=BASE, headers={"Origin": ORIGIN}, timeout=15, follow_redirects=False
    ) as client:
        health = client.get("/health").raise_for_status().json()
        assert health["contracts_version"] == "1.0.0"
        schema = client.get("/simulation/schema").raise_for_status().json()
        wire.validated("simulation-schema", schema)
        presets = client.get("/simulation/presets").raise_for_status().json()
        wire.validated("simulation-presets", presets)
        assert client.get("/openapi.json").status_code == 200
        session = client.post("/simulation/session", json={}).raise_for_status()
        assert (
            "HttpOnly" in session.headers["set-cookie"]
            and "SameSite=strict" in session.headers["set-cookie"]
        )
        client.headers["Authorization"] = "Bearer " + session.json()["session_token"]
        inputs, results, checks = [], [], []

        def start(index=0, changes=None):
            preset = presets["presets"][index]
            body = {
                "schema_version": "1.0",
                "project_id": "flowops",
                "mode": wire.MODE,
                "preset_id": preset["preset_id"],
                "seed": 7,
                "client_correlation_id": "local-http-contracts-1",
                "input": {**preset["input"], **(changes or {})},
            }
            key = str(uuid.uuid4())
            response = client.post("/simulations", json=body, headers={"Idempotency-Key": key})
            assert response.status_code == 202, response.text
            value = response.json()
            wire.validated("simulation-run", value)
            assert (
                client.post("/simulations", json=body, headers={"Idempotency-Key": key}).json()[
                    "run_id"
                ]
                == value["run_id"]
            )
            inputs.append(body)
            return value["run_id"]

        def settled(run_id):
            until = time.monotonic() + 35
            while time.monotonic() < until:
                value = client.get("/simulations/" + run_id).raise_for_status().json()
                wire.validated("simulation-run", value)
                if (
                    value["status"] in wire.TERMINAL
                    or value["extensions"]["x_business_outcome"] == "APPROVAL_PENDING"
                ):
                    return value
                time.sleep(0.1)
            raise AssertionError("Workflow did not settle within the smoke deadline")

        def record(run_id):
            result = client.get("/simulations/" + run_id + "/result").raise_for_status().json()
            wire.validated("simulation-result", result)
            page = client.get("/simulations/" + run_id + "/events").raise_for_status().json()
            wire.validated("simulation-events", page)
            events = page["events"]
            validate_stream(events, run_id=run_id, project_id="flowops")
            assert page["terminal"] and page["next_sequence"] == len(events)
            replay = (
                client.get(
                    "/simulations/" + run_id + "/events",
                    headers={"Accept": "text/event-stream", "Last-Event-ID": "2"},
                )
                .raise_for_status()
                .text
            )
            frames = [
                json.loads(line[6:])
                for line in replay.splitlines()
                if line.startswith('data: {"component"')
            ]
            assert (
                frames == events[2:]
                and f'event: end\ndata: {{"last_sequence":{len(events)}}}' in replay
            )
            invalid = client.get("/simulations/" + run_id + "/events?after=9999")
            assert invalid.status_code == 409 and invalid.json()["code"] == "INVALID_CURSOR"
            for evidence in result["evidence"]:
                value = client.get("/" + evidence["reference"]).raise_for_status().json()
                assert (
                    hashlib.sha256(wire.compact(value["content"])).hexdigest() == evidence["sha256"]
                )
            assert result["usage"]["provider_calls"] == 0 and result["cost"]["kind"] == "NONE"
            results.append({"run_id": run_id, "result": result, "events": events})
            return result

        for preset in (0, 4, 0):
            run_id = start(preset)
            state = settled(run_id)
            assert state["status"] == "SUCCEEDED"
            record(run_id)
        for name in ("outcome", "proposal", "decision", "seed"):
            assert results[0]["result"]["output"][name] == results[2]["result"]["output"][name]
        assert (
            results[0]["result"]["output"]["outcome"] != results[1]["result"]["output"]["outcome"]
        )
        checks += [
            "two_distinct_bounded_inputs",
            "seed_7_semantic_repeatability",
            "ordered_poll_sse_replay",
            "evidence_hashes",
            "zero_provider_usage",
        ]
        run_id = start(1)
        assert settled(run_id)["extensions"]["x_business_outcome"] == "APPROVAL_PENDING"
        pending = client.get("/simulations/" + run_id + "/result")
        assert pending.status_code == 409 and pending.json()["code"] == "CONFLICT"
        approval = (
            client.get("/simulations/" + run_id + "/proposal").raise_for_status().json()["approval"]
        )
        body = {k: approval[k] for k in ("request_id", "action_digest")}
        body["decision"] = "approve"
        path = "/operator/simulations/" + run_id + "/approval"
        assert client.post(path, json=body).status_code == 401
        operator = (ROOT / ".local/contracts/operator_token").read_text().strip()
        assert (
            client.post(
                path, json=body, headers={"Authorization": "Bearer " + operator}
            ).status_code
            == 200
        )
        assert settled(run_id)["extensions"]["x_business_outcome"] == "SIMULATED_APPLIED"
        record(run_id)
        checks += ["pending_result_conflict", "app_owned_approval"]
        handoffs = (
            client.get("/simulations/" + run_id + "/handoffs").raise_for_status().json()["handoffs"]
        )
        assert len(handoffs) == 5
        for item in handoffs:
            wire.validated("agent-handoff", item)
        issuer = json.loads((ROOT / ".local/contracts/test_issuer").read_text())
        result = results[0]["result"]
        event = {
            "schema_version": "1.0",
            "event_id": str(uuid.uuid4()),
            "source": {
                "application_id": "resolveai",
                "environment": "test",
                "tenant_id": "flowops-sandbox",
            },
            "audience": "flowops",
            "occurred_at": wire.utc(now()),
            "correlation_id": "local-http-contracts-1",
            "trace_id": result["run_id"],
            "sensitivity": "PUBLIC",
            "idempotency_key": "completion:" + result["run_id"],
            "kind": "incident.investigation.completed.v1",
            "mode": wire.MODE,
            "provenance": result["provenance"],
            "payload": {
                "incident_id": "local-test",
                "investigation_id": "local-test",
                "outcome": "EXPLAINED",
                "evidence": [],
            },
            "integrity": {"algorithm": "HMAC-SHA256", "key_id": "local-test-only"},
        }
        signed = sign_event(event, bytes.fromhex(issuer["key"]))
        headers = {"Authorization": "Bearer " + issuer["bearer"]}
        receipt = client.post("/events", json=signed, headers=headers)
        assert receipt.status_code == 202 and receipt.json()["status"] == "ACCEPTED"
        assert client.post("/events", json=signed, headers=headers).json()["status"] == "DUPLICATE"
        event["payload"]["outcome"] = "ABSTAINED"
        assert (
            client.post(
                "/events", json=sign_event(event, bytes.fromhex(issuer["key"])), headers=headers
            ).status_code
            == 409
        )
        assert client.post("/events", json=signed).status_code == 401
        checks += [
            "signed_inbox_actual_http",
            "duplicate_receipt",
            "conflicting_delivery",
            "public_session_cannot_send_service_events",
        ]
        failed = start(changes={"api_response_mode": "timeout"})
        assert settled(failed)["status"] == "FAILED"
        record(failed)
        checks.append("failure_terminal_result")
        if restart:
            command("stop", "worker")
            try:
                queued = start()
                command("restart", "db", "api")
            finally:
                command("up", "--no-deps", "-d", "worker")
            for _ in range(50):
                try:
                    if client.get("/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.2)
            assert settled(queued)["status"] == "SUCCEEDED"
            record(queued)
            assert (
                client.post("/events", json=signed, headers=headers).json()["status"] == "DUPLICATE"
            )
            checks += ["database_api_worker_restart_recovery", "durable_inbox_after_restart"]
        report = {
            "executed_at": wire.utc(now()),
            "application_revision": health["source_revision"],
            "contracts_version": "1.0.0",
            "wire_version": "1.0",
            "environment": "test",
            "transport": "actual_http_to_isolated_uvicorn_postgresql_worker",
            "peer_sources": "local_test_issuer_only_no_real_peer_connection",
            "input_schema_sha256": schema["input_schema_sha256"],
            "checks": checks,
            "inputs": inputs,
            "runs": results,
        }
        path = ROOT / "docs/contracts-adoption/http-runtime.json"
        path.write_text(json.dumps(report, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "status": "PASS",
                    "runs": len(results),
                    "checks": checks,
                    "source_revision": health["source_revision"],
                }
            )
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--restart", action="store_true")
    run(restart=parser.parse_args().restart)
