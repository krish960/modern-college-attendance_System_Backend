"""Django app registration for the students: student records, photos, class promotion and removal."""
from django.apps import AppConfig


class StudentsConfig(AppConfig):
    """Tells Django this app exists (name and default id type)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "students"
