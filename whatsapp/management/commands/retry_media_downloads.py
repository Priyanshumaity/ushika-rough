from django.conf import settings
from django.core.management.base import BaseCommand

from whatsapp.media import download_attachment, save_refusal
from whatsapp.models import MediaAttachment


class Command(BaseCommand):
    help = (
        "Retry pending and failed media downloads that are still under the attempt limit. "
        "Runs as an admin, but images and PDFs still need a contact level that allows them."
    )

    def add_arguments(self, parser):
        parser.add_argument("ids", nargs="*", type=int, help="Attachment ids to retry, ignoring the attempt limit.")

    def handle(self, *args, ids, **options):
        if ids:
            attachments = MediaAttachment.objects.filter(pk__in=ids)
        else:
            attachments = MediaAttachment.objects.filter(
                download_status__in=[MediaAttachment.DownloadStatus.PENDING, MediaAttachment.DownloadStatus.FAILED],
                download_attempts__lt=settings.WHATSAPP["MEDIA_MAX_ATTEMPTS"],
            )
        ok = failed = refused = 0
        for attachment in attachments.select_related("message__contact__level").order_by("created_at"):
            refusal = save_refusal(attachment, admin=True)
            if refusal:
                refused += 1
                self.stderr.write(f"Attachment {attachment.pk} not saved: {refusal}")
                continue
            outcome = download_attachment(attachment)
            if outcome.ok:
                ok += 1
            else:
                failed += 1
                self.stderr.write(f"Attachment {attachment.pk} failed: {outcome.error}")
        self.stdout.write(self.style.SUCCESS(f"{ok} downloaded, {failed} failed, {refused} not allowed."))
