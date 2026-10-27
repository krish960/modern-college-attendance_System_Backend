"""Django app registration for the reports: read-only reports, dashboard and downloads."""
from django.apps import AppConfig


class ReportsConfig(AppConfig):
    """Tells Django this app exists (name and default id type)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "reports"
