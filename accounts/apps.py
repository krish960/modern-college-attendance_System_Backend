"""App settings for `accounts`. Its only job here is to switch on the failed-login logger."""
from django.apps import AppConfig


class AccountsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "accounts"

    def ready(self):
        # Django calls this once at start-up. Importing the module registers its signal listener.
        from . import security_log  # noqa: F401  (connects the failed-login logger)
