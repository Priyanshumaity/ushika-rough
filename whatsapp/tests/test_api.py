import json
from datetime import datetime, timezone

import responses
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from rest_framework.authtoken.models import Token
from rest_framework.test import APIClient

from whatsapp.models import Contact, ContactLevel, MediaAttachment, Message, MessageStatusEvent

from .helpers import CUSTOMER, GRAPH, PHONE_NUMBER_ID, WhatsAppTestCase

SEND_URL = f"{GRAPH}/{PHONE_NUMBER_ID}/messages"


def meta_accepts(rsps, wamid="wamid.SENT1", wa_id=CUSTOMER):
    rsps.post(
        SEND_URL,
        json={
            "messaging_product": "whatsapp",
            "contacts": [{"input": wa_id, "wa_id": wa_id}],
            "messages": [{"id": wamid, "message_status": "accepted"}],
        },
    )


class APITestCase(WhatsAppTestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user("operator", password="s3cure-pass-123")
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Token {Token.objects.create(user=self.user).key}")
        self.contact = Contact.objects.create(wa_id=CUSTOMER, profile_name="Kerry Fisher")

    def make_message(self, wamid, direction=Message.Direction.INBOUND, body="hello", day=1, **fields):
        return Message.objects.create(
            wa_message_id=wamid,
            contact=fields.pop("contact", self.contact),
            direction=direction,
            status=Message.Status.RECEIVED if direction == Message.Direction.INBOUND else Message.Status.SENT,
            message_type=fields.pop("message_type", "text"),
            body=body,
            timestamp=datetime(2026, 10, day, tzinfo=timezone.utc),
            **fields,
        )


class AuthTests(APITestCase):
    def test_endpoints_require_authentication(self):
        anonymous = APIClient()
        urls = ["/api/v1/messages/", "/api/v1/contacts/", "/api/v1/attachments/", "/api/v1/statuses/", "/api/v1/auth/me/"]
        for url in urls:
            self.assertEqual(anonymous.get(url).status_code, 401, url)
        self.assertEqual(anonymous.post("/api/v1/messages/send/", {}).status_code, 401)
        self.assertEqual(anonymous.post("/api/v1/attachments/1/save/").status_code, 401)

    def test_token_endpoint_issues_the_users_token(self):
        response = APIClient().post(
            "/api/v1/auth/token/", {"username": "operator", "password": "s3cure-pass-123"}, format="json"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["token"], Token.objects.get(user=self.user).key)

    def test_login_then_check_who_is_logged_in(self):
        token = APIClient().post(
            "/api/v1/auth/token/", {"username": "operator", "password": "s3cure-pass-123"}, format="json"
        ).json()["token"]
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        response = client.get("/api/v1/auth/me/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["username"], "operator")
        self.assertFalse(response.json()["is_superuser"])

    def test_wrong_password_or_token_is_rejected(self):
        response = APIClient().post("/api/v1/auth/token/", {"username": "operator", "password": "nope"}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("token", response.json())
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION="Token not-a-real-token")
        self.assertEqual(client.get("/api/v1/auth/me/").status_code, 401)

    def test_health_is_public(self):
        self.assertEqual(APIClient().get("/health/").json(), {"status": "ok"})


class ReadAPITests(APITestCase):
    def test_list_filter_and_search_messages(self):
        other = Contact.objects.create(wa_id="447700900123")
        self.make_message("wamid.1", body="order status please", day=1)
        self.make_message("wamid.2", direction=Message.Direction.OUTBOUND, body="it shipped", day=2)
        self.make_message("wamid.3", contact=other, body="hi", day=3)

        response = self.api.get("/api/v1/messages/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["count"], 3)
        self.assertEqual([m["wa_message_id"] for m in response.json()["results"]], ["wamid.3", "wamid.2", "wamid.1"])

        def ids(query):
            return [m["wa_message_id"] for m in self.api.get(f"/api/v1/messages/?{query}").json()["results"]]

        self.assertEqual(ids("direction=outbound"), ["wamid.2"])
        self.assertEqual(ids(f"wa_id={CUSTOMER}"), ["wamid.2", "wamid.1"])
        self.assertEqual(ids("search=shipped"), ["wamid.2"])
        self.assertEqual(ids("since=2026-10-02T00:00:00Z"), ["wamid.3", "wamid.2"])
        self.assertEqual(ids("ordering=timestamp&page_size=1"), ["wamid.1"])

    def test_message_detail_includes_attachments_and_status_events(self):
        message = self.make_message("wamid.D", direction=Message.Direction.OUTBOUND, message_type="image")
        MediaAttachment.objects.create(message=message, media_id="m1", media_type="image")
        MessageStatusEvent.objects.create(
            message=message, wa_message_id="wamid.D", status="sent", timestamp=message.timestamp
        )
        data = self.api.get(f"/api/v1/messages/{message.pk}/").json()
        self.assertEqual(data["contact"], {"id": self.contact.pk, "wa_id": CUSTOMER, "profile_name": "Kerry Fisher"})
        self.assertEqual(data["attachments"][0]["media_id"], "m1")
        self.assertIsNone(data["attachments"][0]["download_url"])
        self.assertEqual(data["status_events"][0]["status"], "sent")
        self.assertIn("payload", data)

        statuses = self.api.get(f"/api/v1/messages/{message.pk}/statuses/").json()
        self.assertEqual([s["status"] for s in statuses["results"]], ["sent"])
        by_wamid = self.api.get("/api/v1/statuses/?wa_message_id=wamid.D").json()
        self.assertEqual(by_wamid["count"], 1)

    def test_contacts_include_message_counts_and_level(self):
        self.contact.level = ContactLevel.objects.create(name="Full", can_save_images=True, can_save_pdfs=True)
        self.contact.save()
        self.make_message("wamid.1", day=1)
        self.make_message("wamid.2", day=5)
        Contact.objects.create(wa_id="447700900123")
        results = self.api.get("/api/v1/contacts/").json()["results"]
        self.assertEqual(results[0]["wa_id"], CUSTOMER)
        self.assertEqual(results[0]["message_count"], 2)
        self.assertEqual(results[0]["last_message_at"], "2026-10-05T00:00:00Z")
        self.assertEqual(results[0]["level"], "Full")
        self.assertEqual(results[1]["message_count"], 0)
        self.assertIsNone(results[1]["level"])
        by_level = self.api.get(f"/api/v1/contacts/?level={self.contact.level.pk}").json()["results"]
        self.assertEqual([c["wa_id"] for c in by_level], [CUSTOMER])

    def test_download_serves_the_stored_file(self):
        message = self.make_message("wamid.F", message_type="document")
        attachment = MediaAttachment(
            message=message, media_id="m2", media_type="document", mime_type="application/pdf",
            download_status=MediaAttachment.DownloadStatus.DOWNLOADED,
        )
        attachment.file.save("report.pdf", ContentFile(b"%PDF-data"), save=True)

        response = self.api.get(f"/api/v1/attachments/{attachment.pk}/download/", HTTP_ACCEPT="application/pdf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"%PDF-data")
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn('attachment; filename="report.pdf"', response["Content-Disposition"])

    def test_download_of_a_file_that_is_not_ready_is_404(self):
        message = self.make_message("wamid.P", message_type="image")
        attachment = MediaAttachment.objects.create(message=message, media_id="m3", media_type="image")
        response = self.api.get(f"/api/v1/attachments/{attachment.pk}/download/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "This attachment has not been downloaded.")

    def test_openapi_schema_is_served(self):
        self.user.is_staff = True
        self.user.save()
        self.api.force_authenticate(self.user)
        response = self.api.get("/api/schema/")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"/api/v1/messages/send/", response.content)


class SendAPITests(APITestCase):
    def send(self, **body):
        return self.api.post("/api/v1/messages/send/", body, format="json")

    @responses.activate
    def test_send_text_message(self):
        meta_accepts(responses)
        response = self.send(to="+1 (631) 555-1234", type="text", text="Your order shipped", reply_to="wamid.IN")

        self.assertEqual(response.status_code, 201, response.content)
        sent = json.loads(responses.calls[0].request.body)
        self.assertEqual(
            sent,
            {
                "messaging_product": "whatsapp",
                "recipient_type": "individual",
                "to": CUSTOMER,
                "type": "text",
                "text": {"body": "Your order shipped", "preview_url": False},
                "context": {"message_id": "wamid.IN"},
            },
        )
        self.assertEqual(responses.calls[0].request.headers["Authorization"], "Bearer test-access-token")

        message = Message.objects.get(direction=Message.Direction.OUTBOUND)
        self.assertEqual(message.wa_message_id, "wamid.SENT1")
        self.assertEqual(message.status, Message.Status.ACCEPTED)
        self.assertEqual(message.contact, self.contact)
        self.assertEqual(message.sent_by, self.user)
        self.assertEqual(response.json()["wa_message_id"], "wamid.SENT1")
        self.assertEqual(response.json()["sent_by"], "operator")

    @responses.activate
    def test_send_template_message(self):
        meta_accepts(responses)
        components = [{"type": "body", "parameters": [{"type": "text", "text": "Kerry"}]}]
        response = self.send(to=CUSTOMER, type="template", template_name="order_update",
                             language_code="en_GB", components=components)
        self.assertEqual(response.status_code, 201, response.content)
        sent = json.loads(responses.calls[0].request.body)
        self.assertEqual(
            sent["template"], {"name": "order_update", "language": {"code": "en_GB"}, "components": components}
        )

    @responses.activate
    def test_send_document_by_link(self):
        meta_accepts(responses)
        response = self.send(to=CUSTOMER, type="document", media_link="https://example.com/i.pdf",
                             caption="Invoice", filename="invoice.pdf")
        self.assertEqual(response.status_code, 201, response.content)
        sent = json.loads(responses.calls[0].request.body)
        self.assertEqual(
            sent["document"], {"link": "https://example.com/i.pdf", "caption": "Invoice", "filename": "invoice.pdf"}
        )

    @responses.activate
    def test_rejected_send_is_recorded_as_failed(self):
        responses.post(
            SEND_URL,
            status=400,
            json={"error": {"message": "(#131030) Recipient phone number not in allowed list", "type": "OAuthException",
                            "code": 131030, "error_data": {"messaging_product": "whatsapp",
                                                           "details": "Recipient phone number not in allowed list"}}},
        )
        response = self.send(to=CUSTOMER, type="text", text="hello")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["status"], "failed")
        message = Message.objects.get(direction=Message.Direction.OUTBOUND)
        self.assertEqual(message.status, Message.Status.FAILED)
        self.assertIsNone(message.wa_message_id)
        self.assertEqual(message.error_code, "131030")
        self.assertEqual(message.error_details, "Recipient phone number not in allowed list")

    @responses.activate
    def test_idempotency_key_prevents_a_double_send(self):
        meta_accepts(responses)
        first = self.send(to=CUSTOMER, type="text", text="once", idempotency_key="order-42-shipped")
        second = self.send(to=CUSTOMER, type="text", text="once", idempotency_key="order-42-shipped")

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.json()["id"], second.json()["id"])
        self.assertEqual(len(responses.calls), 1)

    def test_validation_errors(self):
        cases = [
            ({"to": CUSTOMER, "type": "text"}, "text"),
            ({"to": CUSTOMER, "type": "template"}, "template_name"),
            ({"to": CUSTOMER, "type": "image"}, "media_link"),
            ({"to": CUSTOMER, "type": "image", "media_id": "1", "media_link": "https://x.test/a.jpg"}, "media_link"),
            ({"to": CUSTOMER, "type": "image", "media_link": "http://x.test/a.jpg"}, "media_link"),
            ({"to": CUSTOMER, "type": "audio", "media_id": "1", "caption": "no"}, "caption"),
            ({"to": CUSTOMER, "type": "image", "media_id": "1", "filename": "a.jpg"}, "filename"),
            ({"to": CUSTOMER, "type": "template", "template_name": "x", "text": "no"}, "text"),
            ({"to": "12", "type": "text", "text": "hi"}, "to"),
            ({"to": CUSTOMER, "type": "location", "text": "hi"}, "type"),
        ]
        for body, field in cases:
            with self.subTest(body=body):
                response = self.send(**body)
                self.assertEqual(response.status_code, 400, response.content)
                self.assertIn(field, response.json())
        self.assertFalse(Message.objects.exists())
