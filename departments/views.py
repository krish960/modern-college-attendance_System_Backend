"""API views for departments: the college Admin creates and edits them; others can only read their own department."""
from django.db.models import Prefetch
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from accounts.models import User
from accounts.permissions import IsCollegeAdmin
from academics.models import ClassRoom
from core.aggregates import count_by
from core.audit import log_action
from students.models import Student

from .models import Department
from .serializers import DepartmentSerializer


class DepartmentViewSet(
    mixins.CreateModelMixin,
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """
    Admin: full management (no hard delete - departments are deactivated).
    HOD / Staff: read-only access to their own department.
    """

    serializer_class = DepartmentSerializer
    search_fields = ["name", "code"]
    filterset_fields = ["is_active"]
    ordering_fields = ["name", "created_at"]

    def get_permissions(self):
        """Any signed-in user may read; creating, editing and switching on or off needs the college Admin."""
        if self.action in ("list", "retrieve"):
            return [IsAuthenticated()]
        return [IsCollegeAdmin()]

    def get_queryset(self):
        """Departments with counts of active staff, students and classes, and their active HOD attached (fewer database queries)."""
        qs = (
            Department.objects.annotate(
                staff_count=count_by(User.objects.filter(role=User.Role.STAFF, is_active=True), "department"),
                student_count=count_by(Student.objects.filter(is_active=True), "department"),
                class_count=count_by(ClassRoom.objects.filter(is_active=True), "department"),
            )
            .prefetch_related(
                Prefetch(
                    "users",
                    queryset=User.objects.filter(role=User.Role.HOD, is_active=True),
                    to_attr="active_hods",
                )
            )
            .order_by("name")
        )
        user = self.request.user
        if user.role != User.Role.ADMIN:  # everyone except the Admin sees only their own department
            qs = qs.filter(pk=user.department_id)
        return qs

    def perform_create(self, serializer):
        """Save the new department and write the audit log."""
        dept = serializer.save()
        log_action(self.request.user, "DEPARTMENT_CREATED", dept, department=dept,
                   new={"name": dept.name, "code": dept.code})

    def perform_update(self, serializer):
        """Save the edit and write the old and new values to the audit log."""
        old = {"name": serializer.instance.name, "code": serializer.instance.code,
               "description": serializer.instance.description}
        dept = serializer.save()
        log_action(self.request.user, "DEPARTMENT_UPDATED", dept, department=dept, old=old,
                   new={"name": dept.name, "code": dept.code, "description": dept.description})

    def _set_active(self, request, value):
        """Switch the department on or off (soft delete). The audit log is written only when the value really changes."""
        dept = self.get_object()
        if dept.is_active != value:
            dept.is_active = value
            dept.save(update_fields=["is_active"])
            log_action(request.user, "DEPARTMENT_ACTIVATED" if value else "DEPARTMENT_DEACTIVATED",
                       dept, department=dept, old={"is_active": not value}, new={"is_active": value})
        return Response(self.get_serializer(self.get_queryset().get(pk=dept.pk)).data, status=status.HTTP_200_OK)

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        """Make the department inactive."""
        return self._set_active(request, False)

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        """Make the department active again."""
        return self._set_active(request, True)
