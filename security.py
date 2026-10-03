"""
Auth primitives: JWT access tokens, opaque refresh/session tokens, and the
FastAPI dependencies routers use to require a logged-in (or admin) developer.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config import settings
from database import storage
from models import Developer

bearer_scheme = HTTPBearer(auto_error=False)


# Access tokens (short-lived JWTs)

def create_access_token(developer_id: int, session_id: str) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(developer_id),
        "sid": session_id,
        "iat": now,
        "exp": now + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES),
        "type": "access",
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_access_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Access token expired")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Invalid access token")


# Refresh / session tokens

def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def issue_session(developer_id: int) -> tuple[str, str]:
    """Create a new session (refresh token) for a developer.

    Returns (access_token, refresh_token).
    """
    session_id = secrets.token_hex(16)
    refresh_token = secrets.token_urlsafe(48)
    now = datetime.now(timezone.utc)

    storage.insert(
        "tokens",
        {
            "id": session_id,
            "developer_id": developer_id,
            "refresh_token_hash": _hash_token(refresh_token),
            "created_at": storage.format_iso(now),
            "expires_at": storage.format_iso(now + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)),
            "last_used_at": storage.format_iso(now),
        },
    )

    access_token = create_access_token(developer_id, session_id)
    return access_token, refresh_token


def rotate_session(refresh_token: str) -> tuple[str, str]:
    """Exchange a valid refresh token for a new access + refresh token pair."""
    token_hash = _hash_token(refresh_token)
    session = storage.find_one("tokens", refresh_token_hash=token_hash)
    if session is None:
        raise HTTPException(status_code=400, detail="Invalid or expired refresh token")

    expires_at = storage.parse_iso(session["expires_at"])
    if expires_at < datetime.now(timezone.utc):
        storage.delete_where("tokens", lambda r: r["id"] == session["id"])
        raise HTTPException(status_code=400, detail="Invalid or expired refresh token")

    # Rotate: invalidate the old refresh token, mint a fresh session.
    storage.delete_where("tokens", lambda r: r["id"] == session["id"])
    return issue_session(session["developer_id"])


def revoke_session(session_id: str) -> None:
    storage.delete_where("tokens", lambda r: r["id"] == session_id)


def revoke_all_sessions(developer_id: int) -> int:
    return storage.delete_where("tokens", lambda r: r["developer_id"] == developer_id)


# FastAPI dependencies

def _developer_row_to_model(row: dict) -> Developer:
    from serializers import developer_public
    return Developer(**developer_public(row))


def get_current_developer(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> Developer:
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication error: No authentication token provided",
            headers={"WWW-Authenticate": "Bearer"},
        )

    payload = decode_access_token(credentials.credentials)
    session_id = payload.get("sid")
    session = storage.find_one("tokens", id=session_id)
    if session is None:
        raise HTTPException(status_code=401, detail="Session has been revoked")

    developer_row = storage.find_one("developers", id=session["developer_id"])
    if developer_row is None:
        raise HTTPException(status_code=401, detail="Developer account no longer exists")

    storage.update_where(
        "tokens", {"id": session_id}, {"last_used_at": storage.now_iso()}
    )

    return _developer_row_to_model(developer_row)


def get_current_developer_optional(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> Optional[Developer]:
    if credentials is None or not credentials.credentials:
        return None
    try:
        return get_current_developer(credentials)
    except HTTPException:
        return None


def get_active_ban(developer_id: int) -> Optional[dict]:
    now = datetime.now(timezone.utc)
    rows = storage.find_all("bans", lambda b: b.get("developer_id") == developer_id)
    active = []
    for ban in rows:
        revoked_at = ban.get("revoked_at")
        if revoked_at is None or storage.parse_iso(revoked_at) > now:
            active.append(ban)
    active.sort(key=lambda b: (b.get("revoked_at") is not None, b.get("revoked_at") or "", b.get("id", 0)), reverse=False)
    return active[0] if active else None


def require_not_banned(developer: Developer = Depends(get_current_developer)) -> Developer:
    ban = get_active_ban(developer.id)
    if ban is not None:
        raise HTTPException(status_code=403, detail=ban.get("reason") or "You are banned from accessing this resource")
    return developer


def require_admin(developer: Developer = Depends(get_current_developer)) -> Developer:
    if not developer.admin:
        raise HTTPException(status_code=403, detail="Admin only")
    return developer


def get_current_session_id(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
) -> str:
    """Like get_current_developer, but returns the session id (used to
    revoke *just* the current session, e.g. DELETE /v1/me/token)."""
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Authentication error: No authentication token provided")
    payload = decode_access_token(credentials.credentials)
    session_id = payload.get("sid")
    if storage.find_one("tokens", id=session_id) is None:
        raise HTTPException(status_code=401, detail="Session has been revoked")
    return session_id
