"""Shared fixtures: test settings, Cloud API payload builders, and a signed-webhook test case."""

import hashlib
import json
import shutil
import tempfile

from django.test import TestCase, override_settings
from django.urls import reverse

from whatsapp.security import compute_signature

APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
ACCESS_TOKEN = "test-access-token"
PHONE_NUMBER_ID = "109876543210"
GRAPH = "https://graph.facebook.com/v25.0"
CUSTOMER = "16315551234"

TEST_WHATSAPP = {
    "ACCESS_TOKEN": ACCESS_TOKEN,
    "PHONE_NUMBER_ID": PHONE_NUMBER_ID,
    "BUSINESS_ACCOUNT_ID": "WABA123",
    "APP_SECRET": APP_SECRET,
    "VERIFY_TOKEN": VERIFY_TOKEN,
    "API_VERSION": "v25.0",
    "GRAPH_BASE_URL": "https://graph.facebook.com",
    "REQUEST_TIMEOUT": 5,
    "DOWNLOAD_MEDIA": True,
    "MEDIA_TYPES": ["image", "document", "audio", "video", "sticker"],
    "MEDIA_MAX_BYTES": 1024 * 1024,
    "MEDIA_MAX_ATTEMPTS": 5,
}


def whatsapp_settings(**overrides):
    return override_settings(WHATSAPP={**TEST_WHATSAPP, **overrides})


# --- Payload builders (shapes follow Meta's Cloud API webhook reference) -------------


def envelope(*, messages=None, statuses=None, contacts=None, field="messages"):
    value = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "15550001111", "phone_number_id": PHONE_NUMBER_ID},
    }
    if contacts is not None:
        value["contacts"] = contacts
    if messages is not None:
        value["messages"] = messages
    if statuses is not None:
        value["statuses"] = statuses
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "WABA123", "changes": [{"field": field, "value": value}]}],
    }


def contact_entry(wa_id=CUSTOMER, name="Kerry Fisher"):
    return {"profile": {"name": name}, "wa_id": wa_id}


def text_message(wamid="wamid.TEXT1", body="Hello USHIKA", sender=CUSTOMER, timestamp=1761600000, **extra):
    return {"from": sender, "id": wamid, "timestamp": str(timestamp), "type": "text", "text": {"body": body}, **extra}


def image_message(wamid="wamid.IMG1", media_id="111222333", sha256="", caption="A photo", sender=CUSTOMER):
    return {
        "from": sender,
        "id": wamid,
        "timestamp": "1761600100",
        "type": "image",
        "image": {"caption": caption, "mime_type": "image/jpeg", "sha256": sha256, "id": media_id},
    }


def document_message(
    wamid="wamid.DOC1", media_id="444555666", filename="invoice.pdf", mime_type="application/pdf", sender=CUSTOMER
):
    return {
        "from": sender,
        "id": wamid,
        "timestamp": "1761600200",
        "type": "document",
        "document": {"filename": filename, "mime_type": mime_type, "sha256": "", "id": media_id},
    }


def audio_message(wamid="wamid.AUD1", media_id="888999000", sender=CUSTOMER):
    return {
        "from": sender,
        "id": wamid,
        "timestamp": "1761600300",
        "type": "audio",
        "audio": {"mime_type": "audio/ogg; codecs=opus", "sha256": "", "id": media_id, "voice": True},
    }


def status_entry(wamid, status, timestamp, recipient=CUSTOMER, errors=None):
    entry = {"id": wamid, "status": status, "timestamp": str(timestamp), "recipient_id": recipient}
    if errors:
        entry["errors"] = errors
    return entry


def mock_media(rsps, media_id, content, *, mime_type="image/jpeg", sha256=None, file_size=None, content_type=None):
    """Register Meta's two media calls: the metadata lookup and the file download."""
    url = f"https://lookaside.fbsbx.com/whatsapp_business/attachments/{media_id}"
    rsps.get(
        f"{GRAPH}/{media_id}",
        json={
            "messaging_product": "whatsapp",
            "url": url,
            "mime_type": mime_type,
            "sha256": hashlib.sha256(content).hexdigest() if sha256 is None else sha256,
            "file_size": len(content) if file_size is None else file_size,
            "id": media_id,
        },
    )
    rsps.get(url, body=content, content_type=content_type or mime_type)
    return url


class WhatsAppTestCase(TestCase):
    """Runs with test credentials, inline background tasks, and a throwaway MEDIA_ROOT."""

    @classmethod
    def setUpClass(cls):
        cls.media_root = tempfile.mkdtemp(prefix="ushika-test-media-")
        cls.addClassCleanup(shutil.rmtree, cls.media_root, ignore_errors=True)
        cls.enterClassContext(
            override_settings(
                WHATSAPP=TEST_WHATSAPP,
                MEDIA_ROOT=cls.media_root,
                USE_CELERY=False,
                PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],  # fast test users
            )
        )
        super().setUpClass()

    def post_webhook(self, payload, *, secret=APP_SECRET, signature=None, raw_body=None):
        body = raw_body if raw_body is not None else json.dumps(payload).encode()
        if signature is None:
            signature = compute_signature(body, secret)
        headers = {"X-Hub-Signature-256": signature} if signature else {}
        # Background work is scheduled with transaction.on_commit; run it as a real request would.
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(
                reverse("whatsapp:webhook"), data=body, content_type="application/json", headers=headers
            )
