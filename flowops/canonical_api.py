"""Contracts 1.0.0 API over the existing durable FlowOps domain model.

Separate entrypoint for private adoption/cutover. The deployed legacy API stays
available from flowops.api:create_app until an explicit coordinated cutover.
"""

import asyncio
import hashlib
import hmac
import re
import time
import uuid
from datetime import timedelta
from typing import cast

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError
from sqlalchemy import delete, func, select, text
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import wire
from ._vendor.ecosystem_contracts import ContractError
from .config import Settings
from .contracts import FIXTURES_VERSION, MODE, PRESET_IDS, PRESETS, ApprovalDecision, Scenario
from .db import Event, Run, ServiceInbox, ServiceOutbox, Session, StreamSlot, database, now
from .security import COOKIE, BoundaryMiddleware, csrf, digest, guard, mint, rate, verify_token
from .service_events import ServiceReceiver
from .workflow import TERMINAL, terminate


def create_app(settings: Settings | None = None):
    settings = settings or Settings.load()
    wire.provenance(settings.source_revision)  # Missing release provenance fails closed at startup.
    wire.schema_document(settings)  # Invalid configured limits also fail closed.
    engine, sessions = database(settings)
    app = FastAPI(
        title="FlowOps Contracts 1.0.0 adapter", version="1.0.0", docs_url=None, redoc_url=None
    )
    app.state.settings, app.state.engine, app.state.sessions = settings, engine, sessions
    app.add_middleware(BoundaryMiddleware, origin=settings.origin, canonical=True)
    from .peer_workflow import PeerWorkflow

    peers = PeerWorkflow(settings)
    peers.profiles()
    receiver = ServiceReceiver(settings, sessions)
    app.state.service_receiver = receiver
    router = APIRouter()

    def correlation(request):
        return getattr(request.state, "correlation_id", str(uuid.uuid4()))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        status = (
            exc.status_code
            if exc.status_code in {401, 403, 404, 409, 410, 413, 422, 429, 503}
            else 503
        )
        if exc.status_code == 405:
            status = 404
        codes = {
            401: "UNAUTHENTICATED",
            403: "FORBIDDEN",
            404: "NOT_FOUND",
            409: "CONFLICT",
            410: "RUN_EXPIRED",
            413: "PAYLOAD_TOO_LARGE",
            422: "VALIDATION_ERROR",
            429: "RATE_LIMITED",
            503: "SERVICE_UNAVAILABLE",
        }
        code = "INVALID_CURSOR" if exc.detail == "INVALID_CURSOR" else codes[status]
        retry = (exc.headers or {}).get("Retry-After")
        return JSONResponse(
            wire.error(code, status, correlation(request), retry_after=retry),
            status,
            headers=exc.headers,
        )

    @app.exception_handler(RequestValidationError)
    @app.exception_handler(ValidationError)
    async def invalid(request, exc):
        return JSONResponse(wire.error("VALIDATION_ERROR", 422, correlation(request)), 422)

    @app.exception_handler(ContractError)
    async def contract_failure(request, exc):
        return JSONResponse(wire.error("SERVICE_UNAVAILABLE", 503, correlation(request)), 503)

    @app.exception_handler(Exception)
    async def unavailable(request, exc):
        return JSONResponse(wire.error("SERVICE_UNAVAILABLE", 503, correlation(request)), 503)

    def origin(request):
        if request.headers.get("origin") != settings.origin:
            raise HTTPException(403, "FORBIDDEN")

    def identity(request: Request):
        authorization = request.headers.get("authorization", "")
        if authorization:
            if not authorization.startswith("Bearer "):
                raise HTTPException(401, "UNAUTHENTICATED")
            value = authorization[7:]
        else:
            value = request.cookies.get(COOKIE, "")
        owner = verify_token(settings, value)
        with sessions.begin() as db:
            session = db.get(Session, owner)
            if not session or session.expires_at <= now():
                raise HTTPException(401, "UNAUTHENTICATED")
            guard(db)
            rate(db, "canonical-reads:" + owner, 600)
        return owner

    def mutation(request: Request, owner: str = Depends(identity)):
        if not request.headers.get("authorization"):
            origin(request)
            if not hmac.compare_digest(
                request.headers.get("x-csrf-token", ""), csrf(settings, owner)
            ):
                raise HTTPException(403, "FORBIDDEN")
        elif request.headers.get("origin") not in {None, settings.origin}:
            raise HTTPException(403, "FORBIDDEN")
        return owner

    def operator(request: Request):
        origin(request)
        supplied = request.headers.get("authorization", "")
        expected = "Bearer " + settings.operator_token
        if not settings.operator_token or not hmac.compare_digest(supplied, expected):
            raise HTTPException(401, "UNAUTHENTICATED")
        return "configured_operator"

    def owned(db, run_id, owner, *, lock=False):
        query = select(Run).where(
            Run.id == run_id, Run.owner_id == owner, Run.environment == settings.environment
        )
        run = db.scalar(query.with_for_update() if lock else query)
        if not run or not run.context.get("contracts_v1"):
            raise HTTPException(404, "NOT_FOUND")
        if run.expires_at <= now():
            raise HTTPException(410, "RUN_EXPIRED")
        return run

    @router.get("/openapi.json", include_in_schema=False)
    def api_spec():
        from .openapi import document

        return document(app)

    @router.get("/health")
    def health():
        with sessions() as db:
            db.execute(text("SELECT 1"))
        return {
            "status": "ok",
            "project": "flowops",
            "contracts_version": "1.0.0",
            "source_revision": settings.source_revision,
        }

    @router.get("/project")
    def project():
        return wire.validated(
            "project-identity",
            {
                "schema_version": "1.0",
                "project_id": "flowops",
                "origin": "https://portfolio.gappazappa.in",
                "case_study_route": "/projects/flowops",
                "api_prefix": settings.api_prefix,
                "app_route": None,
            },
        )

    @router.post("/simulation/session")
    def bootstrap(request: Request):
        origin(request)
        ip = digest(settings.signing_key, request.client.host if request.client else "unknown")
        with sessions.begin() as db:
            guard(db)
            rate(db, "session-ip:" + ip, 120, 3600)
            rate(db, "session-global", 1000, 3600)
            owner, token = mint(db, settings)
            expiry = db.get(Session, owner).expires_at
        response = JSONResponse(
            {
                "session_token": token,
                "csrf_token": csrf(settings, owner),
                "expires_at": wire.utc(expiry),
                "mode": MODE,
            }
        )
        response.set_cookie(
            COOKIE,
            token,
            httponly=True,
            secure=settings.origin.startswith("https://"),
            samesite="strict",
            max_age=settings.retention_seconds,
            path=settings.api_prefix,
        )
        return response

    @router.get("/simulation/schema")
    def schema():
        return wire.schema_document(settings)

    @router.get("/simulation/presets")
    def presets():
        rows = []
        for preset in PRESETS:
            data = cast(dict, preset["input"]).copy()
            seed = data.pop("seed")
            rows.append(
                {
                    "schema_version": "1.0",
                    "project_id": "flowops",
                    "preset_id": preset["id"],
                    "title": preset["title"],
                    "description": preset["description"],
                    "mode": MODE,
                    "seed": seed,
                    "input": data,
                    "fixtures_version": FIXTURES_VERSION,
                }
            )
        return wire.validated(
            "simulation-presets",
            {"schema_version": "1.0", "project_id": "flowops", "presets": rows},
        )

    @router.post("/simulations", status_code=202)
    def start(body: dict, request: Request, owner: str = Depends(mutation)):
        try:
            wire.validated("simulation-start", body)
        except ContractError:
            raise HTTPException(422, "VALIDATION_ERROR") from None
        request.state.correlation_id = body["client_correlation_id"]
        if body["project_id"] != "flowops" or body["mode"] != MODE:
            raise HTTPException(403, "FORBIDDEN")
        if "seed" in body["input"] or body.get("preset_id") not in {*PRESET_IDS, None}:
            raise HTTPException(422, "VALIDATION_ERROR")
        inputs = Scenario.model_validate({**body["input"], "seed": body["seed"]}).model_dump()
        key = request.headers.get("idempotency-key", "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{8,64}", key):
            raise HTTPException(422, "VALIDATION_ERROR")
        peer_authorization = None
        profile_id = body.get("extensions", {}).get("x_peer_profile")
        if profile_id is not None:
            supplied = request.headers.get("x-flowops-operator-token", "")
            if not settings.operator_token or not hmac.compare_digest(
                supplied, settings.operator_token
            ):
                raise HTTPException(403, "FORBIDDEN")
            if not isinstance(profile_id, str):
                raise HTTPException(422, "VALIDATION_ERROR")
            try:
                peer_authorization = peers.authorize(profile_id)
            except (PermissionError, ValueError, OSError):
                raise HTTPException(403, "FORBIDDEN") from None
        checksum = hashlib.sha256(wire.compact(body)).hexdigest()
        with sessions.begin() as db:
            guard(db)
            previous = db.scalar(
                select(Run).where(
                    Run.owner_id == owner,
                    Run.request_key == key,
                    Run.environment == settings.environment,
                )
            )
            if previous:
                if previous.request_digest != checksum or not previous.context.get("contracts_v1"):
                    raise HTTPException(409, "CONFLICT")
                owned(db, previous.id, owner)
                return wire.run_document(previous)
            ip = digest(settings.signing_key, request.client.host if request.client else "unknown")
            rate(db, "runs:" + owner, 30)
            rate(db, "runs-ip:" + ip, 120)
            rate(db, "runs-global", 240)
            active = Run.status.in_(["ACCEPTED", "RUNNING"])
            if (
                db.scalar(select(func.count()).select_from(Run).where(active))
                >= min(16, settings.max_active)
                or db.scalar(
                    select(func.count()).select_from(Run).where(active, Run.owner_id == owner)
                )
                >= min(4, settings.per_session_active)
                or db.scalar(select(func.count()).select_from(ServiceOutbox)) >= 1000
            ):
                raise HTTPException(429, "RATE_LIMITED", headers={"Retry-After": "5"})
            created = now()
            run = Run(
                id=str(uuid.uuid4()),
                owner_id=owner,
                environment=settings.environment,
                request_key=key,
                request_digest=checksum,
                input=inputs,
                created_at=created,
                context={
                    **({"peer_authorization": peer_authorization} if peer_authorization else {}),
                    "contracts_v1": {
                        "version": "1.0.0",
                        "tenant_id": settings.contracts_tenant_id,
                        "client_correlation_id": body["client_correlation_id"],
                        "provenance": wire.provenance(
                            settings.source_revision, inputs["policy_revision"]
                        ),
                    },
                },
                expires_at=created + timedelta(seconds=settings.retention_seconds),
                deadline_at=created + timedelta(seconds=min(settings.run_seconds, 120)),
            )
            db.add(run)
            db.flush()
            return wire.run_document(run)

    @router.get("/simulations/{run_id}")
    def state(run_id: str, owner: str = Depends(identity)):
        with sessions() as db:
            return wire.run_document(owned(db, run_id, owner))

    @router.get("/simulations/{run_id}/result")
    def result(run_id: str, owner: str = Depends(identity)):
        with sessions() as db:
            run = owned(db, run_id, owner)
            if run.status not in TERMINAL:
                raise HTTPException(409, "CONFLICT")
            return wire.result_document(run)

    @router.get("/simulations/{run_id}/evidence/{evidence_id}")
    def get_evidence(run_id: str, evidence_id: str, owner: str = Depends(identity)):
        with sessions() as db:
            run = owned(db, run_id, owner)
            contents = wire.evidence_content(run)
            if evidence_id not in contents:
                raise HTTPException(404, "NOT_FOUND")
            return {
                "evidence": wire.evidence(run, [evidence_id])[0],
                "content": contents[evidence_id],
            }

    @router.get("/simulations/{run_id}/investigations")
    def investigations(run_id: str, owner: str = Depends(identity)):
        with sessions() as db:
            owned(db, run_id, owner)
            rows = db.scalars(
                select(ServiceInbox)
                .where(ServiceInbox.run_id == run_id)
                .order_by(ServiceInbox.received_at)
                .limit(32)
            )
            return {
                "findings": [row.event["payload"] for row in rows],
                "business_authority": "NONE",
            }

    @router.post("/simulations/{run_id}/cancel")
    def cancel(run_id: str, owner: str = Depends(mutation)):
        with sessions.begin() as db:
            run = owned(db, run_id, owner, lock=True)
            if run.status not in TERMINAL:
                if run.deadline_at <= now():
                    terminate(db, run, "EXPIRED", "BLOCKED", "DEADLINE_EXCEEDED")
                else:
                    terminate(db, run, "CANCELLED", "BLOCKED", "CANCELLED_BY_OWNER")
            return wire.run_document(run)

    @router.get("/simulations/{run_id}/peers")
    def peer_results(run_id: str, owner: str = Depends(identity)):
        with sessions() as db:
            run = owned(db, run_id, owner)
            return {
                "authorization": run.context.get("peer_authorization"),
                "inference": run.context.get("peer_inference"),
                "knowledge": run.context.get("peer_knowledge"),
                "business_authority": "NONE",
            }

    @router.get("/simulations/{run_id}/handoffs")
    def handoffs(run_id: str, owner: str = Depends(identity)):
        with sessions() as db:
            run = owned(db, run_id, owner)
            return {
                "handoffs": [
                    wire.validated("agent-handoff", value)
                    for value in run.context.get("contract_handoffs", [])
                ]
            }

    @router.get("/simulations/{run_id}/proposal")
    def proposal(run_id: str, owner: str = Depends(identity)):
        with sessions() as db:
            run = owned(db, run_id, owner)
            from .workflow import result_for

            output = result_for(run)["output"]
            return {
                "final": False,
                "business_outcome": run.outcome,
                "proposal": output.get("proposal"),
                "approval": output.get("approval"),
                "trusted_approval_from_client": False,
            }

    @router.post("/operator/simulations/{run_id}/approval")
    def approve(run_id: str, body: ApprovalDecision, actor: str = Depends(operator)):
        with sessions.begin() as db:
            run = db.scalar(
                select(Run)
                .where(Run.id == run_id, Run.environment == settings.environment)
                .with_for_update()
            )
            if not run or not run.context.get("contracts_v1"):
                raise HTTPException(404, "NOT_FOUND")
            if run.expires_at <= now():
                raise HTTPException(410, "RUN_EXPIRED")
            from .approvals import decide_approval

            return wire.run_document(decide_approval(db, run, body, settings, actor))

    def cursor(request, count):
        values = [
            value
            for value in (request.query_params.get("after"), request.headers.get("last-event-id"))
            if value is not None
        ]
        if any(not re.fullmatch(r"[0-9]{1,4}", value) for value in values):
            raise HTTPException(409, "INVALID_CURSOR")
        numbers = [int(value) for value in values]
        if len(set(numbers)) > 1 or any(value > min(count, 1024) for value in numbers):
            raise HTTPException(409, "INVALID_CURSOR")
        return numbers[0] if numbers else 0

    def page(db, run, after):
        rows = db.scalars(
            select(Event)
            .where(Event.run_id == run.id, Event.sequence > after)
            .order_by(Event.sequence)
            .limit(128)
        )
        result, size = [], 0
        for row in rows:
            value = wire.validated("simulation-event", row.wire_payload)
            increment = len(wire.compact(value)) + 64
            if size + increment > 250000:
                break
            size += increment
            result.append(value)
        return wire.validated(
            "simulation-events",
            {
                "schema_version": "1.0",
                "run_id": run.id,
                "events": result,
                "next_sequence": result[-1]["sequence"] if result else after,
                "terminal": run.status in TERMINAL,
            },
        )

    @router.get("/simulations/{run_id}/events")
    def events(run_id: str, request: Request, owner: str = Depends(identity)):
        with sessions() as db:
            run = owned(db, run_id, owner)
            after = cursor(request, run.event_count)
            if "text/event-stream" not in request.headers.get("accept", ""):
                return page(db, run, after)
        slot = str(uuid.uuid4())
        with sessions.begin() as db:
            guard(db)
            db.execute(delete(StreamSlot).where(StreamSlot.expires_at < now()))
            if (
                db.scalar(select(func.count()).select_from(StreamSlot)) >= 32
                or db.scalar(
                    select(func.count()).select_from(StreamSlot).where(StreamSlot.owner_id == owner)
                )
                >= 2
            ):
                raise HTTPException(429, "RATE_LIMITED")
            db.add(StreamSlot(id=slot, owner_id=owner, expires_at=now() + timedelta(seconds=15)))

        async def stream():
            position, started, sent = after, time.monotonic(), 0
            try:
                while time.monotonic() - started < 10:
                    if await request.is_disconnected():
                        return
                    with sessions() as db:
                        session = db.get(Session, owner)
                        if not session or session.expires_at <= now():
                            return
                        try:
                            run = owned(db, run_id, owner)
                        except HTTPException:
                            return
                        batch = page(db, run, position)
                        count = run.event_count
                    for event in batch["events"]:
                        frame = f"id: {event['sequence']}\nevent: step\ndata: {wire.compact(event).decode()}\n\n"
                        if sent + len(frame.encode()) > 250000:
                            return
                        sent += len(frame.encode())
                        position = event["sequence"]
                        yield frame
                    if batch["terminal"] and position == count:
                        yield f'event: end\ndata: {{"last_sequence":{position}}}\n\n'
                        return
                    yield ": heartbeat\n\n"
                    await asyncio.sleep(0.25)
            finally:
                with sessions.begin() as db:
                    db.execute(delete(StreamSlot).where(StreamSlot.id == slot))

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @router.post("/events", status_code=202)
    def service_event(body: dict, request: Request):
        return receiver.receive(body, request.headers.get("authorization", ""))

    app.include_router(router)
    # Direct-prefix alias for private adoption tests and existing prefix-preserving
    # edges. A canonical stripping edge targets the identical unprefixed routes.
    app.include_router(router, prefix=settings.api_prefix, include_in_schema=False)
    from .openapi import document

    app.openapi = lambda: document(app)  # type: ignore[method-assign]
    return app
