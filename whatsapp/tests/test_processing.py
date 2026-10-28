from datetime import datetime, timezone

import responses

from whatsapp import messaging
from whatsapp.models import Contact, Message, MessageStatusEvent
from whatsapp.processing import process_payload, record_status

from .helpers import (
    CUSTOMER,
    GRAPH,
    PHONE_NUMBER_ID,
    WhatsAppTestCase,
    contact_entry,
    envelope,
    status_entry,
    text_message,
)


def outbound_message(wamid="wamid.OUT1", status=Message.Status.ACCEPTED):
    contact, _ = Contact.objects.get_or_create(wa_id=CUSTOMER)
    return Message.objects.create(
        wa_message_id=wamid,
        contact=contact,
        direction=Message.Direction.OUTBOUND,
        status=status,
        message_type="text",
        body="Hi",
        timestamp=datetime(2026, 10, 27, tzinfo=timezone.utc),
    )


class InboundMessageTests(WhatsAppTestCase):
    def test_text_message_creates_contact_and_message(self):
        process_payload(envelope(contacts=[contact_entry()], messages=[text_message(timestamp=1761600000)]))

        message = Message.objects.get()
        self.assertEqual(message.wa_message_id, "wamid.TEXT1")
        self.assertEqual(message.direction, Message.Direction.INBOUND)
        self.assertEqual(message.status, Message.Status.RECEIVED)
        self.assertEqual(message.message_type, "text")
        self.assertEqual(message.body, "Hello USHIKA")
        self.assertEqual(message.phone_number_id, PHONE_NUMBER_ID)
        self.assertEqual(message.timestamp, datetime.fromtimestamp(1761600000, tz=timezone.utc))
        self.assertEqual(message.payload["text"]["body"], "Hello USHIKA")
        self.assertEqual(message.contact.wa_id, CUSTOMER)
        self.assertEqual(message.contact.profile_name, "Kerry Fisher")

    def test_profile_name_follows_the_latest_message(self):
        process_payload(envelope(contacts=[contact_entry(name="Kerry")], messages=[text_message("wamid.A")]))
        process_payload(envelope(contacts=[contact_entry(name="Kerry F.")], messages=[text_message("wamid.B")]))
        self.assertEqual(Contact.objects.get().profile_name, "Kerry F.")

    def test_same_message_in_a_different_delivery_is_stored_once(self):
        process_payload(envelope(contacts=[contact_entry()], messages=[text_message()]))
        # Same wamid, different envelope (so a different body hash): still one row.
        process_payload(envelope(contacts=[contact_entry()], messages=[text_message(), text_message("wamid.NEW")]))
        self.assertEqual(Message.objects.count(), 2)
        self.assertEqual(Message.objects.filter(wa_message_id="wamid.TEXT1").count(), 1)

    def test_malformed_messages_are_skipped(self):
        process_payload(envelope(messages=[{"type": "text", "text": {"body": "no id"}}]))
        self.assertFalse(Message.objects.exists())

    def test_body_is_extracted_for_non_text_types(self):
        messages = [
            {"from": CUSTOMER, "id": "wamid.I", "timestamp": "1", "type": "interactive",
             "interactive": {"type": "button_reply", "button_reply": {"id": "yes", "title": "Yes please"}}},
            {"from": CUSTOMER, "id": "wamid.L", "timestamp": "1", "type": "location",
             "location": {"latitude": -1.28, "longitude": 36.82, "name": "Office", "address": "Nairobi"}},
            {"from": CUSTOMER, "id": "wamid.R", "timestamp": "1", "type": "reaction",
             "reaction": {"message_id": "wamid.X", "emoji": "👍"}},
            {"from": CUSTOMER, "id": "wamid.B", "timestamp": "1", "type": "button",
             "button": {"payload": "STOP", "text": "Stop promotions"}},
        ]
        process_payload(envelope(contacts=[contact_entry()], messages=messages))
        bodies = dict(Message.objects.values_list("wa_message_id", "body"))
        self.assertEqual(bodies["wamid.I"], "Yes please")
        self.assertEqual(bodies["wamid.L"], "Office - Nairobi (-1.28,36.82)")
        self.assertEqual(bodies["wamid.R"], "👍")
        self.assertEqual(bodies["wamid.B"], "Stop promotions")

    def test_reply_context_is_stored(self):
        process_payload(envelope(messages=[text_message(context={"from": PHONE_NUMBER_ID, "id": "wamid.ORIGINAL"})]))
        self.assertEqual(Message.objects.get().context_wa_message_id, "wamid.ORIGINAL")

    def test_unsupported_message_keeps_whatsapps_error(self):
        unsupported = {
            "from": CUSTOMER, "id": "wamid.U", "timestamp": "1", "type": "unsupported",
            "errors": [{"code": 131051, "title": "Message type unknown",
                        "error_data": {"details": "Message type is currently not supported."}}],
        }
        process_payload(envelope(messages=[unsupported]))
        message = Message.objects.get()
        self.assertEqual(message.error_code, "131051")
        self.assertEqual(message.error_title, "Message type unknown")
        self.assertEqual(message.error_details, "Message type is currently not supported.")


