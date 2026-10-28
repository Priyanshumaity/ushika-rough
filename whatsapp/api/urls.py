from django.urls import include, path
from rest_framework.authtoken.views import obtain_auth_token
from rest_framework.routers import DefaultRouter

from . import views

app_name = "api"

router = DefaultRouter()
router.register("contacts", views.ContactViewSet, basename="contact")
router.register("messages", views.MessageViewSet, basename="message")
router.register("attachments", views.MediaAttachmentViewSet, basename="attachment")
router.register("statuses", views.MessageStatusEventViewSet, basename="status")

urlpatterns = [
    path("auth/token/", obtain_auth_token, name="token"),
    path("auth/me/", views.current_user, name="me"),
    path("", include(router.urls)),
]
