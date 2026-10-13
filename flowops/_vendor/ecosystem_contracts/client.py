"""Bounded synchronous example. Never retries a mutation automatically."""

from __future__ import annotations

import json
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .validation import ContractError, SchemaRegistry


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SimulationClient:
    def __init__(self, base_url: str, project: str, session_token: str, *, allow_loopback=False):
        parts = urlparse(base_url)
        local = parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1")
        if parts.username or parts.password or parts.query or parts.fragment or not parts.hostname:
            raise ValueError("Invalid base URL")
        if parts.scheme != "https" and not (allow_loopback and local):
            raise ValueError("HTTPS required except explicit local tests")
        if project not in ("resolveai", "flowops", "knowledgeops", "agentops"):
            raise ValueError("Unknown project")
        self.base = base_url.rstrip("/") + f"/api/projects/{project}"
        self.token = session_token
        self.registry = SchemaRegistry()

    def request(self, path: str, response_schema: str, *, payload=None, request_schema=None):
        if not path.startswith("/") or ".." in path or "://" in path:
            raise ValueError("Expected a local API path")
        if payload is not None:
            if request_schema is None:
                raise ValueError("Request schema is required")
            self.registry.validate(request_schema, payload)
        body = None if payload is None else json.dumps(payload, allow_nan=False).encode()
        if body and len(body) > 65536:
            raise ContractError("Request too large")
        req = Request(
            self.base + path,
            data=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with build_opener(NoRedirect()).open(req, timeout=10) as response:
                data = response.read(262145)
        except HTTPError as error:
            raise ContractError(f"API returned HTTP {error.code}") from None
        if len(data) > 262144:
            raise ContractError("Response too large")
        try:
            result = json.loads(data)
        except (ValueError, UnicodeError):
            raise ContractError("Invalid JSON response") from None
        self.registry.validate(response_schema, result)
        return result
