"""Adds verified Telegram accounts to the existing AI-Socratus server.

The existing /api/chat and GEMINI_* environment variables remain compatible
with Android. No provider API keys or bot tokens are sent to Flutter.
"""
import os
import time
from pathlib import Path
from collections import defaultdict, deque
from typing import Literal
from urllib.parse import urlparse
import httpx

from fastapi import Depends, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from socratus import Settings, create_app as create_socratus_app, fail
from storage import Store
from telegram_auth import InvalidTelegramData, new_session, session_hash, validate_init_data

DATA_KEYS = frozenset({
    "beta_favorites", "beta_university", "beta_language", "beta_resume", "beta_chat",
    "email", "phone", "city", "unt_score", "user_interests", "user_status",
    "resume_fio", "resume_position", "resume_birthdate", "resume_gender",
    "resume_marital", "resume_edu_type", "resume_phone", "resume_salary",
    "resume_telegram", "resume_about", "resume_city", "resume_experience",
    "resume_education", "resume_languages_list", "resume_interests_list",
    "applied_jobs_list", "liked_unis",
})
STATUSES = {"", "applicant", "student", "graduate"}


class LoginInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    init_data: str = Field(min_length=1, max_length=16384)


class SyncInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data: dict[str, str]


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    user_status: Literal["applicant", "student", "graduate"]
    email: str = Field(default="", max_length=254)
    phone: str = Field(default="", max_length=40)
    user_interests: str = Field(default="[]", max_length=20000)


class LeadInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: dict


class LeadStatusInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lead_ids: list[str] = Field(max_length=30)


