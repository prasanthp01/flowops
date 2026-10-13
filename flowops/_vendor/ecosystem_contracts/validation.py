from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource


class ContractError(ValueError):
    """Safe error: never includes an input value or raw validator diagnostics."""


class SchemaRegistry:
    def __init__(self, directory: Path | None = None):
        if directory is None:
            directory = Path(__file__).parent / "schemas/v1"
            if not directory.is_dir():
                directory = Path(__file__).resolve().parents[2] / "schemas/v1"
        self.schemas = {
            p.name.removesuffix(".schema.json"): json.loads(p.read_text())
            for p in sorted(directory.glob("*.schema.json"))
        }
        if not self.schemas:
            raise ContractError("Contract schemas are unavailable")
        registry = Registry().with_resources(
            (schema["$id"], Resource.from_contents(schema)) for schema in self.schemas.values()
        )
        self.validators = {}
        for name, schema in self.schemas.items():
            Draft202012Validator.check_schema(schema)
            self.validators[name] = Draft202012Validator(
                schema, registry=registry, format_checker=FormatChecker()
            )

    def validate(self, name: str, data: Any) -> None:
        if name not in self.validators or name == "common":
            raise ContractError("Unknown contract")

        def bounds(value, depth=0):
            if depth > 12:
                raise ContractError("Payload nesting exceeds limit")
            if isinstance(value, dict):
                for child in value.values():
                    bounds(child, depth + 1)
            elif isinstance(value, list):
                for child in value:
                    bounds(child, depth + 1)

        bounds(data)
        try:
            if len(json.dumps(data, allow_nan=False).encode()) > 262144:
                raise ContractError("Payload exceeds size limit")
        except (ValueError, TypeError):
            raise ContractError("Payload is not bounded JSON") from None
        try:
            if next(self.validators[name].iter_errors(data), None) is not None:
                raise ContractError(f"Invalid {name} payload")
        except ContractError:
            raise
        except Exception:
            raise ContractError("Contract reference could not be resolved") from None
        validate_semantics(name, data)


def validate_semantics(name: str, data: dict) -> None:
    """Cross-field checks; authorization and PII redaction belong to the consuming service."""
    mode = data.get("mode")
    prov = data.get("provenance")
    if prov and mode:
        expected = {
            "SANDBOX_DETERMINISTIC": ("SYNTHETIC", "MOCK"),
            "REPLAY_RECORDED": ("RECORDED_SYNTHETIC", "REPLAY"),
            "LIVE_MODEL_AUTHENTICATED": ("AUTHORIZED_PRIVATE", "LIVE_READ_ONLY"),
        }[mode]
        if (prov["data_origin"], prov["connector_kind"]) != expected:
            raise ContractError("Mode and provenance disagree")
    if mode == "SANDBOX_DETERMINISTIC":
        usage = data.get("usage") or data.get("payload", {}).get("usage")
        cost = data.get("cost")
        if usage and (usage["kind"] not in ("SIMULATED", "NONE") or usage["provider_calls"]):
            raise ContractError("Sandbox cannot report actual provider execution")
        if cost and cost["kind"] not in ("SIMULATED", "NONE"):
            raise ContractError("Sandbox cannot report actual cost")
    usage = data.get("usage")
    if usage and usage["kind"] != usage["cost"]["kind"]:
        raise ContractError("Usage and cost markers disagree")
    if name == "simulation-result":
        if data["cost"] != data["usage"]["cost"]:
            raise ContractError("Result and usage cost disagree")
        if (
            data["app_version"] != prov["app_version"]
            or data["fixtures_version"] != prov["fixtures_version"]
        ):
            raise ContractError("Result versions disagree")
        if data["status"] == "SUCCEEDED" and data["errors"]:
            raise ContractError("Successful result cannot contain errors")
    if name == "simulation-run" and data["event_count"] != data["last_sequence"]:
        raise ContractError("Run sequence count disagrees")


def validate_stream(events: list[dict], *, run_id: str, project_id: str, after: int = 0) -> None:
    registry = SchemaRegistry()
    ids: set[str] = set()
    for sequence, event in enumerate(events, after + 1):
        registry.validate("simulation-event", event)
        if event["run_id"] != run_id or event["project_id"] != project_id:
            raise ContractError("Event scope mismatch")
        if event["sequence"] != sequence or event["event_id"] in ids:
            raise ContractError("Event sequence is discontinuous or duplicated")
        ids.add(event["event_id"])


def validate_handoff(data: dict, *, granted_scopes: set[str], tenant_id: str) -> None:
    SchemaRegistry().validate("agent-handoff", data)
    if data["tenant_id"] != tenant_id or not set(data["authorized_tool_scopes"]) <= granted_scopes:
        raise ContractError("Delegation exceeds caller scope")
