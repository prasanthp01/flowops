"""Operator-owned, per-run peer opt-in. Peer content has no business authority."""

import hashlib
import json
from pathlib import Path
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ._vendor.ecosystem_contracts import ContractError
from .interop import PeerClient, PeerGrant, PeerResponseError, deterministic_refusal
from .service_events import environment
from .wire import compact, error, metadata


class Connection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    base_url: str = Field(max_length=256)
    bearer: str = Field(min_length=32, max_length=512, repr=False)
    allow_local_test: bool = False

    def client(self):
        return PeerClient(self.base_url, self.bearer, allow_local_test=self.allow_local_test)


class KnowledgeConnection(Connection):
    collection_ids: list[str] = Field(min_length=1, max_length=4)
    query: str = Field(min_length=3, max_length=500)


class PeerProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    profile_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
    environment: Literal["development", "test"]
    tenant_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")
    synthetic_only: Literal[True]
    knowledge: KnowledgeConnection | None = None
    inference: Connection | None = None

    def fingerprint(self):
        value = self.model_dump()
        for name in ("knowledge", "inference"):
            if value[name]:
                value[name].pop(
                    "bearer"
                )  # Credential rotation does not alter the authorized scope.
        return hashlib.sha256(compact(value)).hexdigest()


class PeerWorkflow:
    def __init__(self, settings):
        self.settings = settings

    def profiles(self):
        if not self.settings.peer_profiles_file:
            return {}
        path = Path(self.settings.peer_profiles_file)
        if path.stat().st_mode & 0o077 or path.stat().st_size > 32768:
            raise PermissionError("Peer configuration must be private and bounded")
        value = json.loads(path.read_text())
        if not isinstance(value, list) or len(value) > 8:
            raise ValueError("Invalid peer profiles")
        profiles = {}
        for item in value:
            profile = PeerProfile.model_validate(item)
            if profile.profile_id in profiles or not (profile.knowledge or profile.inference):
                raise ValueError("Invalid peer profiles")
            for connection in (profile.knowledge, profile.inference):
                if connection:
                    connection.client()  # Validate fixed destination before accepting a grant.
            profiles[profile.profile_id] = profile
        return profiles

    def snapshot(self, profile):
        if (
            not profile
            or profile.environment != environment(self.settings.environment)
            or profile.tenant_id != self.settings.contracts_tenant_id
        ):
            raise PermissionError("Peer profile authorization denied")
        return {
            "profile_id": profile.profile_id,
            "fingerprint": profile.fingerprint(),
            "tenant_id": profile.tenant_id,
            "environment": profile.environment,
            "authorized_by": self.settings.operator_actor,
        }

    def authorize(self, profile_id):
        return self.snapshot(self.profiles().get(profile_id))

    def configured(self, run):
        snapshot = run.context.get("peer_authorization")
        if not snapshot:
            return None
        profile = self.profiles().get(snapshot["profile_id"])
        current = self.snapshot(profile)
        if current != snapshot or metadata(run)["tenant_id"] != current["tenant_id"]:
            raise PermissionError("Peer profile revoked or changed")
        return profile

    def inference(self, run):
        if not run.context.get("peer_authorization"):
            return None
        try:
            profile = self.configured(run)
            if not profile.inference:
                return None
            grant = PeerGrant(run.id, profile.tenant_id, frozenset({"inference:request"}))
            route = profile.inference.client().inference(run, grant)
            return {
                "route": route,
                "error": None,
                "business_authority": "NONE",
                "usage_confirmation": "PEER_REPORTED",
            }
        except (
            PeerResponseError,
            PermissionError,
            ContractError,
            httpx.HTTPError,
            ValueError,
            OSError,
        ) as exc:
            return deterministic_refusal(run, exc)

    def knowledge(self, run):
        if not run.context.get("peer_authorization"):
            return None
        try:
            profile = self.configured(run)
            if not profile.knowledge:
                return None
            connection = profile.knowledge
            grant = PeerGrant(
                run.id,
                profile.tenant_id,
                frozenset({"knowledge:read"}),
                frozenset(connection.collection_ids),
            )
            result = connection.client().knowledge(
                run, grant, connection.collection_ids, query=connection.query
            )
            return {"response": result, "error": None, "business_authority": "NONE"}
        except (
            PeerResponseError,
            PermissionError,
            ContractError,
            httpx.HTTPError,
            ValueError,
            OSError,
        ) as exc:
            if isinstance(exc, PeerResponseError):
                value = error(
                    exc.error["code"], exc.error["http_status"], exc.error["correlation_id"]
                )
            else:
                code, status = (
                    ("FORBIDDEN", 403)
                    if isinstance(exc, PermissionError)
                    else ("SERVICE_UNAVAILABLE", 503)
                )
                value = error(code, status, metadata(run)["client_correlation_id"])
            return {"response": None, "error": value, "business_authority": "NONE"}
