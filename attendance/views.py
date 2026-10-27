"""API views for attendance (lectures, marking, history, corrections, timetable).

Who can do what: a Teacher (role STAFF) marks and submits their own lectures and asks
for corrections. The HOD manages timetable and reviews corrections in their own
department. The Admin can read everything. The real rules live in services.py.
"""
from django.utils import timezone
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from accounts.models import User
from accounts.permissions import IsAdminOrHOD, IsHOD, IsHODOrStaff, IsStaffTeacher
from core.audit import log_action
from core.ordering import roll_order
from core.scoping import department_scope

from . import services
from .filters import LectureFilter, RecordFilter
from .models import AttendanceCorrectionRequest, AttendanceRecord, Lecture, TimetableSlot
from .serializers import (
    AttendanceRecordSerializer,
    CorrectionSerializer,
    GenerateSerializer,
    LectureSerializer,
    MarkSerializer,
    ReviewSerializer,
    TimetableSlotSerializer,
)

# Path from a Lecture to its department, used for department scoping.
LECTURE_DEPT = "teacher_subject_assignment__classroom__department_id"


def scope_lectures(qs, user, prefix=""):
    """Admin: all. HOD: own department. Staff: only their own lectures."""
    qs = department_scope(qs, user, prefix + LECTURE_DEPT)
    if user.role == User.Role.STAFF:
        qs = qs.filter(**{prefix + "teacher_subject_assignment__teacher": user})
    return qs


class LectureViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Create, list and open lectures, plus mark/submit/cancel actions.

    Visibility is scoped: Admin sees all, HOD sees own department, Teacher sees own lectures.
    """
    serializer_class = LectureSerializer
    filterset_class = LectureFilter
    search_fields = [
        "teacher_subject_assignment__subject__name", "teacher_subject_assignment__subject__code",
        "teacher_subject_assignment__classroom__name",
        "teacher_subject_assignment__teacher__first_name", "teacher_subject_assignment__teacher__last_name",
    ]
    ordering_fields = ["lecture_date", "start_time", "status"]

    def get_permissions(self):
        # Create/cancel: HOD or Teacher. Mark/submit: only the Teacher (role STAFF).
        if self.action == "create":
            return [IsHODOrStaff()]
        if self.action in ("mark", "submit"):
            return [IsStaffTeacher()]
        if self.action == "cancel":
            return [IsHODOrStaff()]
        return super().get_permissions()

    def get_queryset(self):
        # Department scoping happens in scope_lectures().
        # No per-lecture COUNT over the attendance table here: with millions of records that made the
        # list take seconds. Submitted lectures carry stored totals; the serializer counts drafts.
        qs = Lecture.objects.select_related(
            "teacher_subject_assignment__teacher", "teacher_subject_assignment__subject",
            "teacher_subject_assignment__classroom__department", "submitted_by",
        ).order_by("-lecture_date", "start_time", "id")
        return scope_lectures(qs, self.request.user)

    def perform_create(self, serializer):
        """Save the new lecture and write an audit log entry."""
        lecture = serializer.save()
        log_action(self.request.user, "LECTURE_SCHEDULED", lecture,
                   department=lecture.teacher_subject_assignment.classroom.department,
                   new={"date": str(lecture.lecture_date), "start": str(lecture.start_time)})

    def _roster_payload(self, lecture):
        """Build the marking-screen data: the lecture, its students with marks, and an 'editable' flag.

        Editable only for the owning teacher while the lecture is DRAFT and not in the future.
        """
        user = self.request.user
        lecture = self.get_queryset().get(pk=lecture.pk)  # re-read: the service may have changed it
        marks = {r.student_id: r.status for r in lecture.records.all()}
        students = [
            {"id": s.id, "roll_number": s.roll_number, "name": s.name, "card_code": s.card_code, "is_active": s.is_active,
             "has_photo": s.photo_updated_at is not None,
             "photo_version": int(s.photo_updated_at.timestamp()) if s.photo_updated_at else 0,
             "status": marks.get(s.id)}
            for s in services.roster_students(lecture)
        ]
        assignment = lecture.teacher_subject_assignment
        editable = (
            lecture.status == Lecture.Status.DRAFT and user.role == User.Role.STAFF
            and assignment.teacher_id == user.id and assignment.is_active
            and lecture.lecture_date <= timezone.localdate()
        )
        data = LectureSerializer(lecture, context=self.get_serializer_context()).data
        return {"lecture": data, "students": students, "editable": editable}

    # (the roster action below is read-only)
    @action(detail=True, methods=["get"])
    def roster(self, request, pk=None):
        """Students of the class with their current mark (null = unmarked)."""
        return Response(self._roster_payload(self.get_object()))

    @action(detail=True, methods=["post"])
    def mark(self, request, pk=None):
        """Save marks without submitting (draft)."""
        lecture = self.get_object()
        serializer = MarkSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.save_attendance(lecture.pk, request.user, serializer.validated_data["records"], submit=False)
        return Response(self._roster_payload(lecture))

    @action(detail=True, methods=["post"])
    def submit(self, request, pk=None):
        """Save the given marks (if any) and submit. Every active student must be marked."""
        lecture = self.get_object()
        serializer = MarkSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.save_attendance(lecture.pk, request.user, serializer.validated_data["records"], submit=True)
        return Response(self._roster_payload(lecture))

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """Cancel a DRAFT lecture (owner teacher or department HOD). An optional reason is audit logged."""
        lecture = self.get_object()
        services.cancel_lecture(lecture.pk, request.user, request.data.get("reason", ""))
        return Response(self._roster_payload(lecture))


class AttendanceRecordViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    Read-only history of SUBMITTED attendance. There is deliberately no create /
    update / delete here: submitted attendance only changes via approved corrections.
    """

    serializer_class = AttendanceRecordSerializer
    filterset_class = RecordFilter
    search_fields = ["student__name", "student__roll_number"]
    ordering_fields = ["lecture__lecture_date", "student__roll_number"]

    def get_queryset(self):
        """Only SUBMITTED lectures have records worth reading, and each user sees only their own scope."""
        qs = AttendanceRecord.objects.filter(lecture__status=Lecture.Status.SUBMITTED).select_related(
            "lecture__teacher_subject_assignment__subject", "lecture__teacher_subject_assignment__classroom",
            "lecture__teacher_subject_assignment__teacher", "student", "marked_by",
        ).prefetch_related("correction_requests")
        qs = scope_lectures(qs, self.request.user, prefix="lecture__")
        if self.request.query_params.get("lecture"):  # one register: show it in roll-number order
            return qs.order_by(*roll_order("student__"))
        return qs.order_by("-lecture_id", "-id")  # newest first, using the index (no sort over millions of rows)


class CorrectionViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Correction requests: a teacher creates, the HOD approves or rejects.

    Teachers see only their own requests; an HOD sees the department's; Admin sees all.
    """
    serializer_class = CorrectionSerializer
    filterset_fields = ["status", "requested_by"]
    search_fields = ["record__student__name", "record__student__roll_number", "reason"]

    def get_permissions(self):
        # Only a Teacher asks for a correction; only the HOD decides on it.
        if self.action == "create":
            return [IsStaffTeacher()]
        if self.action in ("approve", "reject"):
            return [IsHOD()]
        return super().get_permissions()

    def get_queryset(self):
        """Teachers see their own requests, an HOD the department's, the Admin all."""
        user = self.request.user
        qs = AttendanceCorrectionRequest.objects.select_related(
            "record__student", "record__lecture__teacher_subject_assignment__subject",
            "record__lecture__teacher_subject_assignment__classroom", "requested_by", "reviewed_by",
        )
        qs = department_scope(qs, user, "record__lecture__" + LECTURE_DEPT)
        if user.role == User.Role.STAFF:
            qs = qs.filter(requested_by=user)
        return qs

    def _review(self, request, approve):
        """Shared code for approve and reject. Remarks are required."""
        correction = self.get_object()  # 404 for other departments
        serializer = ReviewSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        services.review_correction(correction.pk, request.user, approve, serializer.validated_data["remarks"])
        return Response(self.get_serializer(self.get_queryset().get(pk=correction.pk)).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """HOD approves: the mark is changed."""
        return self._review(request, True)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        """HOD rejects: the mark stays as it was."""
        return self._review(request, False)


class TimetableViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin,
                       mixins.UpdateModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet):
    """Weekly timetable. HOD manages; admin and staff can read (scoped)."""

    serializer_class = TimetableSlotSerializer
    filterset_fields = ["weekday", "is_active", "assignment"]
    search_fields = [
        "assignment__subject__name", "assignment__classroom__name",
        "assignment__teacher__first_name", "assignment__teacher__last_name",
    ]

    def get_permissions(self):
        # Reading follows the default permissions; changing the timetable is HOD only.
        if self.action in ("list", "retrieve"):
            return super().get_permissions()
        return [IsHOD()]

    def get_queryset(self):
        user = self.request.user
        # Teachers only see their own slots; the HOD sees the department's.
        qs = TimetableSlot.objects.select_related("assignment__subject", "assignment__classroom", "assignment__teacher")
        qs = department_scope(qs, user, "assignment__classroom__department_id")
        if user.role == User.Role.STAFF:
            qs = qs.filter(assignment__teacher=user)
        return qs

    @action(detail=False, methods=["post"])
    def generate(self, request):
        """Create dated DRAFT lectures from the active timetable slots (safe to repeat)."""
        serializer = GenerateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        created = services.generate_lectures(request.user, **serializer.validated_data)
        return Response({"created": created}, status=status.HTTP_200_OK)
