"""API views for notifications (each user reads their own) and the audit log (read-only, for the Admin and HODs)."""
import django_filters
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.permissions import IsAdminOrHOD
from core.scoping import department_scope

from .models import AuditLog, Notification
from .serializers import AuditLogSerializer, NotificationSerializer


class NotificationViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """A user only ever sees their own notifications."""

    serializer_class = NotificationSerializer
    filterset_fields = ["is_read"]
    search_fields = ["title", "message"]

    def get_queryset(self):
        """Only the notifications that belong to the logged-in user."""
        return Notification.objects.filter(user=self.request.user)

    @action(detail=True, methods=["post"])
    def read(self, request, pk=None):
        """Mark one notification as read."""
        note = self.get_object()
        if not note.is_read:
            note.is_read = True
            note.save(update_fields=["is_read"])
        return Response(self.get_serializer(note).data)

    @action(detail=False, methods=["post"], url_path="read-all")
    def read_all(self, request):
        """Mark all of my unread notifications as read and say how many changed."""
        updated = self.get_queryset().filter(is_read=False).update(is_read=True)
        return Response({"updated": updated})

    @action(detail=False, methods=["get"], url_path="unread-count")
    def unread_count(self, request):
        """How many unread notifications I have (for the bell icon)."""
        return Response({"count": self.get_queryset().filter(is_read=False).count()})


class AuditLogFilter(django_filters.FilterSet):
    """Filter the audit log by action, type of record, department and a date range (by the day it was written)."""
    date_from = django_filters.DateFilter(field_name="created_at", lookup_expr="date__gte")
    date_to = django_filters.DateFilter(field_name="created_at", lookup_expr="date__lte")

    class Meta:
        model = AuditLog
        fields = ["action", "entity_type", "department"]


class AuditLogViewSet(mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """Read-only. Admin sees all departments; an HOD sees only their own."""

    serializer_class = AuditLogSerializer
    permission_classes = [IsAdminOrHOD]
    filterset_class = AuditLogFilter
    search_fields = ["action", "entity_type", "reason", "actor__username", "actor__first_name", "actor__last_name"]

    def get_queryset(self):
        """Audit entries limited to the user's department (the Admin sees all)."""
        qs = AuditLog.objects.select_related("actor", "department")
        return department_scope(qs, self.request.user)
