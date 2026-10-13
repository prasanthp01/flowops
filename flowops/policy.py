"""Authorization is deterministic and rechecked at the action boundary."""

import hashlib
import json
from typing import Literal, TypedDict

from .contracts import WORKFLOW_VERSION, Decision, Proposal, Scenario


class Policy(TypedDict):
    threshold_minor: int
    return_days: int
    evidence_id: str


POLICIES: dict[str, Policy] = {
    "2026-01": {"threshold_minor": 10000, "return_days": 30, "evidence_id": "POLICY-2026-01"},
    "2026-02": {"threshold_minor": 5000, "return_days": 14, "evidence_id": "POLICY-2026-02"},
}


def propose(s: Scenario) -> Proposal:
    action: Literal["refund_request", "inventory_hold", "investigation_ticket"] = (
        "refund_request"
        if s.refund_requested_minor
        else ("inventory_hold" if s.inventory_available == 0 else "investigation_ticket")
    )
    return Proposal(
        action=action,
        amount_minor=s.refund_requested_minor,
        currency=s.currency,
        order_id=s.order_id,
    )


def decide(s: Scenario, p: Proposal) -> Decision:
    def result(status, code, rationale):
        return Decision(
            status=status, code=code, rationale=rationale, policy_version=s.policy_revision
        )

    if not s.permission_granted:
        return result(
            "BLOCK", "PERMISSION_DENIED", "The caller lacks the required simulated operation scope."
        )
    if s.order_id != s.refund_order_id or s.risk_flags:
        return result("BLOCK", "RISK_BLOCKED", "Identifier consistency or risk checks failed.")
    if p != propose(s):
        return result(
            "BLOCK", "PROPOSAL_TAMPERED", "The proposal differs from authorized source facts."
        )
    policy = POLICIES.get(s.policy_revision)
    if not policy or not s.evidence_complete:
        return result(
            "APPROVAL_REQUIRED",
            "MISSING_EVIDENCE",
            "Policy or supporting evidence requires operator review.",
        )
    if p.action == "refund_request":
        if not s.return_conditions_met or s.order_age_days > policy["return_days"]:
            return result(
                "BLOCK",
                "INELIGIBLE_RETURN",
                "Return conditions or the versioned return window are not met.",
            )
        if s.delivery_status != "delivered" or p.amount_minor > policy["threshold_minor"]:
            return result(
                "APPROVAL_REQUIRED",
                "REVIEW_REQUIRED",
                "Delivery state or refund amount requires explicit approval.",
            )
    return result(
        "ALLOW", "POLICY_ELIGIBLE", "The synthetic proposal satisfies the versioned policy."
    )


def action_digest(run_id: str, environment: str, proposal: dict) -> str:
    data = {
        "run_id": run_id,
        "environment": environment,
        "workflow_version": WORKFLOW_VERSION,
        "proposal": proposal,
    }
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
