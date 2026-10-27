"""Serializers for classes, subjects and teacher assignments (the HOD of a department manages them).

They clean the text the browser sends and check that nothing is duplicated and that everything stays inside the HOD's department."""
from rest_framework import serializers

from accounts.models import User
from core.notifications import notify
from core.validators import title_text

from .models import ClassRoom, Subject, TeacherSubjectAssignment


class ClassRoomSerializer(serializers.ModelSerializer):
    """Reads and writes a class. `label` and `student_count` are only for showing; the department is set by the server."""
    label = serializers.CharField(read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True)
    student_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = ClassRoom
        fields = [
            "id", "department", "department_name", "name", "semester", "division", "academic_year",
            "is_active", "label", "student_count", "created_at",
        ]
        read_only_fields = ["id", "department", "created_at"]
        validators = []  # the duplicate check is done by hand in validate() below

    def validate_name(self, value):
        """Collapse extra spaces inside the class name; letters, numbers and . , & ( ) - / only."""
        return title_text(value, "Class name")

    def validate_division(self, value):
        """Divisions are stored as capital letters, like "A"."""
        return value.strip().upper()

    def validate(self, attrs):
        """Refuse a second class with the same name, semester, division and academic year in this department."""
        # on an edit, a field that was not sent keeps its old value
        get = lambda f: attrs.get(f, getattr(self.instance, f, None))  # noqa: E731
        dept = self.context["request"].user.department
        qs = ClassRoom.objects.filter(
            department=dept, name__iexact=get("name"), semester=get("semester"),
            division=get("division"), academic_year=get("academic_year"),
        )
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError("This class already exists for the selected academic year.")
        return attrs


def code_key(code):
    """The code without spaces, dashes, dots or case: what makes two codes 'the same'."""
    return "".join(ch for ch in str(code).upper() if ch.isalnum())


class SubjectSerializer(serializers.ModelSerializer):
    """Reads and writes a subject; the department is set by the server, and name and code must be unique in the department."""
    department_name = serializers.CharField(source="department.name", read_only=True)

    class Meta:
        model = Subject
        fields = ["id", "department", "department_name", "name", "code", "semester", "is_active", "created_at"]
        read_only_fields = ["id", "department", "created_at"]
        validators = []

    def validate_name(self, value):
        """Collapse extra spaces inside the subject name; letters, numbers and . , & ( ) - / only."""
        return title_text(value, "Subject name")

    def validate_code(self, value):
        """Store the code in capitals without spaces; it must contain at least one letter or number."""
        code = "".join(value.split()).upper()  # "cs 101" -> "CS101"
        if not code_key(code):
            raise serializers.ValidationError("The code needs letters or numbers, for example CS101.")
        return code

    def _unique(self, field, value):
        """Fail if another subject of this department already has the same name (ignoring case) or the same code (ignoring spaces, dashes and case)."""
        dept = self.context["request"].user.department
        if field == "code":  # CS 101, CS-101, cs101 and CS_101 are one code
            key = code_key(value)
            clash = [c for c in Subject.objects.filter(department=dept).exclude(pk=getattr(self.instance, "pk", None)).values_list("code", flat=True) if code_key(c) == key]
            if clash:
                raise serializers.ValidationError({field: f"A subject with this code already exists in your department ({clash[0]}). CS 101, CS-101 and cs101 are the same code."})
            return
        qs = Subject.objects.filter(department=dept, **{f"{field}__iexact": value})
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError({field: f"A subject with this {field} already exists in your department."})

    def validate(self, attrs):
        """Run the duplicate check for every name / code that was sent."""
        for field in ("name", "code"):
            if field in attrs:
                self._unique(field, attrs[field])
        return attrs


class AssignmentSerializer(serializers.ModelSerializer):
    """Reads and writes a teacher-subject-class assignment, and makes sure all three belong to the HOD's department."""
    teacher_name = serializers.CharField(source="teacher.display_name", read_only=True)
    subject_name = serializers.CharField(source="subject.name", read_only=True)
    subject_code = serializers.CharField(source="subject.code", read_only=True)
    classroom_name = serializers.CharField(source="classroom.label", read_only=True)

    class Meta:
        model = TeacherSubjectAssignment
        fields = [
            "id", "teacher", "teacher_name", "subject", "subject_name", "subject_code",
            "classroom", "classroom_name", "academic_year", "is_active", "created_at",
        ]
        read_only_fields = ["id", "is_active", "created_at"]
        extra_kwargs = {"academic_year": {"required": False}}
        validators = []

    def validate(self, attrs):
        """Department ownership, active status and no duplicate assignment for the same academic year."""
        dept_id = self.context["request"].user.department_id
        teacher, subject, classroom = attrs["teacher"], attrs["subject"], attrs["classroom"]
        # Querysets are not pre-filtered, so enforce department ownership explicitly.
        if teacher.role != User.Role.STAFF or teacher.department_id != dept_id or not teacher.is_active:
            raise serializers.ValidationError({"teacher": "Choose an active staff member from your department."})
        if subject.department_id != dept_id or not subject.is_active:
            raise serializers.ValidationError({"subject": "Choose an active subject from your department."})
        if classroom.department_id != dept_id or not classroom.is_active:
            raise serializers.ValidationError({"classroom": "Choose an active class from your department."})
        attrs.setdefault("academic_year", classroom.academic_year)  # if no year was sent, use the class's year
        if TeacherSubjectAssignment.objects.filter(
            teacher=teacher, subject=subject, classroom=classroom, academic_year=attrs["academic_year"]
        ).exists():
            raise serializers.ValidationError("This teacher is already assigned to this subject and class.")
        return attrs

    def create(self, validated_data):
        """Save the assignment and send the teacher a notification about it."""
        assignment = super().create(validated_data)
        notify(
            assignment.teacher, "New teaching assignment",
            f"You were assigned {assignment.subject.name} for {assignment.classroom.label}.",
        )
        return assignment
