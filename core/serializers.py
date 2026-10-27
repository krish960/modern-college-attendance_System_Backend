"""Serializers that turn notifications and audit log entries into JSON (both are read-only)."""
from rest_framework import serializers

from .models import AuditLog, Notification


class NotificationSerializer(serializers.ModelSerializer):
    """A notification as JSON. Every field is read-only; changing is_read is done by the read actions in the view."""

    class Meta:
        model = Notification
        fields = ["id", "title", "message", "is_read", "created_at"]
        read_only_fields = fields


class AuditLogSerializer(serializers.ModelSerializer):
    """An audit log entry as JSON, with the actor's and department's names added. Read-only."""
    actor_name = serializers.SerializerMethodField()
    department_name = serializers.CharField(source="department.name", read_only=True, default=None)

    class Meta:
        model = AuditLog
        fields = [
            "id", "actor", "actor_name", "department", "department_name", "action", "entity_type",
            "entity_id", "old_value", "new_value", "reason", "extra", "created_at",
        ]
        read_only_fields = fields

    def get_actor_name(self, obj):
        """The actor's display name, or "System" when no user did it (or the user was removed)."""
        return obj.actor.display_name if obj.actor else "System"
