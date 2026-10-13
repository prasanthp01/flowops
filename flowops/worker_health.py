"""Liveness probe refreshed only after a successful worker iteration."""

import time
from pathlib import Path

HEARTBEAT = Path("/tmp/flowops-worker-heartbeat")


def mark_ready():
    HEARTBEAT.touch(mode=0o600, exist_ok=True)


def healthy(path=HEARTBEAT, max_age=60):
    try:
        age = time.time() - path.stat().st_mtime
        return 0 <= age < max_age
    except OSError:
        return False


if __name__ == "__main__":
    raise SystemExit(0 if healthy() else 1)
