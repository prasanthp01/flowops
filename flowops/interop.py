"""Explicit, fail-closed peer adapters. No credentials or grants come from public input."""

import json
from dataclasses import dataclass, field

import httpx

from ._vendor.ecosystem_contracts import ContractError, validate_handoff
from .agents import SCOPES
from .contracts import MODE, Handoff
from .db import now
from .service_events import environment
from .wire import error, evidence, metadata, usage, utc, validated

ROLE_MAP = {
    "supervisor": "SUPERVISOR",
    "research": "RETRIEVAL",
    "policy": "RETRIEVAL",
    "operations": "EXECUTION",
    "verifier": "REVIEW",
}
WIRE_SCOPES = {
    "supervisor": {"workflow:read", "workflow:propose"},
    "research": {"workflow:read"},
    "policy": {"workflow:read"},
    "operations": {"workflow:propose"},
    "verifier": {"workflow:read"},
}


def handoff_document(raw, run, *, granted_scopes, tenant_id):
    native = raw if isinstance(raw, Handoff) else Handoff.model_validate_json(json.dumps(raw))
    if (
        native.workflow_id != run.id
        or native.correlation_id != run.id
        or native.environment != run.environment
        or set(native.delegated_scopes) != SCOPES[native.agent]
        or native.deadline <= now()
        or tenant_id != metadata(run)["tenant_id"]
    ):
        raise ContractError("Invalid native delegation context")
    value = {
        "schema_version": "1.0",
        "workflow_id": run.id,
        "task_id": native.task_id,
        "agent_role": ROLE_MAP[native.agent],
        "authorized_tool_scopes": sorted(WIRE_SCOPES[native.agent]),
        "output_schema_version": native.output_schema,
        "context_refs": [run.id],
        "evidence_refs": evidence(run, native.evidence_ids),
        "deadline": utc(native.deadline),
        "status": "ASSIGNED",
        "decision": "PENDING",
        "error": None,
        "correlation_id": metadata(run)["client_correlation_id"],
        "tenant_id": tenant_id,
        "mode": MODE,
    }
    validate_handoff(value, granted_scopes=set(granted_scopes), tenant_id=tenant_id)
    return value


@dataclass(frozen=True)
class PeerGrant:
    """Constructed by the private service configuration/authorization layer only."""

    workflow_id: str
    tenant_id: str
    scopes: frozenset[str]
    collections: frozenset[str] = frozenset()

    def require(self, run, scope):
        if (
            self.workflow_id != run.id
            or self.tenant_id != metadata(run)["tenant_id"]
            or scope not in self.scopes
            or run.deadline_at <= now()
            or run.status not in {"ACCEPTED", "RUNNING"}
        ):
            raise PermissionError("Peer authorization denied")


class PeerResponseError(Exception):
    """A validated, bounded error detached from the closed HTTP stream."""

    def __init__(self, value):
        self.error = value
        super().__init__(value["code"])


