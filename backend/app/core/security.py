import hashlib
import hmac
import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Annotated

import jwt
from fastapi import Depends, Header, HTTPException

from app.core.config import Settings, get_settings

log = logging.getLogger(__name__)

ALGORITHMS = ["ES256", "RS256"]  # asymmetric only: never HS256 or "none"
SIGNATURE_TOLERANCE_SEC = 300  # webhook replay window


@dataclass(frozen=True)
class CurrentUser:
    id: str
    email: str | None = None


def _unauthorized(detail: str = "Not authenticated") -> HTTPException:
    return HTTPException(401, detail, headers={"WWW-Authenticate": "Bearer"})


@lru_cache
def _jwks(url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(url, cache_keys=True, lifespan=600)


def _signing_key(token: str, s: Settings):
    url = f"{s.supabase_url}/auth/v1/.well-known/jwks.json"
    return _jwks(url).get_signing_key_from_jwt(token).key


# Plain `def`, not `async def`: the JWKS fetch is blocking network I/O, and
# FastAPI runs sync dependencies in a thread pool so the event loop stays free.
def get_current_user(
    s: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header()] = None,
) -> CurrentUser:
    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _unauthorized()
    try:
        claims = jwt.decode(
            token,
            _signing_key(token, s),
            algorithms=ALGORITHMS,
            audience="authenticated",
            issuer=f"{s.supabase_url}/auth/v1",
            options={"require": ["exp", "iat", "sub", "aud", "iss"]},
        )
    except jwt.PyJWKClientConnectionError as e:
        log.error("jwks unreachable", extra={"error": str(e)})
        raise HTTPException(503, "Auth service unavailable") from e
    except jwt.PyJWTError as e:
        log.info("token rejected", extra={"reason": str(e)})
        raise _unauthorized("Invalid token") from e
    return CurrentUser(id=claims["sub"], email=claims.get("email"))


def verify_tool_secret(
    s: Annotated[Settings, Depends(get_settings)],
    x_tool_secret: Annotated[str | None, Header()] = None,
) -> None:
    if not s.tool_webhook_secret:
        raise HTTPException(503, "Tool secret not configured")  # fail closed
    if not x_tool_secret or not hmac.compare_digest(
        x_tool_secret.encode(), s.tool_webhook_secret.encode()
    ):
        raise HTTPException(401, "Bad tool secret")


def verify_signature(
    body: bytes, header: str | None, secret: str, now: float | None = None
) -> bool:
    """Check a 't=<unix>,v0=<hex hmac-sha256 of "<t>.<body>">' signature header."""
    if not header or not secret:
        return False
    parts = dict(p.strip().split("=", 1) for p in header.split(",") if "=" in p)
    ts, sig = parts.get("t", ""), parts.get("v0", "")
    if not ts.isdigit() or not sig:
        return False
    now = time.time() if now is None else now
    if abs(now - int(ts)) > SIGNATURE_TOLERANCE_SEC:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)
