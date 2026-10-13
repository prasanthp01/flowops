"""Owned, disposable local real-peer rehearsal; never uses production or model providers.

Uses pinned source archives and fresh private credentials. Stops only processes and
container created by this invocation; the owned PostgreSQL volume is retained.
"""

import argparse
import hashlib
import io
import json
import os
import secrets
import subprocess
import tarfile
import time
import uuid
from pathlib import Path

import httpx
import psycopg
from psycopg import sql

from flowops import wire
from flowops.db import now

ROOT = Path(__file__).resolve().parents[1]
KO_REVISION = "172a6c9e47de361a6d484f98b52c0cc5752727ff"
AO_REVISION = "50bb507dafdd2c98d8d008c7307a9235439690dd"
PG_IMAGE = (
    "postgres:18-alpine@sha256:77f585114c32fbca283dc835b0596f4e52b51b4c6662d7810b2f4084f60a1873"
)
DOCKER = ["docker", "--context", "colima"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--knowledge-source", type=Path, required=True)
    parser.add_argument("--agent-source", type=Path, required=True)
    args = parser.parse_args()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    subprocess.run(
        ["git", "diff", "--exit-code", "HEAD", "--", "flowops"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    stamp = now().strftime("%Y%m%dT%H%M%SZ")
    directory = ROOT.parents[1] / "work" / ("peer-profile-" + stamp)
    directory.mkdir(mode=0o700, parents=True)
    private = directory / "private"
    private.mkdir(mode=0o700)
    children = []
    owned_container = None
    name = "flowops-peer-" + stamp.lower()
    volume = name + "-db"
    report = {
        "checked_at": wire.utc(now()),
        "source_revisions": {
            "flowops": revision,
            "knowledgeops": KO_REVISION,
            "agentops": AO_REVISION,
        },
        "scope": "REAL_PRIVATE_HTTP_FROM_PERSISTED_OPT_IN_WORKFLOW",
        "production_modified": False,
        "paid_calls_enabled": False,
        "checks": [],
        "retained_volume": volume,
    }

    def secret_file(filename, value):
        path = private / filename
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
        return path

    def snapshot(source, revision, slug):
        data = subprocess.check_output(["git", "archive", revision], cwd=source)
        target = directory / slug
        target.mkdir()
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            archive.extractall(target, filter="data")
        return target

    def launch(label, python, cwd, command, env):
        log = secret_file(label + ".log", "").open("a")
        child = subprocess.Popen(
            [str(python), *command],
            cwd=cwd,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **env},
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        log.close()
        children.append(child)
        return child

    def finish(child):
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)

    def ready(url):
        for _ in range(80):
            try:
                response = httpx.get(url, timeout=1)
                if response.status_code == 200:
                    return response.json()
            except httpx.HTTPError:
                pass
            time.sleep(0.2)
        raise RuntimeError("Owned private service failed readiness; inspect private logs")

    try:
        ko = snapshot(args.knowledge_source, KO_REVISION, "knowledgeops")
        ao = snapshot(args.agent_source, AO_REVISION, "agentops")
        admin, ko_owner, ko_app, fo_owner, fo_app = [secrets.token_hex(24) for _ in range(5)]
        envfile = secret_file("postgres.env", "POSTGRES_PASSWORD=" + admin + "\n")
        owned_container = subprocess.check_output(
            [
                *DOCKER,
                "run",
                "-d",
                "--name",
                name,
                "--label",
                "flowops.rehearsal=peer-profile",
                "--memory",
                "512m",
                "--cpus",
                "1",
                "--pids-limit",
                "128",
                "-v",
                volume + ":/var/lib/postgresql",
                "-p",
                "127.0.0.1:18647:5432",
                "--env-file",
                str(envfile),
                PG_IMAGE,
            ],
            text=True,
        ).strip()
        connection = None
        for _ in range(80):
            try:
                connection = psycopg.connect(
                    host="127.0.0.1",
                    port=18647,
                    user="postgres",
                    password=admin,
                    dbname="postgres",
                    autocommit=True,
                    connect_timeout=1,
                )
                break
            except psycopg.OperationalError:
                time.sleep(0.25)
        if not connection:
            raise RuntimeError("Owned PostgreSQL startup failed")
        with connection:
            for role, password in [
                ("knowledgeops_owner", ko_owner),
                ("knowledgeops_app", ko_app),
                ("flowops_owner", fo_owner),
                ("flowops_runtime", fo_app),
            ]:
                connection.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}"
                    ).format(sql.Identifier(role), sql.Literal(password))
                )
            for database, owner in [
                ("knowledgeops", "knowledgeops_owner"),
                ("flowops", "flowops_owner"),
            ]:
                connection.execute(
                    sql.SQL("CREATE DATABASE {} OWNER {}").format(
                        sql.Identifier(database), sql.Identifier(owner)
                    )
                )
        ko_python = args.knowledge_source / ".venv/bin/python"
        ao_python = args.agent_source / ".venv/bin/python"
        ko_env = {
            "KO_DATABASE_URL": f"postgresql://knowledgeops_owner:{ko_owner}@127.0.0.1:18647/knowledgeops",
            "KO_PUBLIC_ORIGIN": "http://127.0.0.1:18648",
            "KO_SECURE_COOKIES": "false",
            "KO_CONTRACTS_SOURCE_REVISION": KO_REVISION,
            "KO_CONTRACTS_ENVIRONMENT": "development",
        }
        migration = launch(
            "knowledge-migration", ko_python, ko, ["scripts/migrate_and_seed.py"], ko_env
        )
        assert migration.wait(timeout=60) == 0, "Knowledge migration failed; inspect private log"
        subprocess.run(
            [
                str(ko_python),
                "scripts/configure_contracts_credentials.py",
                "--directory",
                str(private / "knowledge"),
            ],
            cwd=ko,
            check=True,
            capture_output=True,
        )
        ko_token = json.loads((private / "knowledge/probe-tokens.json").read_text())[
            "flowops_staff"
        ]
        ko_env.update(
            KO_DATABASE_URL=f"postgresql://knowledgeops_app:{ko_app}@127.0.0.1:18647/knowledgeops",
            KO_KNOWLEDGE_CREDENTIALS_FILE=str(private / "knowledge/server.json"),
        )
        launch(
            "knowledge-api",
            ko_python,
            ko,
            [
                "-m",
                "uvicorn",
                "knowledgeops.contracts_api:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                "18648",
                "--no-access-log",
            ],
            ko_env,
        )
        ao_token = secrets.token_urlsafe(32)
        ao_config = secret_file(
            "agent-server.json",
            json.dumps(
                {
                    "global_budget_microusd": 0,
                    "credentials": [
                        {
                            "application_id": "flowops",
                            "environment": "development",
                            "tenant_id": "demo",
                            "audience": "agentops",
                            "token_sha256": hashlib.sha256(ao_token.encode()).hexdigest(),
                            "scopes": ["inference:request", "usage:read"],
                            "budget_microusd": 0,
                        }
                    ],
                }
            ),
        )
        launch(
            "agent-api",
            ao_python,
            ao,
            [
                "-m",
                "uvicorn",
                "agentops.canonical_app:create_prefixed_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                "18649",
                "--no-access-log",
                "--no-proxy-headers",
            ],
            {
                "AGENTOPS_DB": str(private / "agentops.db"),
                "AGENTOPS_CONTRACT_CONFIG": str(ao_config),
                "AGENTOPS_SOURCE_REVISION": AO_REVISION,
            },
        )
        ready("http://127.0.0.1:18648/api/projects/knowledgeops/health")
        ready("http://127.0.0.1:18649/healthz")
        profile = {
            "profile_id": "peer-rehearsal",
            "environment": "development",
            "tenant_id": "demo",
            "synthetic_only": True,
            "knowledge": {
                "base_url": "http://127.0.0.1:18648/api/projects/knowledgeops",
                "bearer": ko_token,
                "allow_local_test": True,
                "collection_ids": ["operations"],
                "query": "answer citation original source passage document version",
            },
            "inference": {
                "base_url": "http://127.0.0.1:18649/api/projects/agentops",
                "bearer": ao_token,
                "allow_local_test": True,
            },
        }
        profile_path = secret_file("flowops-profiles.json", json.dumps([profile]))
        operator = secrets.token_hex(32)
        fo_env = {
            "FLOWOPS_DATABASE_URL": f"postgresql+psycopg://flowops_owner:{fo_owner}@127.0.0.1:18647/flowops",
            "FLOWOPS_SIGNING_KEY": secrets.token_hex(32),
            "FLOWOPS_OPERATOR_TOKEN": operator,
            "FLOWOPS_ENVIRONMENT": "local",
            "FLOWOPS_ORIGIN": "http://127.0.0.1:18665",
            "FLOWOPS_SOURCE_REVISION": revision,
            "FLOWOPS_CONTRACTS_TENANT_ID": "demo",
            "FLOWOPS_PEER_PROFILES_FILE": str(profile_path),
        }
        python = ROOT / ".venv/bin/python"
        migration = launch(
            "flowops-migration", python, ROOT, ["-m", "alembic", "upgrade", "head"], fo_env
        )
        assert migration.wait(timeout=30) == 0
        grants = launch("flowops-grants", python, ROOT, ["-m", "scripts.grants"], fo_env)
        assert grants.wait(timeout=30) == 0
        fo_env["FLOWOPS_DATABASE_URL"] = (
            f"postgresql+psycopg://flowops_runtime:{fo_app}@127.0.0.1:18647/flowops"
        )
        launch(
            "flowops-api",
            python,
            ROOT,
            [
                "-m",
                "uvicorn",
                "flowops.canonical_api:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                "18665",
                "--no-access-log",
            ],
            fo_env,
        )
        health = ready("http://127.0.0.1:18665/health")
        assert health["source_revision"] == revision
        origin = "http://127.0.0.1:18665"
        with httpx.Client(
            base_url=origin + "/api/projects/flowops", headers={"Origin": origin}, timeout=12
        ) as client:
            token = (
                client.post("/simulation/session", json={})
                .raise_for_status()
                .json()["session_token"]
            )
            client.headers["Authorization"] = "Bearer " + token
            preset = client.get("/simulation/presets").raise_for_status().json()["presets"][0]
            body = {
                "schema_version": "1.0",
                "project_id": "flowops",
                "mode": wire.MODE,
                "seed": 7,
                "client_correlation_id": "real-peer-profile-1",
                "input": preset["input"],
                "extensions": {"x_peer_profile": "peer-rehearsal"},
            }
            assert (
                client.post(
                    "/simulations", json=body, headers={"Idempotency-Key": str(uuid.uuid4())}
                ).status_code
                == 403
            )
            created = client.post(
                "/simulations",
                json=body,
                headers={
                    "Idempotency-Key": str(uuid.uuid4()),
                    "X-FlowOps-Operator-Token": operator,
                },
            )
            assert created.status_code == 202, created.text
            run_id = created.json()["run_id"]
            base = "/simulations/" + run_id
            worker_env = {**fo_env, "FLOWOPS_OPERATOR_TOKEN": ""}
            worker = launch("flowops-worker", python, ROOT, ["-m", "flowops.workflow"], worker_env)

            def settle():
                for _ in range(200):
                    state = client.get(base).raise_for_status().json()
                    if (
                        state["status"] in wire.TERMINAL
                        or state["extensions"]["x_business_outcome"] == "APPROVAL_PENDING"
                    ):
                        return state
                    time.sleep(0.1)
                raise RuntimeError("Persisted workflow did not settle")

            state = settle()
            assert state["extensions"]["x_business_outcome"] == "APPROVAL_PENDING", state
            peers = client.get(base + "/peers").raise_for_status().json()
            assert peers["inference"]["route"]["outcome"] == "POLICY_BLOCKED", peers["inference"]
            assert peers["inference"]["error"]["code"] == "POLICY_BLOCKED"
            wire.validated("route-decision", peers["inference"]["route"])
            knowledge = peers["knowledge"]["response"]
            assert knowledge["status"] == "OK", peers["knowledge"]
            assert any(c["source_id"] == "search-guide" for c in knowledge["citations"])
            for citation in knowledge["citations"]:
                assert (
                    hashlib.sha256(citation["passage"].encode()).hexdigest()
                    == citation["evidence"]["sha256"]
                )
            finish(worker)
            worker = launch(
                "flowops-worker-resumed", python, ROOT, ["-m", "flowops.workflow"], worker_env
            )
            assert client.get(base + "/peers").json() == peers
            approval = client.get(base + "/proposal").raise_for_status().json()["approval"]
            decision = {k: approval[k] for k in ("request_id", "action_digest")}
            decision["decision"] = "approve"
            assert (
                client.post(
                    "/operator" + base + "/approval",
                    json=decision,
                    headers={"Authorization": "Bearer " + operator},
                ).status_code
                == 200
            )
            assert settle()["status"] == "SUCCEEDED"
            result = client.get(base + "/result").raise_for_status().json()
            wire.validated("simulation-result", result)
            assert result["extensions"]["x_peer_inference_outcome"] == "POLICY_BLOCKED"
            assert result["extensions"]["x_peer_knowledge_status"] == "OK"
            assert result["usage"]["provider_calls"] == 0 and result["cost"]["kind"] == "NONE"
            report.update(
                input=body,
                run_id=run_id,
                peer_results=peers,
                result=result,
                events=client.get(base + "/events").raise_for_status().json(),
            )
            report["checks"] = [
                "public_opt_in_denied",
                "operator_opt_in_persisted",
                "real_worker_to_knowledgeops_positive_fixture",
                "knowledge_citation_hashes",
                "real_worker_to_agentops_policy_blocked",
                "original_constraints_preserved",
                "typed_deterministic_fallback",
                "mandatory_operator_review",
                "worker_restart_checkpoint_preserved",
                "completed_synthetic_workflow",
                "zero_provider_usage",
                "bounded_readable_peer_error",
            ]
        # Read local synthetic AgentOps bookkeeping; policy rejection must not admit paid/provider work.
        import sqlite3

        with sqlite3.connect(private / "agentops.db") as db:
            names = [
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            ]
            report["agentops_tables"] = {
                name: db.execute('SELECT count(*) FROM "' + name + '"').fetchone()[0]
                for name in names
            }
        report["status"] = "PASS"
    finally:
        for child in reversed(children):
            finish(child)
        if owned_container:
            subprocess.run(
                [*DOCKER, "stop", owned_container], check=True, capture_output=True, timeout=30
            )
        report["cleanup"] = (
            "Owned service processes and database container stopped; private logs and owned database volume retained. Existing port 8766 and peer-owner resources untouched."
        )
        target = ROOT / "docs/contracts-adoption/peer-profile-runtime.json"
        target.write_text(json.dumps(report, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "source_revisions": report["source_revisions"],
                "checks": report["checks"],
                "cleanup": report["cleanup"],
            }
        )
    )


if __name__ == "__main__":
    main()
