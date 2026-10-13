import json
from pathlib import Path

from flowops.contracts import (
    ApprovalDecision,
    Handoff,
    OperationsOutput,
    PolicyOutput,
    Proposal,
    ResearchOutput,
    Scenario,
    StartRequest,
    SupervisorOutput,
    VerifierOutput,
)
from flowops.runtime import InferenceRequest

MODELS = {
    "flowops-input-v1": Scenario,
    "flowops-start-v1": StartRequest,
    "flowops-handoff-v1": Handoff,
    "flowops-proposal-v1": Proposal,
    "flowops-approval-decision-v1": ApprovalDecision,
    "flowops-inference-request-v1": InferenceRequest,
    "supervisor-output-v1": SupervisorOutput,
    "research-output-v1": ResearchOutput,
    "policy-output-v1": PolicyOutput,
    "operations-output-v1": OperationsOutput,
    "verifier-output-v1": VerifierOutput,
}
for name, model in MODELS.items():
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        **model.model_json_schema(),
    }
    Path("schemas", name + ".schema.json").write_text(json.dumps(schema, indent=2) + "\n")
print(f"Exported {len(MODELS)} schemas.")
