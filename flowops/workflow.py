"""Durable, bounded state machine with compare-and-fence checkpoint commits."""

import asyncio
import hashlib
import time
import uuid
from datetime import timedelta

from sqlalchemy import and_, delete, or_, select

from . import __version__
from .agents import ConnectorFailure, handoff, operations, research, retrieve_policy, verify
from .config import Settings
from .contracts import (
    FIXTURES_VERSION,
    MODE,
    WORKFLOW_VERSION,
    PolicyOutput,
    Proposal,
    ResearchOutput,
    Scenario,
    SupervisorOutput,
    VerifierOutput,
)
from .db import Approval, ApprovalRecord, Effect, Event, RateBucket, Run, Session, StreamSlot, now
from .policy import action_digest, decide
from .runtime import SafeRuntime

PHASES = [
    "validation",
    "plan",
    "parallel_research",
    "operations_proposal",
    "risk_review",
    "approval_pause",
    "action_simulation",
    "audit_report",
]
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"}


class StaleClaim(Exception):
    pass


def emit(
    db,
    run: Run,
    step: str,
    status: str,
    title: str,
    overview: str,
    technical: dict,
    duration: float | None = None,
):
    run.event_count += 1
    event_id = str(uuid.uuid4())
    payload = {
        "schema_version": "1.0",
        "project_id": "flowops",
        "run_id": run.id,
        "event_id": event_id,
        "sequence": run.event_count,
        "occurred_at": now().isoformat(),
        "step_id": step,
        "step_type": step,
        "component": technical.get("agent", "workflow"),
        "status": status,
        "mode": MODE,
        "title": title,
        "overview": overview,
        "technical": {"adapter_kind": "deterministic_role_adapter", **technical},
        "duration_ms": round(duration, 3) if duration is not None else None,
        "trace_id": run.id,
        "usage": {
            "kind": "NONE",
            "input_tokens": 0,
            "output_tokens": 0,
            "estimated_cost": 0,
            "currency": "EUR",
        },
    }
    wire_payload = None
    if run.context.get("contracts_v1"):
        from .interop import handoff_document
        from .wire import event_document

        native = list(technical.get("handoffs", []))
        if "handoff" in technical:
            native.append(technical["handoff"])
        if native:
            values = [
                handoff_document(
                    value,
                    run,
                    granted_scopes={"workflow:read", "workflow:propose"},
                    tenant_id=run.context["contracts_v1"]["tenant_id"],
                )
                for value in native
            ]
            run.context = {
                **run.context,
                "contract_handoffs": [*run.context.get("contract_handoffs", []), *values],
            }
        wire_payload = event_document(run, payload)
    db.add(
        Event(
            id=event_id,
            run_id=run.id,
            sequence=run.event_count,
            payload=payload,
            wire_payload=wire_payload,
        )
    )


def result_for(run: Run) -> dict:
    return {
        "schema_version": "1.0",
        "project_id": "flowops",
        "run_id": run.id,
        "mode": MODE,
        "status": run.status,
        "app_version": __version__,
        "fixtures_version": FIXTURES_VERSION,
        "output": {
            "outcome": run.outcome,
            "safety_status": "SYNTHETIC_ONLY",
            "proposal": run.context.get("proposal"),
            "decision": run.context.get("decision"),
            "receipt": run.context.get("receipt"),
            "approval": run.context.get("approval"),
            "workflow_version": WORKFLOW_VERSION,
            "policy_version": run.input["policy_revision"],
            "connector_kind": "in_process_mock_http",
            "data_provenance": "versioned_synthetic_fixtures",
            "seed": run.input["seed"],
            "fallback": run.context.get("runtime"),
            "retry_count": run.context.get("retry_count", 0),
            "limitations": [
                "No real money movement or customer messages.",
                "No live LLM inference.",
                "Hosting resources still have a cost.",
            ],
        },
        "evidence": [{"id": e, "kind": "synthetic"} for e in run.context.get("evidence", [])],
        "errors": run.context.get("errors", []),
        "duration_ms": round(
            ((run.completed_at or now()) - run.created_at).total_seconds() * 1000, 3
        ),
        "cost": {"kind": "ACTUAL", "value": 0, "currency": run.input["currency"]},
    }


