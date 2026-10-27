"""
Optional network lock for the API.

If API_ALLOWED_NETWORKS is set (for example "103.20.1.0/24,10.0.0.0/8"), requests to /api/ from any other
address get "403 Forbidden", whatever token they carry. If it is empty, nothing is blocked and only the
normal login rules apply.
Behind nginx (DJANGO_BEHIND_PROXY=true) the client address is the LAST entry of X-Forwarded-For,
the one nginx added itself, because the earlier entries can be faked by the caller.
"""
import ipaddress

from django.conf import settings
from django.http import JsonResponse


def _client_ip(request):
    """The address the request really came from (see the note at the top about proxies)."""
    if getattr(settings, "BEHIND_PROXY", False):
        forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded:
            return forwarded.split(",")[-1].strip()
    return request.META.get("REMOTE_ADDR", "")


class ApiNetworkLockMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        networks = settings.API_ALLOWED_NETWORKS
        if networks and request.path.startswith("/api/"):
            try:
                ip = ipaddress.ip_address(_client_ip(request))
                allowed = any(ip in net for net in networks)
            except ValueError:
                allowed = False  # an address we cannot read is never allowed
            if not allowed:
                return JsonResponse({"detail": "This network is not allowed to use the API."}, status=403)
        return self.get_response(request)
