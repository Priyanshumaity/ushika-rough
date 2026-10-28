"""Background work. Runs on Celery when settings.USE_CELERY is on, otherwise inline after the DB commit."""

import logging

from celery import shared_task
from django.conf import settings
from django.db import transaction

from . import media
from .models import MediaAttachment

logger = logging.getLogger(__name__)


@shared_task(bind=True, max_retries=3, ignore_result=True)
def download_media_task(self, attachment_id: int) -> None:
    attachment = MediaAttachment.objects.filter(pk=attachment_id).first()
    if attachment is None:
        logger.warning("Attachment %s no longer exists; skipping download", attachment_id)
        return
    outcome = media.download_attachment(attachment)
    # Only a Celery worker retries with backoff; inline runs leave failures for
    # the retry endpoint or `manage.py retry_media_downloads`.
    if not outcome.ok and outcome.retryable and not self.request.called_directly:
        raise self.retry(countdown=30 * 2**self.request.retries)


def schedule_media_download(attachment_id: int) -> None:
    """Run the download once the surrounding transaction commits, so the row is visible to it."""

    def run():
        if settings.USE_CELERY:
            download_media_task.delay(attachment_id)
        else:
            download_media_task(attachment_id)

    # robust=True: a failing download must not turn the webhook response into a 500.
    transaction.on_commit(run, robust=True)
