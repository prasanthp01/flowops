# Portfolio and external integration

Live page: https://portfolio.gappazappa.in/projects/flowops. API prefix: `/api/projects/flowops/*`. Public HTTPS and browser verification passed on 13 October 2026; see deployment status (historical receipt; not distributed).

The independent FlowOps service serves its case study, simulator and assets through three scoped Nginx locations. The existing portfolio root and the other three project pages are preserved. Sidebar links point to their verified public routes. Local `/` remains a small preview index.

## Deployed proxy behavior

`operations/nginx-flowops.locations.conf` preserves request paths, uses Docker DNS, disables stream buffering and caching, and clears arbitrary forwarded identity headers. The production origin is exactly `https://portfolio.gappazappa.in`; cookies are Secure, HttpOnly, SameSite=Strict and scoped to `/api/projects/flowops`. The API alone joins the existing `agentops_edge` proxy network; database and worker remain private.

Only `/projects/flowops`, `/projects/flowops/*`, and `/api/projects/flowops/*` are owned by this release. Root, ResolveAI legacy routes and other project prefixes remain under their existing owners. The shared proxy network and current Compose configuration must be preserved during later shared-host releases. See operations and rollback (historical receipt; not distributed).

## AgentOps and enterprise connectors

The local sandbox remains useful without AgentOps. `runtime.py` contains the replaceable model contract, compatible request metadata and deterministic/mock degradation. A live implementation still needs an agreed request-content envelope, response schema, authenticated endpoint/audience, approved models/regions, pricing version, budget reservations and usage reconciliation. Public live mode remains rejected until those controls are implemented and tested. There is no fallback directly to a paid provider when AgentOps is down.

Private CRM/ERP adapters and outbound webhooks are not implemented. They require operator-owned configuration, verified destinations and DNS/redirect policy, scoped credentials, output validation/redaction, approval binding, target-side idempotency/reconciliation and retry delivery history. Do not treat the mock HTTP transport as a production connector.
