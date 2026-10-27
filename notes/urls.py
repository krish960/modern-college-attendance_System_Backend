"""URL routes for notes: the staff API and the public student pages."""
from django.urls import path
from rest_framework.routers import SimpleRouter

from .views import NoteViewSet, PublicFileView, PublicNotesView, ShareView

# Teacher/HOD/admin side (needs sign-in): manage notes under /notes/.
router = SimpleRouter()
router.register("notes", NoteViewSet, basename="note")

urlpatterns = [
    path("notes/share/", ShareView.as_view(), name="notes-share"),
] + router.urls

# What students open: /s/<code>/ and /s/<code>/f/<id>/ (plain pages straight from the server, not part of the app and not under /api/).
public_urlpatterns = [
    path("s/<str:token>/", PublicNotesView.as_view(), name="public-notes"),
    path("s/<str:token>/f/<int:note_id>/", PublicFileView.as_view(), name="public-note-file"),
]
