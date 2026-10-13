# Local operation, backup and rollback

## Ownership and startup

This repository owns only the `flowops-local` Compose project and its named resources. Never run host-wide cleanup or delete another application's containers, networks or volumes. The default Docker context on the development machine is not its running Colima engine; use `docker --context colima` per command here.

```sh
python3 scripts/init_local.py
docker --context colima compose up --build -d
docker --context colima compose ps
curl --fail http://127.0.0.1:8765/api/projects/flowops/health
```

Open `/projects/flowops` on the same origin. `localhost` and `127.0.0.1` are different browser origins: use the configured `FLOWOPS_ORIGIN` consistently. Outside local/test environments an HTTPS origin is mandatory.

For local development tests only:

```sh
docker --context colima compose -f compose.yaml -f compose.qa.yaml up --build -d
uv sync --frozen --python 3.12
uv run python scripts/dev.py .venv/bin/alembic upgrade head
uv run python scripts/dev.py .venv/bin/pytest -q
```

`scripts/dev.py` loads secrets without printing them or putting them in command arguments. Its database is strictly `flowops_test`; the tests assert that scope before clearing fixtures. The runtime's separate `flowops` database and other projects are preserved.

## Run lifecycle and recovery

The worker commits after each phase. If a process dies, an expired lease permits a new claim; the old claim version cannot checkpoint or simulate an action. Steps time out below their lease duration. Up to three attempts use persisted exponential backoff with jitter and honor Retry-After. A lost fake action response is reconciled using a logical operation digest, independent of a worker claim.

An approval pause has no active worker lease and expires after 10 minutes. The browser can cancel it or refresh its status. Inspect and approve/reject using `scripts/operator_cli.py` with the exact proposal digest. Approval does not authorize a real transfer or notification. Rejections, cancellation and expiry produce a terminal audit.

If a database outage kills the worker, Compose restarts it. When the database returns, the durable queue resumes; the 60-second processing deadline still applies. Runs older than the configured hour expire and are removed by worker maintenance. Logs should remain bounded by Docker's configured rotation; configure daemon or service log retention before public deployment.

## Backup and tested restore

```sh
uv run python operations/verify_restore.py
```

This local-only drill uses the named Colima context. It dumps the FlowOps database, encrypts it with AES-256-CBC/PBKDF2 and an encrypt-then-HMAC-SHA256 tag, verifies the tag before decryption, restores into a newly named `flowops_restore_*` database, and compares counts and ordered content digests for runs, events, effects, approval requests and decisions. It removes only that disposable restore database. Encrypted backups and the key remain in ignored, owner-protected `.local` directories. Do not commit or publish them.

Run the drill while no user submissions or retention changes are occurring, so the post-dump source comparison is meaningful. For real deployment, keep a separately protected recovery key, establish rotation, encrypt offsite backups, and test loss of the host—not only a same-host restore.

```sh
uv run python scripts/recovery_drill.py
```

The restart drill briefly stops this project's worker, queues a synthetic run, stops/restarts its database, resumes the worker and checks the final result. It never stops unrelated services. See `docs/evidence/restart.json` and `restore.json` for observed results.

## Rollback

Before a future update, record the current Git revision and image digest, make a verified backup, and test the new revision in an isolated project. For an application-only rollback, stop API/worker, retain the database volume, start the previous compatible image, then verify readiness and a synthetic scenario. Do not run a destructive schema downgrade on retained user data. Restore into a new database/volume and verify before switching if a schema rollback is required.

The initial migration's up/down/up behavior is tested only against the disposable test database. That is not authorization to downgrade a production database. Base Compose deliberately has no reset/reseed command.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Docker socket unavailable | Use the running engine's explicit context; do not change another project's daemon |
| Database port refused in host tests | Add `compose.qa.yaml`; it attaches the loopback QA network and port |
| Migration container exits | Read only this project's migration log; check scoped secret mounts and schema owner |
| 403 Origin / CSRF | Use the exact configured origin and fetch a session first |
| 401 session expired | Reload; browser history is a recorded view, not renewed server authority |
| 429 capacity | Cancel unused pending runs or wait for expiry; do not remove safety caps to pass tests |
| Approval does not resume | Verify exact digest, expiry, environment and operator decision; refresh status |
| Trace ends in failure | Inspect stable error code, retries, deadline and checkpoint; internal exception text is intentionally not returned |

Public release still requires reviewed reverse-proxy routing, host resource checks, offsite/key-custody procedures and independent review.
