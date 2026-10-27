"""URL routes for attendance: lectures, records, corrections, timetable and the elective (subject-students) screen."""
from django.urls import path
from rest_framework.routers import SimpleRouter

from .exclusions import SubjectStudentsView
from .views import AttendanceRecordViewSet, CorrectionViewSet, LectureViewSet, TimetableViewSet

# The router builds the list/detail URLs for each viewset automatically.
router = SimpleRouter()
router.register("lectures", LectureViewSet, basename="lecture")
router.register("attendance-records", AttendanceRecordViewSet, basename="attendance-record")
router.register("corrections", CorrectionViewSet, basename="correction")
router.register("timetable", TimetableViewSet, basename="timetable")

urlpatterns = router.urls + [path("subject-students/", SubjectStudentsView.as_view(), name="subject-students")]