@dataclass
class PeerClient:
    """A fixed pre-authorized service endpoint; sandbox-only, no provider fallback."""

    base_url: str
    bearer: str = field(repr=False)
    transport: httpx.BaseTransport | None = field(default=None, repr=False)
    allow_local_test: bool = False

    def __post_init__(self):
        from urllib.parse import urlsplit

        parsed = urlsplit(self.base_url)
        local = (
            self.allow_local_test
            and parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost"}
        )
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or (parsed.scheme != "https" and not local)
            or len(self.bearer) < 32
        ):
            raise ValueError("Invalid private peer configuration")

    def _post(self, path, body, schema):
        with httpx.Client(transport=self.transport, timeout=2, follow_redirects=False) as client:
            with client.stream(
                "POST",
                self.base_url.rstrip("/") + path,
                headers={"Authorization": "Bearer " + self.bearer},
                json=body,
            ) as response:
                data = bytearray()
                import time

                started = time.monotonic()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > 262144 or time.monotonic() - started > 5:
                        raise ContractError("Peer response exceeds bounds")
                status = response.status_code
        try:
            value = json.loads(data)
        except (ValueError, RecursionError):
            raise ContractError("Peer response is not bounded JSON") from None
        if status != 200:
            value = validated("error", value)
            if value["http_status"] != status:
                raise ContractError("Peer error status mismatch")
            # Keep the validated value accessible without an unread response or
            # retaining a request object that contains service credentials.
            raise PeerResponseError(value)
        return validated(schema, value)

    def knowledge(self, run, grant: PeerGrant, collection_ids, *, query=None):
        grant.require(run, "knowledge:read")
        if not set(collection_ids) <= grant.collections:
            raise PermissionError("Collection authorization denied")
        request = validated(
            "knowledge-query",
            {
                "schema_version": "1.0",
                "application_id": "flowops",
                "tenant_id": grant.tenant_id,
                "query": query or "Synthetic refund policy " + run.input["policy_revision"],
                "collection_ids": list(collection_ids),
                "max_results": 3,
                "max_excerpt_chars": 300,
                "correlation_id": metadata(run)["client_correlation_id"],
                "mode": MODE,
            },
        )
        response = self._post("/knowledge/query", request, "knowledge-response")
        if response["correlation_id"] != request["correlation_id"] or response["mode"] != MODE:
            raise ContractError("Knowledge response scope mismatch")
        return response  # Untrusted data; never interpreted as policy or approval.

    def inference(self, run, grant: PeerGrant):
        grant.require(run, "inference:request")
        operation = "inference:" + run.id
        request = validated(
            "inference-request",
            {
                "schema_version": "1.0",
                "application_id": "flowops",
                "environment": environment(run.environment),
                "tenant_id": grant.tenant_id,
                "task_type": "order_resolution_review",
                "risk_class": "HIGH",
                "required_capabilities": ["STRUCTURED_OUTPUT"],
                "data_classification": "PUBLIC",
                "allowed_regions": ["eu-test"],
                "latency_deadline_ms": 2000,
                "max_input_tokens": 1024,
                "max_output_tokens": 256,
                "max_cost": 0,
                "currency": run.input["currency"],
                "routing_policy_version": "flowops-sandbox-no-spend-1",
                "correlation_id": metadata(run)["client_correlation_id"],
                "trace_id": run.id,
                "operation_id": operation,
                "mode": MODE,
                "context_refs": [run.id],
                "extensions": {},
            },
        )
        response = self._post("/inference", request, "route-decision")
        if any(
            response[k] != request[k]
            for k in (
                "operation_id",
                "correlation_id",
                "trace_id",
                "mode",
                "routing_policy_version",
            )
        ):
            raise ContractError("Inference response scope mismatch")
        usages = [response["usage"], *(attempt["usage"] for attempt in response["attempts"])]
        if any(
            u["provider_calls"]
            or u["kind"] not in {"NONE", "SIMULATED"}
            or u["cost"]["value"] != 0
            or u["cost"]["currency"] != request["currency"]
            for u in usages
        ):
            raise ContractError("Sandbox peer cannot execute or bill a provider")
        selected = response["selected_deployment"]
        if selected and (
            selected["region"] not in request["allowed_regions"]
            or not set(request["required_capabilities"]) <= set(selected["capabilities"])
        ):
            raise ContractError("Selected deployment violates request constraints")
        return response


def no_usage_payload(run):
    return validated(
        "ai-usage-recorded",
        {
            "operation_id": "inference:" + run.id,
            "provider": "none",
            "deployment": "deterministic",
            "model_version": "none",
            "region": "eu-test",
            "usage": usage(run.id, run.input["currency"], "inference"),
            "attempt": 1,
        },
    )


def deterministic_refusal(run, exc):
    """Preserve a policy refusal without rewriting or retrying a weaker request."""
    if isinstance(exc, PeerResponseError):
        remote = exc.error
        safe = error(remote["code"], remote["http_status"], remote["correlation_id"])
        outcome = (
            remote["code"]
            if remote["code"] in {"POLICY_BLOCKED", "BUDGET_EXCEEDED"}
            else "AI_UNAVAILABLE"
        )
    else:
        code, status = (
            ("FORBIDDEN", 403) if isinstance(exc, PermissionError) else ("SERVICE_UNAVAILABLE", 503)
        )
        safe = error(code, status, metadata(run)["client_correlation_id"])
        outcome = "POLICY_BLOCKED" if code == "FORBIDDEN" else "AI_UNAVAILABLE"
    route = validated(
        "route-decision",
        {
            "schema_version": "1.0",
            "operation_id": "inference:" + run.id,
            "routing_policy_version": "flowops-sandbox-no-spend-1",
            "outcome": outcome,
            "selected_deployment": None,
            "rejected_candidates": [],
            "attempts": [],
            "fallback_kind": "DETERMINISTIC",
            "mode": MODE,
            "correlation_id": metadata(run)["client_correlation_id"],
            "trace_id": run.id,
            "usage": usage(run.id, run.input["currency"], "inference"),
        },
    )
    return {
        "route": route,
        "error": safe,
        "business_authority": "NONE",
        "usage_confirmation": "REFUSED" if isinstance(exc, PeerResponseError) else "UNCONFIRMED",
    }
