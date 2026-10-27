"""
Security logging: writes a warning line every time someone fails to sign in, so the
administrator can spot password guessing in the log file.
"""
import logging

from django.contrib.auth.signals import user_login_failed
from django.dispatch import receiver

log = logging.getLogger("app.security")


def client_ip(request):
    """Best guess of the caller's IP address ("-" if unknown). Used only for log lines."""
    if request is None:
        return "-"
    # Behind a proxy the real address is the first entry of X-Forwarded-For.
    # (It can be faked by a client, so never use it for security decisions, only for logs.)
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    return (forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR")) or "-"


# Django sends the user_login_failed signal on every bad login; this function listens to it.
@receiver(user_login_failed)
def record_failed_login(sender, credentials, request=None, **kwargs):
    # Only the username is logged, never the password that was typed.
    log.warning("Failed sign-in username=%r ip=%s", str(credentials.get("username", ""))[:150], client_ip(request))
