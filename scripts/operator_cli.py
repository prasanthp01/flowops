"""Local authenticated approval CLI; requires the exact displayed action digest."""

import argparse
import json
from pathlib import Path

import httpx

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("run_id")
parser.add_argument("--decision", choices=["approve", "reject"])
parser.add_argument("--digest")
args = parser.parse_args()
if args.decision and not args.digest:
    parser.error("--digest is required for an explicit decision")
root = Path(__file__).resolve().parents[1]
token = (root / ".local/secrets/operator_token").read_text().strip()
with httpx.Client(
    base_url="http://127.0.0.1:8765/api/projects/flowops",
    headers={"Authorization": "Bearer " + token},
    timeout=10,
) as client:
    response = client.get(f"/operator/simulations/{args.run_id}")
    response.raise_for_status()
    state = response.json()
    if args.decision:
        approval = state["result"]["output"]["approval"]
        if not approval or approval["action_digest"] != args.digest:
            raise SystemExit("The expected action digest does not match the current proposal")
        response = client.post(
            f"/operator/simulations/{args.run_id}/approval",
            json={
                "request_id": approval["request_id"],
                "action_digest": args.digest,
                "decision": args.decision,
            },
        )
        response.raise_for_status()
        state = response.json()
    print(json.dumps(state, indent=2))
