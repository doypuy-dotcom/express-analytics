"""Supabase Auth (email + password) verification.

The frontend signs in directly against Supabase Auth and receives a JWT. This
API never sees a password; it only verifies the token on every request.

Supabase issues tokens in two schemes depending on project age:

    HS256  -- signed with the project's shared JWT secret (legacy, still the
              default for most projects)
    RS256/ES256 -- signed with a rotating key, verified via the project JWKS

Both are supported because which one a new project gets is not something we
can choose, and finding out on deadline day is not a good use of the day.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_JWKS_CACHE: dict = {"keys": None, "fetched": 0.0}
_JWKS_TTL = 600.0

bearer = HTTPBearer(auto_error=False)


@dataclass
class User:
    id: str
    email: str | None
    role: str


def _allow_anonymous() -> bool:
    """Local development escape hatch.

    Deliberately requires an explicit opt-in env var. It is never set in the
    deployed environment, and /health reports it so that an accidentally open
    deployment is visible rather than silent.
    """
    return os.environ.get("ALLOW_ANONYMOUS", "").lower() in ("1", "true", "yes")


def _jwks_keys() -> dict:
    import urllib.request
    import json

    now = time.time()
    if _JWKS_CACHE["keys"] and now - _JWKS_CACHE["fetched"] < _JWKS_TTL:
        return _JWKS_CACHE["keys"]
    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    if not base:
        raise HTTPException(500, "SUPABASE_URL is not configured")
    url = f"{base}/auth/v1/.well-known/jwks.json"
    with urllib.request.urlopen(url, timeout=10) as r:
        data = json.loads(r.read())
    _JWKS_CACHE.update(keys=data, fetched=now)
    return data


def decode_token(token: str) -> dict:
    secret = os.environ.get("SUPABASE_JWT_SECRET", "").strip()
    opts = {"verify_aud": False}  # Supabase sets aud="authenticated"

    header = jwt.get_unverified_header(token)
    alg = header.get("alg", "HS256")

    if alg == "HS256":
        if not secret:
            raise HTTPException(
                500,
                "SUPABASE_JWT_SECRET is not set. Copy it from Supabase: "
                "Project settings -> API -> JWT Settings -> JWT Secret.",
            )
        return jwt.decode(token, secret, algorithms=["HS256"], options=opts)

    # Asymmetric: find the signing key by kid.
    from jwt import PyJWKClient  # lazy: only needed for RS/ES projects

    base = os.environ.get("SUPABASE_URL", "").rstrip("/")
    client = PyJWKClient(f"{base}/auth/v1/.well-known/jwks.json")
    key = client.get_signing_key_from_jwt(token).key
    return jwt.decode(token, key, algorithms=[alg], options=opts)


async def current_user(
    request: Request,
    cred: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> User:
    if cred is None or not cred.credentials:
        if _allow_anonymous():
            return User(id="anonymous", email=None, role="anon")
        raise HTTPException(401, "กรุณาเข้าสู่ระบบ (missing bearer token)")

    try:
        claims = decode_token(cred.credentials)
    except HTTPException:
        raise
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "เซสชันหมดอายุ กรุณาเข้าสู่ระบบใหม่")
    except Exception as exc:
        raise HTTPException(401, f"โทเคนไม่ถูกต้อง ({exc})")

    return User(
        id=claims.get("sub", ""),
        email=claims.get("email"),
        role=claims.get("role", "authenticated"),
    )
