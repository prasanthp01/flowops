"""Publish the pinned contracts for implemented routes, plus project extensions."""

import json
from pathlib import Path

from fastapi.openapi.utils import get_openapi


def document(app):
    if app.openapi_schema:
        return app.openapi_schema
    generated = get_openapi(title=app.title, version=app.version, routes=app.routes)
    frozen = json.loads(
        (Path(__file__).parents[1] / "contracts-v1/openapi/ecosystem-v1.json").read_text()
    )
    for path, operations in frozen["paths"].items():
        if path in generated["paths"]:
            generated["paths"][path] = operations
    generated.setdefault("components", {}).setdefault("schemas", {}).update(
        frozen["components"]["schemas"]
    )
    generated["components"]["securitySchemes"] = frozen["components"]["securitySchemes"]
    generated["servers"] = [{"url": "/api/projects/flowops"}]
    generated["info"]["description"] = (
        "FlowOps canonical boundary: Contracts 1.0.0, wire 1.0. Separate local adoption entrypoint; public v0.1.0 remains legacy. Peer query/inference routes are clients, not served endpoints."
    )
    app.openapi_schema = generated
    return generated
