"""Database tables for students.

Student = one person on a class register (never hard deleted, only made inactive: this is the soft delete).
StudentRemovalRequest = a teacher asks the HOD to take a student off the register.
ClassPromotionRequest = a request to move a whole class to the next class (needs HOD and admin approval)."""
from django.conf import settings
from django.db import models


def normalize_roll_number(value):
    """No spaces, upper case, and for plain numbers no leading zeros: "01", " 1 " and "001" are all roll 1 (one student, not three)."""
    roll = "".join(str(value).split()).upper()
    return (roll.lstrip("0") or "0") if roll.isdigit() else roll


def normalize_card_code(value):
    """Clean an ID card code so the same card always looks the same: no spaces, upper case."""
    return "".join(str(value or "").split()).upper()


class Student(models.Model):
    """A student belongs to exactly one class (and therefore one department)."""

    department = models.ForeignKey("departments.Department", on_delete=models.PROTECT, related_name="students")
    classroom = models.ForeignKey("academics.ClassRoom", on_delete=models.PROTECT, related_name="students")
    roll_number = models.CharField(max_length=20)
    name = models.CharField(max_length=120)
    # The code printed on the student's college ID card (QR / barcode text), linked once so a teacher can scan the card.
    card_code = models.CharField(max_length=100, blank=True, default="")
    # Small JPEG of the student (re-encoded and resized on upload). Kept in the database so access always goes through the API's
    # login and department checks, and so a database backup is a complete backup.
    photo = models.BinaryField(null=True, blank=True, editable=False)
    photo_updated_at = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="registered_students"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["classroom_id", "roll_number"]
        constraints = [
            # One roll number per class, no matter which teacher registers the student.
            models.UniqueConstraint(fields=["classroom", "roll_number"], name="uniq_roll_number_per_class"),
            # One ID card belongs to one student (blank = no card registered yet).
            models.UniqueConstraint(fields=["card_code"], condition=~models.Q(card_code=""), name="uniq_card_code"),
        ]

    def save(self, *args, **kwargs):
        """Tidy the roll number and card code before saving, and copy the department from the class."""
        self.roll_number = normalize_roll_number(self.roll_number)
        self.card_code = normalize_card_code(self.card_code)
        self.department_id = self.classroom.department_id  # always derived from the class
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.roll_number} - {self.name}"


class StudentRemovalRequest(models.Model):
    """A teacher asks the HOD to take a student off the register (the student becomes inactive, never hard-deleted)."""

    class Status(models.TextChoices):
        """Removal request states: PENDING until the HOD decides, then APPROVED or REJECTED."""
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    student = models.ForeignKey(Student, on_delete=models.CASCADE, related_name="removal_requests")
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="student_removal_requests")
    reason = models.TextField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="reviewed_student_removals"
    )
    remarks = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            # Only one waiting removal request per student.
            models.UniqueConstraint(fields=["student"], condition=models.Q(status="PENDING"), name="one_pending_removal_per_student"),
        ]

    def __str__(self):
        return f"Removal of {self.student} ({self.status})"


class ClassPromotionRequest(models.Model):
    """
    Moving a whole class to the next year (for example MSc I to MSc II) needs two approvals, in this order:
    the HOD first, then the college admin. Only after the admin approves do the students really move.
    A teacher asks (status WAITING_HOD); when the HOD asks himself the HOD step is already done (WAITING_ADMIN).
    """

    class Status(models.TextChoices):
        """Promotion steps. WAITING_HOD -> WAITING_ADMIN -> DONE, or REJECTED / CANCELLED at any open step."""
        WAITING_HOD = "WAITING_HOD", "Waiting for HOD"
        WAITING_ADMIN = "WAITING_ADMIN", "Waiting for admin"
        DONE = "DONE", "Promoted"
        REJECTED = "REJECTED", "Rejected"
        CANCELLED = "CANCELLED", "Cancelled"

    # The two states that mean the request is still being decided (used to find open requests).
    OPEN = ("WAITING_HOD", "WAITING_ADMIN")

    department = models.ForeignKey("departments.Department", on_delete=models.PROTECT, related_name="promotion_requests")
    from_classroom = models.ForeignKey("academics.ClassRoom", on_delete=models.PROTECT, related_name="promotions_out")
    to_classroom = models.ForeignKey("academics.ClassRoom", on_delete=models.PROTECT, related_name="promotions_in")
    # Student ids that stay behind in the old class (for example students who failed).
    leave_behind = models.JSONField(default=list, blank=True)
    note = models.TextField(blank=True)
    status = models.CharField(max_length=14, choices=Status.choices, default=Status.WAITING_HOD)
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="promotion_requests")
    created_at = models.DateTimeField(auto_now_add=True)

    hod_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    hod_at = models.DateTimeField(null=True, blank=True)
    hod_remarks = models.TextField(blank=True)

    admin_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    admin_at = models.DateTimeField(null=True, blank=True)
    admin_remarks = models.TextField(blank=True)

    rejected_by_role = models.CharField(max_length=5, blank=True)  # "HOD" or "ADMIN": who said no
    moved_count = models.PositiveIntegerField(default=0)  # filled when the promotion is carried out
    clash_count = models.PositiveIntegerField(default=0)  # students not moved because the roll number was taken

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            # One open request per old class, so two people cannot promote the same class twice.
            models.UniqueConstraint(fields=["from_classroom"], condition=models.Q(status__in=("WAITING_HOD", "WAITING_ADMIN")),
                                    name="one_open_promotion_per_class"),
        ]

    def __str__(self):
        return f"Promotion {self.from_classroom} -> {self.to_classroom} ({self.status})"
