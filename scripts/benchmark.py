"""Bounded burst benchmark, not a sustained-capacity certification. Loopback only."""

import json
import platform
import statistics
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from http_example import API, session, submit, wait, wait_ready


def percentile(values, p):
    return sorted(values)[min(len(values) - 1, round((len(values) - 1) * p))]


def main():
    wait_ready()
    clients = [session() for _ in range(50)]
    samples = []
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            result = subprocess.run(
                [
                    "docker",
                    "--context",
                    "colima",
                    "stats",
                    "--no-stream",
                    "--format",
                    "{{json .}}",
                    "flowops-local-api-1",
                    "flowops-local-worker-1",
                    "flowops-local-db-1",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode == 0:
                samples.append(
                    {
                        "at": datetime.now(UTC).isoformat(),
                        "containers": [
                            {k: d[k] for k in ["Name", "CPUPerc", "MemUsage"]}
                            for d in map(json.loads, result.stdout.splitlines())
                        ],
                    }
                )
            stop.wait(1)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    warm = submit(clients[0])
    wait(clients[0], warm["run_id"])
    results = []
    try:
        for count in [1, 10, 25, 50]:
            barrier = threading.Barrier(count)

            def run(index, barrier=barrier):
                barrier.wait()
                started = time.perf_counter()
                try:
                    record = submit(clients[index])
                    accepted = time.perf_counter() - started
                    result = wait(clients[index], record["run_id"])
                    events = (
                        clients[index]
                        .get(API + f"/simulations/{record['run_id']}/events?format=json")
                        .json()["events"]
                    )
                    first = datetime.fromisoformat(events[0]["occurred_at"])
                    created = datetime.fromisoformat(record["created_at"])
                    return {
                        "success": result["output"]["outcome"] == "SIMULATED_APPLIED",
                        "api_ms": accepted * 1000,
                        "workflow_ms": result["duration_ms"],
                        "queue_ms": (first - created).total_seconds() * 1000,
                        "retries": result["output"]["retry_count"],
                    }
                except Exception as exc:
                    return {"success": False, "error_type": type(exc).__name__}

            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=count) as pool:
                rows = list(pool.map(run, range(count)))
            duration = time.perf_counter() - started
            success = [r for r in rows if r["success"]]
            summary = {
                "virtual_users": count,
                "workflows": count,
                "pattern": "one simultaneous workflow per user",
                "duration_seconds": round(duration, 3),
                "succeeded": len(success),
                "failed": count - len(success),
                "throughput_per_second": round(len(success) / duration, 3),
                "raw": rows,
            }
            if success:
                summary.update(
                    {
                        "workflow_p50_ms": round(
                            statistics.median([r["workflow_ms"] for r in success]), 3
                        ),
                        "workflow_p95_ms": round(
                            percentile([r["workflow_ms"] for r in success], 0.95), 3
                        ),
                        "api_p95_ms": round(percentile([r["api_ms"] for r in success], 0.95), 3),
                        "queue_p95_ms": round(
                            percentile([r["queue_ms"] for r in success], 0.95), 3
                        ),
                        "retry_count": sum(r["retries"] for r in success),
                        "external_llm_cost_per_success": 0,
                    }
                )
            results.append(summary)
            print(json.dumps({k: v for k, v in summary.items() if k != "raw"}), flush=True)
    finally:
        stop.set()
        sampler.join(timeout=12)
        for client in clients:
            client.close()
    report = {
        "measured_at": datetime.now(UTC).isoformat(),
        "driver": "Python httpx ThreadPoolExecutor",
        "host_platform": platform.system() + " " + platform.machine(),
        "docker_engine": {"vcpus": 4, "memory_bytes": 6197448704},
        "container_limits": {
            "api": "0.75 CPU / 256 MiB",
            "worker": "0.75 CPU / 256 MiB",
            "db": "0.75 CPU / 384 MiB",
        },
        "warmup": "1 complete workflow",
        "dataset": "eligible_refund / synthetic-1.0",
        "model_mode": "SANDBOX_DETERMINISTIC",
        "limits": [
            "Short burst test; no sustained-capacity or production SLA claim.",
            "Memory/CPU snapshots are sampled, not guaranteed peaks.",
            "One worker; unrelated preexisting containers share the Docker engine.",
            "Includes client polling overhead; no paid inference.",
        ],
        "runs": results,
        "resource_samples": samples,
    }
    Path("docs/evidence/benchmark.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    main()