def terminate(db, run: Run, status: str, outcome: str, code: str):
    run.status, run.outcome, run.completed_at, run.lease_until = status, outcome, now(), None
    if run.context.get("contracts_v1"):
        # A deadline failure takes effect at the deadline; observation may occur
        # later after a stopped worker resumes. Preserve both timestamps.
        from .wire import utc

        if status == "EXPIRED" and run.expires_at > now():
            run.status = "FAILED"
        if code in {"RUN_EXPIRED", "APPROVAL_EXPIRED", "DEADLINE_EXCEEDED"}:
            run.context = {**run.context, "timeout_observed_at": utc(now())}
            logical_deadline = run.deadline_at
            if code == "APPROVAL_EXPIRED":
                logical_deadline = min(run.created_at + timedelta(seconds=90), logical_deadline)
            run.completed_at = min(now(), logical_deadline, run.created_at + timedelta(seconds=120))
    run.claim_version += 1
    run.context = {
        **run.context,
        "errors": [*run.context.get("errors", []), {"code": code, "retryable": False}],
    }
    emit(
        db,
        run,
        "audit_report",
        "BLOCKED" if outcome == "BLOCKED" else "FAILED",
        code.replace("_", " ").title(),
        "The workflow stopped safely; no real business action was performed.",
        {"code": code},
    )
    run.result = result_for(run)
    if run.context.get("contracts_v1") and run.status == "FAILED":
        from .service_events import reserve_failure

        reserve_failure(db, run, code)


