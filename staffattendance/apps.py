"""Django app registration for the staffattendance: staff daily attendance and leave requests."""
from django.apps import AppConfig


class StaffAttendanceConfig(AppConfig):
    """Tells Django this app exists (name and default id type)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "staffattendance"
