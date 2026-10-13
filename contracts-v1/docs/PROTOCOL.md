# Simulation and service protocol v1

Contract release: 1.0.0. Wire `schema_version`: `1.0`. Canonical schemas are in `schemas/v1`; the five ZIP schemas were unreleased starter material. This initial release deliberately closes their missing provenance, typing and error fields. It is not wire-compatible with every unmodified starter fixture. Consumers must pin this release and implement an explicit adapter.

## HTTP paths and ownership

The public origin is `https://portfolio.gappazappa.in`. Portfolio owns `/` and `/projects/{slug}`. The edge strips `/api/projects/{slug}` before forwarding to an independently owned service. Internal paths are `/simulation/schema`, `/simulation/presets`, `/simulations`, `/simulations/{run_id}`, `/simulations/{run_id}/events`, `/simulations/{run_id}/result`, and optionally `/simulations/{run_id}/cancel`. Never redirect API errors to the homepage. Full `/projects/{slug}/app/` routes are optional and require separate asset/auth/deep-link validation.

Schema and presets are public metadata. Creating or reading a run requires a short-lived, project-scoped anonymous sandbox session or authorized private session. The project owns session bootstrap. The development mock provides `POST /simulation/session`; this helper is not a mandatory product endpoint. The portable clients use a bearer session token. Cookie sessions require same-origin checks and CSRF protection. Native browser EventSource cannot attach arbitrary bearer headers: use fetch streaming or an appropriately scoped HttpOnly cookie. Do not put credentials in SSE query strings.

POST creation returns HTTP 202 and a `simulation-run`. Do not automatically retry POST without a project-supported idempotency policy. IDs must be unpredictable at runtime. They are not authorization credentials. Foreign and nonexistent run IDs return the same 404. An authorized owner of a retained tombstone receives 410; after tombstone removal use 404.

## Lifecycle

Allowed transitions: ACCEPTED → RUNNING → SUCCEEDED/FAILED/CANCELLED; ACCEPTED may also fail or cancel before starting. A retained terminal run may become EXPIRED after its TTL. A synchronous implementation may return a terminal state directly with HTTP 202. Terminal business outcomes cannot be changed by duplicate cancellation. After expiry, return 410 instead of exposing removed output. A pending result returns typed 409 CONFLICT; never label an in-progress result final. Complete timestamps must be UTC and present only on terminal states. Cancellation is optional: advertise it in product documentation and return 404 if unsupported.

## SSE, polling and reconnection

Sequence starts at 1, increments by exactly one per run, and never resets. There are at most 1,024 steps. Persist or retain the ordered event log for the full advertised retention window. `event_id` is unique within the run. `event_count` and `last_sequence` agree. Validate ownership before returning any bytes, including on reconnection.

`GET .../events` accepts `Accept: text/event-stream`. Each event is UTF-8, with `id: <sequence>`, `event: step`, and one JSON `data:` value conforming to `simulation-event`, followed by a blank line. Comments such as `: heartbeat` are transport-only, carry no sequence, and should arrive at least every 15 seconds during otherwise idle active streams. Disable buffering and caching. A terminal stream sends `event: end` with `data: {"last_sequence":N}`; the client then reads the final result. A broken connection is not a successful run.

Reconnect with `Last-Event-ID` equal to the last accepted sequence. Polling uses `Accept: application/json` and `?after=N`, returning `simulation-events`; `next_sequence` is the last returned sequence (or the unchanged cursor for an empty page), not the next unreceived value. Both transports return strictly greater sequences. If both cursors are present they must agree. Negative, malformed or future cursors return 409 INVALID_CURSOR. Old expired logs return 410 after ownership verification. Responses are capped at 262,144 bytes; paginate before that cap, including in a long-lived SSE connection. Clients deduplicate on `(run_id, sequence)`, reject gaps/mismatched run/project IDs, and fetch missing pages before presenting a complete timeline.

## Payload and mode boundaries

Requests are at most 65,536 bytes; client responses at most 262,144 bytes; maximum JSON depth is 12 (root depth 0). JSON Schema bounds collections and strings, while the portable validators enforce total bytes/depth. `input` and `output` support bounded structured JSON; each app must validate its own narrower input/output schema and publish the input schema plus SHA-256 of compact, sorted-key UTF-8 JSON. Do not resolve untrusted remote schema references. No arbitrary URLs, SQL, shell or provider keys are accepted merely because the envelope validates.

