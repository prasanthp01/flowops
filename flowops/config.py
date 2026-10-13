"""Configuration is operator-owned. No browser-supplied connector settings."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


def secret(name: str) -> str:
    file = os.getenv(f"{name}_FILE")
    return Path(file).read_text().strip() if file else os.getenv(name, "")


@dataclass(frozen=True)
class Settings:
    database_url: str = field(repr=False)
    signing_key: str = field(repr=False)
    environment: str = "local"
    origin: str = "http://127.0.0.1:8765"
    operator_token: str = field(default="", repr=False)
    operator_actor: str = "local-operator"
    max_active: int = 64
    per_session_active: int = 3
    lease_seconds: int = 15
    run_seconds: int = 60
    retention_seconds: int = 3600
    approval_seconds: int = 600
    retry_base: float = 0.4
    api_prefix: str = "/api/projects/flowops"
    source_revision: str = ""
    contracts_tenant_id: str = "flowops-sandbox"
    service_credentials_file: str = field(default="", repr=False)
    peer_profiles_file: str = field(default="", repr=False)

    def __post_init__(self):
        if not self.database_url.startswith("postgresql+psycopg://"):
            raise ValueError("PostgreSQL with psycopg is required")
        if len(self.signing_key) < 32:
            raise ValueError("FLOWOPS_SIGNING_KEY must contain at least 32 characters")
        if self.environment not in {"local", "test", "staging", "production"}:
            raise ValueError("Unsupported environment")
        if self.environment != "local" and self.environment != "test":
            if not self.origin.startswith("https://"):
                raise ValueError("HTTPS origin required outside local/test")
        if self.operator_token and len(self.operator_token) < 32:
            raise ValueError("Operator token must contain at least 32 characters")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}", self.contracts_tenant_id):
            raise ValueError("Invalid configured contract tenant")

    @classmethod
    def load(cls):
        return cls(
            database_url=secret("FLOWOPS_DATABASE_URL"),
            signing_key=secret("FLOWOPS_SIGNING_KEY"),
            environment=os.getenv("FLOWOPS_ENVIRONMENT", "local"),
            origin=os.getenv("FLOWOPS_ORIGIN", "http://127.0.0.1:8765"),
            operator_token=secret("FLOWOPS_OPERATOR_TOKEN"),
            source_revision=os.getenv("FLOWOPS_SOURCE_REVISION", ""),
            contracts_tenant_id=os.getenv("FLOWOPS_CONTRACTS_TENANT_ID", "flowops-sandbox"),
            service_credentials_file=os.getenv("FLOWOPS_SERVICE_CREDENTIALS_FILE", ""),
            peer_profiles_file=os.getenv("FLOWOPS_PEER_PROFILES_FILE", ""),
        )
