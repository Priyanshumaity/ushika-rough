"""Outbound messages: build the Cloud API payload, send it, and record the result.

Every send is stored before the API call, so failed sends are kept with Meta's
error. Outside the 24-hour customer-service window WhatsApp only accepts
template messages; that rejection comes back as an ordinary failed send.
"""

import logging
import re

from django.db import IntegrityError, transaction
from django.utils import timezone

from . import processing
from .client import WhatsAppAPIError, WhatsAppClient
from .models import Message

logger = logging.getLogger(__name__)

MEDIA_SEND_TYPES = {"image", "document", "audio", "video", "sticker"}
SEND_TYPES = {"text", "template"} | MEDIA_SEND_TYPES


def normalize_phone(value: str) -> str:
    """'+1 (631) 555-1234' -> '16315551234'. WhatsApp wants digits in international format."""
    return re.sub(r"\D", "", value or "")


def build_payload(
    to: str,
    message_type: str,
    *,
    text: str = "",
    preview_url: bool = False,
    template_name: str = "",
    language_code: str = "en_US",
    components: list | None = None,
    media_link: str = "",
    media_id: str = "",
    caption: str = "",
    filename: str = "",
    reply_to: str = "",
) -> dict:
    payload = {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to, "type": message_type}
    if message_type == "text":
        payload["text"] = {"body": text, "preview_url": preview_url}
    elif message_type == "template":
        template = {"name": template_name, "language": {"code": language_code}}
        if components:
            template["components"] = components
        payload["template"] = template
    elif message_type in MEDIA_SEND_TYPES:
        media = {"id": media_id} if media_id else {"link": media_link}
        if caption and message_type in {"image", "video", "document"}:
            media["caption"] = caption
        if filename and message_type == "document":
            media["filename"] = filename
        payload[message_type] = media
    else:
        raise ValueError(f"Unsupported message type: {message_type}")
    if reply_to:
        payload["context"] = {"message_id": reply_to}
    return payload


def send_message(
    *, to: str, message_type: str, user=None, idempotency_key: str | None = None, client=None, **content
) -> tuple[Message, bool]:
    """Send a message and record it. Returns (message, created).

    created is False when idempotency_key matches an earlier request; that
    message is returned untouched and nothing is sent again.
    """
    if idempotency_key:
        existing = Message.objects.filter(idempotency_key=idempotency_key).first()
        if existing:
            return existing, False

    wa_id = normalize_phone(to)
    payload = build_payload(wa_id, message_type, **content)
    client = client or WhatsAppClient()
    try:
        with transaction.atomic():
            message = Message.objects.create(
                contact=processing.upsert_contact(wa_id),
                direction=Message.Direction.OUTBOUND,
                status=Message.Status.PENDING,
                message_type=message_type,
                body=content.get("text") or content.get("caption") or content.get("template_name", ""),
                phone_number_id=client.phone_number_id,
                context_wa_message_id=content.get("reply_to", ""),
                payload=payload,
                idempotency_key=idempotency_key or None,
                sent_by=user if getattr(user, "is_authenticated", False) else None,
                timestamp=timezone.now(),
            )
    except IntegrityError:
        # A concurrent request with the same idempotency key got there first.
        return Message.objects.get(idempotency_key=idempotency_key), False

    try:
        response = client.send_message(payload)
    except WhatsAppAPIError as exc:
        message.status = Message.Status.FAILED
        message.error_code = str(exc.code or exc.status_code or "")[:32]
        message.error_title = str(exc.title)[:255]
        message.error_details = exc.details
        message.status_updated_at = timezone.now()
        message.save()
        logger.warning("Outbound message %s failed: %s", message.pk, exc)
        return message, True

    sent = (response.get("messages") or [{}])[0]
    message.wa_message_id = sent.get("id")
    message.status = Message.Status.ACCEPTED
    message.status_updated_at = timezone.now()
    # WhatsApp may canonicalise the number (e.g. country-specific digits); follow its wa_id.
    returned_wa_id = ((response.get("contacts") or [{}])[0]).get("wa_id")
    if returned_wa_id and returned_wa_id != message.contact.wa_id:
        message.contact = processing.upsert_contact(returned_wa_id)
    message.save()
    processing.reconcile_orphan_statuses(message)
    message.refresh_from_db()
    logger.info("Outbound %s message %s accepted as %s", message_type, message.pk, message.wa_message_id)
    return message, True
