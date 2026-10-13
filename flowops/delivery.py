"""Opt-in bounded outbox sender; network destinations and credentials are operator-owned."""

import re
import time
import uuid
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import or_, select

from ._vendor.ecosystem_contracts import ContractError
from ._vendor.ecosystem_contracts.signatures import sign_event
from .db import ServiceOutbox, now
from .wire import utc, validated


class FailureDelivery:
    def __init__(
        self, sessions, *, url, bearer, signing_key, key_id, environment, tenant_id, transport=None
    ):
        parsed = urlsplit(url)
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not parsed.hostname
            or parsed.scheme not in {"https", "http"}
            or (parsed.scheme == "http" and environment not in {"test", "development"})
            or len(signing_key) < 32
            or len(bearer) < 32
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}", key_id)
        ):
            raise ValueError("Invalid operator-owned delivery configuration")
        self.sessions, self.url = sessions, url
        self.bearer, self.key, self.key_id = bearer, signing_key, key_id
        self.environment, self.tenant_id, self.transport = environment, tenant_id, transport

    def claim(self):
        with self.sessions.begin() as db:
            row = db.scalar(
                select(ServiceOutbox)
                .where(
                    ServiceOutbox.status == "PENDING",
                    ServiceOutbox.next_attempt_at <= now(),
                    ServiceOutbox.logical_event["source"]["environment"].astext == self.environment,
                    ServiceOutbox.logical_event["source"]["tenant_id"].astext == self.tenant_id,
                    or_(ServiceOutbox.lease_until.is_(None), ServiceOutbox.lease_until < now()),
                )
                .order_by(ServiceOutbox.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if not row:
                return None
            if row.attempts >= 5 or row.created_at + timedelta(days=1) <= now():
                row.status, row.last_code = "DEAD_LETTER", "DELIVERY_EXHAUSTED"
                return None
            row.attempts += 1
            row.claim_version += 1
            row.lease_until = now() + timedelta(seconds=15)
            return row.operation_id, row.claim_version, row.logical_event

    def settle(self, operation, version, code, *, retry_after=0):
        with self.sessions.begin() as db:
            row = db.scalar(
                select(ServiceOutbox)
                .where(ServiceOutbox.operation_id == operation)
                .with_for_update()
            )
            if (
                not row
                or row.claim_version != version
                or not row.lease_until
                or row.lease_until <= now()
            ):
                return False
            row.last_code, row.lease_until = code, None
            if code == "ACKNOWLEDGED":
                row.status = "DELIVERED"
            elif code == "REJECTED" or row.attempts >= 5:
                row.status = "DEAD_LETTER"
            else:
                row.next_attempt_at = now() + timedelta(
                    seconds=max(min(3600, retry_after), min(60, 2**row.attempts))
                )
            return True

    def deliver_once(self):
        claim = self.claim()
        if not claim:
            return False
        operation, version, logical = claim
        event = sign_event(
            {
                **logical,
                "event_id": str(uuid.uuid4()),
                "occurred_at": utc(now()),
                "integrity": {"algorithm": "HMAC-SHA256", "key_id": self.key_id},
            },
            self.key,
        )
        validated("cross-project-event", event)
        try:
            started = time.monotonic()
            with httpx.Client(
                transport=self.transport, timeout=2, follow_redirects=False
            ) as client:
                with client.stream(
                    "POST", self.url, headers={"Authorization": "Bearer " + self.bearer}, json=event
                ) as response:
                    data = bytearray()
                    for chunk in response.iter_bytes():
                        data.extend(chunk)
                        if len(data) > 262144 or time.monotonic() - started > 8:
                            raise ContractError("Oversized service response")
                    if response.status_code == 202:
                        import json

                        receipt = validated("event-receipt", json.loads(data))
                        if (
                            receipt["event_id"] != event["event_id"]
                            or receipt["correlation_id"] != event["correlation_id"]
                        ):
                            raise ContractError("Receipt identity mismatch")
                        self.settle(operation, version, "ACKNOWLEDGED")
                    elif response.status_code in {429, 503}:
                        after = response.headers.get("retry-after", "0")
                        self.settle(
                            operation,
                            version,
                            "RETRYABLE",
                            retry_after=int(after) if after.isdigit() else 0,
                        )
                    else:
                        self.settle(operation, version, "REJECTED")
        except (httpx.HTTPError, ContractError, ValueError):
            self.settle(operation, version, "UNCERTAIN")
        return True


def main():
    """Explicit operator invocation only; never started by the default worker."""
    import argparse
    import json
    import os
    from pathlib import Path

    from .config import Settings
    from .db import database

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    settings = Settings.load()
    # A mounted private file, never request data or CLI secret arguments.
    config = json.loads(Path(os.environ["FLOWOPS_DELIVERY_CONFIG_FILE"]).read_text())
    engine, sessions = database(settings)
    delivery = FailureDelivery(
        sessions,
        **{k: config[k] for k in ("url", "bearer", "key_id")},
        signing_key=bytes.fromhex(config["signing_key_hex"]),
        environment="development" if settings.environment == "local" else settings.environment,
        tenant_id=settings.contracts_tenant_id,
    )
    try:
        while True:
            worked = delivery.deliver_once()
            if args.once:
                return
            if not worked:
                time.sleep(1)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
