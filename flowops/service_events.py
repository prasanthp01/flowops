"""Authenticated service inbox and transactional failure outbox. No business approvals."""

import hashlib
import hmac
import json
from pathlib import Path

import rfc8785
from fastapi import HTTPException
from sqlalchemy import func, select

from ._vendor.ecosystem_contracts import ContractError
from ._vendor.ecosystem_contracts.signatures import verify_event
from .db import Run, ServiceInbox, ServiceOutbox
from .security import guard, rate
from .wire import MODE, evidence, metadata, validated


def environment(value):
    return "development" if value == "local" else value


def logical_content(event):
    return {
        key: value
        for key, value in event.items()
        if key not in {"event_id", "occurred_at", "integrity"}
    }


def logical_digest(event):
    return hashlib.sha256(rfc8785.dumps(logical_content(event))).hexdigest()


def dedup_key(event):
    return hashlib.sha256(
        rfc8785.dumps(
            [
                event["source"]["application_id"],
                event["source"]["environment"],
                event["source"]["tenant_id"],
                event["audience"],
                event["idempotency_key"],
            ]
        )
    ).hexdigest()


def reserve_failure(db, run, code):
    """Called inside the same transaction as the failure and final audit event."""
    operation = "workflow-failed:" + run.id
    if db.get(ServiceOutbox, operation):
        return
    meta = metadata(run)
    logical = {
        "schema_version": "1.0",
        "source": {
            "application_id": "flowops",
            "environment": environment(run.environment),
            "tenant_id": meta["tenant_id"],
        },
        "audience": "resolveai",
        "correlation_id": meta["client_correlation_id"],
        "trace_id": run.id,
        "sensitivity": "PUBLIC",
        "idempotency_key": operation,
        "kind": "workflow.execution.failed.v1",
        "mode": MODE,
        "provenance": meta["provenance"],
        "payload": {
            "workflow_id": run.id,
            "execution_id": run.id,
            "failure_code": code,
            "severity": 2,
            "evidence": evidence(run),
        },
    }
    db.add(ServiceOutbox(operation_id=operation, logical_event=logical))


class ServiceReceiver:
    def __init__(self, settings, sessions):
        self.settings, self.sessions = settings, sessions
        self.credentials = []
        if settings.service_credentials_file:
            data = json.loads(Path(settings.service_credentials_file).read_text())
            if not isinstance(data, list) or len(data) > 16:
                raise ValueError("Invalid service credential configuration")
            for entry in data:
                if set(entry) != {
                    "token_sha256",
                    "application_id",
                    "environment",
                    "tenant_id",
                    "scopes",
                    "keys",
                }:
                    raise ValueError("Invalid service credential configuration")
                if (
                    len(bytes.fromhex(entry["token_sha256"])) != 32
                    or not entry["keys"]
                    or len(entry["keys"]) > 4
                ):
                    raise ValueError("Invalid service credential or key configuration")
                if any(len(bytes.fromhex(value)) < 32 for value in entry["keys"].values()):
                    raise ValueError("Service signing keys require at least 32 random bytes")
            self.credentials = data

    def authenticate(self, authorization):
        token = authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        hashed = hashlib.sha256(token.encode()).hexdigest()
        for credential in self.credentials:
            if token and hmac.compare_digest(hashed, credential["token_sha256"]):
                return credential
        raise HTTPException(401, "UNAUTHENTICATED")

    def receive(self, event, authorization):
        credential = self.authenticate(authorization)
        try:
            validated("cross-project-event", event)
        except ContractError:
            raise HTTPException(422, "VALIDATION_ERROR") from None
        if (
            credential["application_id"] != "resolveai"
            or "workflow:read" not in credential["scopes"]
            or credential["environment"] != environment(self.settings.environment)
            or credential["tenant_id"] != self.settings.contracts_tenant_id
            or event["kind"] != "incident.investigation.completed.v1"
            or event["mode"] != MODE
        ):
            raise HTTPException(403, "FORBIDDEN")
        key = credential["keys"].get(event["integrity"]["key_id"])
        if key is None:
            raise HTTPException(403, "FORBIDDEN")
        try:
            verify_event(
                event,
                bytes.fromhex(key),
                audience="flowops",
                source_app="resolveai",
                environment=credential["environment"],
                tenant_id=credential["tenant_id"],
            )
        except ContractError:
            raise HTTPException(403, "FORBIDDEN") from None
        key, checksum = dedup_key(event), logical_digest(event)
        with self.sessions.begin() as db:
            guard(db)
            rate(db, "service:" + credential["token_sha256"], 120)
            existing = db.get(ServiceInbox, key)
            if existing:
                if not hmac.compare_digest(existing.logical_digest, checksum):
                    raise HTTPException(409, "CONFLICT")
                status = "DUPLICATE"
            else:
                run = db.scalar(
                    select(Run)
                    .where(
                        Run.id == event["trace_id"], Run.environment == self.settings.environment
                    )
                    .with_for_update()
                )
                if (
                    not run
                    or not run.context.get("contracts_v1")
                    or metadata(run)["tenant_id"] != credential["tenant_id"]
                    or metadata(run)["client_correlation_id"] != event["correlation_id"]
                ):
                    raise HTTPException(404, "NOT_FOUND")
                if db.scalar(select(func.count()).select_from(ServiceInbox)) >= 25000:
                    raise HTTPException(429, "RATE_LIMITED", headers={"Retry-After": "60"})
                # The annotation IS the domain work. No workflow state, outcome,
                # approval, receipt or action is changed by a remote finding.
                db.add(
                    ServiceInbox(dedup_key=key, logical_digest=checksum, run_id=run.id, event=event)
                )
                status = "ACCEPTED"
        return validated(
            "event-receipt",
            {
                "schema_version": "1.0",
                "event_id": event["event_id"],
                "status": status,
                "correlation_id": event["correlation_id"],
            },
        )
