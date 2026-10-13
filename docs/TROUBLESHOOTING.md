# Troubleshooting

- **Docker cannot connect:** select your running engine with `DOCKER_CONTEXT` or `docker --context NAME`; do not change the machine-wide default. Check `docker info`.
- **Port already allocated:** stop only an old instance you own, or change the loopback host-port mapping and the matching public origin together.
- **Configuration already exists:** this protects local credentials. Reuse the existing configuration; do not overwrite it to fix an unrelated error.
- **403 or session failure:** use the documented `127.0.0.1` URL, not `localhost`. The browser origin, configured origin and proxy host must match. HTTPS requires secure cookies. Do not disable CSRF or host checks.
- **No result:** inspect `docker compose ps` and bounded service logs. Check database and worker readiness before submitting another run. Do not paste full environment files or database URLs into issues.
- **Schema mismatch:** use one release's application, worker and migrations together. Back up first, run migrations once, then start workers. Never roll back a database by deleting its volume.
- **No permitted evidence or policy refusal:** inspect the trace; these are expected outcomes for restricted access, missing evidence, an ineligible model or an exhausted budget.
- **Updating:** back up data and configuration, pull a reviewed revision, build immutable images, migrate once if needed, verify health and a synthetic run, and retain the previous image for rollback. See [operations](../operations/runbook.md).
