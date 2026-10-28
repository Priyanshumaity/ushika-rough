from unittest import mock

from django.core.management import call_command
from django.urls import reverse

from whatsapp.models import Message, WebhookEvent
from whatsapp.security import compute_signature

from .helpers import (
    APP_SECRET,
    VERIFY_TOKEN,
    WhatsAppTestCase,
    contact_entry,
    envelope,
    text_message,
    whatsapp_settings,
)


class WebhookVerificationTests(WhatsAppTestCase):
    def verify(self, **params):
        query = {"hub.mode": "subscribe", "hub.verify_token": VERIFY_TOKEN, "hub.challenge": "1158201444", **params}
        return self.client.get(reverse("whatsapp:webhook"), query)

    def test_returns_challenge_for_correct_token(self):
        response = self.verify()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"1158201444")
        self.assertEqual(response["Content-Type"], "text/plain")

    def test_rejects_wrong_token(self):
        self.assertEqual(self.verify(**{"hub.verify_token": "nope"}).status_code, 403)

    def test_rejects_other_modes_and_missing_challenge(self):
        self.assertEqual(self.verify(**{"hub.mode": "unsubscribe"}).status_code, 400)
        self.assertEqual(self.verify(**{"hub.challenge": ""}).status_code, 400)

    def test_rejects_everything_when_verify_token_is_not_configured(self):
        with whatsapp_settings(VERIFY_TOKEN=""):
            self.assertEqual(self.verify(**{"hub.verify_token": ""}).status_code, 403)


class WebhookSignatureTests(WhatsAppTestCase):
    payload = envelope(contacts=[contact_entry()], messages=[text_message()])

    def test_missing_signature_is_rejected(self):
        response = self.post_webhook(self.payload, signature="")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(WebhookEvent.objects.exists())

    def test_signature_from_another_secret_is_rejected(self):
        response = self.post_webhook(self.payload, secret="someone-elses-secret")
        self.assertEqual(response.status_code, 403)
        self.assertFalse(Message.objects.exists())

    def test_tampered_body_is_rejected(self):
        original = b'{"object": "whatsapp_business_account", "entry": []}'
        tampered = b'{"object": "whatsapp_business_account", "entry": [{}]}'
        response = self.post_webhook(None, raw_body=tampered, signature=compute_signature(original, APP_SECRET))
        self.assertEqual(response.status_code, 403)

    def test_everything_is_rejected_when_app_secret_is_not_configured(self):
        with whatsapp_settings(APP_SECRET=""):
            response = self.post_webhook(self.payload, secret="")
        self.assertEqual(response.status_code, 403)

    def test_signed_invalid_json_is_a_bad_request(self):
        self.assertEqual(self.post_webhook(None, raw_body=b"not json").status_code, 400)
        self.assertEqual(self.post_webhook(None, raw_body=b"[1, 2]").status_code, 400)

    def test_valid_signature_is_accepted_and_event_stored(self):
        response = self.post_webhook(self.payload)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})
        event = WebhookEvent.objects.get()
        self.assertEqual(event.status, WebhookEvent.Status.PROCESSED)
        self.assertEqual(event.object_type, "whatsapp_business_account")
        self.assertEqual(Message.objects.count(), 1)


class WebhookDeliveryTests(WhatsAppTestCase):
    def test_identical_redelivery_is_acknowledged_as_duplicate(self):
        payload = envelope(contacts=[contact_entry()], messages=[text_message()])
        self.assertEqual(self.post_webhook(payload).json(), {"status": "ok"})
        second = self.post_webhook(payload)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json(), {"status": "duplicate"})
        self.assertEqual(WebhookEvent.objects.count(), 1)
        self.assertEqual(Message.objects.count(), 1)

    def test_processing_failure_is_recorded_and_can_be_replayed(self):
        payload = envelope(contacts=[contact_entry()], messages=[text_message()])
        with mock.patch("whatsapp.processing.process_payload", side_effect=RuntimeError("database hiccup")):
            response = self.post_webhook(payload)
        # Still 200, so Meta does not retry the same payload for days.
        self.assertEqual(response.status_code, 200)
        event = WebhookEvent.objects.get()
        self.assertEqual(event.status, WebhookEvent.Status.FAILED)
        self.assertIn("database hiccup", event.error)
        self.assertFalse(Message.objects.exists())

        call_command("reprocess_webhooks", stdout=mock.MagicMock(), stderr=mock.MagicMock())
        event.refresh_from_db()
        self.assertEqual(event.status, WebhookEvent.Status.PROCESSED)
        self.assertEqual(event.attempts, 2)
        self.assertEqual(Message.objects.count(), 1)

    def test_redelivery_of_a_failed_event_is_processed_again(self):
        payload = envelope(contacts=[contact_entry()], messages=[text_message()])
        with mock.patch("whatsapp.processing.process_payload", side_effect=RuntimeError("boom")):
            self.post_webhook(payload)
        self.assertEqual(self.post_webhook(payload).json(), {"status": "ok"})
        self.assertEqual(Message.objects.count(), 1)

    def test_other_fields_and_objects_are_ignored(self):
        self.assertEqual(self.post_webhook(envelope(field="account_update")).status_code, 200)
        self.assertEqual(self.post_webhook({"object": "page", "entry": []}).status_code, 200)
        self.assertFalse(Message.objects.exists())
        self.assertEqual(WebhookEvent.objects.filter(status=WebhookEvent.Status.PROCESSED).count(), 2)
