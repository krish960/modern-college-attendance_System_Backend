"""Top-level URL map: every app's API lives under /api/, and the public notes pages for students are added at the end."""
from django.urls import include, path

from notes.urls import public_urlpatterns

urlpatterns = [
    path("api/", include("accounts.urls")),
    path("api/", include("departments.urls")),
    path("api/", include("academics.urls")),
    path("api/", include("students.urls")),
    path("api/", include("attendance.urls")),
    path("api/", include("reports.urls")),
    path("api/", include("staffattendance.urls")),
    path("api/", include("notes.urls")),
    path("api/", include("core.urls")),
] + public_urlpatterns  # the secret-link pages students open (no /api/ prefix)


def not_found(request, exception=None):
    """Unknown address: a short plain answer, no framework page."""
    from django.http import JsonResponse
    if request.path.startswith("/api/"):
        return JsonResponse({"detail": "Not found."}, status=404)
    from notes.public_page import message_page
    return message_page("Page not found", "There is nothing at this address.", 404)


# Django calls this for every unknown address
handler404 = not_found