class Workflow:
    def __init__(self, sessions, settings: Settings):
        self.sessions, self.settings = sessions, settings
        self.runtime = SafeRuntime()
        from .peer_workflow import PeerWorkflow

        self.peers = PeerWorkflow(settings)

    def claim(self) -> tuple[str, int] | None:
        with self.sessions.begin() as db:
            t = now()
            run = db.scalar(
                select(Run)
                .where(
                    Run.environment == self.settings.environment,
                    Run.status.in_(["ACCEPTED", "RUNNING"]),
                    Run.outcome != "APPROVAL_PENDING",
                    Run.next_attempt_at <= t,
                    or_(Run.lease_until.is_(None), Run.lease_until < t),
                )
                .order_by(Run.next_attempt_at, Run.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not run:
                return None
            if run.deadline_at <= t or run.attempts >= 3:
                terminate(
                    db,
                    run,
                    "FAILED",
                    "FAILED",
                    "DEADLINE_EXCEEDED" if run.deadline_at <= t else "DEAD_LETTER",
                )
                return None
            run.status = "RUNNING"
            run.claim_version += 1
            run.attempts += 1
            run.lease_until = t + timedelta(seconds=self.settings.lease_seconds)
            emit(
                db,
                run,
                PHASES[run.phase],
                "STARTED",
                PHASES[run.phase].replace("_", " ").title(),
                "A persisted workflow step has started.",
                {"attempt": run.attempts, "claim_version": run.claim_version},
            )
            return run.id, run.claim_version

    def fenced(self, db, run_id: str, version: int) -> Run:
        run = db.scalar(
            select(Run)
            .where(Run.id == run_id, Run.environment == self.settings.environment)
            .with_for_update()
        )
        if (
            not run
            or run.status in TERMINAL
            or run.claim_version != version
            or not run.lease_until
            or run.lease_until <= now()
            or run.deadline_at <= now()
            or run.expires_at <= now()
        ):
            raise StaleClaim()
        return run

    def heartbeat(self, run_id: str, version: int):
        with self.sessions.begin() as db:
            run = self.fenced(db, run_id, version)
            run.lease_until = now() + timedelta(seconds=self.settings.lease_seconds)

    def authorized_approval(self, db, run: Run, proposal: dict) -> bool:
        a = db.scalar(select(Approval).where(Approval.run_id == run.id))
        if (
            not a
            or a.expires_at <= now()
            or a.environment != run.environment
            or a.workflow_version != WORKFLOW_VERSION
            or a.action_digest != action_digest(run.id, run.environment, proposal)
        ):
            return False
        record = db.scalar(select(ApprovalRecord).where(ApprovalRecord.request_id == a.id))
        return bool(record and record.decision == "approve" and record.actor)

    def apply_effect(self, run_id: str, version: int) -> dict:
        with self.sessions.begin() as db:
            run = self.fenced(db, run_id, version)
            s = Scenario.model_validate(run.input)
            p = Proposal.model_validate(run.context["proposal"])
            d = decide(s, p)
            if not VerifierOutput.model_validate(run.context["review"]).consistent:
                raise PermissionError("Verifier consistency check failed")
            needs_review = (
                d.status == "APPROVAL_REQUIRED"
                or run.context.get("runtime", {}).get("status") == "AI_UNAVAILABLE"
            )
            if d.status == "BLOCK" or (
                needs_review and not self.authorized_approval(db, run, p.model_dump())
            ):
                raise PermissionError("Action authorization denied")
            operation_id = action_digest(run.id, run.environment, p.model_dump())
            existing = db.get(Effect, operation_id)
            if existing:
                return {**existing.receipt, "reconciled": True}
            receipt = {
                "operation_id": operation_id,
                "kind": "SIMULATED_APPLIED",
                "action": p.action,
                "amount_minor": p.amount_minor,
                "currency": p.currency,
                "order_id": p.order_id,
                "real_transaction": False,
                "notification_sent": False,
            }
            db.add(Effect(operation_id=operation_id, run_id=run.id, receipt=receipt))
            lost = s.api_response_mode == "lost_response"
        if lost:
            raise ConnectorFailure("AMBIGUOUS_RESPONSE_RECONCILE")
        return receipt

    async def compute(self, run: Run, version: int) -> dict:
        s = Scenario.model_validate(run.input)
        evidence = run.context.get("evidence", [])

        def contract(agent):
            return handoff(run.id, agent, run.environment, run.deadline_at, evidence)

        if run.phase == 0:
            return {
                "input_validated": True,
                "mode": MODE,
                "untrusted_approval_ignored": s.existing_approvals != "none",
            }
        if run.phase == 1:
            runtime = self.runtime.analyze(run.id, s.model_response_mode).model_dump()
            peer = await asyncio.to_thread(self.peers.inference, run)
            if peer and (peer["error"] or peer["route"]["outcome"] != "SUCCEEDED"):
                runtime.update(
                    status="AI_UNAVAILABLE",
                    reason="Peer inference refused or unavailable; deterministic proposal requires operator review.",
                )
            return {
                **({"peer_inference": peer} if peer else {}),
                "runtime": runtime,
                "agent": "supervisor",
                "handoff": contract("supervisor").model_dump(mode="json"),
                **SupervisorOutput(
                    plan=["research", "policy", "operations", "verifier"],
                    route="inventory_resolution"
                    if s.inventory_available == 0 and not s.refund_requested_minor
                    else "refund_investigation",
                ).model_dump(),
            }
        if run.phase == 2:
            rc, pc = contract("research"), contract("policy")
            facts, policy = await asyncio.gather(
                research(s, rc, run.attempts), retrieve_policy(s, pc)
            )
            peer = await asyncio.to_thread(self.peers.knowledge, run)
            return {
                **({"peer_knowledge": peer} if peer else {}),
                "research": facts,
                "policy": policy,
                "handoffs": [rc.model_dump(mode="json"), pc.model_dump(mode="json")],
                "evidence": facts["evidence_ids"] + policy["evidence_ids"],
                "parallel": True,
            }
        if run.phase == 3:
            c = contract("operations")
            typed_facts = ResearchOutput.model_validate(run.context["research"])
            return {
                **operations(s, c, typed_facts),
                "agent": "operations",
                "handoff": c.model_dump(mode="json"),
            }
        if run.phase == 4:
            c = contract("verifier")
            review = verify(
                s,
                c,
                run.context["proposal"],
                evidence,
                ResearchOutput.model_validate(run.context["research"]),
                PolicyOutput.model_validate(run.context["policy"]),
            )
            decision = decide(s, Proposal.model_validate(run.context["proposal"])).model_dump()
            if not review["consistent"]:
                decision.update(
                    status="BLOCK",
                    code="EVIDENCE_MISMATCH",
                    rationale="The retrieved facts, policy and proposal are inconsistent.",
                )
            return {
                "review": review,
                "agent": "verifier",
                "handoff": c.model_dump(mode="json"),
                "decision": decision,
            }
        if run.phase == 5:
            return {"gate": True}
        if run.phase == 6:
            return {"receipt": self.apply_effect(run.id, version)}
        return {"audit_complete": True}

    def checkpoint(self, run_id: str, version: int, output: dict, elapsed: float):
        with self.sessions.begin() as db:
            run = self.fenced(db, run_id, version)
            step = PHASES[run.phase]
            run.context = {
                **run.context,
                **{k: v for k, v in output.items() if k not in {"handoff", "handoffs", "agent"}},
            }
            if run.phase == 5:
                d = run.context["decision"]
                if d["status"] == "BLOCK":
                    terminate(db, run, "SUCCEEDED", "BLOCKED", d["code"])
                    return
                needs_review = (
                    d["status"] == "APPROVAL_REQUIRED"
                    or run.context["runtime"]["status"] == "AI_UNAVAILABLE"
                )
                if needs_review:
                    a = Approval(
                        id=str(uuid.uuid4()),
                        run_id=run.id,
                        proposal=run.context["proposal"],
                        action_digest=action_digest(
                            run.id, run.environment, run.context["proposal"]
                        ),
                        environment=run.environment,
                        workflow_version=WORKFLOW_VERSION,
                        expires_at=min(
                            now() + timedelta(seconds=self.settings.approval_seconds),
                            run.created_at + timedelta(seconds=90),
                        )
                        if run.context.get("contracts_v1")
                        else now() + timedelta(seconds=self.settings.approval_seconds),
                    )
                    db.add(a)
                    approval = {
                        "request_id": a.id,
                        "action_digest": a.action_digest,
                        "expires_at": a.expires_at.isoformat(),
                        "environment": a.environment,
                        "workflow_version": a.workflow_version,
                        "proposal": a.proposal,
                    }
                    run.context = {**run.context, "approval": approval}
                    if run.context.get("contracts_v1"):
                        run.deadline_at = a.expires_at
                    run.outcome = "APPROVAL_PENDING"
                    run.phase = 6
                    run.lease_until = None
                    run.attempts = 0
                    emit(
                        db,
                        run,
                        step,
                        "WAITING",
                        "Operator approval required",
                        "The exact proposal is paused. Public input cannot grant approval.",
                        {"approval": approval, "decision": d},
                        elapsed,
                    )
                    run.result = result_for(run)
                    return
            if run.phase == 6:
                run.outcome = "SIMULATED_APPLIED"
            emit(
                db,
                run,
                step,
                "SUCCEEDED",
                step.replace("_", " ").title(),
                {
                    0: "Bounded synthetic input passed server validation.",
                    1: "The supervisor selected a declared workflow template.",
                    2: "Independent CRM, ERP, inventory and policy reads completed.",
                    3: "Operations prepared a proposal; no real business action occurred.",
                    4: "The verifier reviewed evidence; deterministic policy made the authorization decision.",
                    5: "The proposal passed the deterministic authorization gate.",
                    6: "The fake action gateway recorded one idempotent synthetic receipt.",
                    7: "The run is complete with its evidence and audit trail.",
                }[run.phase],
                output,
                elapsed,
            )
            if run.phase == 2:
                emit(
                    db,
                    run,
                    "policy_retrieval",
                    "SUCCEEDED",
                    "Policy evidence retrieved",
                    "The approved synthetic policy revision was inspected.",
                    output["policy"],
                    elapsed,
                )
            if run.phase == 7:
                run.status, run.completed_at = "SUCCEEDED", now()
                run.result = result_for(run)
            else:
                run.phase += 1
            run.attempts, run.lease_until, run.next_attempt_at = 0, None, now()

    def fail(self, run_id: str, version: int, failure: ConnectorFailure):
        with self.sessions.begin() as db:
            run = self.fenced(db, run_id, version)
            run.context = {**run.context, "retry_count": run.context.get("retry_count", 0) + 1}
            if run.attempts >= 3:
                terminate(db, run, "FAILED", "FAILED", "DEAD_LETTER_" + failure.code)
                return
            # Seeded jitter changes retry timing, never the policy result.
            h = (
                hashlib.sha256(f"{run.input['seed']}:{run.phase}:{run.attempts}".encode()).digest()[
                    0
                ]
                / 255
            )
            delay = max(
                failure.retry_after, self.settings.retry_base * 2 ** (run.attempts - 1) + h * 0.1
            )
            run.next_attempt_at = now() + timedelta(seconds=delay)
            run.lease_until = None
            emit(
                db,
                run,
                PHASES[run.phase],
                "WAITING",
                "Retry scheduled",
                "The failed mock request will retry from a durable checkpoint.",
                {
                    "code": failure.code,
                    "retryable": True,
                    "attempt": run.attempts,
                    "retry_after_seconds": delay,
                },
            )

    async def run_once(self) -> bool:
        claim = self.claim()
        if not claim:
            return False
        run_id, version = claim
        with self.sessions() as db:
            run = db.get(Run, run_id)
        started = time.perf_counter()
        try:
            output = await asyncio.wait_for(
                self.compute(run, version), timeout=min(5, self.settings.lease_seconds / 2)
            )
            self.checkpoint(run_id, version, output, (time.perf_counter() - started) * 1000)
        except StaleClaim:
            pass
        except (ConnectorFailure, TimeoutError) as exc:
            try:
                self.fail(
                    run_id,
                    version,
                    exc if isinstance(exc, ConnectorFailure) else ConnectorFailure("STEP_TIMEOUT"),
                )
            except StaleClaim:
                pass
        except Exception:
            # Never place exception strings, inputs or credentials in public traces.
            with self.sessions.begin() as db:
                try:
                    r = self.fenced(db, run_id, version)
                    terminate(db, r, "FAILED", "BLOCKED", "INTERNAL_SAFE_STOP")
                except StaleClaim:
                    pass
        return True

    def maintain(self):
        with self.sessions.begin() as db:
            t = now()
            pending = db.scalars(
                select(Run)
                .where(
                    Run.environment == self.settings.environment,
                    Run.status.in_(["ACCEPTED", "RUNNING"]),
                    or_(
                        Run.expires_at <= t,
                        and_(Run.outcome != "APPROVAL_PENDING", Run.deadline_at <= t),
                    ),
                )
                .with_for_update(skip_locked=True)
                .limit(100)
            ).all()
            for run in pending:
                terminate(db, run, "EXPIRED", "FAILED", "RUN_EXPIRED")
            expired = db.scalars(
                select(Run)
                .join(Approval, Approval.run_id == Run.id)
                .where(
                    Run.environment == self.settings.environment,
                    Run.outcome == "APPROVAL_PENDING",
                    Approval.expires_at <= t,
                )
                .with_for_update(of=Run, skip_locked=True)
                .limit(100)
            ).all()
            for run in expired:
                terminate(db, run, "EXPIRED", "BLOCKED", "APPROVAL_EXPIRED")
            db.execute(
                delete(Run).where(
                    Run.environment == self.settings.environment,
                    Run.expires_at < t,
                    Run.status.in_(TERMINAL),
                )
            )
            db.execute(
                delete(Session).where(Session.expires_at < t, ~Session.id.in_(select(Run.owner_id)))
            )
            db.execute(delete(RateBucket).where(RateBucket.expires_at < t))
            db.execute(delete(StreamSlot).where(StreamSlot.expires_at < t))


def main():
    from .db import database
    from .worker_health import mark_ready

    settings = Settings.load()
    _, sessions = database(settings)
    worker = Workflow(sessions, settings)

    async def serve():
        last = 0.0
        while True:
            if time.monotonic() - last > 10:
                worker.maintain()
                last = time.monotonic()
            had_work = await worker.run_once()
            mark_ready()
            if not had_work:
                await asyncio.sleep(0.1)

    asyncio.run(serve())


if __name__ == "__main__":
    main()
