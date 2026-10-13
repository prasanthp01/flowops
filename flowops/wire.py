"""Contracts 1.0.0 projections; the internal workflow and stored audit stay intact."""

import hashlib
import json
import re
from datetime import UTC, datetime

from . import __version__
from ._vendor.ecosystem_contracts import ContractError, SchemaRegistry
from .contracts import FIXTURES_VERSION, MODE, WORKFLOW_VERSION, Scenario
from .db import Run

REGISTRY = SchemaRegistry()
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED"}
STEP_TYPES = {
    "validation": "VALIDATION",
    "plan": "HANDOFF",
    "parallel_research": "RETRIEVAL",
    "operations_proposal": "EXECUTION",
    "risk_review": "POLICY",
    "approval_pause": "POLICY",
    "action_simulation": "EXECUTION",
    "audit_report": "SUMMARY",
    "policy_retrieval": "RETRIEVAL",
}


def compact(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def validated(name, value):
    REGISTRY.validate(name, value)
    return value


def provenance(source_revision, rules="2026-01"):
    if not re.fullmatch(r"[a-f0-9]{40}", source_revision or ""):
        raise ContractError("Exact application source revision is required")
    return {
        "data_origin": "SYNTHETIC",
        "connector_kind": "MOCK",
        "app_version": __version__,
        "fixtures_version": FIXTURES_VERSION,
        "rules_version": rules,
        "workflow_version": WORKFLOW_VERSION,
        "model_version": "none",
        "prompt_version": "none",
        "source_revision": source_revision,
    }


def metadata(run):
    return run.context["contracts_v1"]


def error(code, status, correlation, *, retry_after=None):
    value = {
        "schema_version": "1.0",
        "code": code,
        "message": code.replace("_", " ").capitalize(),
        "retryable": status in {429, 503},
        "correlation_id": correlation,
        "http_status": status,
    }
    if retry_after is not None:
        value["retry_after_seconds"] = min(3600, max(0, int(retry_after)))
    return validated("error", value)


def usage(run_id, currency, suffix="result"):
    return {
        "usage_id": f"{run_id}:{suffix}",
        "kind": "NONE",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_tokens": 0,
        "provider_calls": 0,
        "cost": {
            "kind": "NONE",
            "value": 0,
            "currency": currency,
            "pricing_version": "no-provider-usage",
        },
    }


def input_schema():
    schema = Scenario.model_json_schema()
    schema["properties"].pop("seed")
    schema["title"] = "FlowOpsContractsInput"
    schema["$id"] = "urn:flowops:scenario:1.0"
    return schema


def schema_document(settings):
    schema = input_schema()
    return validated(
        "simulation-schema",
        {
            "schema_version": "1.0",
            "project_id": "flowops",
            "input_schema_id": schema["$id"],
            "input_schema_sha256": hashlib.sha256(compact(schema)).hexdigest(),
            "input_schema": schema,
            "supported_modes": [MODE],
            "limits": {
                "max_body_bytes": 16384,
                "max_steps": 1024,
                "max_run_seconds": 120,
                "retention_seconds": settings.retention_seconds,
                "max_concurrent_per_session": min(settings.per_session_active, 4),
                "max_concurrent_global": min(settings.max_active, 16),
            },
        },
    )


def run_document(run: Run):
    terminal = run.status in TERMINAL
    return validated(
        "simulation-run",
        {
            "schema_version": "1.0",
            "project_id": "flowops",
            "run_id": run.id,
            "mode": MODE,
            "status": run.status,
            "created_at": utc(run.created_at),
            "completed_at": utc(run.completed_at) if terminal else None,
            "expires_at": utc(run.expires_at),
            "event_count": run.event_count,
            "last_sequence": run.event_count,
            "result_ref": f"/api/projects/flowops/simulations/{run.id}/result"
            if terminal
            else None,
            "provenance": metadata(run)["provenance"],
            "extensions": {
                "x_business_outcome": run.outcome,
                "x_client_correlation_id": metadata(run)["client_correlation_id"],
            },
        },
    )


def evidence_content(run):
    research = run.context.get("research", {})
    values = {
        obj["evidence_id"]: obj
        for obj in research.values()
        if isinstance(obj, dict) and "evidence_id" in obj
    }
    policy = run.context.get("policy", {}).get("policy")
    if policy:
        values[policy["evidence_id"]] = policy
    return values


def evidence(run, ids=None):
    contents = evidence_content(run)
    return [
        {
            "evidence_id": key,
            "origin": "FIXTURE",
            "reference": f"simulations/{run.id}/evidence/{key}",
            "sha256": hashlib.sha256(compact(value)).hexdigest(),
        }
        for key, value in sorted(contents.items())
        if ids is None or key in ids
    ]


def event_document(run, raw):
    technical = raw["technical"]
    decision = technical.get("decision", {})
    decisions = [str(decision["code"])] if isinstance(decision, dict) and "code" in decision else []
    if "code" in technical:
        decisions.append(technical["code"])
    data = {
        key: raw[key]
        for key in (
            "schema_version",
            "project_id",
            "run_id",
            "event_id",
            "sequence",
            "step_id",
            "component",
            "status",
            "mode",
            "title",
            "overview",
            "trace_id",
        )
    }
    data.update(
        occurred_at=utc(raw["occurred_at"]),
        step_type=STEP_TYPES[raw["step_type"]],
        duration_ms=raw["duration_ms"],
        provenance=metadata(run)["provenance"],
        usage=usage(run.id, run.input["currency"], f"step-{raw['sequence']}"),
        technical={
            "action": raw["step_id"],
            "input_schema_version": "flowops-input-1.0",
            "output_schema_version": "flowops-step-1.0",
            "input_ref": run.id,
            "output_ref": raw["event_id"],
            "decisions": decisions,
            "evidence": evidence(run),
            "adapter_kind": "DETERMINISTIC",
            "attempt": max(1, technical.get("attempt", 1)),
            "fallback_reason": technical.get("runtime", {}).get("reason"),
        },
    )
    if raw["status"] == "FAILED":
        data["error"] = error("SERVICE_UNAVAILABLE", 503, metadata(run)["client_correlation_id"])
    return validated("simulation-event", data)


def result_document(run):
    if run.status not in TERMINAL:
        raise ContractError("Pending workflow has no terminal result")
    source = run.result
    assert source is not None
    elapsed = (run.completed_at - run.created_at).total_seconds() * 1000
    output = {**source["output"]}
    output.pop("safety_status", None)
    codes = [e["code"] for e in source["errors"]]
    value = {
        "schema_version": "1.0",
        "project_id": "flowops",
        "run_id": run.id,
        "mode": MODE,
        "status": run.status,
        "app_version": metadata(run)["provenance"]["app_version"],
        "fixtures_version": FIXTURES_VERSION,
        "provenance": metadata(run)["provenance"],
        "output_schema_version": "flowops-result-1.0",
        "output": output,
        "safety_status": "BLOCKED"
        if run.outcome == "BLOCKED"
        else ("SAFE" if run.outcome == "SIMULATED_APPLIED" else "REVIEW_REQUIRED"),
        "evidence": evidence(run),
        "errors": []
        if run.status == "SUCCEEDED"
        else [
            error(
                "SERVICE_UNAVAILABLE" if run.status == "FAILED" else "CONFLICT",
                503 if run.status == "FAILED" else 409,
                metadata(run)["client_correlation_id"],
            )
        ],
        "warnings": codes[:16] if run.status == "SUCCEEDED" else [],
        "limitations": source["output"]["limitations"],
        "duration_ms": round(elapsed, 3),
        "usage": usage(run.id, run.input["currency"]),
        "cost": usage(run.id, run.input["currency"])["cost"],
        "extensions": {"x_client_correlation_id": metadata(run)["client_correlation_id"]},
    }
    if run.context.get("peer_inference"):
        value["extensions"]["x_peer_inference_outcome"] = run.context["peer_inference"]["route"][
            "outcome"
        ]
    if run.context.get("peer_knowledge"):
        lookup = run.context["peer_knowledge"]
        value["extensions"]["x_peer_knowledge_status"] = (
            lookup["response"]["status"] if lookup["response"] else "UNAVAILABLE"
        )
    if "timeout_observed_at" in run.context:
        value["extensions"]["x_timeout_observed_at"] = run.context["timeout_observed_at"]
    return validated("simulation-result", value)