def create_app(*, store=None, bot_token=None, ai_settings=None, transport=None):
    app = create_socratus_app(ai_settings or Settings.from_env(), transport=transport)
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip() if bot_token is None else bot_token
    # A Render ephemeral filesystem must never silently become the profile DB.
    db_url = os.getenv("DATABASE_URL", "").strip()
    sqlite_path = os.getenv("SQLITE_PATH", "data/miniapp.sqlite3")
    if store is None and os.getenv("RENDER") and not db_url and not os.getenv("SQLITE_PATH"):
        raise RuntimeError("Set DATABASE_URL or SQLITE_PATH on a persistent disk")
    database = store or Store(db_url, sqlite_path)
    app.state.profile_store = database
    attempts = defaultdict(deque)
    def rate_limit(key, limit=20):
        now = time.monotonic()
        for old in list(attempts):
            while attempts[old] and attempts[old][0] < now - 60:
                attempts[old].popleft()
            if not attempts[old]:
                del attempts[old]
        if len(attempts) >= 5000 and key not in attempts:
            raise HTTPException(429, "rate_limit")
        bucket = attempts[key]
        if len(bucket) >= limit:
            raise HTTPException(429, "rate_limit")
        bucket.append(now)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        # Never echo raw initData or submitted personal information.
        if request.url.path == '/api/chat':
            return fail(422, 'invalid_request', 'Нужны message (1–2000 символов) и history (до 10 записей user/assistant, content до 12000 символов).')
        return JSONResponse({"detail": "invalid_request"}, status_code=422)

    def current_user(authorization: str = Header(default="")):
        if not authorization.startswith("Bearer ") or len(authorization) > 200:
            raise HTTPException(401, "telegram_login_required")
        uid = database.session_user(session_hash(authorization[7:]))
        if not uid:
            raise HTTPException(401, "session_expired")
        return uid

    @app.middleware("http")
    async def protect_body(request: Request, call_next):
        if request.url.path.startswith("/api/telegram/") and request.method in {"POST", "PUT", "PATCH"}:
            chunks, size = [], 0
            import asyncio
            try:
                async with asyncio.timeout(10):
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > 524288:
                            return JSONResponse({"detail": "body_too_large"}, 413)
                        chunks.append(chunk)
            except TimeoutError:
                return JSONResponse({"detail": "request_timeout"}, 408)
            request._body = b"".join(chunks)
        return await call_next(request)

    @app.post("/api/telegram/login")
    def login(payload: LoginInput, request: Request):
        rate_limit('login:' + (request.client.host if request.client else 'unknown'), 60)
        if not token:
            raise HTTPException(503, "telegram_not_configured")
        try:
            identity = validate_init_data(payload.init_data, token)
        except InvalidTelegramData:
            raise HTTPException(401, "invalid_or_expired_telegram_data") from None
        session, hashed = new_session()
        expires = int(time.time()) + 86400
        return {**database.login(identity, hashed, expires), "token": session, "expires_at": expires}

    @app.get("/api/telegram/me")
    def me(uid=Depends(current_user)):
        return database.profile(uid)

    @app.post("/api/telegram/logout")
    def logout(uid=Depends(current_user), authorization: str = Header()):
        database.logout(session_hash(authorization[7:]))
        return {"ok": True}

    @app.patch("/api/telegram/profile")
    def update_profile(payload: ProfileInput, uid=Depends(current_user)):
        return database.update(uid, payload.model_dump())

    @app.put("/api/telegram/sync")
    def sync(payload: SyncInput, uid=Depends(current_user)):
        # Identity is never editable through a profile or sync payload.
        if set(payload.data) - DATA_KEYS:
            raise HTTPException(422, "unknown_profile_fields")
        if any(len(v) > 120000 for v in payload.data.values()):
            raise HTTPException(422, "profile_value_too_large")
        if payload.data.get("user_status", "") not in STATUSES:
            raise HTTPException(422, "invalid_status")
        return database.update(uid, payload.data)

    async def crm(method, payload):
        # Preserve legacy consultation/application flows without exposing a webhook
        # credential in the Flutter bundle. It is configured only on this server.
        base = os.getenv("BITRIX_WEBHOOK_URL", "").strip().rstrip('/')
        parsed = urlparse(base)
        if parsed.scheme != 'https' or not parsed.hostname or not parsed.hostname.endswith(('.bitrix24.kz', '.bitrix24.com', '.bitrix24.ru')):
            raise HTTPException(503, "crm_not_configured")
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                result = await client.post(f'{base}/{method}.json', json=payload)
                result.raise_for_status()
                data = result.json()
            if 'error' in data or 'result' not in data:
                raise ValueError()
            return data['result']
        except (httpx.HTTPError, ValueError):
            raise HTTPException(502, "crm_unavailable") from None

    @app.post('/api/telegram/leads')
    async def lead(payload: LeadInput, uid=Depends(current_user)):
        rate_limit('lead:' + uid, 5)
        allowed = {'TITLE', 'NAME', 'PHONE', 'COMMENTS', 'SOURCE_ID', 'UTM_SOURCE'}
        if set(payload.fields) - allowed:
            raise HTTPException(422, 'invalid_lead_fields')
        result = await crm('crm.lead.add', {'fields': payload.fields})
        if not str(result).isdigit():
            raise HTTPException(502, 'invalid_lead_response')
        database.save_lead(uid, result)
        return {'result': int(result)}

    @app.post('/api/telegram/leads/status')
    async def statuses(payload: LeadStatusInput, uid=Depends(current_user)):
        rate_limit('statuses:' + uid, 4)
        result = {}
        # Never accept a client-provided Telegram ID or expose another user's lead.
        for lead_id in dict.fromkeys(payload.lead_ids):
            if not lead_id.isdigit() or not database.owns_lead(uid, lead_id):
                continue
            record = await crm('crm.lead.get', {'id': lead_id})
            status = {'CONVERTED': 'accepted', 'JUNK': 'rejected'}.get(record.get('STATUS_ID'), 'pending')
            result[lead_id] = {'found': True, 'status': status}
        return result

    origins = [x.strip() for x in os.getenv("ALLOWED_ORIGINS", "").split(",") if x.strip()]
    if "*" in origins:
        raise RuntimeError("ALLOWED_ORIGINS must list explicit Mini App origins")
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False,
                       allow_methods=["GET", "POST", "PUT", "PATCH", "OPTIONS"],
                       allow_headers=["Authorization", "Content-Type"])
    web_dir = Path(__file__).resolve().parent.parent / 'web_dist'
    if (web_dir / 'index.html').is_file():
        # One origin for both UI and API: no secrets or server URL in the build.
        app.mount('/', StaticFiles(directory=web_dir, html=True), name='miniapp')
        @app.middleware('http')
        async def static_cache(request, call_next):
            response = await call_next(request)
            if not request.url.path.startswith('/api/') and request.url.path != '/health':
                response.headers['Cache-Control'] = 'public, max-age=0, must-revalidate'
            return response
    return app


app = create_app()
