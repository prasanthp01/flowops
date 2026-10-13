import asyncio
import hashlib
import hmac
import json
import re
import time
import uuid
from datetime import timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import delete, func, select, text
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .config import Settings
from .contracts import MODE, PRESETS, ApprovalDecision, Scenario, StartRequest
from .db import Event, Run, Session, StreamSlot, database, now
from .security import COOKIE, BoundaryMiddleware, csrf, digest, guard, mint, rate, verify_token
from .workflow import TERMINAL, result_for, terminate


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.load()
    engine, sessions = database(settings)
    prefix = settings.api_prefix
    app = FastAPI(
        title="FlowOps synthetic workflow API",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=prefix + "/openapi.json",
    )
    app.state.settings, app.state.sessions, app.state.engine = settings, sessions, engine
    app.add_middleware(BoundaryMiddleware, origin=settings.origin)
    from urllib.parse import urlsplit

    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[
            urlsplit(settings.origin).hostname or "localhost",
            "127.0.0.1",
            "localhost",
            "testserver",
        ],
    )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Locations can include attacker-supplied extra-field names; never echo those.
        return JSONResponse(
            {
                "detail": "INVALID_INPUT",
                "message": "Use the published schema and bounded synthetic values.",
            },
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def server_error(request, exc):
        return JSONResponse({"detail": "INTERNAL_ERROR"}, status_code=500)

    def origin(request: Request):
        if request.headers.get("origin") != settings.origin:
            raise HTTPException(403, "ORIGIN_DENIED")

    def identity(request: Request):
        owner = verify_token(settings, request.cookies.get(COOKIE, ""))
        with sessions.begin() as db:
            session = db.get(Session, owner)
            if not session or session.expires_at <= now():
                raise HTTPException(401, "SESSION_EXPIRED")
            guard(db)
            rate(db, "reads:" + owner, 600)
        return owner

    def mutation(request: Request, owner: str = Depends(identity)):
        origin(request)
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), csrf(settings, owner)):
            raise HTTPException(403, "CSRF_DENIED")
        return owner

    def operator(request: Request):
        auth = request.headers.get("authorization", "")
        if not settings.operator_token or not hmac.compare_digest(
            auth, "Bearer " + settings.operator_token
        ):
            raise HTTPException(401, "OPERATOR_AUTH_REQUIRED")
        if request.headers.get("origin") not in {None, settings.origin}:
            raise HTTPException(403, "ORIGIN_DENIED")
        return settings.operator_actor

    def owned(db, run_id: str, owner: str | None = None, lock=False):
        query = select(Run).where(Run.id == run_id, Run.environment == settings.environment)
        if owner is not None:
            query = query.where(Run.owner_id == owner)
        if lock:
            query = query.with_for_update()
        run = db.scalar(query)
        if not run:
            raise HTTPException(404, "RUN_NOT_FOUND")
        if run.expires_at <= now():
            raise HTTPException(410, "RUN_EXPIRED")
        return run

    def state(run):
        return {
            "schema_version": "1.0",
            "project_id": "flowops",
            "run_id": run.id,
            "mode": MODE,
            "status": run.status,
            "outcome": run.outcome,
            "event_count": run.event_count,
            "created_at": run.created_at.isoformat(),
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "result_ref": f"{prefix}/simulations/{run.id}/result",
            "input": run.input,
            "expires_at": run.expires_at.isoformat(),
            "app_version": __version__,
            "fixtures_version": "synthetic-1.0",
            "connector_kind": "in_process_mock_http",
            "data_provenance": "synthetic",
        }

    @app.get(prefix + "/health")
    def health():
        with sessions() as db:
            db.execute(text("SELECT 1"))
        return {"status": "ok", "project": "flowops", "version": __version__}

    @app.post(prefix + "/session", dependencies=[Depends(origin)])
    def session_start(request: Request):
        ip = digest(settings.signing_key, request.client.host if request.client else "unknown")
        with sessions.begin() as db:
            guard(db)
            rate(db, "session-ip:" + ip, 120, 3600)
            rate(db, "session-global", 1000, 3600)
            try:
                owner = verify_token(settings, request.cookies.get(COOKIE, ""))
                existing = db.get(Session, owner)
            except HTTPException:
                existing = None
            if existing and existing.expires_at > now():
                value = request.cookies[COOKIE]
            else:
                owner, value = mint(db, settings)
        response = JSONResponse(
            {
                "csrf_token": csrf(settings, owner),
                "mode": MODE,
                "retention_seconds": settings.retention_seconds,
            }
        )
        response.set_cookie(
            COOKIE,
            value,
            httponly=True,
            secure=settings.origin.startswith("https://"),
            samesite="strict",
            max_age=settings.retention_seconds,
            path=prefix,
        )
        return response

    @app.get(prefix + "/simulation/schema")
    def schema():
        return {
            "schema_version": "1.0",
            "project_id": "flowops",
            "input_schema": Scenario.model_json_schema(),
            "request_schema": StartRequest.model_json_schema(),
            "limits": {
                "max_input_bytes": 16384,
                "run_seconds": settings.run_seconds,
                "retention_seconds": settings.retention_seconds,
                "max_active": settings.max_active,
                "per_session_active": settings.per_session_active,
            },
        }

    @app.get(prefix + "/simulation/presets")
    def presets():
        return {"presets": PRESETS}

    @app.post(prefix + "/simulations", status_code=202)
    def start(body: StartRequest, request: Request, owner: str = Depends(mutation)):
        key = request.headers.get("idempotency-key", "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{8,64}", key):
            raise HTTPException(422, "IDEMPOTENCY_KEY_REQUIRED")
        content = body.model_dump()
        checksum = hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()
        with sessions.begin() as db:
            guard(db)
            existing = db.scalar(
                select(Run).where(
                    Run.owner_id == owner,
                    Run.environment == settings.environment,
                    Run.request_key == key,
                )
            )
            if existing:
                if existing.request_digest != checksum:
                    raise HTTPException(409, "IDEMPOTENCY_CONFLICT")
                return state(existing)
            ip = digest(settings.signing_key, request.client.host if request.client else "unknown")
            rate(db, "runs:" + owner, 30)
            rate(db, "runs-ip:" + ip, 120)
            rate(db, "runs-global", 240)
            active = Run.status.in_(["ACCEPTED", "RUNNING"])
            if (
                db.scalar(select(func.count()).select_from(Run).where(active))
                >= settings.max_active
            ):
                raise HTTPException(429, "GLOBAL_CAPACITY", headers={"Retry-After": "5"})
            if (
                db.scalar(
                    select(func.count()).select_from(Run).where(active, Run.owner_id == owner)
                )
                >= settings.per_session_active
            ):
                raise HTTPException(429, "SESSION_CAPACITY", headers={"Retry-After": "5"})
            run = Run(
                id=str(uuid.uuid4()),
                owner_id=owner,
                environment=settings.environment,
                request_key=key,
                request_digest=checksum,
                input=body.input.model_dump(),
                expires_at=now() + timedelta(seconds=settings.retention_seconds),
                deadline_at=now() + timedelta(seconds=settings.run_seconds),
            )
            db.add(run)
            db.flush()
            return state(run)

    @app.get(prefix + "/simulations/{run_id}")
    def get_run(run_id: uuid.UUID, owner: str = Depends(identity)):
        with sessions() as db:
            return state(owned(db, str(run_id), owner))

    @app.get(prefix + "/simulations/{run_id}/result")
    def get_result(run_id: uuid.UUID, owner: str = Depends(identity)):
        with sessions() as db:
            run = owned(db, str(run_id), owner)
            return run.result or result_for(run)

    @app.get(prefix + "/simulations/{run_id}/events")
    def events(
        run_id: uuid.UUID,
        request: Request,
        after: int = Query(0, ge=0, le=1000),
        format: str = Query("sse", pattern="^(sse|json)$"),
        owner: str = Depends(identity),
    ):
        if request.headers.get("last-event-id"):
            try:
                header_cursor = int(request.headers["last-event-id"])
                if not 0 <= header_cursor <= 1000:
                    raise ValueError()
                after = max(after, header_cursor)
            except ValueError:
                raise HTTPException(422, "INVALID_EVENT_CURSOR") from None
        with sessions() as db:
            run = owned(db, str(run_id), owner)
            if after < 0 or after > run.event_count:
                raise HTTPException(422, "INVALID_EVENT_CURSOR")
            if format == "json":
                rows = db.scalars(
                    select(Event)
                    .where(Event.run_id == run.id, Event.sequence > after)
                    .order_by(Event.sequence)
                    .limit(128)
                ).all()
                return {
                    "events": [r.payload for r in rows],
                    "cursor": rows[-1].sequence if rows else after,
                    "status": run.status,
                    "outcome": run.outcome,
                }
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
                raise HTTPException(429, "STREAM_CAPACITY")
            db.add(StreamSlot(id=slot, owner_id=owner, expires_at=now() + timedelta(seconds=15)))

        async def stream():
            cursor, started = after, time.monotonic()
            try:
                while time.monotonic() - started < 10:
                    if await request.is_disconnected():
                        break
                    with sessions() as db:
                        try:
                            run = owned(db, str(run_id), owner)
                        except HTTPException:
                            break
                        rows = db.scalars(
                            select(Event)
                            .where(Event.run_id == run.id, Event.sequence > cursor)
                            .order_by(Event.sequence)
                            .limit(128)
                        ).all()
                        done = run.status in TERMINAL or run.outcome == "APPROVAL_PENDING"
                    for row in rows:
                        cursor = row.sequence
                        yield f"id: {cursor}\nevent: step\ndata: {json.dumps(row.payload)}\n\n"
                    if done:
                        yield "event: settled\ndata: {}\n\n"
                        break
                    yield ": heartbeat\n\n"
                    await asyncio.sleep(0.25)
            finally:
                with sessions.begin() as db:
                    db.execute(delete(StreamSlot).where(StreamSlot.id == slot))

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"}
        )

    @app.post(prefix + "/simulations/{run_id}/cancel")
    def cancel(run_id: uuid.UUID, owner: str = Depends(mutation)):
        with sessions.begin() as db:
            run = owned(db, str(run_id), owner, lock=True)
            if run.status not in TERMINAL:
                terminate(db, run, "CANCELLED", "BLOCKED", "CANCELLED_BY_OWNER")
            return state(run)

    @app.get(prefix + "/operator/simulations/{run_id}")
    def operator_run(run_id: uuid.UUID, actor: str = Depends(operator)):
        with sessions() as db:
            run = owned(db, str(run_id))
            return {**state(run), "result": run.result or result_for(run)}

    @app.post(prefix + "/operator/simulations/{run_id}/approval")
    def approve(run_id: uuid.UUID, body: ApprovalDecision, actor: str = Depends(operator)):
        with sessions.begin() as db:
            run = owned(db, str(run_id), lock=True)
            from .approvals import decide_approval

            return state(decide_approval(db, run, body, settings, actor))

    static = Path(__file__).parent / "static"
    app.mount("/projects/flowops/assets", StaticFiles(directory=static), name="assets")

    @app.get("/projects/flowops", include_in_schema=False)
    @app.get("/projects/flowops/", include_in_schema=False)
    def page():
        return FileResponse(static / "index.html")

    @app.get("/", include_in_schema=False)
    def home():
        return HTMLResponse(
            '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>FlowOps local preview</title><link rel="stylesheet" href="/projects/flowops/assets/style.css"><main class="local-home"><p class="eyebrow">LOCAL PROJECT PREVIEW</p><h1>FlowOps</h1><p>Auditable order-resolution workflows, with synthetic data.</p><a class="button" href="/projects/flowops">Open the case study & simulator →</a><p>The shared portfolio is a separate integration.</p></main></html>'
        )

    return app
