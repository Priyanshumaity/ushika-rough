"""Turn Cloud API webhook payloads into Contact / Message / MediaAttachment / MessageStatusEvent rows.

Meta can deliver the same event more than once (retries, re-batched deliveries),
so every write here is idempotent: messages are keyed on their wamid and status
events on (wamid, status, timestamp). Concurrent duplicates are caught by the
database unique constraints, not just by the existence checks.
"""

import hashlib
import logging
from datetime import datetime, timezone as dt_timezone

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from . import media, tasks
from .models import Contact, MediaAttachment, Message, MessageStatusEvent, WebhookEvent

logger = logging.getLogger(__name__)

MEDIA_MESSAGE_TYPES = {"image", "document", "audio", "video", "sticker"}


# --- Webhook events -----------------------------------------------------------


def record_webhook_event(body: bytes, payload: dict) -> tuple[WebhookEvent, bool]:
    return WebhookEvent.objects.get_or_create(
        payload_sha256=hashlib.sha256(body).hexdigest(),
        defaults={"payload": payload, "object_type": str(payload.get("object", ""))[:64]},
    )


def process_webhook_event(event: WebhookEvent) -> bool:
    """Process a stored event. Failures are recorded on the event, never raised."""
    event.attempts += 1
    try:
        with transaction.atomic():
            process_payload(event.payload)
    except Exception as exc:
        logger.exception("Failed to process webhook event %s", event.pk)
        event.status = WebhookEvent.Status.FAILED
        event.error = f"{type(exc).__name__}: {exc}"
    else:
        event.status = WebhookEvent.Status.PROCESSED
        event.error = ""
        event.processed_at = timezone.now()
    event.save(update_fields=["attempts", "status", "error", "processed_at"])
    return event.status == WebhookEvent.Status.PROCESSED


def process_payload(payload: dict) -> None:
    if payload.get("object") != "whatsapp_business_account":
        logger.info("Ignoring webhook for object %r", payload.get("object"))
        return
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") != "messages":
                logger.info("Ignoring webhook field %r", change.get("field"))
                continue
            process_messages_value(change.get("value") or {})


def process_messages_value(value: dict) -> None:
    phone_number_id = (value.get("metadata") or {}).get("phone_number_id", "")
    profile_names = {
        contact["wa_id"]: (contact.get("profile") or {}).get("name", "")
        for contact in value.get("contacts") or []
        if contact.get("wa_id")
    }
    for raw in value.get("messages") or []:
        store_inbound_message(raw, phone_number_id, profile_names)
    for raw in value.get("statuses") or []:
        record_status(raw)
    for error in value.get("errors") or []:
        logger.error("WhatsApp reported a webhook-level error: %s", error)


# --- Inbound messages ---------------------------------------------------------


def store_inbound_message(raw: dict, phone_number_id: str = "", profile_names: dict | None = None) -> Message | None:
    """Store one inbound message. Returns None for duplicates and malformed entries."""
    wamid = raw.get("id")
    sender = raw.get("from")
    if not wamid or not sender:
        logger.warning("Skipping inbound message without id/from")
        return None
    if Message.objects.filter(wa_message_id=wamid).exists():
        logger.info("Duplicate message %s ignored", wamid)
        return None

    contact = upsert_contact(sender, (profile_names or {}).get(sender, ""))
    message_type = raw.get("type") or "unknown"
    error = first_error(raw.get("errors"))
    try:
        with transaction.atomic():
            message = Message.objects.create(
                wa_message_id=wamid,
                contact=contact,
                direction=Message.Direction.INBOUND,
                status=Message.Status.RECEIVED,
                message_type=message_type,
                body=extract_body(raw),
                phone_number_id=phone_number_id,
                context_wa_message_id=(raw.get("context") or {}).get("id", ""),
                payload=raw,
                timestamp=parse_timestamp(raw.get("timestamp")),
                **error,
            )
    except IntegrityError:
        logger.info("Duplicate message %s ignored (concurrent delivery)", wamid)
        return None

    media = raw.get(message_type) if message_type in MEDIA_MESSAGE_TYPES else None
    if isinstance(media, dict) and media.get("id"):
        create_attachment(message, message_type, media)
    logger.info("Stored inbound %s message %s", message_type, wamid)  # no phone numbers or text in logs
    return message


def create_attachment(message: Message, media_type: str, info: dict) -> MediaAttachment:
    attachment = MediaAttachment(
        message=message,
        media_id=str(info["id"]),
        media_type=media_type,
        mime_type=info.get("mime_type", ""),
        sha256=info.get("sha256", ""),
        filename=info.get("filename", ""),
        caption=info.get("caption", ""),
    )
    # Normally nothing is downloaded here: files are saved on request through the API.
    # With WHATSAPP_DOWNLOAD_MEDIA on, files the contact's level allows are fetched now.
    config = settings.WHATSAPP
    downloadable = (
        config["DOWNLOAD_MEDIA"]
        and media_type in config["MEDIA_TYPES"]
        and not media.save_refusal(attachment, admin=False)
    )
    attachment.download_status = (
        MediaAttachment.DownloadStatus.PENDING if downloadable else MediaAttachment.DownloadStatus.SKIPPED
    )
    attachment.save()
    if downloadable:
        tasks.schedule_media_download(attachment.pk)
    return attachment


