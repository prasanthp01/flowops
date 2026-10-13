"""RFC 8785 canonical signatures with source-bound key lookup by the caller."""

from __future__ import annotations

import copy
import hashlib
import hmac
from datetime import datetime, timezone

import rfc8785

from .validation import ContractError, SchemaRegistry


def signing_bytes(event: dict) -> bytes:
    value = copy.deepcopy(event)
    value["integrity"].pop("digest", None)
    return rfc8785.dumps(value)


def sign_event(event: dict, key: bytes) -> dict:
    if len(key) < 32:
        raise ContractError("Signing keys must contain at least 32 bytes")
    result = copy.deepcopy(event)
    result["integrity"]["digest"] = hmac.new(key, signing_bytes(result), hashlib.sha256).hexdigest()
    return result


def verify_event(
    event: dict,
    key: bytes,
    *,
    audience: str,
    source_app: str,
    environment: str,
    tenant_id: str,
    now: datetime | None = None,
) -> None:
    SchemaRegistry().validate("cross-project-event", event)
    source = event["source"]
    if (
        event["audience"],
        source["application_id"],
        source["environment"],
        source["tenant_id"],
    ) != (audience, source_app, environment, tenant_id):
        raise ContractError("Event identity mismatch")
    expected = sign_event(event, key)["integrity"]["digest"]
    if not hmac.compare_digest(expected, event["integrity"]["digest"]):
        raise ContractError("Invalid event signature")
    clock = now or datetime.now(timezone.utc)
    if (
        abs(
            (
                clock - datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00"))
            ).total_seconds()
        )
        > 300
    ):
        raise ContractError("Event timestamp outside replay window")
