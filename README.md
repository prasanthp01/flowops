# FlowOps

An auditable order-resolution workflow. A supervisor coordinates research, policy, operations and risk review; deterministic checks control approvals and simulated actions.

Version **0.2.0**. The default application is a self-hostable deterministic demonstration with synthetic data. It executes real application code, makes no paid model calls and changes no customer systems. [Try the public demo](https://portfolio.gappazappa.in/projects/flowops).

## Run locally

Install Docker Engine with Compose v2. Also install Python 3.12+ for the configuration helper. Use the Docker context that runs your engine; do not change another application's context or configuration.

```sh
git clone https://github.com/prasanthp01/flowops.git
cd flowops
python3 scripts/init_local.py
docker compose up --build -d --wait
```

Open **http://127.0.0.1:8765/projects/flowops**. The default stack publishes only a loopback port. Generated credentials are ignored by Git; keep them private. Configuration helpers preserve existing files. Stop with `docker compose stop`; retain volumes and secrets to resume later. See [troubleshooting](docs/TROUBLESHOOTING.md).

![Project demonstration](docs/images/demo.png)

## Try it

Choose **Eligible refund**: an 80.00 request on a 120.00 delivered order returns `SIMULATED_APPLIED`. Choose **ERP recovery** to see two failed connector attempts followed by one simulated receipt. **Approval required** pauses until an operator approves the exact proposal digest.

Change inputs, run again, inspect chronological server events, switch to the technical view and compare results. Recorded results are labelled separately from a new execution.

## How it works

FastAPI validates bounded inputs. PostgreSQL stores jobs, checkpoints, approvals and an ordered event log. A separate worker claims leased jobs and applies fenced writes. Specialist handoffs carry typed evidence; the authorization gate checks identity, money limits, policy revision and approval bindings before the fake action gateway records an idempotent receipt.

- [Architecture and tradeoffs](architecture.md)
- [API, request formats and examples](docs/API.md)
- [Operations, backup and recovery](operations/runbook.md)
- [Security boundaries and reporting](SECURITY.md)

## Scope and production deployment

The shipped connectors and actions are synthetic. No real refund, customer notification, enterprise connector or live-model call is enabled. The optional Contracts v1 entrypoint and peer clients are separate from the default browser API; see [API documentation](docs/API.md).

For an internet-facing deployment, follow [deployment requirements](docs/DEPLOYMENT_GUIDE.md): configured HTTPS origin, secure cookies, narrow ingress, isolated storage, resource limits, tested backups and monitored health. The default local command is not an internet-facing deployment. Production readiness applies to the documented synthetic scope; live inference and enterprise workloads require separate validation.

## Verify and contribute

Python verification uses Python 3.12 and `uv`. The lockfile pins the resolved dependencies.

```sh
uv sync --frozen --python 3.12
docker compose -f compose.yaml -f compose.qa.yaml up --build -d --wait
uv run python scripts/dev.py .venv/bin/alembic upgrade head
uv run python scripts/dev.py .venv/bin/pytest -q
uv run ruff check flowops tests scripts operations migrations
uv run mypy flowops
```

Database tests require disposable databases and refuse ordinary application databases. Never point tests, load tools or recovery drills at a live deployment. The [release report](docs/RELEASE_READINESS.md) records current test results and remaining limits. Hosted CI repeats the relevant checks on pushes and pull requests. See [CONTRIBUTING.md](CONTRIBUTING.md) for the test environment and review process.

## Performance and cost

Use `uv run python scripts/benchmark.py` in an isolated environment and record the revision, hardware, concurrency and duration. Synthetic provider costs and modeled latency are not invoices or live-provider measurements. Keyless runs have zero external model spend; hosting and CPU costs remain separate. No commercial-scale capacity or independent penetration-test claim is made.

## License

MIT for the original implementation. Bundled dependencies and assets retain their own licenses; see [third-party notices](NOTICE.md). No personal CV, portrait, deployment credentials or private customer data is included.
