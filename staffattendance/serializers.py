"""Input checking for the staff attendance "mark" endpoint."""
from rest_framework import serializers

from .models import StaffAttendance


class MarkSerializer(serializers.Serializer):
    """Checks the input when an admin/HOD marks one staff member (user id, optional date, status, note)."""
    user = serializers.IntegerField()
    date = serializers.DateField(required=False)
    # AUTO means "decide Present or Late from the check-in time" (see services.py).
    status = serializers.ChoiceField(choices=[*StaffAttendance.Status.values, "AUTO"])
    note = serializers.CharField(required=False, allow_blank=True, max_length=200)
    scan = serializers.BooleanField(required=False, default=False)  # true when it came from scanning an ID card
