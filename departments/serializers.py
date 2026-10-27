"""Serializer for departments, plus a small helper that finds the department's HOD."""
from rest_framework import serializers

from accounts.models import User
from core.validators import title_text

from .models import Department


def hod_summary(department):
    """The department's single active HOD (or None)."""
    # `active_hods` is pre-loaded by DepartmentViewSet; if it is missing we ask the database instead
    hods = getattr(department, "active_hods", None)
    hod = hods[0] if hods else (None if hods is not None else department.users.filter(
        role=User.Role.HOD, is_active=True).first())
    if hod is None:
        return None
    return {"id": hod.id, "name": hod.get_full_name() or hod.username, "username": hod.username}


class DepartmentSerializer(serializers.ModelSerializer):
    """Reads and writes a department. The HOD and the three counts are read-only extras for the screen."""
    hod = serializers.SerializerMethodField()
    staff_count = serializers.IntegerField(read_only=True, default=0)
    student_count = serializers.IntegerField(read_only=True, default=0)
    class_count = serializers.IntegerField(read_only=True, default=0)

    class Meta:
        model = Department
        fields = [
            "id", "name", "code", "description", "is_active", "created_at",
            "hod", "staff_count", "student_count", "class_count",
        ]
        read_only_fields = ["id", "is_active", "created_at"]
        validators = []  # uniqueness is checked (case-insensitively) below

    def get_hod(self, obj):
        """The department's HOD as a small dict (or None)."""
        return hod_summary(obj)

    def _check_unique(self, field, value):
        """Fail if another department already has this name or code (ignoring upper / lower case)."""
        qs = Department.objects.filter(**{f"{field}__iexact": value})
        if self.instance:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError(f"A department with this {field} already exists.")

    def validate_name(self, value):
        """Collapse extra spaces, require at least 2 characters and a unique name."""
        value = title_text(value, "Department name")
        if len(value) < 2:
            raise serializers.ValidationError("Name must be at least 2 characters.")
        self._check_unique("name", value)
        return value

    def validate_code(self, value):
        """Store the code in capitals; letters and digits only; it must be unique."""
        value = value.strip().upper()
        if not value.isalnum():
            raise serializers.ValidationError("Code may contain only letters and digits.")
        self._check_unique("code", value)
        return value
