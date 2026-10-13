"""Provider-neutral inference boundary. No paid inference is enabled in the public app.

AgentOps request metadata matches the supplied v1 schema. Content is carried in a
documented outer envelope because that schema has no content field. Integration with
an actual AgentOps installation requires verification of its endpoint/envelope.
"""

from typing import Literal, Protocol

from pydantic import Field

from .contracts import StrictModel


class InferenceRequest(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    application_id: Literal["flowops"] = "flowops"
    environment: str
    task_type: str
    required_capabilities: list[str]
    data_classification: Literal["PUBLIC"] = "PUBLIC"
    allowed_regions: list[str] = Field(min_length=1)
    latency_deadline_ms: int = Field(ge=1, le=5000)
    max_cost: float = Field(ge=0, le=1)
    currency: Literal["EUR", "USD"] = "EUR"
    routing_policy_version: str
    correlation_id: str
    operation_id: str
    mode: Literal["LIVE_MODEL_AUTHENTICATED"] = "LIVE_MODEL_AUTHENTICATED"


class RuntimeResult(StrictModel):
    status: Literal["AI_UNAVAILABLE", "POLICY_BLOCKED", "OK"]
    origin: Literal["deterministic_role_adapter", "mock_provider", "live_provider"]
    reason: str
    calls: int = Field(ge=0)
    actual_external_cost: float = Field(ge=0)


class ModelRuntime(Protocol):
    def analyze(self, operation_id: str, fault: str) -> RuntimeResult: ...


class SafeRuntime:
    def analyze(self, operation_id: str, fault: str) -> RuntimeResult:
        if fault == "deterministic":
            return RuntimeResult(
                status="OK",
                origin="deterministic_role_adapter",
                reason="Declared rule-based plan; no model call.",
                calls=0,
                actual_external_cost=0.0,
            )
        return RuntimeResult(
            status="AI_UNAVAILABLE",
            origin="mock_provider",
            reason=(
                "Mock provider unavailable; use deterministic workflow and mandatory review."
                if fault == "provider_outage"
                else "Mock structured output rejected; use deterministic workflow and mandatory review."
            ),
            calls=0,
            actual_external_cost=0.0,
        )
