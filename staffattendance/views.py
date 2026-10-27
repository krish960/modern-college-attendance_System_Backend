"""API views for teacher (staff) attendance: the HOD marks a day, a teacher checks in, and the day / month / on-leave reports.

The real work (late cut-off, sheets, scoping) is in services.py; the views here check the role and write the audit log."""
from django.db import transaction
from django.utils import timezone
from rest_framework import status as http
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.permissions import IsAdminOrHOD, IsHOD, IsStaffTeacher
from core.audit import log_action
from reports import exporters

from . import services
from .models import LeaveRequest, StaffAttendance
from .serializers import MarkSerializer

ST = StaffAttendance.Status


def export_kind(request):
    """Read ?export= from the URL: "csv", "pdf" or "" for no export. Anything else is an error."""
    kind = (request.query_params.get("export") or "").lower()
    if kind and kind not in ("csv", "pdf"):
        raise ValidationError({"export": "Use csv or pdf."})
    return kind


class DayView(APIView):
    """GET /api/staff-attendance/day/?date=YYYY-MM-DD[&department=ID for the admin]: every teacher with that day's mark."""

    permission_classes = [IsAdminOrHOD]

    def get(self, request):
        """Return every teacher of the department with their mark for the chosen day, plus counts."""
        day = services.parse_day(request.query_params.get("date"))
        rows, summary = services.day_sheet(request.user, day, request.query_params.get("department"))
        return Response({"date": day.isoformat(), "late_after": services.settings.STAFF_LATE_AFTER, "summary": summary, "results": rows})


class MarkView(APIView):
    """POST /api/staff-attendance/mark/: the HOD records (or corrects) one teacher's day. status AUTO = Present or Late by the clock."""

    permission_classes = [IsHOD]

    @transaction.atomic
    def post(self, request):
        """Create or correct one teacher's mark for a day (today or earlier) and write the audit log."""
        data = MarkSerializer(data=request.data)
        data.is_valid(raise_exception=True)
        v = data.validated_data
        day = v.get("date") or timezone.localdate()
        if day > timezone.localdate():
            raise ValidationError({"date": "Attendance cannot be marked for a future date."})
        try:  # the teacher must be in the HOD's own department
            staff = services.department_staff(request.user).get(pk=v["user"])
        except User.DoesNotExist:
            raise NotFound("No such teacher in your department.")
        # AUTO means Present or Late, decided by the time now
        status = services.present_or_late() if v["status"] == "AUTO" else v["status"]
        source = StaffAttendance.Source.SCAN if v["scan"] else StaffAttendance.Source.HOD
        # lock the day's row so two marks at the same moment cannot clash
        record = StaffAttendance.objects.select_for_update().filter(user=staff, date=day).first()
        old = record.status if record else None
        if record is None:
            record = StaffAttendance(user=staff, date=day)
        elif v["scan"] and record.status in (ST.PRESENT, ST.LATE, ST.HALF_DAY):
            # scanning a card twice (or after the teacher checked in) never downgrades or rewrites the mark
            return Response({**services.row_for(staff, record), "unchanged": True})
        record.status, record.source, record.marked_by = status, source, request.user
        record.note = v.get("note", record.note if old else "")  # keep the old note when no new note is sent
        if status in (ST.PRESENT, ST.LATE) and not record.check_in_at:  # the check-in time is only kept for today
            record.check_in_at = timezone.now() if day == timezone.localdate() else None
        record.save()
        log_action(request.user, "STAFF_ATTENDANCE_MARKED", record, department=staff.department,
                   old={"status": old} if old else None, new={"status": status, "date": str(day), "teacher": staff.username},
                   reason=record.note)
        return Response({**services.row_for(staff, record), "unchanged": False})


class CheckInView(APIView):
    """POST /api/staff-attendance/check-in/: a teacher marks themselves present for today (Late after the cut-off time)."""

    permission_classes = [IsStaffTeacher]

    @transaction.atomic
    def post(self, request):
        """A teacher marks themselves present today (Late after the cut-off). A second check-in changes nothing."""
        day = timezone.localdate()
        record = StaffAttendance.objects.select_for_update().filter(user=request.user, date=day).first()
        if record is not None:  # already marked today (by self, HOD, scan or a lecture): do not overwrite
            return Response({**services.row_for(request.user, record), "already": True})
        record = StaffAttendance.objects.create(
            user=request.user, date=day, status=services.present_or_late(), source=StaffAttendance.Source.SELF,
            check_in_at=timezone.now(), marked_by=request.user,
        )
        log_action(request.user, "STAFF_CHECK_IN", record, department=request.user.department,
                   new={"status": record.status, "date": str(day)})
        return Response({**services.row_for(request.user, record), "already": False}, status=http.HTTP_201_CREATED)


