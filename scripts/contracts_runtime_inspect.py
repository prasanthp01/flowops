"""Save selected non-secret health/permissions evidence for the isolated local adapter."""

import json
import subprocess
from pathlib import Path

import httpx

from flowops.db import now
from flowops.wire import utc


def capture(args):
    return subprocess.check_output(["docker", *args], text=True).strip()


def run():
    image = json.loads(
        capture(["image", "inspect", "flowops:0.2.0-contracts", "--format", "{{json .}}"])
    )
    containers = []
    for name in ["api", "worker", "db", "migrate"]:
        row = json.loads(
            capture(["inspect", "flowops-contracts-" + name + "-1", "--format", "{{json .}}"])
        )
        containers.append(
            {
                "service": name,
                "state": row["State"]["Status"],
                "exit_code": row["State"]["ExitCode"],
                "health": row["State"].get("Health", {}).get("Status"),
                "restart_count": row["RestartCount"],
                "user": row["Config"]["User"],
                "read_only": row["HostConfig"]["ReadonlyRootfs"],
                "ports": row["NetworkSettings"]["Ports"],
                "image": row["Image"],
            }
        )
    query = "SELECT json_build_object('revision',(SELECT version_num FROM alembic_version),'runs',(SELECT count(*) FROM runs),'inbox',(SELECT count(*) FROM service_inbox),'outbox',(SELECT count(*) FROM service_outbox),'pending_outbox',(SELECT count(*) FROM service_outbox WHERE status='PENDING'),'inbox_update',has_table_privilege('flowops_runtime','service_inbox','UPDATE'),'inbox_delete',has_table_privilege('flowops_runtime','service_inbox','DELETE'),'outbox_delete',has_table_privilege('flowops_runtime','service_outbox','DELETE'))"
    database = json.loads(
        capture(
            [
                "compose",
                "-f",
                "compose.contracts.yaml",
                "exec",
                "-T",
                "db",
                "psql",
                "-U",
                "postgres",
                "-d",
                "flowops",
                "-At",
                "-c",
                query,
            ]
        )
    )
    assert database["revision"] == "0002" and database["inbox"] >= 1 and database["outbox"] >= 1
    assert not any(database[x] for x in ("inbox_update", "inbox_delete", "outbox_delete"))
    with httpx.Client(timeout=15) as client:
        local = client.get("http://127.0.0.1:8766/health").raise_for_status().json()
        assert client.get("http://127.0.0.1:8766/api/projects/flowops/health").json() == local
        public = (
            client.get("https://portfolio.gappazappa.in/api/projects/flowops/health")
            .raise_for_status()
            .json()
        )
        assert public["version"] == "0.1.0", public
    record = {
        "checked_at": utc(now()),
        "application_revision": local["source_revision"],
        "image_id": image["Id"],
        "architecture": image["Architecture"],
        "os": image["Os"],
        "containers": containers,
        "database": database,
        "public_legacy_health": public,
        "public_changed_by_contracts_work": False,
    }
    path = Path(__file__).resolve().parents[1] / "docs/contracts-adoption/runtime-inspection.json"
    path.write_text(json.dumps(record, indent=2) + "\n")
    print(
        json.dumps(
            {
                "local_image_id": image["Id"],
                "local_health": local,
                "database": database,
                "public": public,
            }
        )
    )


if __name__ == "__main__":
    run()
