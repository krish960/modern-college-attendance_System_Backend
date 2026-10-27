"""Serializers for attendance: they turn models into JSON and check incoming data.

The validation rules here (no overlapping lectures, backdate limit, reason required,
teachers only touch their own records) are business rules, not just format checks.
"""
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import serializers

from academics.models import TeacherSubjectAssignment
from accounts.models import User
from core.validators import reason_text

from . import services
from .models import AttendanceCorrectionRequest, AttendanceRecord, Lecture, TimetableSlot


# ----------------------------------------------------------------------------
# Lectures
# ----------------------------------------------------------------------------
class LectureSerializer(serializers.ModelSerializer):
    """JSON shape of a Lecture, with names and attendance counts added for the screens."""
    subject_name = serializers.CharField(source="teacher_subject_assignment.subject.name", read_only=True)
    classroom = serializers.IntegerField(source="teacher_subject_assignment.classroom_id", read_only=True)
    classroom_name = serializers.CharField(source="teacher_subject_assignment.classroom.label", read_only=True)
    teacher = serializers.IntegerField(source="teacher_subject_assignment.teacher_id", read_only=True)
    teacher_name = serializers.CharField(source="teacher_subject_assignment.teacher.display_name", read_only=True)
    department_name = serializers.CharField(source="teacher_subject_assignment.classroom.department.name", read_only=True)
    submitted_by_name = serializers.CharField(source="submitted_by.display_name", read_only=True, default=None)
    present_count = serializers.SerializerMethodField()
    absent_count = serializers.SerializerMethodField()
    marked_count = serializers.SerializerMethodField()

    class Meta:
        model = Lecture
        fields = [
            "id", "teacher_subject_assignment", "subject_name", "classroom", "classroom_name", "teacher",
            "teacher_name", "department_name", "lecture_date", "start_time", "end_time", "status", "submitted_at",
            "submitted_by", "submitted_by_name", "present_count", "absent_count", "marked_count", "created_at",
        ]
        read_only_fields = ["id", "status", "submitted_at", "submitted_by", "created_at"]
        validators = []

    # Submitted lectures use totals stored on the lecture row (fast). Drafts are counted live.
    def _counts(self, obj):
        """(present, absent) for one lecture: stored totals when submitted, a small count while still a draft."""
        cached = getattr(obj, "_attendance_counts", None)
        if cached is None:
            if obj.status == Lecture.Status.SUBMITTED:
                cached = (obj.present_total, obj.absent_total)
            elif obj.status == Lecture.Status.CANCELLED:
                cached = (0, 0)
            else:
                agg = obj.records.aggregate(
                    present=Count("id", filter=Q(status=AttendanceRecord.Status.PRESENT)),
                    absent=Count("id", filter=Q(status=AttendanceRecord.Status.ABSENT)),
                )
                cached = (agg["present"], agg["absent"])
            obj._attendance_counts = cached
        return cached

    def get_present_count(self, obj):
        """Number of PRESENT marks."""
        return self._counts(obj)[0]

    def get_absent_count(self, obj):
        """Number of ABSENT marks."""
        return self._counts(obj)[1]

    def get_marked_count(self, obj):
        """How many students have been marked so far (present + absent)."""
        return sum(self._counts(obj))

    def validate_teacher_subject_assignment(self, assignment):
        """Who may schedule a lecture: a teacher for own assignment, an HOD within own department."""
        user = self.context["request"].user
        if not assignment.is_active:
            raise serializers.ValidationError("This assignment is not active.")
        if user.role == User.Role.STAFF and assignment.teacher_id != user.id:
            raise serializers.ValidationError("You can only create lectures for your own assignments.")
        if user.role == User.Role.HOD and assignment.classroom.department_id != user.department_id:
            raise serializers.ValidationError("Invalid assignment.")
        return assignment

    def validate_lecture_date(self, value):
        """Stop lectures being added too far in the past (ATTENDANCE_BACKDATE_DAYS setting)."""
        oldest = timezone.localdate() - timedelta(days=settings.ATTENDANCE_BACKDATE_DAYS)
        if value < oldest:
            raise serializers.ValidationError(
                f"Lectures older than {settings.ATTENDANCE_BACKDATE_DAYS} days cannot be added. Ask your HOD.")
        return value

    def validate(self, attrs):
        # Rule: end after start, and no overlap with another (non-cancelled) lecture of the
        # same teacher or the same class on that date.
        """Business rules for a new lecture: end after start, and no clash with the same teacher or class."""
        start, end = attrs["start_time"], attrs["end_time"]
        if end <= start:
            raise serializers.ValidationError({"end_time": "End time must be after the start time."})
        assignment = attrs["teacher_subject_assignment"]
        clash = (
            Lecture.objects.filter(lecture_date=attrs["lecture_date"], start_time__lt=end, end_time__gt=start)
            .exclude(status=Lecture.Status.CANCELLED)
            .filter(Q(teacher_subject_assignment__teacher=assignment.teacher)
                    | Q(teacher_subject_assignment__classroom=assignment.classroom))
        )
        if clash.exists():
            raise serializers.ValidationError(
                "That time overlaps another lecture of this teacher or class on the same date."
            )
        return attrs


