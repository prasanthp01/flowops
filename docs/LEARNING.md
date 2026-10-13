# Reverse-engineering course and demo storyboard

## Study path

1. **Input and trust boundary:** read `contracts.py`, then `security.py` and API admission. Trace a malformed amount and see why its rejected value never appears in the response.
2. **Durable state:** follow `Run`, `Event` and the admission guard in `db.py`. Explain why committing one run row also commits the work marker.
3. **Supervisor to specialists:** trace `Workflow.compute` through declared planning, parallel research/policy, operations and verifier. Inspect the exported handoff/output schemas and the backend events in the UI.
4. **Authorization:** follow `decide`, immutable `Approval`, append-only `ApprovalRecord`, and `apply_effect`. Change one field in an approval digest and run its regression test.
5. **Failure and replay:** select `503_twice`, `timeout` and `lost_response`. Compare durable backoff, dead-letter escalation and receipt reconciliation. Study why a claim version is not an operation identity.
6. **Process recovery:** run the crash/restart tests and the isolated recovery drill. Observe which writes are fenced after a lease expires.
7. **Evidence and UI:** trace one persisted event through SSE, polling deduplication, the inspector and recorded comparison. Explain why changed form values do not mutate an earlier run.
8. **Release assessment:** read the test, benchmark and restore receipts. Separate measured local behavior from public deployment, real inference and production capacity.

Exercise: introduce a third synthetic policy revision through a new versioned policy record. Add boundary cases to the external evaluation dataset, export its schema, verify meaningful route/outcome changes, and compare runs. Do not add a live connector or weaken approval controls as part of this exercise.

## 90-second video storyboard — not recorded

- 0–12s: show the actual case-study hero and explain the order-escalation problem.
- 12–28s: run an eligible refund, inspect research evidence and the fake receipt. Keep the synthetic-mode badge visible.
- 28–43s: change the amount above the threshold; show the approval pause and exact proposal binding. State that an anonymous visitor cannot approve it.
- 43–57s: demonstrate stock shortage and its alternative/hold proposal.
- 57–72s: run the ERP outage; inspect retries and the recovered result.
- 72–83s: run the mismatched-ID scenario and show the deterministic block.
- 83–90s: show the real test/benchmark receipts and local-only limitations. No invented live-provider or public-deployment claim.
