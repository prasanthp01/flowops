import pytest
from pydantic import ValidationError

from flowops.agents import handoff, require_scope
from flowops.contracts import Scenario
from flowops.db import now
from flowops.policy import action_digest, decide, propose


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({}, "ALLOW"),
        ({"refund_requested_minor": 10000}, "ALLOW"),
        ({"refund_requested_minor": 10001}, "APPROVAL_REQUIRED"),
        ({"order_age_days": 30}, "ALLOW"),
        ({"order_age_days": 31}, "BLOCK"),
        ({"policy_revision": "2026-02"}, "APPROVAL_REQUIRED"),
        (
            {"policy_revision": "2026-02", "refund_requested_minor": 5000, "order_age_days": 14},
            "ALLOW",
        ),
        (
            {"policy_revision": "2026-02", "refund_requested_minor": 5000, "order_age_days": 15},
            "BLOCK",
        ),
        ({"permission_granted": False}, "BLOCK"),
        ({"return_conditions_met": False}, "BLOCK"),
        ({"refund_order_id": "SYN-ORDER-002"}, "BLOCK"),
        ({"risk_flags": ["policy_injection"]}, "BLOCK"),
        ({"policy_revision": "unknown"}, "APPROVAL_REQUIRED"),
        ({"evidence_complete": False}, "APPROVAL_REQUIRED"),
        ({"delivery_status": "incorrect"}, "APPROVAL_REQUIRED"),
        ({"delivery_status": "delayed"}, "APPROVAL_REQUIRED"),
        ({"refund_requested_minor": 0, "inventory_available": 0}, "ALLOW"),
        ({"currency": "USD"}, "ALLOW"),
    ],
)
def test_policy_boundaries(changes, expected):
    s = Scenario(**changes)
    assert decide(s, propose(s)).status == expected


@pytest.mark.parametrize(
    "bad",
    [
        {"order_amount_minor": 0},
        {"refund_requested_minor": -1},
        {"refund_requested_minor": 1.2},
        {"refund_requested_minor": 13000},
        {"currency": "BTC"},
        {"order_age_days": 366},
        {"seed": -1},
        {"permission_granted": "true"},
        {"inventory_available": True},
        {"order_id": "real-customer@example.com"},
        {"connector_url": "http://127.0.0.1"},
        {"risk_flags": ["fraud_signal", "fraud_signal"]},
    ],
)
def test_invalid_inputs(bad):
    with pytest.raises(ValidationError):
        Scenario(**bad)


def test_tampered_proposal():
    s = Scenario()
    proposal = propose(s).model_copy(update={"amount_minor": 1})
    assert decide(s, proposal).code == "PROPOSAL_TAMPERED"


def test_approval_bound_to_scope_version_and_amount():
    p = propose(Scenario()).model_dump()
    original = action_digest("run-1", "local", p)
    assert (
        len(
            {
                original,
                action_digest("run-2", "local", p),
                action_digest("run-1", "staging", p),
                action_digest("run-1", "local", {**p, "amount_minor": 8001}),
            }
        )
        == 4
    )


def test_specialist_cannot_expand_scope():
    c = handoff("run", "research", "test", now(), [])
    with pytest.raises(PermissionError):
        require_scope(c, "proposal:write")
    forged = c.model_copy(update={"delegated_scopes": ["proposal:write"]})
    with pytest.raises(PermissionError):
        require_scope(forged, "proposal:write")
