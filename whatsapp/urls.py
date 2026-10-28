from django.urls import path

from .webhooks import WhatsAppWebhookView

app_name = "whatsapp"

urlpatterns = [
    path("whatsapp/", WhatsAppWebhookView.as_view(), name="webhook"),
]
