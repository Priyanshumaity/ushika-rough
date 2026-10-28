"""The endpoint Meta calls: subscription handshake (GET) and event delivery (POST)."""

import json
import logging

from django.http import HttpResponse, HttpResponseBadRequest, HttpResponseForbidden, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from . import processing, security
from .models import WebhookEvent

logger = logging.getLogger(__name__)


@method_decorator(csrf_exempt, name="dispatch")
class WhatsAppWebhookView(View):
    http_method_names = ["get", "post"]

    def get(self, request):
        mode = request.GET.get("hub.mode")
        challenge = request.GET.get("hub.challenge")
        if mode != "subscribe" or not challenge:
            return HttpResponseBadRequest("Invalid verification request")
        if not security.is_valid_verify_token(request.GET.get("hub.verify_token")):
            logger.warning("Webhook verification rejected: verify token mismatch")
            return HttpResponseForbidden("Verification token mismatch")
        logger.info("Webhook subscription verified")
        return HttpResponse(challenge, content_type="text/plain")

    def post(self, request):
        body = request.body
        if not security.is_valid_signature(body, request.headers.get("X-Hub-Signature-256")):
            logger.warning("Webhook rejected: missing or invalid X-Hub-Signature-256")
            return HttpResponseForbidden("Invalid signature")

        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return HttpResponseBadRequest("Body is not valid JSON")
        if not isinstance(payload, dict):
            return HttpResponseBadRequest("Body must be a JSON object")

        event, created = processing.record_webhook_event(body, payload)
        if not created and event.status == WebhookEvent.Status.PROCESSED:
            logger.info("Duplicate webhook delivery ignored (event %s)", event.pk)
            return JsonResponse({"status": "duplicate"})

        # Always answer 200 once the signature is valid. A non-200 makes Meta retry
        # the same payload for up to 7 days; failures are kept on the WebhookEvent
        # and can be replayed with `manage.py reprocess_webhooks`.
        processing.process_webhook_event(event)
        return JsonResponse({"status": "ok"})
