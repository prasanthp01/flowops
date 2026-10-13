"""Strict project contracts. Money is integer minor units; EUR/USD only, both 2 decimals."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MODE: Literal["SANDBOX_DETERMINISTIC"] = "SANDBOX_DETERMINISTIC"
WORKFLOW_VERSION: Literal["flowops-1.0"] = "flowops-1.0"
FIXTURES_VERSION = "synthetic-1.0"
PRESET_IDS = (
    "eligible_refund",
    "approval_required",
    "inventory_shortage",
    "erp_unavailable",
    "suspicious_request",
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Scenario(StrictModel):
    order_amount_minor: int = Field(default=12000, ge=1, le=1000000)
    refund_requested_minor: int = Field(default=8000, ge=0, le=1000000)
    currency: Literal["EUR", "USD"] = "EUR"
    delivery_status: Literal["delivered", "delayed", "incorrect", "unshipped"] = "delivered"
    order_age_days: int = Field(default=10, ge=0, le=365)
    inventory_available: int = Field(default=5, ge=0, le=1000)
    policy_revision: Literal["2026-01", "2026-02", "unknown"] = "2026-01"
    customer_type: Literal["standard", "priority"] = "standard"
    return_conditions_met: bool = True
    evidence_complete: bool = True
    permission_granted: bool = True
    order_id: Literal["SYN-ORDER-001", "SYN-ORDER-002"] = "SYN-ORDER-001"
    refund_order_id: Literal["SYN-ORDER-001", "SYN-ORDER-002"] = "SYN-ORDER-001"
    api_response_mode: Literal["healthy", "429_once", "503_twice", "timeout", "lost_response"] = (
        "healthy"
    )
    risk_flags: list[Literal["identity_mismatch", "policy_injection", "fraud_signal"]] = Field(
        default_factory=list, max_length=3
    )
    existing_approvals: Literal["none", "untrusted_claim"] = "none"
    model_response_mode: Literal["deterministic", "provider_outage", "invalid_output"] = (
        "deterministic"
    )
    seed: int = Field(default=7, ge=0, le=4294967295)

    @model_validator(mode="after")
    def valid_amounts(self):
        if self.refund_requested_minor > self.order_amount_minor:
            raise ValueError("Refund cannot exceed order amount")
        if len(set(self.risk_flags)) != len(self.risk_flags):
            raise ValueError("Risk flags must be unique")
        return self


class StartRequest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    project_id: Literal["flowops"] = "flowops"
    mode: Literal["SANDBOX_DETERMINISTIC"] = MODE
    preset_id: (
        Literal[
            "eligible_refund",
            "approval_required",
            "inventory_shortage",
            "erp_unavailable",
            "suspicious_request",
        ]
        | None
    ) = None
    input: Scenario
    client_correlation_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")


class Handoff(StrictModel):
    workflow_id: str
    task_id: str
    agent: Literal["supervisor", "research", "policy", "operations", "verifier"]
    delegated_scopes: list[str]
    evidence_ids: list[str]
    output_schema: str
    deadline: datetime
    environment: str
    workflow_version: Literal["flowops-1.0"] = WORKFLOW_VERSION
    correlation_id: str
    origin: Literal["deterministic_role_adapter"] = "deterministic_role_adapter"


class Proposal(StrictModel):
    action: Literal["refund_request", "inventory_hold", "investigation_ticket"]
    amount_minor: int = Field(ge=0, le=1000000)
    currency: Literal["EUR", "USD"]
    order_id: Literal["SYN-ORDER-001", "SYN-ORDER-002"]
    notification: Literal["draft_only"] = "draft_only"


class Decision(StrictModel):
    status: Literal["ALLOW", "APPROVAL_REQUIRED", "BLOCK"]
    code: str
    rationale: str
    policy_version: str


class ApprovalDecision(StrictModel):
    request_id: str = Field(pattern=r"^[a-f0-9-]{36}$")
    action_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    decision: Literal["approve", "reject"]


class SupervisorOutput(StrictModel):
    plan: list[Literal["research", "policy", "operations", "verifier"]] = Field(
        min_length=4, max_length=4
    )
    plan_kind: Literal["declared_rule_template"] = "declared_rule_template"
    route: Literal["inventory_resolution", "refund_investigation"]


class CustomerFact(StrictModel):
    customer_id: Literal["SYN-CUSTOMER-001"]
    customer_type: Literal["standard", "priority"]
    evidence_id: Literal["CRM-001"]


class OrderFact(StrictModel):
    order_id: Literal["SYN-ORDER-001", "SYN-ORDER-002"]
    amount_minor: int = Field(ge=1, le=1000000)
    delivery_status: Literal["delivered", "delayed", "incorrect", "unshipped"]
    evidence_id: Literal["ERP-001"]


class InventoryFact(StrictModel):
    available: int = Field(ge=0, le=1000)
    alternative: Literal["SYN-ITEM-ALT"]
    evidence_id: Literal["STOCK-001"]


class ResearchOutput(StrictModel):
    customer: CustomerFact
    order: OrderFact
    inventory: InventoryFact
    requests: Literal[3] = 3
    parallel: Literal[True] = True
    transport: Literal["in_process_mock_http"] = "in_process_mock_http"
    evidence_ids: list[str] = Field(min_length=3, max_length=3)


class PolicyDocument(StrictModel):
    threshold_minor: int = Field(ge=0, le=1000000)
    return_days: int = Field(ge=0, le=365)
    evidence_id: Literal["POLICY-2026-01", "POLICY-2026-02"]


class PolicyOutput(StrictModel):
    revision: Literal["2026-01", "2026-02", "unknown"]
    policy: PolicyDocument | None
    evidence_ids: list[str] = Field(max_length=1)
    authorized: Literal[True] = True


class OperationsOutput(StrictModel):
    proposal: Proposal
    priority: Literal["expedited", "normal"]
    notification: Literal["Draft only; no message is sent."] = "Draft only; no message is sent."
    alternative: Literal["SYN-ITEM-ALT"] | None


class VerifierOutput(StrictModel):
    consistent: bool
    evidence_complete: bool
    advisory_only: Literal[True] = True
    cannot_grant_approval: Literal[True] = True


PRESETS = [
    {
        "id": "eligible_refund",
        "title": "Eligible refund",
        "description": "A returned order within delegated limits.",
        "input": Scenario().model_dump(),
    },
    {
        "id": "approval_required",
        "title": "Approval required",
        "description": "A refund above the policy threshold.",
        "input": Scenario(order_amount_minor=48000, refund_requested_minor=35000).model_dump(),
    },
    {
        "id": "inventory_shortage",
        "title": "Inventory shortage",
        "description": "An unshipped order with no available stock.",
        "input": Scenario(
            delivery_status="unshipped", inventory_available=0, refund_requested_minor=0
        ).model_dump(),
    },
    {
        "id": "erp_unavailable",
        "title": "ERP recovery",
        "description": "Two temporary ERP failures before recovery.",
        "input": Scenario(api_response_mode="503_twice").model_dump(),
    },
    {
        "id": "suspicious_request",
        "title": "Suspicious request",
        "description": "The refund references a different order.",
        "input": Scenario(refund_order_id="SYN-ORDER-002").model_dump(),
    },
]