def upsert_contact(wa_id: str, profile_name: str = "") -> Contact:
    contact, created = Contact.objects.get_or_create(wa_id=wa_id, defaults={"profile_name": profile_name})
    if not created and profile_name and contact.profile_name != profile_name:
        contact.profile_name = profile_name
        contact.save(update_fields=["profile_name", "updated_at"])
    return contact


def extract_body(raw: dict) -> str:
    """A human-readable text for any message type, used for listing and search."""
    message_type = raw.get("type")
    content = raw.get(message_type) if isinstance(raw.get(message_type), dict) else {}
    if message_type == "text":
        return content.get("body", "")
    if message_type in MEDIA_MESSAGE_TYPES:
        return content.get("caption", "") or content.get("filename", "")
    if message_type == "button":
        return content.get("text", "")
    if message_type == "interactive":
        reply = content.get("button_reply") or content.get("list_reply") or {}
        return reply.get("title", "")
    if message_type == "reaction":
        return content.get("emoji", "")
    if message_type == "location":
        label = " - ".join(part for part in (content.get("name"), content.get("address")) if part)
        coords = f"{content.get('latitude')},{content.get('longitude')}"
        return f"{label} ({coords})" if label else coords
    if message_type == "contacts":
        names = [(c.get("name") or {}).get("formatted_name", "") for c in raw.get("contacts") or []]
        return ", ".join(name for name in names if name)
    return ""


# --- Delivery statuses ----------------------------------------------------------


def record_status(raw: dict) -> MessageStatusEvent | None:
    """Store one status notification and advance the message's status. None for duplicates."""
    wamid = raw.get("id")
    status = raw.get("status")
    if not wamid or not status:
        logger.warning("Skipping status without id/status")
        return None

    message = Message.objects.filter(wa_message_id=wamid).first()
    try:
        with transaction.atomic():
            event = MessageStatusEvent.objects.create(
                message=message,
                wa_message_id=wamid,
                status=status,
                timestamp=parse_timestamp(raw.get("timestamp")),
                recipient_id=raw.get("recipient_id", ""),
                conversation_id=(raw.get("conversation") or {}).get("id", ""),
                pricing_category=(raw.get("pricing") or {}).get("category", ""),
                payload=raw,
                **first_error(raw.get("errors")),
            )
    except IntegrityError:
        logger.info("Duplicate status %s for %s ignored", status, wamid)
        return None

    if message is None:
        # Sent from outside USHIKA, or the webhook beat our own send() to saving
        # the wamid; reconcile_orphan_statuses() links it up in the second case.
        logger.info("Status %s for unknown message %s recorded without a message", status, wamid)
    else:
        apply_status(message, event)
    return event


def apply_status(message: Message, event: MessageStatusEvent) -> bool:
    """Move the message's status forward. Stale or unknown statuses are kept only as events."""
    rank = Message.STATUS_RANK.get(event.status)
    if rank is None:
        return False
    lower = [status for status, other in Message.STATUS_RANK.items() if other < rank]
    fields = {"status": event.status, "status_updated_at": event.timestamp, "updated_at": timezone.now()}
    if event.status == Message.Status.FAILED:
        fields.update(
            error_code=event.error_code, error_title=event.error_title, error_details=event.error_details
        )
    # A conditional UPDATE, so two concurrent status webhooks can't move it backwards.
    updated = Message.objects.filter(pk=message.pk, status__in=lower).update(**fields)
    if updated:
        message.refresh_from_db()
    return bool(updated)


def reconcile_orphan_statuses(message: Message) -> None:
    """Attach status events that arrived before this outbound message's wamid was saved."""
    if not message.wa_message_id:
        return
    orphans = MessageStatusEvent.objects.filter(wa_message_id=message.wa_message_id, message__isnull=True)
    for event in orphans.order_by("timestamp", "id"):
        event.message = message
        event.save(update_fields=["message"])
        apply_status(message, event)


# --- Helpers ------------------------------------------------------------------


def parse_timestamp(value) -> datetime:
    try:
        return datetime.fromtimestamp(int(value), tz=dt_timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return timezone.now()


def first_error(errors) -> dict:
    """Map the first entry of a Cloud API `errors` array to the model's error fields."""
    if not errors:
        return {}
    error = errors[0] or {}
    return {
        "error_code": str(error.get("code", ""))[:32],
        "error_title": (error.get("title") or error.get("message") or "")[:255],
        "error_details": (error.get("error_data") or {}).get("details", "") or error.get("message", ""),
    }