class MineView(APIView):
    """GET /api/staff-attendance/me/?month=YYYY-MM: my own days (and today's mark)."""

    permission_classes = [IsStaffTeacher]

    def get(self, request):
        """The teacher's own marks for a month (newest first) and the mark for today."""
        first, last = services.parse_month(request.query_params.get("month"))
        records = StaffAttendance.objects.filter(user=request.user, date__gte=first, date__lte=last).select_related("marked_by").order_by("-date")
        today = StaffAttendance.objects.filter(user=request.user, date=timezone.localdate()).select_related("marked_by").first()
        results = [
            {"date": r.date.isoformat(), "status": r.status, "source": r.source, "note": r.note,
             "check_in_at": r.check_in_at.isoformat() if r.check_in_at else None}
            for r in records
        ]
        return Response({
            "month": first.strftime("%Y-%m"), "month_label": first.strftime("%B %Y"), "late_after": services.settings.STAFF_LATE_AFTER,
            "today": services.row_for(request.user, today), "results": results,
        })


class MonthlyView(APIView):
    """GET /api/staff-attendance/monthly/?month=YYYY-MM[&department=ID][&export=csv|pdf]"""

    permission_classes = [IsAdminOrHOD]

    def get(self, request):
        """The month grid of staff attendance as JSON, or as a CSV / PDF file when ?export= is given."""
        sheet = services.monthly_sheet(request.user, request.query_params.get("month"), request.query_params.get("department"))
        kind = export_kind(request)
        if not kind:
            return Response(sheet)
        days = sheet["days"]
        if kind == "csv":
            headers = ["Staff ID", "Name"] + [f"{d:02d}" for d in days] + ["Present", "Late", "Absent", "Leave"]
            rows = [[r["employee_id"], r["name"]] + [r["marks"].get(str(d), "") for d in days] + [r["present"], r["late"], r["absent"], r["leave"]] for r in sheet["results"]]
            return exporters.export_csv(f'staff-attendance-{sheet["month"]}', headers, rows)
        return exporters.export_sheet_pdf(
            f'staff-attendance-{sheet["month"]}', f'Staff Attendance: {sheet["month_label"]}',
            "P present, L late, H half day, A absent, V on leave (blank = not recorded)",
            [(f"{d:02d}", w) for d, w in zip(days, sheet["weekdays"])],
            [(r["employee_id"], r["name"], [r["marks"].get(str(d), "") for d in days], [r["present"], r["late"], r["absent"]]) for r in sheet["results"]],
            tail_headers=("P", "L", "A"),
        )


class OnLeaveView(APIView):
    """GET /api/staff-attendance/on-leave/?date=YYYY-MM-DD: teachers marked On leave that day (the admin: whole college, the HOD: own department)."""

    permission_classes = [IsAdminOrHOD]

    def get(self, request):
        """List teachers marked On leave on a day, with the dates and reason of their approved leave request."""
        day = services.parse_day(request.query_params.get("date"))
        qs = StaffAttendance.objects.filter(date=day, status=StaffAttendance.Status.LEAVE, user__is_active=True, user__role=User.Role.STAFF)
        if request.user.role == User.Role.HOD:  # an HOD only sees their own department; the Admin sees all
            qs = qs.filter(user__department_id=request.user.department_id)
        qs = qs.select_related("user", "user__profile", "user__department").order_by("user__department__name", "user__first_name", "user__id")
        rows = list(qs)
        leaves = {}  # teacher id -> the approved leave request that covers this day
        for lr in LeaveRequest.objects.filter(status=LeaveRequest.Status.APPROVED, from_date__lte=day, to_date__gte=day, user__in=[r.user_id for r in rows]):
            leaves.setdefault(lr.user_id, lr)
        out = []
        for r in rows:
            u, lr = r.user, leaves.get(r.user_id)
            profile = getattr(u, "profile", None)
            out.append({
                "user": u.id, "name": u.get_full_name() or u.username, "employee_id": profile.employee_id if profile else "",
                "department": u.department.name if u.department else "",
                "from_date": lr.from_date if lr else None, "to_date": lr.to_date if lr else None,
                "reason": lr.reason if lr else "", "note": r.note,
            })
        return Response({"date": day.isoformat(), "count": len(out), "results": out})
