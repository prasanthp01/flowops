"""Shared deterministic approval authority for legacy and canonical APIs."""

import uuid
from datetime import timedelta

from fastapi import HTTPException
from sqlalchemy import select

from .db import Approval, ApprovalRecord, now
from .workflow import TERMINAL, emit, terminate


def decide_approval(db, run, body, settings, actor):
    a = db.scalar(select(Approval).where(Approval.id == body.request_id, Approval.run_id == run.id))
    if not a or a.action_digest != body.action_digest or a.environment != settings.environment:
        raise HTTPException(409, "APPROVAL_BINDING_MISMATCH")
    if a.expires_at <= now():
        raise HTTPException(409, "APPROVAL_EXPIRED")
    record = db.scalar(select(ApprovalRecord).where(ApprovalRecord.request_id == a.id))
    if record:
        if record.decision == body.decision and record.actor == actor:
            return run
        raise HTTPException(409, "APPROVAL_ALREADY_DECIDED")
    if run.status in TERMINAL or run.outcome != "APPROVAL_PENDING":
        raise HTTPException(409, "NOT_AWAITING_APPROVAL")
    db.add(
        ApprovalRecord(id=str(uuid.uuid4()), request_id=a.id, actor=actor, decision=body.decision)
    )
    emit(
        db,
        run,
        "approval_pause",
        "SUCCEEDED" if body.decision == "approve" else "BLOCKED",
        "Operator decision recorded",
        "The authenticated operator decision is bound to this exact synthetic proposal.",
        {
            "request_id": a.id,
            "action_digest": a.action_digest,
            "decision": body.decision,
            "actor_kind": "configured_operator",
        },
    )
    if body.decision == "reject":
        terminate(db, run, "SUCCEEDED", "BLOCKED", "OPERATOR_REJECTED")
    else:
        run.outcome, run.result, run.next_attempt_at = "PROPOSED", None, now()
        run.deadline_at = now() + timedelta(seconds=settings.run_seconds)
        if run.context.get("contracts_v1"):
            run.deadline_at = min(run.deadline_at, run.created_at + timedelta(seconds=120))
    return run
