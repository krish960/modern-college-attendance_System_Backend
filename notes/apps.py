"""Django app registration for the notes: files and notes teachers share with a class."""
from django.apps import AppConfig


class NotesConfig(AppConfig):
    """Tells Django this app exists (name and default id type)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "notes"
