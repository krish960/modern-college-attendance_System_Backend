"""Database tables for staff attendance (one mark per teacher per day) and for leave requests."""
from django.conf import settings
from django.db import models


class StaffAttendance(models.Model):
    """One teacher's attendance for one day: written by the teacher's own check-in or by the HOD."""

    class Status(models.TextChoices):
        """The marks a day can have."""
        PRESENT = "PRESENT", "Present"
        LATE = "LATE", "Late"
        HALF_DAY = "HALF_DAY", "Half day"
        ABSENT = "ABSENT", "Absent"
        LEAVE = "LEAVE", "On leave"

    class Source(models.TextChoices):
        """How the mark was made: who or what wrote it."""
        SELF = "SELF", "Own check-in"
        HOD = "HOD", "Marked by HOD"
        SCAN = "SCAN", "ID card scan"
        LECTURE = "LECTURE", "Took a lecture"
        LEAVE_REQUEST = "LEAVEREQ", "Approved leave"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="staff_attendance")  # the teacher
    date = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices)
    source = models.CharField(max_length=8, choices=Source.choices)
    check_in_at = models.DateTimeField(null=True, blank=True)  # when the teacher arrived, if known
    note = models.CharField(max_length=200, blank=True)
    marked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="staff_attendance_marked"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-date", "user_id"]
        # the database itself refuses a second mark for the same teacher and day
        constraints = [models.UniqueConstraint(fields=["user", "date"], name="one_staff_record_per_day")]

    def __str__(self):
        """Short text for the admin site and logs."""
        return f"{self.user} {self.date} {self.status}"


class LeaveRequest(models.Model):
    """A teacher asks the HOD for leave (one or more days, with a reason). Approval marks those days On leave."""

    class Status(models.TextChoices):
        """Where the request is: waiting, being looked at, or decided."""
        PENDING = "PENDING", "Pending"
        PROCESSING = "PROCESSING", "In process"  # the HOD has seen it and is looking into it
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        CANCELLED = "CANCELLED", "Cancelled"

    OPEN = ("PENDING", "PROCESSING")  # not decided yet

    class Kind(models.TextChoices):
        """The type of leave a teacher can ask for."""
        CASUAL = "CASUAL", "CL – Casual leave"
        SICK = "SICK", "ML – Medical leave"
        EARNED = "EARNED", "Earned / privilege leave"
        DUTY = "DUTY", "DL – Duty leave (exam, training, college work)"
        STUDY = "STUDY", "Study / research leave"
        MATERNITY = "MATERNITY", "Maternity / paternity leave"
        UNPAID = "UNPAID", "WPL – Leave without pay"
        OTHER = "OTHER", "Other"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="leave_requests")
    leave_type = models.CharField(max_length=10, choices=Kind.choices, default=Kind.OTHER)
    from_date = models.DateField()
    to_date = models.DateField()
    reason = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="reviewed_leave_requests"
    )
    remarks = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]

    def __str__(self):
        """Short text for the admin site and logs."""
        return f"Leave {self.user} {self.from_date}..{self.to_date} ({self.status})"
