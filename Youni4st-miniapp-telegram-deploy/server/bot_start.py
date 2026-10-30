"""Private /start greeting; Telegram credentials never leave the backend."""
from contextlib import asynccontextmanager
import hashlib
import hmac
import logging
import os
import httpx
from fastapi import HTTPException, Request

APP_URL = 'https://youni4st-server.onrender.com'

def webhook_secret(token):
    return hmac.new(token.encode(), b'youni4st-start-webhook-v1', hashlib.sha256).hexdigest()

def greeting(message):
    if not isinstance(message, dict): return None
    chat = message.get('chat', {})
    sender = message.get('from', {})
    text = message.get('text', '').split()
    if chat.get('type') != 'private' or not text or text[0].lower() not in ('/start', '/start@youni4st_bot'): return None
    if not isinstance(chat.get('id'), int) or sender.get('is_bot'): return None
    name = str(sender.get('first_name', '')).strip()[:100]
    hello = f'Привет, {name}! 👋' if name else 'Привет! 👋'
    return {'chat_id': chat['id'], 'text': hello + '\n\nДобро пожаловать в Youni4st!\n\nЗдесь ты можешь выбрать вуз, найти курсы, работу и стажировки, создать резюме и задать вопрос AI-Сократусу.\n\nНажми кнопку ниже — начнём твой следующий шаг!',
            'reply_markup': {'inline_keyboard': [[{'text':'Открыть Youni4st 🚀','web_app': {'url': APP_URL}}]]}}

def install_bot(app, token, *, transport=None):
    secret = webhook_secret(token)
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with previous(application):
            if os.getenv('RENDER') and token:
                try:
                    async with httpx.AsyncClient(timeout=15, transport=transport) as client:
                        r = await client.post(f'https://api.telegram.org/bot{token}/setWebhook', json={
                            'url': APP_URL+'/api/telegram/webhook', 'secret_token': secret,
                            'allowed_updates': ['message'], 'drop_pending_updates': False})
                        if r.status_code != 200 or not r.json().get('ok'):
                            logging.warning('Telegram /start webhook registration failed; check bot token')
                except Exception:
                    logging.warning('Telegram /start webhook registration unavailable; redeploy to retry')
            yield
    app.router.lifespan_context = lifespan

    @app.post('/api/telegram/webhook')
    async def webhook(request: Request):
        supplied = request.headers.get('X-Telegram-Bot-Api-Secret-Token', '')
        if not token or not hmac.compare_digest(supplied, secret):
            raise HTTPException(403, 'invalid_webhook')
        try: update = await request.json()
        except ValueError: raise HTTPException(400, 'invalid_update') from None
        payload = greeting(update.get('message')) if isinstance(update, dict) else None
        if payload:
            try:
                async with httpx.AsyncClient(timeout=15, transport=transport) as client:
                    r = await client.post(f'https://api.telegram.org/bot{token}/sendMessage', json=payload)
                    if r.status_code == 403: return {'ok': True}
                    if r.status_code != 200 or not r.json().get('ok'): raise ValueError()
            except Exception:
                raise HTTPException(502, 'telegram_delivery_failed') from None
        return {'ok': True}
