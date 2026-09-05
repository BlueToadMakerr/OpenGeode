from __future__ import annotations

from typing import Optional

import httpx
from fastapi import HTTPException

from config import settings

GITHUB_DEVICE_CODE_URL = "https://github.com/login/device/code"
GITHUB_ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_USER_API_URL = "https://api.github.com/user"

DEFAULT_HEADERS = {"Accept": "application/json"}


def _require_oauth_app_configured() -> None:
    if not settings.GITHUB_CLIENT_ID or not settings.GITHUB_CLIENT_SECRET:
        raise HTTPException(
            status_code=500,
            detail=(
                "GitHub OAuth is not configured on this server. Set "
                "GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET."
            ),
        )


async def start_device_flow() -> dict:
    _require_oauth_app_configured()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            GITHUB_DEVICE_CODE_URL,
            data={"client_id": settings.GITHUB_CLIENT_ID, "scope": "read:user"},
            headers=DEFAULT_HEADERS,
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail="Failed to start GitHub device flow")
    data = resp.json()
    if "error" in data:
        raise HTTPException(status_code=502, detail=f"GitHub error: {data.get('error_description', data['error'])}")
    return data


async def poll_device_flow(device_code: str) -> dict:
    _require_oauth_app_configured()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            GITHUB_ACCESS_TOKEN_URL,
            data={
                "client_id": settings.GITHUB_CLIENT_ID,
                "client_secret": settings.GITHUB_CLIENT_SECRET,
                "device_code": device_code,
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            },
            headers=DEFAULT_HEADERS,
        )
    data = resp.json()
    error = data.get("error")
    if error == "authorization_pending":
        return {"pending": True}
    if error == "slow_down":
        return {"slow_down": True, "interval": data.get("interval")}
    if error in ("expired_token", "access_denied"):
        return {"expired": True}
    if error:
        return {"error": error}
    return {"access_token": data["access_token"]}


def build_web_authorize_url(state: str) -> str:
    _require_oauth_app_configured()
    return (
        f"{GITHUB_AUTHORIZE_URL}"
        f"?client_id={settings.GITHUB_CLIENT_ID}"
        f"&redirect_uri={settings.GITHUB_CALLBACK_URL}"
        f"&scope=read:user"
        f"&state={state}"
    )


async def exchange_code_for_token(code: str) -> str:
    _require_oauth_app_configured()
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            GITHUB_ACCESS_TOKEN_URL,
            data={
                "client_id": settings.GITHUB_CLIENT_ID,
                "client_secret": settings.GITHUB_CLIENT_SECRET,
                "code": code,
                "redirect_uri": settings.GITHUB_CALLBACK_URL,
            },
            headers=DEFAULT_HEADERS,
        )
    data = resp.json()
    if "error" in data or "access_token" not in data:
        raise HTTPException(status_code=400, detail="Invalid or expired authorization code")
    return data["access_token"]


async def fetch_github_user(access_token: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            GITHUB_USER_API_URL,
            headers={**DEFAULT_HEADERS, "Authorization": f"Bearer {access_token}"},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=400, detail="Invalid GitHub access token")
    data = resp.json()
    return {
        "github_id": data["id"],
        "username": data["login"],
        "display_name": data.get("name") or data["login"],
    }
