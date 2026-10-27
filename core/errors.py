"""One place that decides what the API sends back when an error happens (used as the DRF exception handler)."""
import logging
import uuid

from rest_framework.response import Response
from rest_framework.views import exception_handler

# the logger that writes unexpected errors to the server log file
log = logging.getLogger("app.errors")


def api_exception_handler(exc, context):
    """
    Normal API errors (validation, permission, not found) keep DRF's clear messages.
    Anything unexpected becomes a plain 500 for the user: no traceback, no SQL, no file names.
    The full details go to logs/server.log together with a short reference the user can quote.
    """
    response = exception_handler(exc, context)  # DRF's own handler: it knows the normal API errors
    if response is not None:
        return response
    # unexpected error: make a short random reference so the log entry can be found later
    reference = uuid.uuid4().hex[:8]
    request = context.get("request")
    log.error("Unhandled error ref=%s %s %s user=%s", reference, getattr(request, "method", "-"),
              getattr(request, "path", "-"), getattr(getattr(request, "user", None), "pk", None), exc_info=exc)
    return Response(
        {"detail": f"Something went wrong on the server. Please try again. If it keeps happening, tell the administrator and quote reference {reference}.",
         "reference": reference},
        status=500,
    )
