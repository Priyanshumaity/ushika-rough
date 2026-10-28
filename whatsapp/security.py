"""Webhook authentication: the verify-token handshake and X-Hub-Signature-256 payload signatures."""

import hashlib
import hmac

from django.conf import settings

SIGNATURE_PREFIX = "sha256="


def compute_signature(body: bytes, app_secret: str) -> str:
    """Return the X-Hub-Signature-256 header value Meta sends for `body`."""
    digest = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def is_valid_signature(body: bytes, header_value: str | None) -> bool:
    """True only if the header is an HMAC-SHA256 of the raw body keyed with the app secret.

    With no app secret configured every request is rejected: an unconfigured
    server must not silently accept unsigned payloads.
    """
    app_secret = settings.WHATSAPP["APP_SECRET"]
    if not app_secret or not header_value:
        return False
    header_value = header_value.strip()
    if not header_value.startswith(SIGNATURE_PREFIX):
        return False
    expected = compute_signature(body, app_secret)
    return hmac.compare_digest(expected.encode(), header_value.lower().encode())


def is_valid_verify_token(token: str | None) -> bool:
    expected = settings.WHATSAPP["VERIFY_TOKEN"]
    if not expected or not token:
        return False
    return hmac.compare_digest(expected.encode(), token.encode())
