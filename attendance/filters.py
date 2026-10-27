"""Query-string filters for the lecture and attendance-record list APIs (e.g. ?date_from=...&subject=3)."""
import django_filters

from .models import AttendanceRecord, Lecture


class LectureFilter(django_filters.FilterSet):
    """Filters for the lecture list: by date or date range, subject, class, teacher, semester, assignment and status."""
    date = django_filters.DateFilter(field_name="lecture_date")
    date_from = django_filters.DateFilter(field_name="lecture_date", lookup_expr="gte")
    date_to = django_filters.DateFilter(field_name="lecture_date", lookup_expr="lte")
    subject = django_filters.NumberFilter(field_name="teacher_subject_assignment__subject")
    classroom = django_filters.NumberFilter(field_name="teacher_subject_assignment__classroom")
    teacher = django_filters.NumberFilter(field_name="teacher_subject_assignment__teacher")
    semester = django_filters.NumberFilter(field_name="teacher_subject_assignment__classroom__semester")
    assignment = django_filters.NumberFilter(field_name="teacher_subject_assignment")

    class Meta:
        model = Lecture
        fields = ["status"]


class RecordFilter(django_filters.FilterSet):
    """Filters for the attendance record list: date range, subject, class, assignment, lecture, student, status."""
    date_from = django_filters.DateFilter(field_name="lecture__lecture_date", lookup_expr="gte")
    date_to = django_filters.DateFilter(field_name="lecture__lecture_date", lookup_expr="lte")
    subject = django_filters.NumberFilter(field_name="lecture__teacher_subject_assignment__subject")
    classroom = django_filters.NumberFilter(field_name="lecture__teacher_subject_assignment__classroom")
    assignment = django_filters.NumberFilter(field_name="lecture__teacher_subject_assignment")

    class Meta:
        model = AttendanceRecord
        fields = ["lecture", "student", "status"]
