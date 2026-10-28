from django.core.management.base import BaseCommand

from whatsapp.models import WebhookEvent
from whatsapp.processing import process_webhook_event


class Command(BaseCommand):
    help = "Replay stored webhook events that failed processing (or specific ones by id)."

    def add_arguments(self, parser):
        parser.add_argument("ids", nargs="*", type=int, help="Event ids to replay. Default: every failed event.")

    def handle(self, *args, ids, **options):
        events = WebhookEvent.objects.filter(pk__in=ids) if ids else WebhookEvent.objects.filter(
            status=WebhookEvent.Status.FAILED
        )
        processed = failed = 0
        for event in events.order_by("received_at"):
            if process_webhook_event(event):
                processed += 1
            else:
                failed += 1
                self.stderr.write(f"Event {event.pk} failed again: {event.error}")
        self.stdout.write(self.style.SUCCESS(f"{processed} processed, {failed} failed."))