class StatusTests(WhatsAppTestCase):
    def test_statuses_advance_and_never_go_backwards(self):
        message = outbound_message()
        process_payload(envelope(statuses=[status_entry("wamid.OUT1", "sent", 100)]))
        process_payload(envelope(statuses=[status_entry("wamid.OUT1", "read", 300)]))
        process_payload(envelope(statuses=[status_entry("wamid.OUT1", "delivered", 200)]))  # arrives late

        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.READ)
        self.assertEqual(message.status_updated_at, datetime.fromtimestamp(300, tz=timezone.utc))
        self.assertEqual(
            list(message.status_events.values_list("status", flat=True)), ["sent", "delivered", "read"]
        )

    def test_failed_status_records_the_error(self):
        message = outbound_message()
        errors = [{"code": 131047, "title": "Re-engagement message",
                   "error_data": {"details": "More than 24 hours have passed since the customer last replied."}}]
        process_payload(envelope(statuses=[status_entry("wamid.OUT1", "failed", 100, errors=errors)]))

        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.FAILED)
        self.assertEqual(message.error_code, "131047")
        self.assertIn("24 hours", message.error_details)

    def test_duplicate_status_is_ignored(self):
        outbound_message()
        payload = envelope(statuses=[status_entry("wamid.OUT1", "delivered", 100)])
        process_payload(payload)
        self.assertIsNone(record_status(status_entry("wamid.OUT1", "delivered", 100)))
        self.assertEqual(MessageStatusEvent.objects.count(), 1)

    def test_unknown_status_values_are_kept_without_changing_the_message(self):
        message = outbound_message()
        process_payload(envelope(statuses=[status_entry("wamid.OUT1", "deleted", 100)]))
        message.refresh_from_db()
        self.assertEqual(message.status, Message.Status.ACCEPTED)
        self.assertEqual(MessageStatusEvent.objects.get().status, "deleted")

    @responses.activate
    def test_status_that_beats_the_send_response_is_linked_afterwards(self):
        # Meta can fire the "sent" webhook before our send() call has saved the wamid.
        process_payload(envelope(statuses=[status_entry("wamid.RACE", "sent", 100)]))
        self.assertIsNone(MessageStatusEvent.objects.get().message)

        responses.post(
            f"{GRAPH}/{PHONE_NUMBER_ID}/messages",
            json={"messaging_product": "whatsapp", "contacts": [{"input": CUSTOMER, "wa_id": CUSTOMER}],
                  "messages": [{"id": "wamid.RACE"}]},
        )
        message, _ = messaging.send_message(to=CUSTOMER, message_type="text", text="hi")

        self.assertEqual(message.status, Message.Status.SENT)
        self.assertEqual(MessageStatusEvent.objects.get().message, message)
