"""URL routes for reports, downloads and the dashboard."""
from django.urls import path

from .downloads import EverythingDownloadView, LeaveDownloadView, PeopleDownloadView
from .records import StudentRecordsView, StudentYearView
from .views import DailyReportView, DashboardView, MonthlySheetView, StudentSummaryView

# Reports are read-only screens; downloads return CSV or PDF files.
urlpatterns = [
    path("reports/daily/", DailyReportView.as_view(), name="report-daily"),
    path("reports/student-summary/", StudentSummaryView.as_view(), name="report-student-summary"),
    path("reports/monthly-sheet/", MonthlySheetView.as_view(), name="report-monthly-sheet"),
    path("downloads/all/", EverythingDownloadView.as_view(), name="download-all"),
    path("downloads/leave/", LeaveDownloadView.as_view(), name="download-leave"),
    path("downloads/<str:what>/", PeopleDownloadView.as_view(), name="download-people"),
    path("reports/student-records/", StudentRecordsView.as_view(), name="report-student-records"),
    path("reports/student-records/<int:pk>/", StudentYearView.as_view(), name="report-student-year"),
    path("dashboard/", DashboardView.as_view(), name="dashboard"),
]
