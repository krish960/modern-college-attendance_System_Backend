"""Teacher leave requests: the serializer that checks a request, the filter, and the API (ask, cancel, approve, reject).

A teacher asks, the HOD decides, the Admin can only read. Approving a leave marks the covered days as On leave in the
staff attendance, except days where the teacher was already present."""
import datetime

import django_filters
from django.db import transaction
from django.utils import timezone
from rest_framework import mixins, serializers, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from accounts.models import User
from accounts.permissions import IsHOD, IsStaffTeacher
from attendance.serializers import ReviewSerializer
from core.audit import log_action
from core.notifications import notify, notify_many
from core.scoping import department_scope
from core.validators import reason_text

from .models import LeaveRequest, StaffAttendance

ST = StaffAttendance.Status
MAX_DAYS = 31  # one request can cover at most this many calendar days
BACKDATE_DAYS = 7  # leave can start at most this many days in the past
KEEP = (ST.PRESENT, ST.LATE, ST.HALF_DAY)  # the teacher did come: an approved leave never overwrites that


def leave_days(req):
    """The days a leave covers: every date from..to except Sundays."""
    d, out = req.from_date, []
    while d <= req.to_date:
        if d.weekday() != 6:
            out.append(d)
        d += datetime.timedelta(days=1)
    return out


class LeaveSerializer(serializers.ModelSerializer):
    """Shows a leave request with extra read-only names, and checks the dates, reason and leave type of a new one."""
    teacher_name = serializers.CharField(source="user.display_name", read_only=True)
    employee_id = serializers.CharField(source="user.profile.employee_id", read_only=True, default="")
    department_name = serializers.CharField(source="user.department.name", read_only=True, default="")
    reviewed_by_name = serializers.CharField(source="reviewed_by.display_name", read_only=True, default=None)
    reason = serializers.CharField(trim_whitespace=True, max_length=1000)
    days = serializers.SerializerMethodField()
    leave_type = serializers.ChoiceField(choices=LeaveRequest.Kind.choices)
    leave_type_label = serializers.CharField(source="get_leave_type_display", read_only=True)

    class Meta:
        model = LeaveRequest
        fields = ["id", "user", "teacher_name", "employee_id", "department_name", "leave_type", "leave_type_label", "from_date", "to_date", "days", "reason", "status",
                  "reviewed_by_name", "remarks", "created_at", "reviewed_at"]
        read_only_fields = ["id", "user", "status", "remarks", "created_at", "reviewed_at"]

    def get_days(self, obj):
        """Number of working days the leave covers (Sundays are not counted)."""
        return len(leave_days(obj))

    def validate_reason(self, value):
        """The reason must be at least 5 characters after removing spaces."""
        return reason_text(value)

    def validate(self, attrs):
        """Date rules (order, how far back, maximum length), the medical-leave rule, and no overlap with another open or approved leave."""
        today = timezone.localdate()
        start, end = attrs["from_date"], attrs["to_date"]
        if end < start:
            raise serializers.ValidationError({"to_date": "The last day cannot be before the first day."})
        if start < today - datetime.timedelta(days=BACKDATE_DAYS):
            raise serializers.ValidationError({"from_date": f"Leave can be requested at most {BACKDATE_DAYS} days back. Ask your HOD to mark older days."})
        if (end - start).days + 1 > MAX_DAYS:
            raise serializers.ValidationError({"to_date": f"One request can cover at most {MAX_DAYS} days."})
        profile = getattr(self.context["request"].user, "profile", None)
        # medical leave is only for permanent staff, not for provisional staff
        if attrs.get("leave_type") == LeaveRequest.Kind.SICK and profile and profile.employment == "PROVISIONAL":
            raise serializers.ValidationError({"leave_type": "Medical leave (ML) is for permanent staff only. Choose another type, such as Casual leave (CL) or Leave without pay (WPL)."})
        # two date ranges overlap when each one starts before the other ends
        clash = LeaveRequest.objects.filter(
            user=self.context["request"].user, status__in=(*LeaveRequest.OPEN, LeaveRequest.Status.APPROVED),
            from_date__lte=end, to_date__gte=start,
        ).exists()
        if clash:
            raise serializers.ValidationError("You already have a pending or approved leave on some of these days.")
        return attrs


class LeaveFilter(django_filters.FilterSet):
    """Lets the list be filtered by status, teacher and leave type."""
    # status=OPEN means "not decided yet": pending or in process
    status = django_filters.CharFilter(method="by_status")

    class Meta:
        model = LeaveRequest
        fields = ["user", "leave_type"]

    def by_status(self, qs, name, value):
        """OPEN means pending or in process; a valid status name filters exactly; anything else gives an empty list."""
        if value == "OPEN":
            return qs.filter(status__in=LeaveRequest.OPEN)
        return qs.filter(status=value) if value in LeaveRequest.Status.values else qs.none()


