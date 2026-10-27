"""Serializers for students: they turn students, removal requests and promotion requests into JSON and check incoming data.

The checks here are business rules too: who may register in which class, who may move or deactivate a student, and roll number and ID card uniqueness."""
from rest_framework import serializers

from core.validators import card_text, person_name, reason_text, roll_text
from academics.models import ClassRoom, TeacherSubjectAssignment
from accounts.models import User

from .models import ClassPromotionRequest, Student, StudentRemovalRequest, normalize_card_code, normalize_roll_number


def can_access_classroom(user, classroom):
    """HOD: any class in own department. Staff: classes they actively teach."""
    if user.role == User.Role.HOD:
        return classroom.department_id == user.department_id
    if user.role == User.Role.STAFF:
        return TeacherSubjectAssignment.objects.filter(teacher=user, classroom=classroom, is_active=True).exists()
    return False


class StudentSerializer(serializers.ModelSerializer):
    """JSON shape of a student plus the checks for registering or editing one. The photo bytes are never included."""
    classroom_name = serializers.CharField(source="classroom.label", read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True)
    created_by_name = serializers.CharField(source="created_by.display_name", read_only=True, default=None)
    classroom = serializers.PrimaryKeyRelatedField(queryset=ClassRoom.objects.filter(is_active=True))
    # Declared by hand so the friendly "already registered to <student>" message in validate_card_code is the one shown.
    card_code = serializers.CharField(max_length=100, required=False, allow_blank=True)

    has_photo = serializers.SerializerMethodField()
    photo_version = serializers.SerializerMethodField()

    def get_has_photo(self, obj):
        """True when a photo has been uploaded (the image itself is fetched from a separate URL)."""
        return obj.photo_updated_at is not None

    def get_photo_version(self, obj):
        """A number that changes when the photo changes, so the browser reloads the picture only when needed."""
        return int(obj.photo_updated_at.timestamp()) if obj.photo_updated_at else 0

    class Meta:
        model = Student
        fields = [
            "id", "roll_number", "name", "card_code", "has_photo", "photo_version", "classroom", "classroom_name", "department", "department_name",
            "is_active", "created_by", "created_by_name", "created_at",
        ]
        read_only_fields = ["id", "department", "created_by", "created_at"]
        validators = []  # duplicate roll numbers get a friendlier 409 response in the view

    def validate_roll_number(self, value):
        """Clean the roll number (no spaces, no leading zeros) and make sure it is not empty."""
        value = normalize_roll_number(value)
        if not value:
            raise serializers.ValidationError("Roll number is required.")
        value = roll_text(value)  # letters and digits (and - / inside); not "!!", "-5" or "0"
        return value

    def validate_card_code(self, value):
        """Clean the ID card code and refuse it if another student already owns that card."""
        value = normalize_card_code(card_text(value))
        if value:
            clash = Student.objects.filter(card_code=value)
            if self.instance is not None:
                clash = clash.exclude(pk=self.instance.pk)
            other = clash.select_related("classroom").first()
            if other:
                raise serializers.ValidationError(
                    f"This ID card is already registered to {other.name} (roll {other.roll_number}, {other.classroom.label})."
                )
        return value

    def validate_name(self, value):
        """Remove extra spaces from the name and require at least 2 characters."""
        return person_name(value, "Student name", minimum=2)

    def validate_classroom(self, classroom):
        """Who may put a student in a class: teachers and HODs register only in classes they can access; only the HOD or Admin moves a student, and only within the department."""
        user = self.context["request"].user
        if self.instance is not None:
            if classroom.pk != self.instance.classroom_id:
                # Only the HOD or the college admin moves a student, and only inside the student's department.
                if user.role not in (User.Role.HOD, User.Role.ADMIN):
                    raise serializers.ValidationError("Only your HOD can move a student to another class.")
                if classroom.department_id != self.instance.department_id:
                    raise serializers.ValidationError("Choose a class of the same department.")
            return classroom
        if not can_access_classroom(user, classroom):
            raise serializers.ValidationError("You can only register students in classes assigned to you.")
        return classroom

    def validate(self, attrs):
        # Only the HOD may deactivate / reactivate a student: other teachers of the class share the roster.
        """Rules that need several fields: only HOD/Admin may deactivate, and a roll number must stay unique inside the class."""
        if "is_active" in attrs and self.context["request"].user.role not in (User.Role.HOD, User.Role.ADMIN):
            if self.instance is None:
                attrs["is_active"] = True
            elif attrs["is_active"] != self.instance.is_active:
                raise serializers.ValidationError({"is_active": "Only your HOD can deactivate or reactivate a student."})
        # Updating a roll number must not collide with another student in the same class.
        if self.instance is not None and ("roll_number" in attrs or "classroom" in attrs):
            target = attrs.get("classroom") or self.instance.classroom
            roll = attrs.get("roll_number", self.instance.roll_number)
            clash = Student.objects.filter(classroom_id=target.pk, roll_number=roll).exclude(pk=self.instance.pk)
            if clash.exists():
                raise serializers.ValidationError({"roll_number": f"Another student in {target.label} already has roll number {roll}. Change the roll number as well."})
        return attrs


