from django.conf import settings
from django.db import models
from django.utils import timezone
from django.utils.text import get_valid_filename


class ContactLevel(models.Model):
    """Which of a contact's media files may be saved. Levels are created and assigned in the admin."""

    name = models.CharField(max_length=64, unique=True)
    can_save_images = models.BooleanField(default=False)
    can_save_pdfs = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Contact(models.Model):
    wa_id = models.CharField(
        max_length=32, unique=True, help_text="WhatsApp ID: the phone number in international format, digits only."
    )
    profile_name = models.CharField(max_length=255, blank=True)
    level = models.ForeignKey(
        ContactLevel,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="contacts",
        help_text="Decides whether this contact's images and PDFs can be saved. No level: neither can.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]

    def __str__(self):
        return f"{self.profile_name} ({self.wa_id})" if self.profile_name else self.wa_id


class WebhookEvent(models.Model):
    """Raw audit copy of every signed webhook delivery, keyed by the body's hash."""

    class Status(models.TextChoices):
        RECEIVED = "received"
        PROCESSED = "processed"
        FAILED = "failed"

    payload_sha256 = models.CharField(max_length=64, unique=True)
    payload = models.JSONField()
    object_type = models.CharField(max_length=64, blank=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RECEIVED, db_index=True)
    error = models.TextField(blank=True)
    attempts = models.PositiveIntegerField(default=0)
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-received_at"]

    def __str__(self):
        return f"WebhookEvent {self.pk} ({self.status})"


class Message(models.Model):
    class Direction(models.TextChoices):
        INBOUND = "inbound"
        OUTBOUND = "outbound"

    class Status(models.TextChoices):
        RECEIVED = "received"  # inbound messages
        PENDING = "pending"  # outbound: stored, not yet accepted by Meta
        ACCEPTED = "accepted"  # outbound: Cloud API accepted the send request
        SENT = "sent"
        DELIVERED = "delivered"
        READ = "read"
        FAILED = "failed"

    # Outbound statuses only ever move forward; a late "delivered" never overwrites "read".
    STATUS_RANK = {
        Status.PENDING: 0,
        Status.ACCEPTED: 1,
        Status.SENT: 2,
        Status.DELIVERED: 3,
        Status.READ: 4,
        Status.FAILED: 5,
    }

    wa_message_id = models.CharField(
        max_length=255, unique=True, null=True, blank=True, help_text="WhatsApp message id (wamid)."
    )
    contact = models.ForeignKey(Contact, on_delete=models.PROTECT, related_name="messages")
    direction = models.CharField(max_length=8, choices=Direction.choices, db_index=True)
    message_type = models.CharField(max_length=32, db_index=True)
    status = models.CharField(max_length=16, choices=Status.choices, db_index=True)
    body = models.TextField(blank=True, help_text="Text body, media caption, or a readable summary.")
    phone_number_id = models.CharField(max_length=64, blank=True, help_text="Business phone number id.")
    context_wa_message_id = models.CharField(max_length=255, blank=True, help_text="wamid this message replies to.")
    payload = models.JSONField(default=dict, blank=True, help_text="Raw inbound message or outbound request.")
    error_code = models.CharField(max_length=32, blank=True)
    error_title = models.CharField(max_length=255, blank=True)
    error_details = models.TextField(blank=True)
    idempotency_key = models.CharField(max_length=64, unique=True, null=True, blank=True)
    sent_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    timestamp = models.DateTimeField(db_index=True, help_text="When WhatsApp received it, or when we sent it.")
    status_updated_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-timestamp", "-id"]
        indexes = [models.Index(fields=["contact", "-timestamp"], name="message_contact_timestamp")]

    def __str__(self):
        return f"{self.direction} {self.message_type} {self.wa_message_id or self.pk}"


def attachment_upload_to(instance, filename):
    now = timezone.now()
    return f"whatsapp/{now:%Y/%m}/{get_valid_filename(instance.media_id)}/{filename}"


class MediaAttachment(models.Model):
    class DownloadStatus(models.TextChoices):
        PENDING = "pending"
        DOWNLOADED = "downloaded"
        FAILED = "failed"
        SKIPPED = "skipped"  # not downloaded on arrival; save it on request through the API

    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="attachments")
    media_id = models.CharField(max_length=255, db_index=True)
    media_type = models.CharField(max_length=32, help_text="image, document, audio, video or sticker.")
    mime_type = models.CharField(max_length=255, blank=True)
    sha256 = models.CharField(max_length=128, blank=True, help_text="Hash reported by WhatsApp.")
    filename = models.CharField(max_length=255, blank=True, help_text="Original filename (documents).")
    caption = models.TextField(blank=True)
    file = models.FileField(upload_to=attachment_upload_to, max_length=500, blank=True)
    file_size = models.PositiveBigIntegerField(null=True, blank=True)
    download_status = models.CharField(
        max_length=16, choices=DownloadStatus.choices, default=DownloadStatus.PENDING, db_index=True
    )
    download_attempts = models.PositiveIntegerField(default=0)
    last_error = models.TextField(blank=True)
    downloaded_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.media_type} {self.media_id} ({self.download_status})"


class MessageStatusEvent(models.Model):
    """One delivery-status notification (sent/delivered/read/failed) for an outbound message."""

    message = models.ForeignKey(
        Message, null=True, blank=True, on_delete=models.CASCADE, related_name="status_events"
    )
    wa_message_id = models.CharField(max_length=255, db_index=True)
    status = models.CharField(max_length=16, db_index=True)
    timestamp = models.DateTimeField()
    recipient_id = models.CharField(max_length=64, blank=True)
    conversation_id = models.CharField(max_length=255, blank=True)
    pricing_category = models.CharField(max_length=64, blank=True)
    error_code = models.CharField(max_length=32, blank=True)
    error_title = models.CharField(max_length=255, blank=True)
    error_details = models.TextField(blank=True)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["timestamp", "id"]
        constraints = [
            models.UniqueConstraint(fields=["wa_message_id", "status", "timestamp"], name="unique_status_event"),
        ]

    def __str__(self):
        return f"{self.wa_message_id} -> {self.status}"
