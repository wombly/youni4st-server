"""Telegram identity validation and opaque, revocable server sessions."""
import hashlib
import hmac
import json
import secrets
import time
from urllib.parse import parse_qsl, urlparse


class InvalidTelegramData(ValueError):
    pass


def validate_init_data(raw, bot_token, *, now=None, max_age=3600):
    if not bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    if not isinstance(raw, str) or not raw or len(raw) > 16384:
        raise InvalidTelegramData("Invalid initData")
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True)
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError("Duplicate parameter")
        supplied = fields.pop("hash")
        if len(supplied) != 64:
            raise ValueError("Invalid hash")
        # For bot-token HMAC, exclude hash only (signature remains if present).
        data = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
        secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected = hmac.new(secret, data.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, supplied):
            raise ValueError("Signature mismatch")
        age = (time.time() if now is None else now) - int(fields["auth_date"])
        if age < -30 or age > max_age:
            raise ValueError("Expired initData")
        user = json.loads(fields["user"])
        uid = user.get("id")
        if type(uid) is not int or not 0 < uid < 2**53 or user.get("is_bot"):
            raise ValueError("Invalid user")
        first = user.get("first_name")
        if not isinstance(first, str) or not first.strip() or len(first) > 256:
            raise ValueError("Invalid first name")
        result = {"id": str(uid), "first_name": first}
        for field, limit in (("last_name", 256), ("username", 64), ("photo_url", 2048)):
            value = user.get(field) or ""
            if not isinstance(value, str) or len(value) > limit:
                raise ValueError("Invalid user field")
            result[field] = value
        if result["photo_url"] and urlparse(result["photo_url"]).scheme != "https":
            result["photo_url"] = ""
        return result
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise InvalidTelegramData("Telegram data could not be verified") from exc


def new_session():
    token = secrets.token_urlsafe(32)
    return token, session_hash(token)


def session_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()
