"""Minimal keyless HTTP client. Defaults strictly to the local sandbox."""

import json
import time
import uuid
from contextlib import closing

import httpx

BASE = "http://127.0.0.1:8765"
API = "/api/projects/flowops"


def wait_ready(timeout=30):
    deadline = time.monotonic() + timeout
    with httpx.Client(base_url=BASE, timeout=2) as client:
        while time.monotonic() < deadline:
            try:
                if client.get(API + "/health").status_code == 200:
                    return
            except httpx.TransportError:
                pass
            time.sleep(0.25)
    raise TimeoutError("Local FlowOps API did not become ready")


def session():
    client = httpx.Client(base_url=BASE, headers={"Origin": BASE}, timeout=15)
    response = client.post(API + "/session", json={})
    response.raise_for_status()
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
    return client


def submit(client, inputs=None, preset_id="eligible_refund"):
    response = client.post(
        API + "/simulations",
        json={
            "schema_version": "1.0",
            "project_id": "flowops",
            "mode": "SANDBOX_DETERMINISTIC",
            "preset_id": preset_id,
            "input": inputs or {},
        },
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    response.raise_for_status()
    return response.json()


def wait(client, run_id, timeout=70):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(API + f"/simulations/{run_id}")
        response.raise_for_status()
        run = response.json()
        if (
            run["status"] in {"SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"}
            or run["outcome"] == "APPROVAL_PENDING"
        ):
            result = client.get(API + f"/simulations/{run_id}/result")
            result.raise_for_status()
            return result.json()
        time.sleep(0.15)
    raise TimeoutError("Local workflow did not settle within the client deadline")


if __name__ == "__main__":
    wait_ready()
    with closing(session()) as client:
        run = submit(client)
        print(json.dumps(wait(client, run["run_id"]), indent=2))
