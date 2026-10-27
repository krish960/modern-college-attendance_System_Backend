"""API views for classes, subjects and teacher assignments.

Everyone signed in can read (limited to their own department; a teacher sees only what they teach). Only the HOD can
create or change them. Nothing is deleted: items are switched inactive so old attendance stays valid."""
from rest_framework import mixins, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from accounts.models import User
from accounts.permissions import IsHOD
from core.aggregates import count_by
from core.audit import log_action
from core.scoping import department_scope
from students.models import Student

from .models import ClassRoom, Subject, TeacherSubjectAssignment
from .serializers import AssignmentSerializer, ClassRoomSerializer, SubjectSerializer


class HODWritesMixin:
    """Everyone authenticated may read (scoped); only the department's HOD may write."""

    def get_permissions(self):
        """Reading uses the default permission (any signed-in user); every write needs the HOD."""
        if self.action in ("list", "retrieve"):
            return super().get_permissions()
        return [IsHOD()]


class ClassRoomViewSet(HODWritesMixin, mixins.CreateModelMixin, mixins.ListModelMixin,
                       mixins.RetrieveModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    """Create, list, read and edit classes (no delete: set is_active to false instead)."""

    serializer_class = ClassRoomSerializer
    search_fields = ["name", "division", "academic_year"]
    filterset_fields = ["is_active", "semester", "division", "academic_year", "department"]

    def get_queryset(self):
        """Classes in the user's department, each with its count of active students; a teacher sees only classes they teach."""
        user = self.request.user
        qs = ClassRoom.objects.select_related("department").annotate(
            student_count=count_by(Student.objects.filter(is_active=True), "classroom")
        ).order_by("name", "semester", "division")
        qs = department_scope(qs, user)
        if user.role == User.Role.STAFF:  # teachers only see classes they teach
            mine = TeacherSubjectAssignment.objects.filter(teacher=user, is_active=True)
            qs = qs.filter(id__in=mine.values("classroom_id"))
        return qs

    def perform_create(self, serializer):
        """Save the class in the HOD's own department and write the audit log."""
        obj = serializer.save(department=self.request.user.department)
        log_action(self.request.user, "CLASS_CREATED", obj, department=obj.department, new={"label": obj.label})

    def perform_update(self, serializer):
        """Save the changes and write the audit log."""
        obj = serializer.save()
        log_action(self.request.user, "CLASS_UPDATED", obj, department=obj.department,
                   new={"label": obj.label, "is_active": obj.is_active})


class SubjectViewSet(HODWritesMixin, mixins.CreateModelMixin, mixins.ListModelMixin,
                     mixins.RetrieveModelMixin, mixins.UpdateModelMixin, viewsets.GenericViewSet):
    """Create, list, read and edit subjects (no delete: set is_active to false instead)."""

    serializer_class = SubjectSerializer
    search_fields = ["name", "code"]
    filterset_fields = ["is_active", "semester", "department"]

    def get_queryset(self):
        """Subjects in the user's department; a teacher sees only subjects they teach."""
        user = self.request.user
        qs = department_scope(Subject.objects.select_related("department"), user)
        if user.role == User.Role.STAFF:
            mine = TeacherSubjectAssignment.objects.filter(teacher=user, is_active=True)
            qs = qs.filter(id__in=mine.values("subject_id"))
        return qs

    def perform_create(self, serializer):
        """Save the subject in the HOD's own department and write the audit log."""
        obj = serializer.save(department=self.request.user.department)
        log_action(self.request.user, "SUBJECT_CREATED", obj, department=obj.department, new={"name": obj.name, "code": obj.code})

    def perform_update(self, serializer):
        """Save the changes and write the audit log."""
        obj = serializer.save()
        log_action(self.request.user, "SUBJECT_UPDATED", obj, department=obj.department,
                   new={"name": obj.name, "code": obj.code, "is_active": obj.is_active})


class AssignmentViewSet(HODWritesMixin, mixins.CreateModelMixin, mixins.ListModelMixin,
                        mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    Teacher <-> subject <-> class mappings. Only the HOD creates / (de)activates
    them. The teacher, subject and class of a mapping can never be edited (history
    depends on them) - deactivate and create a new mapping instead.
    """

    serializer_class = AssignmentSerializer
    search_fields = ["teacher__first_name", "teacher__last_name", "subject__name", "classroom__name"]
    filterset_fields = ["is_active", "teacher", "subject", "classroom", "academic_year"]

    def get_queryset(self):
        """Assignments of the user's department (found through the class); a teacher sees only their own."""
        user = self.request.user
        qs = TeacherSubjectAssignment.objects.select_related("teacher", "subject", "classroom")
        qs = department_scope(qs, user, "classroom__department_id")
        if user.role == User.Role.STAFF:
            qs = qs.filter(teacher=user)
        return qs

    def perform_create(self, serializer):
        """Save the assignment and write the audit log."""
        obj = serializer.save()
        log_action(self.request.user, "ASSIGNMENT_CREATED", obj, department=obj.classroom.department,
                   new={"teacher": obj.teacher.username, "subject": obj.subject.name, "class": obj.classroom.label})

    def _set_active(self, value):
        """Switch the assignment on or off. The audit log is written only when the value really changes."""
        obj = self.get_object()
        if obj.is_active != value:
            obj.is_active = value
            obj.save(update_fields=["is_active"])
            log_action(self.request.user, "ASSIGNMENT_ACTIVATED" if value else "ASSIGNMENT_DEACTIVATED", obj,
                       department=obj.classroom.department, old={"is_active": not value}, new={"is_active": value})
        return Response(self.get_serializer(obj).data)

    @action(detail=True, methods=["post"])
    def deactivate(self, request, pk=None):
        """Stop this assignment (the teacher can no longer take lectures for it)."""
        return self._set_active(False)

    @action(detail=True, methods=["post"])
    def activate(self, request, pk=None):
        """Start this assignment again."""
        return self._set_active(True)
