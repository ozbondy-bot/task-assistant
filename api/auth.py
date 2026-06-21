import hashlib
import hmac
import json
import os
from urllib.parse import unquote, parse_qsl


def validate_telegram_init_data(init_data: str, bot_token: str) -> dict | None:
    """
    Validates Telegram WebApp InitData and returns parsed user dict, or None if invalid.
    See: https://core.telegram.org/bots/webapps#validating-data-received-via-the-web-app
    """
    try:
        parsed = dict(parse_qsl(init_data, keep_blank_values=True))
        hash_received = parsed.pop("hash", None)
        if not hash_received:
            return None

        data_check_string = "\n".join(
            f"{k}={v}" for k, v in sorted(parsed.items())
        )
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        expected_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(expected_hash, hash_received):
            return None

        user_json = parsed.get("user")
        if not user_json:
            return None

        return json.loads(unquote(user_json))
    except Exception:
        return None
