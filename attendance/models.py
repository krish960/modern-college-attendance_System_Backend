"""Database tables for attendance.

Lecture = one class session on a date.
AttendanceRecord = one student's PRESENT/ABSENT mark for a lecture.
TimetableSlot = weekly repeating slot that the HOD turns into lectures.
AttendanceCorrectionRequest = a teacher's request to change a submitted mark.
SubjectExclusion = marks a student as not taking an elective subject.
"""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models import F, Q
from django.utils import timezone


class Lecture(models.Model):
    """
    One scheduled/held session of an assignment on a date. A lecture that is still
    DRAFT is "scheduled but not submitted" - that is what the HOD report calls pending.
    """

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        SUBMITTED = "SUBMITTED", "Submitted"
        CANCELLED = "CANCELLED", "Cancelled"

    teacher_subject_assignment = models.ForeignKey(
        "academics.TeacherSubjectAssignment", on_delete=models.PROTECT, related_name="lectures"
    )
    lecture_date = models.DateField()
    start_time = models.TimeField()
    end_time = models.TimeField()
    # Lecture status flow: DRAFT (created, not yet submitted) -> SUBMITTED (locked) or CANCELLED.
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    submitted_at = models.DateTimeField(null=True, blank=True)
    # Totals stored at submission time (kept in step by approved corrections) so dashboards and
    # reports can add up lectures instead of scanning millions of attendance rows.
    # `manage.py recount_attendance` recomputes them from the records at any time.
    present_total = models.PositiveIntegerField(default=0)
    absent_total = models.PositiveIntegerField(default=0)
    submitted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="submitted_lectures"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-lecture_date", "start_time", "id"]
        constraints = [
            # The same assignment cannot have two lectures starting at the same time on the same day.
            models.UniqueConstraint(
                fields=["teacher_subject_assignment", "lecture_date", "start_time"], name="uniq_lecture_slot"
            ),
            # The database itself refuses a lecture that ends before it starts.
            models.CheckConstraint(condition=Q(end_time__gt=F("start_time")), name="lecture_end_after_start"),
        ]
        # Speeds up the daily report, which filters by date and status.
        indexes = [models.Index(fields=["lecture_date", "status"])]

    def __str__(self):
        return f"{self.teacher_subject_assignment} on {self.lecture_date} {self.start_time:%H:%M}"


class AttendanceRecord(models.Model):
    """One student's mark (PRESENT or ABSENT) for one lecture.

    Once the lecture is SUBMITTED the record is locked. Changes must go through
    an AttendanceCorrectionRequest that an authorised reviewer approves.
    """
    class Status(models.TextChoices):
        PRESENT = "PRESENT", "Present"
        ABSENT = "ABSENT", "Absent"

    lecture = models.ForeignKey(Lecture, on_delete=models.PROTECT, related_name="records")
    student = models.ForeignKey("students.Student", on_delete=models.PROTECT, related_name="attendance_records")
    status = models.CharField(max_length=7, choices=Status.choices)
    marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="marked_records"
    )
    marked_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["student__roll_number"]
        constraints = [
            # A student can have only one record per lecture.
            models.UniqueConstraint(fields=["lecture", "student"], name="uniq_record_per_lecture_student"),
        ]

    def clean(self):
        """Rule: a student can only be marked in a lecture of their own class."""
        if self.student.classroom_id != self.lecture.teacher_subject_assignment.classroom_id:
            raise ValidationError("The student does not belong to this lecture's class.")

    def save(self, *args, allow_submitted_edit=False, **kwargs):
        """Create/update the record, enforcing the 'submitted is locked' rule.

        allow_submitted_edit is only passed as True by the correction approval code.
        """
        if self._state.adding:
            self.clean()  # block records for students outside the lecture's class
        elif not allow_submitted_edit and self.lecture.status == Lecture.Status.SUBMITTED:
            # Only an approved correction (attendance.services.review_correction) may pass the flag.
            raise ValidationError("Submitted attendance cannot be edited directly. Raise a correction request.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Block deleting marks of a submitted lecture (attendance is an official record)."""
        if self.lecture.status == Lecture.Status.SUBMITTED:
            raise ValidationError("Submitted attendance cannot be deleted.")
        return super().delete(*args, **kwargs)

    def __str__(self):
        return f"{self.student} - {self.status}"


class TimetableSlot(models.Model):
    """Weekly recurring slot. The HOD turns slots into dated Lecture rows."""

    WEEKDAYS = [(0, "Monday"), (1, "Tuesday"), (2, "Wednesday"), (3, "Thursday"), (4, "Friday"), (5, "Saturday"), (6, "Sunday")]

    assignment = models.ForeignKey(
        "academics.TeacherSubjectAssignment", on_delete=models.CASCADE, related_name="timetable_slots"
    )
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAYS)
    start_time = models.TimeField()
    end_time = models.TimeField()
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["weekday", "start_time"]
        constraints = [
            # A teacher cannot have the same slot saved twice.
            models.UniqueConstraint(fields=["assignment", "weekday", "start_time"], name="uniq_timetable_slot"),
            models.CheckConstraint(condition=Q(end_time__gt=F("start_time")), name="slot_end_after_start"),
        ]

    def __str__(self):
        return f"{self.assignment} {self.get_weekday_display()} {self.start_time:%H:%M}"


class AttendanceCorrectionRequest(models.Model):
    """A request to change one already-submitted mark.

    Flow: PENDING -> APPROVED (mark is changed) or REJECTED (mark stays).
    The teacher gives a reason; the reviewer may add remarks.
    """
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    record = models.ForeignKey(AttendanceRecord, on_delete=models.PROTECT, related_name="correction_requests")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="correction_requests")
    # old_status is a snapshot of the mark when the request was made, kept for the audit trail.
    old_status = models.CharField(max_length=7, choices=AttendanceRecord.Status.choices)
    requested_status = models.CharField(max_length=7, choices=AttendanceRecord.Status.choices)
    reason = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="reviewed_corrections"
    )
    remarks = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            # Only one open request per record at a time.
            models.UniqueConstraint(fields=["record"], condition=Q(status="PENDING"), name="one_pending_correction_per_record"),
        ]

    def __str__(self):
        return f"Correction #{self.pk} ({self.status})"


class SubjectExclusion(models.Model):
    """
    "This student does not study this subject" (an elective he did not take). He is left out of that subject's
    attendance lists, totals and percentages; everything he already has stays. Other subjects are not affected.
    """

    student = models.ForeignKey("students.Student", on_delete=models.CASCADE, related_name="subject_exclusions")
    subject = models.ForeignKey("academics.Subject", on_delete=models.CASCADE, related_name="exclusions")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # A student can be left out of a subject only once.
        constraints = [models.UniqueConstraint(fields=["student", "subject"], name="uniq_exclusion_student_subject")]

    def __str__(self):
        return f"{self.student_id} not in {self.subject_id}"
