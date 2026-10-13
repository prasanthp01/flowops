import os
import time

from flowops.worker_health import healthy


def test_worker_probe_rejects_missing_or_stale_heartbeat(tmp_path):
    path = tmp_path / "heartbeat"
    assert not healthy(path)
    path.touch()
    assert healthy(path)
    stale = time.time() - 120
    os.utime(path, (stale, stale))
    assert not healthy(path)
