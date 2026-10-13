import asyncio
import hashlib
import hmac
import json
import secrets
import time
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import HTTPException
from sqlalchemy import select
from starlette.responses import JSONResponse

from .db import Guard, RateBucket, Session, now

COOKIE = "flowops_session"


def digest(key: str, value: str) -> str:
    return hmac.new(key.encode(), value.encode(), hashlib.sha256).hexdigest()


def guard(db):
    db.scalar(select(Guard).where(Guard.id == "admission").with_for_update())


def rate(db, key: str, limit: int, seconds: int = 60):
    window = int(time.time()) // seconds
    identifier = hashlib.sha256(f"{key}:{window}".encode()).hexdigest()
    bucket = db.get(RateBucket, identifier)
    if bucket is None:
        bucket = RateBucket(
            id=identifier, count=0, expires_at=now() + timedelta(seconds=seconds * 2)
        )
        db.add(bucket)
    if bucket.count >= limit:
        raise HTTPException(429, "RATE_LIMITED", headers={"Retry-After": str(seconds)})
    bucket.count += 1


def token(settings, session_id: str, expires: int) -> str:
    payload = f"{session_id}.{expires}"
    return f"{payload}.{digest(settings.signing_key, settings.environment + ':' + payload)}"


def csrf(settings, session_id: str) -> str:
    return digest(settings.signing_key, f"csrf:{settings.environment}:{session_id}")


def verify_token(settings, value: str) -> str:
    try:
        session_id, expires, signature = value.split(".")
        if len(session_id) != 64 or int(expires) <= time.time():
            raise ValueError()
        if not hmac.compare_digest(
            signature,
            digest(settings.signing_key, f"{settings.environment}:{session_id}.{expires}"),
        ):
            raise ValueError()
        return session_id
    except (ValueError, TypeError):
        raise HTTPException(401, "SESSION_REQUIRED") from None


def mint(db, settings):
    session_id = secrets.token_hex(32)
    expiry = now() + timedelta(seconds=settings.retention_seconds)
    db.add(Session(id=session_id, expires_at=expiry))
    return session_id, token(settings, session_id, int(expiry.timestamp()))


class BoundaryMiddleware:
    """Bound request bytes and body time before JSON parsing; security headers on all responses."""

    def __init__(self, app, origin: str, canonical: bool = False):
        self.app, self.origin = app, origin
        self.canonical = canonical
        self.slots = asyncio.Semaphore(64)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))

        def failure(detail, status):
            if not self.canonical:
                return JSONResponse({"detail": detail}, status)
            from .wire import error

            mapping = {
                413: ("PAYLOAD_TOO_LARGE", 413),
                408: ("SERVICE_UNAVAILABLE", 503),
                415: ("VALIDATION_ERROR", 422),
                422: ("VALIDATION_ERROR", 422),
                503: ("SERVICE_UNAVAILABLE", 503),
            }
            code, status = mapping[status]
            return JSONResponse(error(code, status, secrets.token_hex(16)), status)

        async def secure_send(message):
            if message["type"] == "http.response.start":
                message.setdefault("headers", []).extend(
                    [
                        (
                            b"content-security-policy",
                            b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
                        ),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
                        (b"cache-control", b"no-store"),
                    ]
                )
                if self.origin.startswith("https://"):
                    message["headers"].append((b"strict-transport-security", b"max-age=31536000"))
            await send(message)

        if self.canonical:
            try:
                host = urlsplit("http://" + headers.get(b"host", b"").decode()).hostname
            except ValueError:
                host = None
            if host not in {"localhost", "127.0.0.1", "testserver", urlsplit(self.origin).hostname}:
                from .wire import error

                return await JSONResponse(error("FORBIDDEN", 403, secrets.token_hex(16)), 403)(
                    scope, receive, secure_send
                )

        if scope["method"] in {"POST", "PUT", "PATCH", "DELETE"}:
            body = bytearray()
            try:
                async with asyncio.timeout(5):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        body.extend(message.get("body", b""))
                        if len(body) > 16384:
                            return await failure("PAYLOAD_TOO_LARGE", 413)(
                                scope, receive, secure_send
                            )
                        if not message.get("more_body"):
                            break
            except TimeoutError:
                return await failure("BODY_TIMEOUT", 408)(scope, receive, secure_send)
            if body and headers.get(b"content-type", b"").split(b";")[0] != b"application/json":
                return await failure("JSON_REQUIRED", 415)(scope, receive, secure_send)
            if self.canonical and body:
                try:

                    def unique(pairs):
                        result = {}
                        for key, value in pairs:
                            if key in result:
                                raise ValueError("Duplicate JSON property")
                            result[key] = value
                        return result

                    data = json.loads(
                        body,
                        object_pairs_hook=unique,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()),
                    )
                    stack = [(data, 0)]
                    while stack:
                        value, depth = stack.pop()
                        if depth > 12:
                            raise ValueError("Excessive JSON depth")
                        if isinstance(value, dict):
                            stack.extend((item, depth + 1) for item in value.values())
                        elif isinstance(value, list):
                            stack.extend((item, depth + 1) for item in value)
                except (ValueError, RecursionError):
                    return await failure("INVALID_JSON", 422)(scope, receive, secure_send)
            used = False
            original = receive

            async def replay():
                nonlocal used
                if not used:
                    used = True
                    return {"type": "http.request", "body": bytes(body), "more_body": False}
                return await original()

            receive = replay
        if self.slots.locked():
            return await failure("SERVER_BUSY", 503)(scope, receive, secure_send)
        async with self.slots:
            await self.app(scope, receive, secure_send)
