"""Strip Discord credentials from text before it is logged, stored or displayed."""
from __future__ import annotations

import re
from typing import Any

_WEBHOOK = re.compile(r"https?://(?:[\w-]+\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/(\d+)/[^\s'\"<>)\]]+")
_TOKEN = re.compile(r"(?<![\w-])(?:[\w-]{24,}\.[\w-]{6,}\.[\w-]{20,}|mfa\.[\w-]{20,})(?![\w-])")


def redact(text: Any) -> str:
    """Webhook IDs are kept because they are not secret and identify the failing rule."""
    return _TOKEN.sub("[TOKEN 已隐藏]", _WEBHOOK.sub(r"[WEBHOOK \1 已隐藏]", str(text)))


def redact_value(value: Any) -> Any:
    """Apply redact to every string inside a JSON-like value."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {key: redact_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    return value