class RemovalRequestSerializer(serializers.ModelSerializer):
    """JSON shape of a student removal request, with names added for the screens."""
    student_name = serializers.CharField(source="student.name", read_only=True)
    roll_number = serializers.CharField(source="student.roll_number", read_only=True)
    classroom_name = serializers.CharField(source="student.classroom.label", read_only=True)
    student_is_active = serializers.BooleanField(source="student.is_active", read_only=True)
    requested_by_name = serializers.CharField(source="requested_by.display_name", read_only=True)
    reviewed_by_name = serializers.CharField(source="reviewed_by.display_name", read_only=True, default=None)
    reason = serializers.CharField(trim_whitespace=True, max_length=1000)

    class Meta:
        model = StudentRemovalRequest
        fields = [
            "id", "student", "student_name", "roll_number", "classroom_name", "student_is_active", "reason", "status",
            "requested_by", "requested_by_name", "reviewed_by_name", "remarks", "created_at", "reviewed_at",
        ]
        read_only_fields = ["id", "status", "requested_by", "remarks", "created_at", "reviewed_at"]

    def validate_reason(self, value):
        """A removal request must come with a reason of at least 5 characters."""
        return reason_text(value)

    def validate_student(self, student):
        """The student must be in a class the teacher teaches, still active, and have no open removal request."""
        user = self.context["request"].user
        if not can_access_classroom(user, student.classroom):
            raise serializers.ValidationError("You can only ask to remove students of classes you teach.")
        if not student.is_active:
            raise serializers.ValidationError("This student is already inactive.")
        if student.removal_requests.filter(status=StudentRemovalRequest.Status.PENDING).exists():
            raise serializers.ValidationError("A removal request for this student is already waiting for the HOD.")
        return student


class PromotionRequestSerializer(serializers.ModelSerializer):
    """Read-only view of a class promotion request, with readable names and who decided each step."""

    from_name = serializers.CharField(source="from_classroom.label", read_only=True)
    to_name = serializers.CharField(source="to_classroom.label", read_only=True)
    department_name = serializers.CharField(source="department.name", read_only=True)
    requested_by_name = serializers.CharField(source="requested_by.display_name", read_only=True)
    hod_name = serializers.CharField(source="hod_by.display_name", read_only=True, default=None)
    admin_name = serializers.CharField(source="admin_by.display_name", read_only=True, default=None)
    status_label = serializers.CharField(source="get_status_display", read_only=True)
    student_count = serializers.SerializerMethodField()

    def get_student_count(self, obj):
        # Students that would move now (or the number that moved, once done).
        """How many students would move now, or how many did move once the promotion is done."""
        if obj.status == "DONE":
            return obj.moved_count
        return Student.objects.filter(classroom=obj.from_classroom, is_active=True).exclude(pk__in=obj.leave_behind).count()

    class Meta:
        model = ClassPromotionRequest
        fields = [
            "id", "department_name", "from_classroom", "from_name", "to_classroom", "to_name", "leave_behind", "note",
            "status", "status_label", "requested_by", "requested_by_name", "created_at", "student_count",
            "hod_name", "hod_at", "hod_remarks", "admin_name", "admin_at", "admin_remarks",
            "rejected_by_role", "moved_count", "clash_count",
        ]
        read_only_fields = fields
