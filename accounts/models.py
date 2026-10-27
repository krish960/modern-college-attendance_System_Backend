"""
Database tables for people: the custom User (with a role and a department) and the StaffProfile
(extra details for HODs and teachers).
"""
from django.contrib.auth.models import AbstractUser
from django.contrib.auth.models import UserManager as DjangoUserManager
from django.db import models
from django.db.models import Q


class UserManager(DjangoUserManager):
    """Same as Django's manager, but command-line superusers become College Admins."""

    def create_superuser(self, username, email=None, password=None, **extra_fields):
        # Superusers created from the command line are College Admins.
        extra_fields.setdefault("role", User.Role.ADMIN)
        return super().create_superuser(username, email, password, **extra_fields)


class User(AbstractUser):
    """
    A login account. The `role` decides what the person may do (ADMIN > HOD > STAFF) and the
    `department` decides whose data they can see. Users are never hard deleted; they are made
    inactive instead (a soft delete), so old attendance records still show who did them.
    """

    # The three roles. Admin runs the college, an HOD runs one department, STAFF are teachers.
    class Role(models.TextChoices):
        ADMIN = "ADMIN", "College Admin"
        HOD = "HOD", "Head of Department"
        STAFF = "STAFF", "Staff / Teacher"

    role = models.CharField(max_length=10, choices=Role.choices, default=Role.STAFF)
    # PROTECT: a department cannot be deleted while people still belong to it.
    department = models.ForeignKey(
        "departments.Department", null=True, blank=True, on_delete=models.PROTECT, related_name="users"
    )
    phone = models.CharField(max_length=20, blank=True)

    objects = UserManager()

    class Meta:
        constraints = [
            # Exactly one ACTIVE HOD per department (enforced by the database).
            models.UniqueConstraint(
                fields=["department"],
                condition=Q(role="HOD", is_active=True),
                name="one_active_hod_per_department",
            ),
            # Only the College Admin may be department-less.
            models.CheckConstraint(
                condition=Q(role="ADMIN") | Q(department__isnull=False),
                name="non_admin_requires_department",
            ),
        ]

    # Full name if set, otherwise the username. Used in lists, audit logs and messages.
    @property
    def display_name(self):
        return self.get_full_name() or self.username

    def __str__(self):
        return f"{self.display_name} ({self.role})"


class StaffProfile(models.Model):
    """Extra details for HODs and teachers."""

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    # Unique inside a department (checked in the serializer); auto-filled as EMP-001, EMP-002...
    employee_id = models.CharField(max_length=30, blank=True)
    designation = models.CharField(max_length=60, blank=True)

    class Employment(models.TextChoices):
        PERMANENT = "PERMANENT", "Permanent"
        PROVISIONAL = "PROVISIONAL", "Provisional"

    # Medical leave (ML) is for permanent staff only.
    employment = models.CharField(max_length=12, choices=Employment.choices, default=Employment.PERMANENT)
    # Code on the staff member's ID card (barcode / QR text) so the HOD can scan it for attendance.
    card_code = models.CharField(max_length=100, blank=True, default="")

    class Meta:
        constraints = [
            # No two people may share a card code (empty codes are allowed many times).
            models.UniqueConstraint(fields=["card_code"], condition=~models.Q(card_code=""), name="uniq_staff_card_code"),
        ]

    def __str__(self):
        return f"Profile of {self.user.username}"