class MarkItemSerializer(serializers.Serializer):
    """One mark in a save request: a student id and PRESENT or ABSENT."""
    student = serializers.IntegerField()
    status = serializers.ChoiceField(choices=AttendanceRecord.Status.choices)


class MarkSerializer(serializers.Serializer):
    """Body of the mark/submit request: a list of marks (may be empty when only submitting)."""
    records = MarkItemSerializer(many=True, allow_empty=True)


# ----------------------------------------------------------------------------
# Records (read-only history)
# ----------------------------------------------------------------------------
class AttendanceRecordSerializer(serializers.ModelSerializer):
    """Read-only view of one attendance record, with lecture and student details for history screens."""
    lecture_date = serializers.DateField(source="lecture.lecture_date", read_only=True)
    start_time = serializers.TimeField(source="lecture.start_time", read_only=True)
    subject_name = serializers.CharField(source="lecture.teacher_subject_assignment.subject.name", read_only=True)
    classroom_name = serializers.CharField(source="lecture.teacher_subject_assignment.classroom.label", read_only=True)
    teacher_name = serializers.CharField(source="lecture.teacher_subject_assignment.teacher.display_name", read_only=True)
    roll_number = serializers.CharField(source="student.roll_number", read_only=True)
    student_name = serializers.CharField(source="student.name", read_only=True)
    marked_by_name = serializers.CharField(source="marked_by.display_name", read_only=True, default=None)
    has_pending_correction = serializers.SerializerMethodField()

    class Meta:
        model = AttendanceRecord
        fields = [
            "id", "lecture", "lecture_date", "start_time", "subject_name", "classroom_name", "teacher_name",
            "student", "roll_number", "student_name", "status", "marked_by_name", "marked_at", "has_pending_correction",
        ]
        read_only_fields = fields

    def get_has_pending_correction(self, obj):
        """True if a correction request for this record is still PENDING."""
        return any(c.status == AttendanceCorrectionRequest.Status.PENDING for c in obj.correction_requests.all())


# ----------------------------------------------------------------------------
# Corrections
# ----------------------------------------------------------------------------
class CorrectionSerializer(serializers.ModelSerializer):
    """A correction request. The teacher sends record, requested_status and reason; the rest is set by the server."""
    lecture_date = serializers.DateField(source="record.lecture.lecture_date", read_only=True)
    subject_name = serializers.CharField(source="record.lecture.teacher_subject_assignment.subject.name", read_only=True)
    classroom_name = serializers.CharField(source="record.lecture.teacher_subject_assignment.classroom.label", read_only=True)
    roll_number = serializers.CharField(source="record.student.roll_number", read_only=True)
    student_name = serializers.CharField(source="record.student.name", read_only=True)
    requested_by_name = serializers.CharField(source="requested_by.display_name", read_only=True)
    reviewed_by_name = serializers.CharField(source="reviewed_by.display_name", read_only=True, default=None)

    class Meta:
        model = AttendanceCorrectionRequest
        fields = [
            "id", "record", "lecture_date", "subject_name", "classroom_name", "roll_number", "student_name",
            "old_status", "requested_status", "reason", "status", "requested_by", "requested_by_name",
            "reviewed_by", "reviewed_by_name", "remarks", "created_at", "reviewed_at",
        ]
        read_only_fields = ["id", "old_status", "status", "requested_by", "reviewed_by", "remarks", "created_at", "reviewed_at"]

    def validate_reason(self, value):
        """A reason of at least 5 characters is mandatory so the HOD can judge the request."""
        return reason_text(value)

    def validate_record(self, record):
        """A teacher may only ask to correct records of their own lectures."""
        user = self.context["request"].user
        if record.lecture.teacher_subject_assignment.teacher_id != user.id:
            # Same wording as a missing record, so teachers cannot probe other people's record ids.
            raise serializers.ValidationError(f'Invalid pk "{record.pk}" - object does not exist.')
        return record

    def create(self, validated_data):
        """Create the request through the service so the audit log and HOD notification happen."""
        return services.create_correction(
            self.context["request"].user, validated_data["record"],
            validated_data["requested_status"], validated_data["reason"],
        )


