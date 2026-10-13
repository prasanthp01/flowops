"""Initialize isolated local adoption secrets. No production or peer credential changes."""

import hashlib
import json
import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
directory = root / ".local/contracts"
directory.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(directory, 0o700)


def save(name, value, readable=True):
    path = directory / name
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as target:
            target.write(value + "\n")
    os.chmod(path, 0o444 if readable else 0o600)
    return path.read_text().strip()


save("admin_password", secrets.token_hex(24))
owner = save("owner_password", secrets.token_hex(24))
runtime = save("runtime_password", secrets.token_hex(24))
save("signing_key", secrets.token_hex(32))
save("operator_token", secrets.token_hex(32))
save("owner_url", f"postgresql+psycopg://flowops_owner:{owner}@db:5432/flowops")
save("runtime_url", f"postgresql+psycopg://flowops_runtime:{runtime}@db:5432/flowops")
issuer = json.loads(
    save(
        "test_issuer",
        json.dumps({"bearer": secrets.token_hex(32), "key": secrets.token_hex(32)}),
        readable=False,
    )
)
save(
    "service_credentials",
    json.dumps(
        [
            {
                "token_sha256": hashlib.sha256(issuer["bearer"].encode()).hexdigest(),
                "application_id": "resolveai",
                "environment": "test",
                "tenant_id": "flowops-sandbox",
                "scopes": ["workflow:read"],
                "keys": {"local-test-only": issuer["key"]},
            }
        ]
    ),
)
print("Isolated local adoption secrets ready; no values printed.")
