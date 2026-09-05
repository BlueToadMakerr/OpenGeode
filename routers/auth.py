from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException

import github_oauth
import security
from config import settings
from database import storage
from models import (
    AuthTokens,
    CallbackParams,
    PollParams,
    RefreshBody,
    TokenLoginParams,
)

router = APIRouter(prefix="/v1/login", tags=["auth"])


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _get_or_create_developer(github_id: int, username: str, display_name: str) -> dict:
    existing = storage.find_one("developers", github_id=github_id)
    if existing:
        # display_name is intentionally NOT re-synced from GitHub here --
        # PUT /v1/me lets a developer set a custom display name, and it
        # should survive future logins instead of getting silently
        # overwritten back to whatever GitHub currently reports. Only
        # `username` (GitHub's login handle) is kept in sync, since that's
        # meant to track GitHub directly.
        if existing["username"] != username:
            existing = storage.update_where(
                "developers", {"id": existing["id"]}, {"username": username}
            )
        return existing

    is_bootstrap_admin = username.lower() in settings.BOOTSTRAP_ADMINS
    row = {
        "id": storage.next_id("developers"),
        "github_id": github_id,
        "username": username,
        "display_name": display_name,
        "verified": is_bootstrap_admin,
        "admin": is_bootstrap_admin,
        "created_at": storage.now_iso(),
    }
    return storage.insert("developers", row)


def _tokens_for(developer_id: int) -> AuthTokens:
    access_token, refresh_token = security.issue_session(developer_id)
    return AuthTokens(access_token=access_token, refresh_token=refresh_token)


# ---------------------------------------------------------------------------
# Device flow
# ---------------------------------------------------------------------------

@router.post(
    "/github",
    summary="Start GitHub device flow authentication",
)
async def start_github_device_flow():
    data = await github_oauth.start_device_flow()

    login_id = secrets.token_hex(16)
    now = datetime.now(timezone.utc)
    storage.insert(
        "login_attempts",
        {
            "uuid": login_id,
            "device_code": data["device_code"],
            "user_code": data["user_code"],
            "verification_uri": data.get("verification_uri") or data.get("verification_uri_complete"),
            "interval": data.get("interval", settings.DEVICE_FLOW_POLL_INTERVAL),
            "expires_at": storage.format_iso(now + timedelta(seconds=data.get("expires_in", settings.DEVICE_FLOW_EXPIRE_SECONDS))),
            "last_poll_at": None,
            "status": "pending",
            "developer_id": None,
        },
    )

    return {
        "error": "",
        "payload": {
            "uuid": login_id,
            "uri": data.get("verification_uri") or data.get("verification_uri_complete"),
            "code": data["user_code"],
            "interval": data.get("interval", settings.DEVICE_FLOW_POLL_INTERVAL),
        },
    }


@router.post(
    "/github/poll",
    summary="Poll GitHub device flow for authentication",
    response_model=None,
)
async def poll_github_device_flow(body: PollParams):
    attempt = storage.find_one("login_attempts", uuid=body.uuid)
    if attempt is None:
        raise HTTPException(status_code=400, detail="Invalid uuid")

    now = datetime.now(timezone.utc)
    expires_at = storage.parse_iso(attempt["expires_at"])
    if expires_at < now:
        storage.delete_where("login_attempts", lambda r: r["uuid"] == body.uuid)
        raise HTTPException(status_code=400, detail="Device code expired, please restart the login flow")

    if attempt["last_poll_at"]:
        last_poll = storage.parse_iso(attempt["last_poll_at"])
        if (now - last_poll).total_seconds() < attempt["interval"]:
            raise HTTPException(status_code=400, detail="Polling too fast, slow down")

    storage.update_where("login_attempts", {"uuid": body.uuid}, {"last_poll_at": storage.format_iso(now)})

    result = await github_oauth.poll_device_flow(attempt["device_code"])

    if result.get("pending"):
        raise HTTPException(status_code=400, detail="Authorization pending")
    if result.get("slow_down"):
        if result.get("interval"):
            storage.update_where("login_attempts", {"uuid": body.uuid}, {"interval": result["interval"]})
        raise HTTPException(status_code=400, detail="Polling too fast, slow down")
    if result.get("expired"):
        storage.delete_where("login_attempts", lambda r: r["uuid"] == body.uuid)
        raise HTTPException(status_code=400, detail="Device code expired or access denied")
    if result.get("error"):
        raise HTTPException(status_code=400, detail=f"GitHub error: {result['error']}")

    github_user = await github_oauth.fetch_github_user(result["access_token"])
    developer = _get_or_create_developer(**github_user)
    storage.delete_where("login_attempts", lambda r: r["uuid"] == body.uuid)

    tokens = _tokens_for(developer["id"])
    return {"error": "", "payload": tokens.model_dump()}


# ---------------------------------------------------------------------------
# Web (authorization code) flow
# ---------------------------------------------------------------------------

@router.post(
    "/github/web",
    summary="Start GitHub web OAuth flow",
)
async def start_github_web_flow():
    state = secrets.token_urlsafe(24)
    now = datetime.now(timezone.utc)
    storage.insert(
        "oauth_states",
        {
            "state": state,
            "created_at": storage.format_iso(now),
            "expires_at": storage.format_iso(now + timedelta(minutes=10)),
        },
    )
    url = github_oauth.build_web_authorize_url(state)
    return {"error": "", "payload": url}


@router.post(
    "/github/callback",
    summary="Handle GitHub OAuth callback",
    response_model=None,
)
async def github_web_callback(body: CallbackParams):
    state_row = storage.find_one("oauth_states", state=body.state)
    if state_row is None:
        raise HTTPException(status_code=404, detail="Invalid or expired state")

    now = datetime.now(timezone.utc)
    expires_at = storage.parse_iso(state_row["expires_at"])
    storage.delete_where("oauth_states", lambda r: r["state"] == body.state)
    if expires_at < now:
        raise HTTPException(status_code=400, detail="OAuth state expired, please restart login")

    access_token = await github_oauth.exchange_code_for_token(body.code)
    github_user = await github_oauth.fetch_github_user(access_token)
    developer = _get_or_create_developer(**github_user)

    tokens = _tokens_for(developer["id"])
    return {"error": "", "payload": tokens.model_dump()}


# ---------------------------------------------------------------------------
# Personal access token login
# ---------------------------------------------------------------------------

@router.post(
    "/github/token",
    summary="Login using a GitHub personal access token",
    response_model=None,
)
async def login_with_token(body: TokenLoginParams):
    try:
        github_user = await github_oauth.fetch_github_user(body.token)
    except HTTPException:
        raise HTTPException(status_code=400, detail="Invalid access token")

    developer = _get_or_create_developer(**github_user)
    tokens = _tokens_for(developer["id"])
    return {"error": "", "payload": tokens.model_dump()}


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

@router.post(
    "/refresh",
    summary="Refresh an access token using a refresh token",
    response_model=None,
)
async def refresh_token(body: RefreshBody):
    try:
        access_token, refresh_token_value = security.rotate_session(body.refresh_token)
    except HTTPException:
        raise HTTPException(status_code=400, detail="Invalid refresh token")
    return {
        "error": "",
        "payload": AuthTokens(access_token=access_token, refresh_token=refresh_token_value).model_dump(),
    }
