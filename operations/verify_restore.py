"""Encrypted backup + disposable restore verification for this local Compose project only."""

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCKER = ["docker", "compose", "-f", "compose.yaml", "-f", "compose.qa.yaml"]


def command(args, data=None):
    r = subprocess.run(args, input=data, capture_output=True, cwd=ROOT, timeout=60)
    if r.returncode:
        raise RuntimeError(
            "Backup/restore command failed: " + r.stderr.decode(errors="replace")[-500:]
        )
    return r.stdout


def sql(database, query):
    return (
        command(
            DOCKER
            + [
                "exec",
                "-T",
                "db",
                "psql",
                "-U",
                "postgres",
                "-d",
                database,
                "-At",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                query,
            ]
        )
        .decode()
        .strip()
    )


def main():
    directory = ROOT / ".local/backups"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    key_path = ROOT / ".local/secrets/backup_key"
    if not key_path.exists():
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    key = key_path.read_bytes()
    tag_key = hashlib.sha256(b"flowops-backup-authentication:" + key).digest()
    dump = command(
        DOCKER
        + [
            "exec",
            "-T",
            "db",
            "pg_dump",
            "-U",
            "postgres",
            "-Fc",
            "--no-owner",
            "--no-privileges",
            "flowops",
        ]
    )
    cipher = command(
        [
            "openssl",
            "enc",
            "-aes-256-cbc",
            "-pbkdf2",
            "-iter",
            "200000",
            "-salt",
            "-pass",
            f"file:{key_path}",
        ],
        dump,
    )
    tag = hmac.new(tag_key, cipher, hashlib.sha256).digest()
    backup = directory / ("flowops-" + str(int(time.time())) + ".dump.enc")
    backup.write_bytes(tag + cipher)
    os.chmod(backup, 0o600)
    payload = backup.read_bytes()
    assert hmac.compare_digest(
        payload[:32], hmac.new(tag_key, payload[32:], hashlib.sha256).digest()
    )
    plaintext = command(
        [
            "openssl",
            "enc",
            "-d",
            "-aes-256-cbc",
            "-pbkdf2",
            "-iter",
            "200000",
            "-pass",
            f"file:{key_path}",
        ],
        payload[32:],
    )
    assert hashlib.sha256(plaintext).digest() == hashlib.sha256(dump).digest()
    target = "flowops_restore_" + str(int(time.time()))
    assert target.startswith("flowops_restore_") and target.replace("_", "").isalnum()
    sql("postgres", f"CREATE DATABASE {target}")
    try:
        command(
            DOCKER
            + [
                "exec",
                "-T",
                "db",
                "pg_restore",
                "-U",
                "postgres",
                "--exit-on-error",
                "--no-owner",
                "--no-privileges",
                "-d",
                target,
            ],
            plaintext,
        )
        comparisons = []
        for table, key_name in [
            ("runs", "id"),
            ("events", "id"),
            ("effects", "operation_id"),
            ("approvals", "id"),
            ("approval_records", "id"),
        ]:
            query = f"SELECT count(*), md5(COALESCE(string_agg(row_to_json(t)::text, '' ORDER BY {key_name}),'')) FROM {table} t"
            source, restored = sql("flowops", query), sql(target, query)
            assert source == restored, (
                table,
                "Restore differs; source may have changed during verification",
            )
            comparisons.append(
                {"table": table, "rows": int(source.split("|")[0]), "content_digest_match": True}
            )
        receipt = {
            "verified_at": datetime.now(UTC).isoformat(),
            "status": "PASS",
            "source": "FlowOps local synthetic database",
            "backup_sha256": hashlib.sha256(payload).hexdigest(),
            "encryption": "AES-256-CBC with PBKDF2 200000 iterations, encrypt-then-HMAC-SHA256; owner-only key file",
            "restore": "pg_restore into a newly created disposable database",
            "tables": comparisons,
            "limitations": [
                "Local restore drill, not an offsite disaster-recovery test.",
                "Production key custody/rotation remains an operator responsibility.",
            ],
        }
        (ROOT / "docs/evidence/restore.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(json.dumps(receipt, indent=2))
    finally:
        sql("postgres", f"DROP DATABASE {target}")


if __name__ == "__main__":
    main()
