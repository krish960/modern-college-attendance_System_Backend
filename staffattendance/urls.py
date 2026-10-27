"""URL routes for staff attendance and leave requests."""
from django.urls import include, path
from rest_framework.routers import SimpleRouter

from .leave import LeaveRequestViewSet
from .views import CheckInView, DayView, MarkView, MineView, MonthlyView, OnLeaveView

# Leave requests get the standard list/create/detail routes plus extra actions.
router = SimpleRouter()
router.register("leave-requests", LeaveRequestViewSet, basename="leave-request")

urlpatterns = [
    path("", include(router.urls)),
    path("staff-attendance/day/", DayView.as_view(), name="staff-attendance-day"),
    path("staff-attendance/mark/", MarkView.as_view(), name="staff-attendance-mark"),
    path("staff-attendance/check-in/", CheckInView.as_view(), name="staff-attendance-check-in"),
    path("staff-attendance/on-leave/", OnLeaveView.as_view(), name="staff-attendance-on-leave"),
    path("staff-attendance/me/", MineView.as_view(), name="staff-attendance-me"),
    path("staff-attendance/monthly/", MonthlyView.as_view(), name="staff-attendance-monthly"),
]
