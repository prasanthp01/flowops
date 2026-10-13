"""Stop/restart only this task's local containers and verify a queued run survives."""

import json
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from http_example import API, session, submit, wait

DOCKER = ["docker", "compose", "-f", "compose.yaml", "-f", "compose.qa.yaml"]


def action(*args):
    subprocess.run(DOCKER + list(args), check=True, capture_output=True, timeout=30)


def main():
    client = session()
    try:
        action("stop", "worker")
        run = submit(client)
        assert client.get(API + f"/simulations/{run['run_id']}").json()["status"] == "ACCEPTED"
        action("stop", "db")
        down = client.get(API + "/health").status_code
        assert down >= 500
        action("start", "db")
        for _ in range(40):
            if client.get(API + "/health").status_code == 200:
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("Database did not recover")
        action("start", "worker")
        result = wait(client, run["run_id"])
        assert result["output"]["outcome"] == "SIMULATED_APPLIED"
        evidence = {
            "verified_at": datetime.now(UTC).isoformat(),
            "status": "PASS",
            "queued_run_survived_database_restart": True,
            "worker_resumed_durable_work": True,
            "unavailable_health_status": down,
            "final_outcome": result["output"]["outcome"],
        }
        Path("docs/evidence/restart.json").write_text(json.dumps(evidence, indent=2) + "\n")
        print(json.dumps(evidence, indent=2))
    finally:
        action("start", "db", "worker")
        client.close()


if __name__ == "__main__":
    main()
