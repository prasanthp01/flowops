"""Verify actual loopback API/worker/DB and operator approval with synthetic data."""

import json
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from http_example import API, session, submit, wait, wait_ready


def main():
    wait_ready()
    rows = []
    with closing(session()) as client:
        presets = client.get(API + "/simulation/presets").json()["presets"]
        expected = [
            "SIMULATED_APPLIED",
            "APPROVAL_PENDING",
            "SIMULATED_APPLIED",
            "SIMULATED_APPLIED",
            "BLOCKED",
        ]
        for preset, outcome in zip(presets, expected, strict=True):
            run = submit(client, preset["input"], preset["id"])
            result = wait(client, run["run_id"])
            assert result["output"]["outcome"] == outcome, result
            events = client.get(API + f"/simulations/{run['run_id']}/events?format=json").json()[
                "events"
            ]
            assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
            rows.append(
                {
                    "scenario": preset["id"],
                    "outcome": outcome,
                    "events": len(events),
                    "duration_ms": result["duration_ms"],
                    "pass": True,
                }
            )
            if outcome == "APPROVAL_PENDING":
                approval = result["output"]["approval"]
                token = Path(".local/secrets/operator_token").read_text().strip()
                r = client.post(
                    API + f"/operator/simulations/{run['run_id']}/approval",
                    headers={"Authorization": "Bearer " + token},
                    json={
                        "request_id": approval["request_id"],
                        "action_digest": approval["action_digest"],
                        "decision": "approve",
                    },
                )
                r.raise_for_status()
                assert wait(client, run["run_id"])["output"]["outcome"] == "SIMULATED_APPLIED"
        lost = wait(client, submit(client, {"api_response_mode": "lost_response"})["run_id"])
        assert lost["output"]["receipt"]["reconciled"]
        assert lost["output"]["retry_count"] == 1
    evidence = {
        "verified_at": datetime.now(UTC).isoformat(),
        "target": "local Docker Compose",
        "scenarios": rows,
        "operator_approval_resume": "PASS",
        "lost_response_reconciliation": "PASS",
        "live_model": "NOT_TESTED",
        "public_deployment": "NOT_TESTED",
    }
    Path("docs/evidence/http-smoke.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(json.dumps(evidence, indent=2))


if __name__ == "__main__":
    main()
