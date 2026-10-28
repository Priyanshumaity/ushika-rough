import base64
import hashlib
from io import StringIO

import responses
from django.contrib.auth import get_user_model
from django.core.management import call_command
from rest_framework.test import APIClient

from whatsapp.media import media_kind
from whatsapp.models import Contact, ContactLevel, MediaAttachment

from .helpers import (
    ACCESS_TOKEN,
    CUSTOMER,
    GRAPH,
    WhatsAppTestCase,
    audio_message,
    contact_entry,
    document_message,
    envelope,
    image_message,
    mock_media,
    whatsapp_settings,
)

JPEG = b"\xff\xd8\xff\xe0" + b"fake-jpeg-bytes" * 20
PDF = b"%PDF-1.7\n" + b"fake-pdf-bytes" * 20
OGG = b"OggS" + b"fake-voice-bytes" * 20
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class MediaDownloadTests(WhatsAppTestCase):
    """Download mechanics, with automatic download on and a contact whose level allows images and PDFs."""

    def setUp(self):
        level = ContactLevel.objects.create(name="Full", can_save_images=True, can_save_pdfs=True)
        Contact.objects.create(wa_id=CUSTOMER, level=level)

    def receive(self, message):
        response = self.post_webhook(envelope(contacts=[contact_entry()], messages=[message]))
        self.assertEqual(response.status_code, 200)
        return MediaAttachment.objects.get()

    def test_image_is_downloaded_and_stored_locally(self):
        with responses.RequestsMock() as rsps:
            mock_media(rsps, "111222333", JPEG)
            attachment = self.receive(image_message())
            auth_headers = [call.request.headers["Authorization"] for call in rsps.calls]

        self.assertEqual(attachment.download_status, MediaAttachment.DownloadStatus.DOWNLOADED)
        self.assertEqual(attachment.download_attempts, 1)
        self.assertEqual(attachment.mime_type, "image/jpeg")
        self.assertEqual(attachment.file_size, len(JPEG))
        self.assertEqual(attachment.caption, "A photo")
        self.assertEqual(attachment.message.body, "A photo")
        # whatsapp/YYYY/MM/<media_id>/<name>; Django adds a suffix rather than overwrite an existing file.
        self.assertRegex(attachment.file.name, r"^whatsapp/\d{4}/\d{2}/111222333/image_111222333(_\w+)?\.jpg$")
        with attachment.file.open("rb") as handle:
            self.assertEqual(handle.read(), JPEG)
        # Both the metadata lookup and the file download carry the bearer token.
        self.assertEqual(auth_headers, [f"Bearer {ACCESS_TOKEN}"] * 2)

    def test_document_filename_is_made_safe(self):
        with responses.RequestsMock() as rsps:
            mock_media(rsps, "444555666", PDF, mime_type="application/pdf")
            attachment = self.receive(document_message(filename="../../secret plans.pdf"))

        self.assertEqual(attachment.download_status, MediaAttachment.DownloadStatus.DOWNLOADED)
        self.assertNotIn("..", attachment.file.name)
        self.assertRegex(attachment.file.name, r"/444555666/secret_plans(_\w+)?\.pdf$")
        self.assertEqual(attachment.message.body, "../../secret plans.pdf")

    def test_sticker_and_voice_note_get_usable_extensions(self):
        from whatsapp.media import safe_filename

        sticker = MediaAttachment(media_id="9", media_type="sticker")
        voice = MediaAttachment(media_id="8", media_type="audio")
        self.assertEqual(safe_filename(sticker, "image/webp"), "sticker_9.webp")
        self.assertEqual(safe_filename(voice, "audio/ogg; codecs=opus"), "audio_8.ogg")

    def test_base64_sha256_is_accepted(self):
        digest = base64.b64encode(hashlib.sha256(JPEG).digest()).decode()
        with responses.RequestsMock() as rsps:
            mock_media(rsps, "111222333", JPEG, sha256=digest)
            attachment = self.receive(image_message())
        self.assertEqual(attachment.download_status, MediaAttachment.DownloadStatus.DOWNLOADED)

    def test_hash_mismatch_fails_without_saving_a_file(self):
        with responses.RequestsMock() as rsps:
            mock_media(rsps, "111222333", JPEG, sha256="0" * 64)
            attachment = self.receive(image_message())
        self.assertEqual(attachment.download_status, MediaAttachment.DownloadStatus.FAILED)
        self.assertIn("SHA-256", attachment.last_error)
        self.assertFalse(attachment.file)

    def test_files_over_the_size_limit_are_rejected(self):
        with whatsapp_settings(MEDIA_MAX_BYTES=100):
            with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
                mock_media(rsps, "111222333", JPEG)  # Meta declares the size up front
                declared = self.receive(image_message())
            self.assertEqual(declared.download_status, MediaAttachment.DownloadStatus.FAILED)
            self.assertIn("limit", declared.last_error)
            declared.delete()

            with responses.RequestsMock() as rsps:
                mock_media(rsps, "777", JPEG, file_size="")  # no declared size: enforced while streaming
                streamed = self.receive(image_message(wamid="wamid.IMG2", media_id="777"))
            self.assertEqual(streamed.download_status, MediaAttachment.DownloadStatus.FAILED)
            self.assertIn("limit", streamed.last_error)
            self.assertFalse(streamed.file)

    def test_html_error_page_is_not_saved_as_media(self):
        with responses.RequestsMock() as rsps:
            mock_media(rsps, "111222333", b"<html>Sorry, this content isn't available</html>",
                       sha256="", content_type="text/html")
            attachment = self.receive(image_message())
        self.assertEqual(attachment.download_status, MediaAttachment.DownloadStatus.FAILED)
        self.assertIn("HTML", attachment.last_error)

    def test_failed_download_can_be_saved_again_through_the_api(self):
        with responses.RequestsMock() as rsps:
            rsps.get(f"{GRAPH}/111222333", status=500, json={"error": {"message": "Temporary", "code": 2}})
            attachment = self.receive(image_message())
        self.assertEqual(attachment.download_status, MediaAttachment.DownloadStatus.FAILED)
        self.assertIn("Temporary", attachment.last_error)

        client = APIClient()
        client.force_authenticate(get_user_model().objects.create_user("ops"))
        with responses.RequestsMock() as rsps:
            mock_media(rsps, "111222333", JPEG)
            response = client.post(f"/api/v1/attachments/{attachment.pk}/save/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["download_status"], "downloaded")
        self.assertEqual(response.json()["download_attempts"], 2)
        self.assertTrue(response.json()["download_url"].endswith(f"/api/v1/attachments/{attachment.pk}/download/"))

    def test_disabled_media_types_are_skipped(self):
        with whatsapp_settings(MEDIA_TYPES=["image"]):
            with responses.RequestsMock() as rsps:
                attachment = self.receive(document_message())
                self.assertEqual(len(rsps.calls), 0)
        self.assertEqual(attachment.download_status, MediaAttachment.DownloadStatus.SKIPPED)
        self.assertEqual(attachment.filename, "invoice.pdf")

    def test_retry_command_respects_the_attempt_limit(self):
        with responses.RequestsMock() as rsps:
            rsps.get(f"{GRAPH}/111222333", status=500, json={"error": {"message": "Temporary", "code": 2}})
            attachment = self.receive(image_message())
        MediaAttachment.objects.filter(pk=attachment.pk).update(download_attempts=5)

        with responses.RequestsMock() as rsps:
            call_command("retry_media_downloads", stdout=StringIO(), stderr=StringIO())
            self.assertEqual(len(rsps.calls), 0)

        with responses.RequestsMock() as rsps:
            mock_media(rsps, "111222333", JPEG)
            call_command("retry_media_downloads", str(attachment.pk), stdout=StringIO(), stderr=StringIO())
        attachment.refresh_from_db()
        self.assertEqual(attachment.download_status, MediaAttachment.DownloadStatus.DOWNLOADED)


class SaveRulesTests(WhatsAppTestCase):
    """Images and PDFs are saved only when the contact's level allows them; other media only by admins."""

    def setUp(self):
        self.contact = Contact.objects.create(wa_id=CUSTOMER)
        self.operator = APIClient()
        self.operator.force_authenticate(get_user_model().objects.create_user("operator"))
        self.admin = APIClient()
        self.admin.force_authenticate(get_user_model().objects.create_superuser("root"))

    def set_level(self, **permissions):
        self.contact.level = ContactLevel.objects.create(name="Test level", **permissions)
        self.contact.save()

    def receive(self, message):
        """Deliver with automatic download off (the default): the file waits to be saved on request."""
        with whatsapp_settings(DOWNLOAD_MEDIA=False), responses.RequestsMock() as rsps:
            response = self.post_webhook(envelope(contacts=[contact_entry()], messages=[message]))
            self.assertEqual(len(rsps.calls), 0)
        self.assertEqual(response.status_code, 200)
        attachment = MediaAttachment.objects.get(message__wa_message_id=message["id"])
        self.assertEqual(attachment.download_status, MediaAttachment.DownloadStatus.SKIPPED)
        return attachment

    def save(self, client, attachment):
        return client.post(f"/api/v1/attachments/{attachment.pk}/save/")

    def test_image_is_saved_on_request_when_the_level_allows_it(self):
        self.set_level(can_save_images=True)
        attachment = self.receive(image_message())
        with responses.RequestsMock() as rsps:
            mock_media(rsps, "111222333", JPEG)
            response = self.save(self.operator, attachment)

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["download_status"], "downloaded")
        attachment.refresh_from_db()
        with attachment.file.open("rb") as handle:
            self.assertEqual(handle.read(), JPEG)

    def test_level_allows_images_and_pdfs_separately(self):
        self.set_level(can_save_images=True)
        attachment = self.receive(document_message())
        with responses.RequestsMock():  # any call to Meta would fail the test
            response = self.save(self.operator, attachment)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "This contact's level does not allow saving PDFs.")
        attachment.refresh_from_db()
        self.assertEqual(attachment.download_status, MediaAttachment.DownloadStatus.SKIPPED)
        self.assertEqual(attachment.download_attempts, 0)

        self.contact.level.can_save_pdfs = True
        self.contact.level.save()
        with responses.RequestsMock() as rsps:
            mock_media(rsps, "444555666", PDF, mime_type="application/pdf")
            self.assertEqual(self.save(self.operator, attachment).status_code, 200)

    def test_contact_without_a_level_has_nothing_saved_even_by_an_admin(self):
        attachment = self.receive(image_message())
        with responses.RequestsMock():
            response = self.save(self.admin, attachment)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["detail"], "This contact's level does not allow saving images.")

    def test_other_media_can_only_be_saved_by_an_admin(self):
        self.set_level(can_save_images=True, can_save_pdfs=True)
        voice = self.receive(audio_message())
        word = self.receive(document_message(wamid="wamid.DOC2", media_id="555", filename="notes.docx",
                                             mime_type=DOCX))
        with responses.RequestsMock():
            self.assertEqual(self.save(self.operator, voice).json()["detail"], "Only admins can save audio files.")
            self.assertEqual(self.save(self.operator, word).json()["detail"], "Only admins can save non-PDF documents.")

        with responses.RequestsMock() as rsps:
            mock_media(rsps, "888999000", OGG, mime_type="audio/ogg")
            response = self.save(self.admin, voice)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["download_status"], "downloaded")

    def test_only_pdf_documents_count_as_pdfs(self):
        kinds = [
            media_kind(MediaAttachment(media_type="image", mime_type="image/jpeg")),
            media_kind(MediaAttachment(media_type="document", mime_type="application/pdf")),
            media_kind(MediaAttachment(media_type="document", mime_type=DOCX)),
            media_kind(MediaAttachment(media_type="sticker", mime_type="image/webp")),
        ]
        self.assertEqual(kinds, ["image", "pdf", "other", "other"])

    def test_automatic_download_follows_the_same_rules(self):
        self.set_level(can_save_images=True)
        with responses.RequestsMock() as rsps:  # automatic download is on in the test settings
            mock_media(rsps, "111222333", JPEG)
            self.post_webhook(envelope(contacts=[contact_entry()],
                                       messages=[image_message(), document_message(), audio_message()]))
        statuses = dict(MediaAttachment.objects.values_list("media_type", "download_status"))
        self.assertEqual(statuses, {"image": "downloaded", "document": "skipped", "audio": "skipped"})

    def test_retry_command_honours_the_contact_level(self):
        attachment = self.receive(image_message())
        stderr = StringIO()
        with responses.RequestsMock():
            call_command("retry_media_downloads", str(attachment.pk), stdout=StringIO(), stderr=stderr)
        self.assertIn("does not allow saving images", stderr.getvalue())
        attachment.refresh_from_db()
        self.assertEqual(attachment.download_status, MediaAttachment.DownloadStatus.SKIPPED)