class ReviewSerializer(serializers.Serializer):
    """Body of approve/reject: remarks are compulsory."""
    remarks = serializers.CharField(allow_blank=False, trim_whitespace=True, max_length=1000)


# ----------------------------------------------------------------------------
# Timetable
# ----------------------------------------------------------------------------
class TimetableSlotSerializer(serializers.ModelSerializer):
    """A weekly repeating slot (weekday + start/end time) for a teacher's assignment."""
    subject_name = serializers.CharField(source="assignment.subject.name", read_only=True)
    classroom_name = serializers.CharField(source="assignment.classroom.label", read_only=True)
    teacher_name = serializers.CharField(source="assignment.teacher.display_name", read_only=True)
    weekday_name = serializers.CharField(source="get_weekday_display", read_only=True)

    class Meta:
        model = TimetableSlot
        fields = [
            "id", "assignment", "subject_name", "classroom_name", "teacher_name", "weekday", "weekday_name",
            "start_time", "end_time", "is_active",
        ]
        read_only_fields = ["id"]
        validators = []

    def validate_assignment(self, assignment):
        """The HOD can only use active assignments of their own department."""
        if assignment.classroom.department_id != self.context["request"].user.department_id or not assignment.is_active:
            raise serializers.ValidationError("Choose an active assignment from your department.")
        return assignment

    def validate(self, attrs):
        # Helper: take the new value if sent, otherwise the value already saved (for partial updates).
        """Checks that the slot's times are sane and that it does not clash with another active slot."""
        get = lambda f: attrs.get(f, getattr(self.instance, f, None))  # noqa: E731
        if get("end_time") <= get("start_time"):
            raise serializers.ValidationError({"end_time": "End time must be after the start time."})
        # Rule: end after start, and the same slot must not exist twice.
        qs = TimetableSlot.objects.filter(assignment=get("assignment"), weekday=get("weekday"), start_time=get("start_time"))
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("This slot already exists.")
        if get("is_active") is not False:
            # One teacher cannot be in two places, and one class cannot have two lectures at once.
            assignment = get("assignment")
            clash = TimetableSlot.objects.filter(
                is_active=True, assignment__is_active=True, weekday=get("weekday"),
                start_time__lt=get("end_time"), end_time__gt=get("start_time"),
            ).filter(Q(assignment__teacher=assignment.teacher) | Q(assignment__classroom=assignment.classroom))
            if self.instance:
                clash = clash.exclude(pk=self.instance.pk)
            other = clash.select_related("assignment__subject", "assignment__classroom", "assignment__teacher").first()
            if other:
                raise serializers.ValidationError(
                    f"Clashes with {other.assignment.subject.name} ({other.assignment.classroom.label}, "
                    f"{other.assignment.teacher.display_name}) on {other.get_weekday_display()} "
                    f"{other.start_time:%H:%M}-{other.end_time:%H:%M}: the same teacher or class is already scheduled then.")
        return attrs


class GenerateSerializer(serializers.Serializer):
    """Body of 'generate lectures': the first and last date to fill."""
    date_from = serializers.DateField()
    date_to = serializers.DateField()
