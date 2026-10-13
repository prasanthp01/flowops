# ADR 001: small durable graph, synthetic action boundary

Status: accepted for the local MVP, 2026-10-13.

Use an explicit PostgreSQL-backed state machine instead of a graph framework. Eight fixed phases, short transactions, leases, fencing, checkpoint replay and typed specialist outputs meet this bounded workflow without a framework's additional runtime and recovery semantics. The run row is the atomic job marker, so no separate broker is required. Parallel research is bounded to three fake HTTP requests plus policy retrieval.

Use plain HTML/CSS/JavaScript for the case study and simulator. The UI is small, framework-independent and can be adapted into a portfolio island without duplicating the backend. The local root is a minimal preview index, not a replacement for the shared portfolio. The real integration session owns the canonical domain and edge.

Use integer minor units rather than floating money values. Synthetic policies are deliberately concrete and versioned. Treat client-supplied approval claims as untrusted inputs, and bind operator decisions to an immutable request digest. Keep approval wait as a business state within the shared `RUNNING` lifecycle; no worker stays leased during that wait.

Keep the supplied schemas unmodified in the brief. Export stronger project schemas separately; use their approved nested `output`/`technical` objects for extended provenance and decision details. Do not falsely claim the shared model request schema specifies an actual AgentOps response or content envelope.

Consequences: no live-model planning, real ERP connector, monetary mutation, customer messaging, external webhook delivery, SSO or multi-tenant claim. Those require separate integration and authorization evidence. A larger workflow graph or longer external calls would require revisiting lease renewal, scheduling and orchestration tooling.
