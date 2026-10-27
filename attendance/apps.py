"""Django app registration for the attendance app."""
from django.apps import AppConfig


class AttendanceConfig(AppConfig):
    """Tells Django this app exists (name and default id type)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "attendance"
