"""
Login rate limits.

A college often sits behind one public IP (everyone on the campus Wi-Fi looks like the same
address), so a single "10 per minute per IP" limit would lock out a whole department at 9 a.m.
Instead: each USERNAME is limited strictly (stops password guessing on one account) and each IP
gets a generous limit (stops one machine hammering many accounts).
"""
from rest_framework.throttling import SimpleRateThrottle


class LoginUsernameThrottle(SimpleRateThrottle):
    """Limits login attempts per USERNAME typed in the form (the rate is set in settings)."""

    scope = "login"  # rate comes from DEFAULT_THROTTLE_RATES["login"]

    def get_cache_key(self, request, view):
        # The cache key is what we count against. No username typed -> nothing to count here.
        username = str(request.data.get("username", "")).strip().lower()
        if not username:
            return None
        return self.cache_format % {"scope": self.scope, "ident": username}


class LoginIPThrottle(SimpleRateThrottle):
    """Limits login attempts per IP address, with a much higher limit than the per-username one."""

    scope = "login_ip"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}
