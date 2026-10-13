"""Load ignored secrets and execute a local command without putting secrets in argv."""

import os
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
secrets_dir = root / ".local/secrets"
env = dict(os.environ)
env.update(
    {
        "FLOWOPS_DATABASE_URL": (secrets_dir / "host_test_url").read_text().strip(),
        "FLOWOPS_SIGNING_KEY": (secrets_dir / "signing_key").read_text().strip(),
        "FLOWOPS_OPERATOR_TOKEN": (secrets_dir / "operator_token").read_text().strip(),
        "FLOWOPS_ENVIRONMENT": "test",
    }
)
raise SystemExit(subprocess.call(sys.argv[1:], env=env, cwd=root))
