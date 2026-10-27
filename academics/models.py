"""Database tables for classes, subjects and the teacher-subject-class assignments."""
from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models

# academic year must look like 2026-27
academic_year_validator = RegexValidator(r"^\d{4}-\d{2}$", "Use the format YYYY-YY, for example 2026-27.")


class ClassRoom(models.Model):
    """A class of students, e.g. 'MCS Part I', semester 1, division A."""

    department = models.ForeignKey("departments.Department", on_delete=models.PROTECT, related_name="classrooms")
    name = models.CharField(max_length=80)
    semester = models.PositiveSmallIntegerField(validators=[MinValueValidator(1), MaxValueValidator(12)])
    division = models.CharField(max_length=5, default="A")
    academic_year = models.CharField(max_length=7, validators=[academic_year_validator])
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name", "semester", "division"]
        # the same class cannot be created twice in one department and academic year
        constraints = [
            models.UniqueConstraint(
                fields=["department", "name", "semester", "division", "academic_year"],
                name="uniq_class_per_department_year",
            )
        ]

    @property
    def label(self):
        """Readable class name used everywhere in the app, e.g. "MCS Part I - Sem 1 - Div A"."""
        return f"{self.name} - Sem {self.semester} - Div {self.division}"

    def __str__(self):
        """The class label."""
        return self.label


class Subject(models.Model):
    """A subject taught in a department, with a short code like CS101. Name and code are each unique inside the department."""

    department = models.ForeignKey("departments.Department", on_delete=models.PROTECT, related_name="subjects")
    name = models.CharField(max_length=120)
    code = models.CharField(max_length=20)
    semester = models.PositiveSmallIntegerField(null=True, blank=True, validators=[MinValueValidator(1), MaxValueValidator(12)])
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        constraints = [
            models.UniqueConstraint(fields=["department", "code"], name="uniq_subject_code_per_department"),
            models.UniqueConstraint(fields=["department", "name"], name="uniq_subject_name_per_department"),
        ]

    def __str__(self):
        """Name followed by the code."""
        return f"{self.name} ({self.code})"


class TeacherSubjectAssignment(models.Model):
    """
    Internal mapping: this teacher teaches this subject to this class in this
    academic year. (Not a homework assignment.)
    """

    teacher = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="teaching_assignments")
    subject = models.ForeignKey(Subject, on_delete=models.PROTECT, related_name="assignments")
    classroom = models.ForeignKey(ClassRoom, on_delete=models.PROTECT, related_name="assignments")
    academic_year = models.CharField(max_length=7, validators=[academic_year_validator])
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["teacher", "subject", "classroom", "academic_year"], name="uniq_teacher_subject_class_year"
            )
        ]

    def clean(self):
        """Model-level check: the teacher must be a staff member and teacher, subject and class must be in one department."""
        dept = self.classroom.department_id
        if self.teacher.role != "STAFF":
            raise ValidationError("Only staff members can be assigned to teach.")
        if not (self.teacher.department_id == self.subject.department_id == dept):
            raise ValidationError("Teacher, subject and class must belong to the same department.")

    def __str__(self):
        """Teacher - subject - class."""
        return f"{self.teacher.display_name} - {self.subject.name} - {self.classroom.label}"
