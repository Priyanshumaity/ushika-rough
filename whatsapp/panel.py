"""Live operator panel: a single page that reads and acts through the REST API with the staff session."""

from django.contrib.admin.views.decorators import staff_member_required
from django.shortcuts import render


@staff_member_required
def panel(request):
    return render(request, "whatsapp/panel.html")
