"""Database tables for notes: the class share link and the notes themselves."""
import os
import secrets
import uuid

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.db import models

# Uploaded notes live outside any web-served folder; they are only handed out by the views in this app.
class PrivateStorage(FileSystemStorage):
    """Reads the folder from settings each time, so tests (and a changed UPLOAD_DIR) work without a restart."""

    @property
    def base_location(self):
        return str(settings.UPLOAD_DIR)

    @property
    def location(self):
        return os.path.abspath(self.base_location)


def get_private_storage():
    """Storage factory used by Note.file (a function so migrations stay simple)."""
    return PrivateStorage()


def new_token():
    """Make a new random secret for a class share link."""
    return secrets.token_urlsafe(24)  # 192 bits: cannot be guessed


def note_path(instance, filename):
    """Decide where on disk an uploaded note is saved: one folder per class, random file name."""
    return f"notes/{instance.classroom_id}/{uuid.uuid4().hex}"  # the original name is kept in the database, not on disk


class ClassShare(models.Model):
    """The secret link a class's students use to see its notes (no sign-in)."""

    classroom = models.OneToOneField("academics.ClassRoom", on_delete=models.CASCADE, related_name="share")
    token = models.CharField(max_length=64, unique=True, default=new_token)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    rotated_at = models.DateTimeField(null=True, blank=True)
    # The link works only until this moment (set by the teacher each time the link is opened); no time = not open.
    expires_at = models.DateTimeField(null=True, blank=True)

    @property
    def is_open(self):
        """True only if the link is switched on AND its time window has not ended."""
        from django.utils import timezone
        return bool(self.is_active and self.expires_at and self.expires_at > timezone.now())


class Note(models.Model):
    """A note, a PDF or any other allowed file that a teacher shares with a class."""

    classroom = models.ForeignKey("academics.ClassRoom", on_delete=models.CASCADE, related_name="notes")
    subject = models.ForeignKey("academics.Subject", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    title = models.CharField(max_length=120)
    body = models.TextField(blank=True, max_length=5000)
    file = models.FileField(storage=get_private_storage, upload_to=note_path, blank=True)
    file_name = models.CharField(max_length=200, blank=True)
    file_size = models.PositiveIntegerField(default=0)
    uploaded_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="notes")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
