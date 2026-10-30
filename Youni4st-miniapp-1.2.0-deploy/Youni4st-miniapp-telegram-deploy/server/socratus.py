"""Android-compatible, stateless Gemini adapter. Never log prompts or API keys."""
import asyncio
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
import math
import os
from pathlib import Path
import re
import time
from typing import Annotated, Literal

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

BASE = "https://generativelanguage.googleapis.com/v1beta"
MAX_REPLY_CHARS = 500
SYSTEM = (
    "Ты AI-Сократус, учебный собеседник. Отвечай на языке пользователя. "
    "Ответ должен занимать не более 500 символов, включая пробелы и переносы строк. "
    "Формулируй короткий законченный ответ. "
    "Помогай понять задачу: задавай уточняющие вопросы, объясняй шаги. "
    "Признавай неопределенность. Не запрашивай персональные данные. "
    "Не выдавай себя за человека, врача или психолога и не давай медицинских рекомендаций."
)


@dataclass(frozen=True)
class Settings:
    api_key: str = ""
    model: str = ""
    enabled: bool = False
    timeout: float = 45
    per_ip: int = 6
    global_rpm: int = 20
    concurrency: int = 2

    def __post_init__(self):
        if self.model and not re.fullmatch(r"[a-zA-Z0-9._-]{1,100}", self.model):
            raise ValueError("GEMINI_MODEL must be a model ID without models/ or URL")
        if not 1 <= self.timeout <= 55:
            raise ValueError("GEMINI_TIMEOUT_SECONDS must be 1..55")
        if not all(1 <= n <= 10000 for n in (self.per_ip, self.global_rpm, self.concurrency)):
            raise ValueError("Rate and concurrency limits must be 1..10000")

    @classmethod
    def from_env(cls):
        load_dotenv(Path(__file__).with_name(".env"), override=False)
        return cls(
            api_key=os.getenv("GEMINI_API_KEY", "").strip(),
            model=os.getenv("GEMINI_MODEL", "").strip(),
            enabled=os.getenv("CHAT_ENABLED", "false").lower() == "true",
            timeout=float(os.getenv("GEMINI_TIMEOUT_SECONDS", "45")),
            per_ip=int(os.getenv("RATE_LIMIT_PER_IP", "6")),
            global_rpm=int(os.getenv("RATE_LIMIT_GLOBAL", "20")),
            concurrency=int(os.getenv("MAX_CONCURRENT_REQUESTS", "2")),
        )


Text = Annotated[str, StringConstraints(strict=True, strip_whitespace=True, min_length=1)]


class Turn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["user", "assistant"]
    content: Annotated[Text, Field(max_length=12000)]


class ChatInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: Annotated[Text, Field(max_length=2000)]
    history: list[Turn] = Field(default_factory=list, max_length=10)


class ChatOutput(BaseModel):
    reply: str = Field(min_length=1, max_length=MAX_REPLY_CHARS)


class ServiceError(Exception):
    def __init__(self, status, code, message, retry=None):
        self.status, self.code, self.message, self.retry = status, code, message, retry


class UTF8JSONResponse(JSONResponse):
    # Windows PowerShell 5 otherwise may decode JSON as a legacy code page.
    media_type = "application/json; charset=utf-8"


def fail(status, code, message, retry=None):
    headers = {"Retry-After": str(retry)} if retry else None
    return UTF8JSONResponse({"error": {"code": code, "message": message}}, status, headers=headers)


class Limiter:
    """Single-process sliding window; bounded memory, no forwarding-header trust."""
    def __init__(self, settings, clock=time.monotonic):
        self.s, self.clock = settings, clock
        self.ips, self.all = {}, deque()

    def admit(self, ip):
        now = self.clock()
        for key in list(self.ips):
            q = self.ips[key]
            while q and q[0] <= now - 60:
                q.popleft()
            if not q:
                del self.ips[key]
        while self.all and self.all[0] <= now - 60:
            self.all.popleft()
        q = self.ips.get(ip, deque())
        for bucket, limit in ((q, self.s.per_ip), (self.all, self.s.global_rpm)):
            if len(bucket) >= limit:
                raise ServiceError(429, "rate_limit", "Слишком много запросов. Подождите.", max(1, math.ceil(60 - now + bucket[0])))
        if ip not in self.ips and len(self.ips) >= 5000:
            raise ServiceError(429, "rate_limit", "Сервер занят. Подождите.", 60)
        q.append(now)
        self.ips[ip] = q
        self.all.append(now)


