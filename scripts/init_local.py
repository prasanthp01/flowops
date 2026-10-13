"""Create ignored local-only secrets once. Never overwrite existing state."""

import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parents[1]
directory = root / ".local" / "secrets"
directory.mkdir(parents=True, exist_ok=True, mode=0o700)
os.chmod(directory, 0o700)


def save(name, value):
    path = directory / name
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(value + "\n")
    # Parent directory is owner-only. Compose mounts only the selected file into
    # each container; readable mode permits its non-root process to read that mount.
    os.chmod(path, 0o444)
    return path.read_text().strip()


admin = save("admin_password", secrets.token_hex(24))
owner = save("owner_password", secrets.token_hex(24))
runtime = save("runtime_password", secrets.token_hex(24))
save("signing_key", secrets.token_hex(32))
save("operator_token", secrets.token_hex(32))
save("owner_url", f"postgresql+psycopg://flowops_owner:{owner}@db:5432/flowops")
save("runtime_url", f"postgresql+psycopg://flowops_runtime:{runtime}@db:5432/flowops")
save("host_test_url", f"postgresql+psycopg://flowops_owner:{owner}@127.0.0.1:55439/flowops_test")
save("host_runtime_url", f"postgresql+psycopg://flowops_runtime:{runtime}@127.0.0.1:55439/flowops")
print("Local secrets are ready in .local/secrets (ignored by Git; values not printed).")
