"""Authentication and request hardening.

v1 shipped every endpoint wide open: /api/dashboard, /api/deals,
/api/pipeline/run and /api/outbound-call were all reachable anonymously,
which means anyone on the internet could spend your Telnyx balance or sign
contracts on your deals. This module closes that.
"""
from __future__ import annotations

import datetime as dt
import hmac
import secrets
from typing import Annotated

import base64
import hashlib

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import AuditLog, User, utcnow

ALGORITHM = "HS256"
TOKEN_TTL_HOURS = 12
MAX_FAILED_LOGINS = 8
LOCKOUT_MINUTES = 15

# PBKDF2-HMAC-SHA256 from the stdlib. Deliberately not bcrypt/passlib: the
# passlib<->bcrypt 4.x version conflict is a recurring production footgun, and
# PBKDF2 at 260k iterations is FIPS-approved and dependency-free.
PBKDF2_ITERATIONS = 260_000
_HASH_PREFIX = "pbkdf2_sha256"

# auto_error=False so we can fall back to the session cookie.
bearer = HTTPBearer(auto_error=False)

COOKIE_NAME = "wai_session"


def hash_password(password: str) -> str:
    if len(password.encode("utf-8")) > 1024:
        raise ValueError("password too long")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return "{}${}${}${}".format(
        _HASH_PREFIX,
        PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password: str, hashed: str) -> bool:
    try:
        prefix, iters, salt_b64, digest_b64 = hashed.split("$", 3)
        if prefix != _HASH_PREFIX:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, int(iters)
        )
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def create_token(user: User) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    payload = {
        "sub": str(user.id),
        "email": user.email,
        "iat": int(now.timestamp()),
        "exp": int((now + dt.timedelta(hours=TOKEN_TTL_HOURS)).timestamp()),
        "jti": secrets.token_hex(8),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_token(token: str) -> dict | None:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except JWTError:
        return None


def authenticate(db: Session, email: str, password: str, ip: str = "") -> User:
    """Return the user or raise HTTPException. Applies lockout after repeated
    failures so the login endpoint cannot be brute forced."""
    email = (email or "").strip().lower()
    user = db.scalar(select(User).where(func_lower(User.email) == email))
    if not user:
        # Constant-ish time: still run a hash to avoid user enumeration timing.
        hash_password(password or "x")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    if user.locked_until and user.locked_until > utcnow():
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            f"Too many attempts. Try again after {user.locked_until:%H:%M} UTC.",
        )

    if not verify_password(password, user.password_hash):
        user.failed_logins += 1
        if user.failed_logins >= MAX_FAILED_LOGINS:
            user.locked_until = utcnow() + dt.timedelta(minutes=LOCKOUT_MINUTES)
            user.failed_logins = 0
            db.add(
                AuditLog(
                    actor=email, action="login_locked", target=email, ip=ip
                )
            )
        db.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    user.failed_logins = 0
    user.locked_until = None
    user.last_login_at = utcnow()
    db.add(AuditLog(actor=email, action="login_success", target=email, ip=ip))
    db.commit()
    return user


def func_lower(col):
    from sqlalchemy import func

    return func.lower(col)


async def current_user(
    request: Request,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)] = None,
    db: Session = Depends(get_db),
) -> User:
    """Resolve the caller from the Authorization header OR the session cookie."""
    token = creds.credentials if creds else None
    if not token:
        token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_token(token)
    if not payload:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or expired session")
    user = db.get(User, int(payload.get("sub", 0)))
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "User no longer exists")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def audit(db: Session, *, actor: str, action: str, target: str = "", ip: str = "", meta: dict | None = None) -> None:
    db.add(AuditLog(actor=actor, action=action, target=target, ip=ip, meta=meta or {}))


def ensure_bootstrap_user(db: Session) -> None:
    """Create the admin user from env on first boot."""
    existing = db.scalar(select(User).limit(1))
    if existing:
        return
    db.add(
        User(
            email=settings.admin_email.strip().lower(),
            name=settings.admin_name,
            password_hash=hash_password(settings.admin_password),
            is_admin=True,
        )
    )
    db.commit()
