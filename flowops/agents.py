"""Differentiated deterministic specialists, not autonomous model reasoning."""

import asyncio
from datetime import datetime
from typing import Literal

import httpx

from .contracts import (
    Handoff,
    OperationsOutput,
    PolicyOutput,
    ResearchOutput,
    Scenario,
    VerifierOutput,
)
from .policy import POLICIES, propose

SCOPES = {
    "supervisor": {
        "crm:read",
        "erp:read",
        "inventory:read",
        "policy:read",
        "proposal:write",
        "risk:review",
    },
    "research": {"crm:read", "erp:read", "inventory:read"},
    "policy": {"policy:read"},
    "operations": {"proposal:write"},
    "verifier": {"risk:review"},
}


class ConnectorFailure(Exception):
    def __init__(self, code: str, retry_after: float = 0):
        self.code, self.retry_after = code, retry_after


def handoff(
    run_id: str,
    agent: Literal["supervisor", "research", "policy", "operations", "verifier"],
    environment: str,
    deadline: datetime,
    evidence: list[str],
) -> Handoff:
    scopes = SCOPES[agent]
    assert scopes <= SCOPES["supervisor"]
    return Handoff(
        workflow_id=run_id,
        task_id=f"{agent}-1",
        agent=agent,
        delegated_scopes=sorted(scopes),
        evidence_ids=evidence,
        output_schema=f"{agent}-output-v1",
        deadline=deadline,
        environment=environment,
        correlation_id=run_id,
    )


def require_scope(contract: Handoff, scope: str):
    if scope not in contract.delegated_scopes or scope not in SCOPES[contract.agent]:
        raise PermissionError("Tool scope denied")


async def research(s: Scenario, contract: Handoff, attempt: int) -> dict:
    """Actual HTTP client calls via an in-process fake connector transport. No network egress."""

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        scope = {"/crm": "crm:read", "/erp": "erp:read", "/inventory": "inventory:read"}.get(path)
        if not scope:
            return httpx.Response(404)
        require_scope(contract, scope)
        if path == "/erp":
            mode = s.api_response_mode
            if mode == "timeout":
                raise httpx.ReadTimeout("Synthetic ERP timeout", request=request)
            if mode == "429_once" and attempt == 1:
                return httpx.Response(429, headers={"Retry-After": "1"})
            if mode == "503_twice" and attempt <= 2:
                return httpx.Response(503)
        payload = {
            "/crm": {
                "customer_id": "SYN-CUSTOMER-001",
                "customer_type": s.customer_type,
                "evidence_id": "CRM-001",
            },
            "/erp": {
                "order_id": s.order_id,
                "amount_minor": s.order_amount_minor,
                "delivery_status": s.delivery_status,
                "evidence_id": "ERP-001",
            },
            "/inventory": {
                "available": s.inventory_available,
                "alternative": "SYN-ITEM-ALT",
                "evidence_id": "STOCK-001",
            },
        }
        return httpx.Response(200, json=payload[path])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="https://synthetic.invalid", timeout=2
    ) as client:
        try:
            responses = await asyncio.gather(
                *(client.get(path) for path in ["/crm", "/erp", "/inventory"])
            )
        except httpx.TimeoutException as exc:
            raise ConnectorFailure("ERP_TIMEOUT") from exc
    for response in responses:
        if response.status_code != 200:
            raise ConnectorFailure(
                f"ERP_{response.status_code}", float(response.headers.get("Retry-After", "0"))
            )
    return ResearchOutput.model_validate(
        {
            "customer": responses[0].json(),
            "order": responses[1].json(),
            "inventory": responses[2].json(),
            "evidence_ids": ["CRM-001", "ERP-001", "STOCK-001"],
        }
    ).model_dump()


async def retrieve_policy(s: Scenario, contract: Handoff) -> dict:
    require_scope(contract, "policy:read")
    return PolicyOutput.model_validate(
        {
            "revision": s.policy_revision,
            "policy": POLICIES.get(s.policy_revision),
            "evidence_ids": [f"POLICY-{s.policy_revision}"]
            if s.policy_revision in POLICIES
            else [],
            "authorized": True,
        }
    ).model_dump()


def operations(s: Scenario, contract: Handoff, facts: ResearchOutput) -> dict:
    require_scope(contract, "proposal:write")
    observed = Scenario.model_validate(
        {
            **s.model_dump(),
            "order_id": facts.order.order_id,
            "order_amount_minor": facts.order.amount_minor,
            "delivery_status": facts.order.delivery_status,
            "inventory_available": facts.inventory.available,
            "customer_type": facts.customer.customer_type,
        }
    )
    return OperationsOutput.model_validate(
        {
            "proposal": propose(observed).model_dump(),
            "priority": "expedited" if observed.customer_type == "priority" else "normal",
            "notification": "Draft only; no message is sent.",
            "alternative": facts.inventory.alternative if facts.inventory.available == 0 else None,
        }
    ).model_dump()


def verify(
    s: Scenario,
    contract: Handoff,
    proposal: dict,
    evidence: list[str],
    facts: ResearchOutput,
    policy: PolicyOutput,
) -> dict:
    require_scope(contract, "risk:review")
    return VerifierOutput.model_validate(
        {
            "consistent": proposal == propose(s).model_dump()
            and facts.order.order_id == s.order_id
            and facts.order.amount_minor == s.order_amount_minor
            and facts.order.delivery_status == s.delivery_status
            and facts.inventory.available == s.inventory_available
            and facts.customer.customer_type == s.customer_type
            and policy.revision == s.policy_revision
            and (policy.policy.model_dump() if policy.policy else None)
            == POLICIES.get(s.policy_revision),
            "evidence_complete": s.evidence_complete and len(evidence) >= 4,
            "advisory_only": True,
            "cannot_grant_approval": True,
        }
    ).model_dump()