class LeaveRequestViewSet(mixins.CreateModelMixin, mixins.ListModelMixin, mixins.RetrieveModelMixin, viewsets.GenericViewSet):
    """
    Teacher: ask for leave with a reason, see own requests, cancel a pending one.
    HOD: sees the department's requests and approves (days become On leave) or rejects with remarks.
    Admin: read-only.
    """

    serializer_class = LeaveSerializer
    filterset_class = LeaveFilter
    search_fields = ["user__first_name", "user__last_name", "reason"]

    def get_permissions(self):
        """Only a teacher asks or cancels, only the HOD decides; reading follows the default (any signed-in role)."""
        if self.action == "create" or self.action == "cancel":
            return [IsStaffTeacher()]
        if self.action in ("approve", "reject", "processing", "pending"):
            return [IsHOD()]
        return super().get_permissions()

    def get_queryset(self):
        """Leave requests of the user's department; a teacher sees only their own."""
        user = self.request.user
        qs = department_scope(LeaveRequest.objects.select_related("user__profile", "user__department", "reviewed_by"), user, "user__department_id")
        if user.role == User.Role.STAFF:
            qs = qs.filter(user=user)
        return qs

    @transaction.atomic
    def perform_create(self, serializer):
        """Save the request for the logged-in teacher, write the audit log and notify the department's HOD."""
        req = serializer.save(user=self.request.user)
        log_action(self.request.user, "LEAVE_REQUESTED", req, department=self.request.user.department,
                   new={"type": req.leave_type, "from": str(req.from_date), "to": str(req.to_date)}, reason=req.reason)
        hods = User.objects.filter(role=User.Role.HOD, department=self.request.user.department, is_active=True)
        notify_many(hods, "Leave request", f"{self.request.user.display_name} asks for {req.get_leave_type_display().lower()} {req.from_date} to {req.to_date}: {req.reason}")

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def cancel(self, request, pk=None):
        """A teacher withdraws a request that is still waiting (pending or in process)."""
        req = LeaveRequest.objects.select_for_update().get(pk=self.get_object().pk)
        if req.status not in LeaveRequest.OPEN:
            raise ValidationError("Only a request that is still waiting can be cancelled.")
        req.status = LeaveRequest.Status.CANCELLED
        req.save(update_fields=["status"])
        log_action(request.user, "LEAVE_CANCELLED", req, department=request.user.department)
        return Response(self.get_serializer(self.get_queryset().get(pk=req.pk)).data)

    @transaction.atomic
    def _review(self, request, approve):
        """Shared code for approve and reject. Approving also writes On leave into the staff attendance of each covered day."""
        req = LeaveRequest.objects.select_for_update().select_related("user").get(pk=self.get_object().pk)
        data = ReviewSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        remarks = data.validated_data["remarks"]
        if req.status not in LeaveRequest.OPEN:
            raise ValidationError("This request has already been reviewed.")
        applied = 0  # how many days were really marked On leave
        if approve:
            for day in leave_days(req):
                record = StaffAttendance.objects.select_for_update().filter(user=req.user, date=day).first()
                if record is None:  # no mark yet for this day: create an On leave mark
                    StaffAttendance.objects.create(user=req.user, date=day, status=ST.LEAVE, source=StaffAttendance.Source.LEAVE_REQUEST,
                                                   note=f"Approved: {req.get_leave_type_display()}", marked_by=request.user)
                    applied += 1
                elif record.status not in KEEP:  # replace Absent / old marks, but never a day the teacher was present
                    record.status, record.source, record.marked_by, record.note = ST.LEAVE, StaffAttendance.Source.LEAVE_REQUEST, request.user, f"Approved: {req.get_leave_type_display()}"
                    record.save(update_fields=["status", "source", "marked_by", "note", "updated_at"])
                    applied += 1
            req.status = LeaveRequest.Status.APPROVED
        else:
            req.status = LeaveRequest.Status.REJECTED
        req.reviewed_by, req.reviewed_at, req.remarks = request.user, timezone.now(), remarks
        req.save(update_fields=["status", "reviewed_by", "reviewed_at", "remarks"])
        log_action(request.user, "LEAVE_APPROVED" if approve else "LEAVE_REJECTED", req, department=req.user.department,
                   new={"type": req.leave_type, "from": str(req.from_date), "to": str(req.to_date), "days_marked": applied}, reason=req.reason, extra={"remarks": remarks})
        verdict = "approved" if approve else "rejected"
        notify(req.user, f"Leave {verdict}", f"Your leave {req.from_date} to {req.to_date} was {verdict}. HOD: {remarks}")
        return Response(self.get_serializer(self.get_queryset().get(pk=req.pk)).data)

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """HOD approves the request."""
        return self._review(request, True)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        """HOD rejects the request."""
        return self._review(request, False)

    @transaction.atomic
    def _mark(self, request, new_status, from_status, title, text):
        """HOD: say that a request is being looked into (In process), or put it back to Pending. Nothing is marked in attendance."""
        req = LeaveRequest.objects.select_for_update().select_related("user").get(pk=self.get_object().pk)
        if req.status != from_status:
            raise ValidationError("This request is not " + ("pending." if from_status == LeaveRequest.Status.PENDING else "in process."))
        remarks = str(request.data.get("remarks", "")).strip()[:1000]
        req.status = new_status
        if remarks:
            req.remarks = remarks
        req.save(update_fields=["status", "remarks"])
        log_action(request.user, f"LEAVE_{new_status}", req, department=req.user.department,
                   new={"from": str(req.from_date), "to": str(req.to_date)}, reason=req.reason, extra={"remarks": remarks})
        notify(req.user, title, f"Your leave {req.from_date} to {req.to_date} {text}" + (f" HOD: {remarks}" if remarks else ""))
        return Response(self.get_serializer(self.get_queryset().get(pk=req.pk)).data)

    @action(detail=True, methods=["post"])
    def processing(self, request, pk=None):
        """HOD moves a pending request to In process."""
        return self._mark(request, LeaveRequest.Status.PROCESSING, LeaveRequest.Status.PENDING, "Leave in process", "is being looked into by your HOD.")

    @action(detail=True, methods=["post"])
    def pending(self, request, pk=None):
        """HOD puts an In process request back to Pending."""
        return self._mark(request, LeaveRequest.Status.PENDING, LeaveRequest.Status.PROCESSING, "Leave is pending", "is back to pending.")
