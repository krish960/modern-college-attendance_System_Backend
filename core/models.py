"""Database tables shared by the whole app: in-app notifications and the append-only audit log."""
from django.conf import settings
from django.db import models


class Notification(models.Model):
    """In-app message for a single user (e.g. 'your correction was approved')."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="notifications")
    title = models.CharField(max_length=120)
    message = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["user", "is_read"])]  # makes "my unread notifications" fast

    def __str__(self):
        """User and title, for the admin site and logs."""
        return f"{self.user} - {self.title}"


class AuditLog(models.Model):
    """Append-only record of sensitive actions. Entries cannot be edited or deleted."""

    # who did it; kept as empty (not deleted) if that user is removed later
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_logs"
    )
    department = models.ForeignKey(
        "departments.Department", on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_logs"
    )
    action = models.CharField(max_length=60)  # what happened, like STUDENT_REGISTERED
    entity_type = models.CharField(max_length=60)  # the kind of record that was changed
    entity_id = models.CharField(max_length=40)  # the id of that record
    old_value = models.JSONField(null=True, blank=True)
    new_value = models.JSONField(null=True, blank=True)
    reason = models.TextField(blank=True)
    extra = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["department", "-created_at"]),
            models.Index(fields=["-created_at"]),
            models.Index(fields=["entity_type", "entity_id"]),
        ]

    def save(self, *args, **kwargs):
        """Allow only the first save (adding a new entry); editing an existing entry is refused."""
        if self.pk and not self._state.adding:
            raise PermissionError("Audit log entries cannot be modified.")
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        """Always refused: audit entries are never removed."""
        raise PermissionError("Audit log entries cannot be deleted.")

    def __str__(self):
        """Action and the record it was about."""
        return f"{self.action} {self.entity_type}#{self.entity_id}"