async def gemini_reply(client, settings, payload):
    contents = [{"role": "model" if t.role == "assistant" else "user", "parts": [{"text": t.content}]} for t in payload.history]
    contents.append({"role": "user", "parts": [{"text": payload.message}]})
    body = {"contents": contents, "systemInstruction": {"parts": [{"text": SYSTEM}]},
            "generationConfig": {"maxOutputTokens": 2048}, "store": False}
    try:
        # A hard total deadline complements HTTPX's per-operation timeouts. No retries.
        async with asyncio.timeout(settings.timeout):
            response = await client.post(f"{BASE}/models/{settings.model}:generateContent", json=body,
                                         headers={"x-goog-api-key": settings.api_key})
    except (TimeoutError, httpx.TimeoutException):
        raise ServiceError(504, "gemini_timeout", "Gemini не ответил вовремя. Попробуйте позже.") from None
    except httpx.RequestError:
        raise ServiceError(503, "gemini_connection", "Не удалось связаться с Gemini.", 30) from None
    status = response.status_code
    if status == 429:
        raise ServiceError(429, "gemini_quota", "Лимит Gemini исчерпан. Проверьте квоты в AI Studio; оплата автоматически не включается.", 60)
    if status in (400, 401, 402, 403, 404):
        raise ServiceError(503, "gemini_configuration", "Администратору нужно проверить ключ, модель, регион и доступ проекта.")
    if status >= 500 or status == 408:
        raise ServiceError(503, "gemini_unavailable", "Gemini временно недоступен.", 30)
    if status != 200:
        raise ServiceError(502, "gemini_error", "Неожиданный ответ Gemini.")
    try:
        data = response.json()
        if data.get("promptFeedback", {}).get("blockReason"):
            raise ServiceError(422, "content_blocked", "Gemini отклонил запрос по правилам безопасности.")
        candidate = data.get("candidates", [{}])[0]
        reason = candidate.get("finishReason")
        if reason not in (None, "STOP", "MAX_TOKENS"):
            raise ServiceError(422, "content_blocked", "Gemini не смог безопасно завершить ответ. Переформулируйте запрос.")
        text = "".join(p.get("text", "") for p in candidate.get("content", {}).get("parts", []) if not p.get("thought")).strip()
        if not text:
            raise ValueError("Empty answer")
        if len(text) > MAX_REPLY_CHARS:
            text = text[:MAX_REPLY_CHARS - 1].rstrip() + "…"
        return text
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        raise ServiceError(502, "gemini_empty", "Gemini вернул пустой или некорректный ответ.") from None


def create_app(settings=None, transport=None):
    s = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(timeout=httpx.Timeout(s.timeout, connect=5),
                                     transport=transport, follow_redirects=False, trust_env=False) as client:
            app.state.gemini = client
            yield

    app = FastAPI(title="AI-Сократус", lifespan=lifespan, default_response_class=UTF8JSONResponse,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.limiter = Limiter(s)
    app.state.active = 0

    @app.exception_handler(ServiceError)
    async def service_error(request, exc):
        return fail(exc.status, exc.code, exc.message, exc.retry)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        # Do not echo the submitted input or validation context.
        return fail(422, "invalid_request", "Нужны message (1–2000 символов) и history (до 10 записей user/assistant, content до 12000 символов).")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.path == "/api/chat":
            if not s.enabled:
                return fail(503, "chat_disabled", "Чат отключён администратором.")
            if not s.api_key or not s.model:
                return fail(503, "not_configured", "Администратору нужно задать GEMINI_API_KEY и GEMINI_MODEL.")
            try:
                app.state.limiter.admit(request.client.host if request.client else "unknown")
            except ServiceError as exc:
                return fail(exc.status, exc.code, exc.message, exc.retry)
            if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
                return fail(415, "content_type", "Используйте application/json.")
            # Stream and cap bytes before FastAPI parses JSON, including chunked requests.
            chunks, size = [], 0
            try:
                async with asyncio.timeout(10):
                    async for chunk in request.stream():
                        size += len(chunk)
                        if size > 524288:
                            return fail(413, "body_too_large", "Запрос превышает 512 КиБ.")
                        chunks.append(chunk)
            except TimeoutError:
                return fail(408, "body_timeout", "Запрос передавался слишком долго.")
            request._body = b"".join(chunks)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/health")
    async def health():
        return {"status": "ok", "chat_enabled": s.enabled, "configured": bool(s.api_key and s.model)}

    @app.post("/api/chat", response_model=ChatOutput)
    async def chat(payload: ChatInput):
        if app.state.active >= s.concurrency:
            raise ServiceError(503, "server_busy", "Сервер занят. Попробуйте позже.", 5)
        app.state.active += 1
        try:
            return ChatOutput(reply=await gemini_reply(app.state.gemini, s, payload))
        finally:
            app.state.active -= 1

    return app


app = create_app()
