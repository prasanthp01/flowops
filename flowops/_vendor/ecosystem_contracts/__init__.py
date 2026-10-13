"""Published schemas are the source of truth; no network resolution is permitted."""

from .validation import ContractError, SchemaRegistry, validate_handoff, validate_stream

__all__ = ["ContractError", "SchemaRegistry", "validate_handoff", "validate_stream"]
__version__ = "1.0.0"
