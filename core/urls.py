from django.urls import path
from rest_framework.routers import SimpleRouter

from .scan import IdentifyCardView

from .views import AuditLogViewSet, NotificationViewSet

router = SimpleRouter()
router.register("notifications", NotificationViewSet, basename="notification")
router.register("audit-logs", AuditLogViewSet, basename="audit-log")

urlpatterns = router.urls + [path("scan/identify/", IdentifyCardView.as_view(), name="scan-identify")]