Same input + seed + fixture/rule/workflow versions must yield the same **semantic output and decisions**. Random run/event IDs, timestamps and measured timings are excluded from equality. Significant input edits must affect relevant decisions. Recorded replay cannot accept changed input and silently reuse a previous answer.

Modes are `SANDBOX_DETERMINISTIC`, `REPLAY_RECORDED`, `LIVE_MODEL_AUTHENTICATED`. Provenance maps respectively to SYNTHETIC/MOCK, RECORDED_SYNTHETIC/REPLAY, and AUTHORIZED_PRIVATE/LIVE_READ_ONLY. Private live inference is opt-in and authenticated. `connector_kind` describes diagnostic/data access; the live model adapter is described by model/prompt versions. This v1 envelope does not authorize business write connectors. Simulated usage never becomes billed usage; no sandbox provider calls are allowed. `NONE` means no cost with value zero. ACTUAL indicates observed usage/accounting evidence, not automatically a reconciled invoice. Record the pricing reference and distinguish estimates.

## Service events and authorization

`POST /events` accepts the `cross-project-event` envelope and returns `event-receipt` with ACCEPTED or DUPLICATE. Authenticate the caller independently; do not trust tenant/application/environment claims from the payload. Bind the credential to source, audience, environment, tenant and event scope. The reference receiver maps workflow failures to `incident:ingest`, investigation completion to `workflow:read`, and usage to `usage:write`. Mandatory kinds and payloads are typed separately.

HMAC-SHA256 signs RFC 8785 canonical JSON of the entire envelope with only `integrity.digest` omitted. The key ID stays inside signed content. Resolve the key by authenticated source/environment/tenant and key ID from a trusted allowlist; do not accept a key from the caller's body. Use at least 32 random bytes, constant-time comparison, explicit key rotation/revocation and a ±300-second timestamp window. The signature helper receives an already selected trusted key; it is not a credential store. Retry delivery with a fresh timestamp/signature when necessary, preserving operation identity and logical content.

Deduplicate on source application + environment + tenant + audience + idempotency key. Same key with different logical content returns a conflict. The delivery ID, timestamp and signature may change; kind, payload, trace/correlation and provenance may not. Keep dedup state for the operation's retry/reconciliation horizon; never prune it before producers stop retrying. Commit receipt and domain work atomically in the consumer's database. The local SQLite reference stores receipts only and executes no business actions.

Sender outboxes reserve work durably, bound attempts, use leases and fencing tokens, honor Retry-After and avoid multiplying retries across layers. A stale worker cannot settle a newer claim. Delivery is at least once; uncertain external effects require reconciliation. The reference outbox does not provide a production worker or provider transport.

## Knowledge, inference and handoffs

Knowledge requests are bounded and authorized to collections before retrieval. DENIED and NO_RESULTS contain no answer or citations; denied queries must not reveal document titles, IDs, counts or existence. Authenticate actual user/tenant permissions independently from the requested collection list.

Inference requests carry immutable operation ID, risk/data class, capabilities, region, token/latency/cost limits and policy version. Context references point to already authorized minimal content; raw prompts are not in this routing envelope. Route decisions list selected/rejected deployments, attempts, usage and fallback reason. The schema does not implement routing or reserve budget. The consuming AgentOps/project code must atomically reserve budgets, enforce policy expiry, and revalidate eligibility on every fallback. An AgentOps outage permits local provider use only when every policy and budget invariant remains enforceable; otherwise retain deterministic processing or human review.

Handoff scopes must be a subset of the caller's authenticated grants, with the same authorized tenant/workflow context. `validate_handoff` checks a supplied grant set; callers must also validate ownership, role/task membership, deadline and business approvals. A review agent cannot grant approval or expand scope. Observable summaries are permitted; raw chain-of-thought is not captured.

## Compatibility

v1 wire schemas are frozen. Strict readers reject unknown top-level fields. Documentation-only changes and new optional **schema resources** can be patch/minor releases; payload extension metadata uses the bounded `extensions.x_*` namespace where offered. Extensions cannot alter security, authorization or outcome semantics. Adding a top-level field is not assumed safe for old strict readers; use a new wire major or explicit version negotiation. The compatibility checker conservatively rejects every structural change to a released schema. Preserve published baseline files and review any baseline change against the pinned release commit.

## Reference specifications

- JSON Schema object/closed-property rules: https://json-schema.org/understanding-json-schema/reference/object
- Ajv draft 2020-12 support: https://ajv.js.org/json-schema.html
- OpenAPI 3.1.1: https://spec.openapis.org/oas/v3.1.1.html
