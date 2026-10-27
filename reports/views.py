"""API views for reports: the daily report, student-wise summary, monthly register sheet and the dashboards.

The views here only check who is asking and read the request; the real calculations are in services.py and the
CSV / PDF files are made by exporters.py."""
from django.conf import settings
from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from academics.models import ClassRoom, Subject, TeacherSubjectAssignment
from accounts.models import User
from accounts.permissions import IsAdminOrHOD
from attendance.models import AttendanceCorrectionRequest, AttendanceRecord, Lecture
from attendance.serializers import LectureSerializer
from attendance.services import dead_draft_q, pending_q
from core.aggregates import count_by
from core.scoping import department_scope
from departments.models import Department
from departments.serializers import hod_summary
from staffattendance.models import LeaveRequest, StaffAttendance
from students.models import Student
from students.serializers import can_access_classroom

from . import exporters, services

# database path from a Lecture to its department, used for department scoping
LECTURE_DEPT = "teacher_subject_assignment__classroom__department_id"


def export_kind(request, allowed=("csv", "pdf")):
    """Read ?export= from the URL: returns one of `allowed` or "" (no export). Anything else is an error."""
    kind = (request.query_params.get("export") or "").lower()
    if kind and kind not in allowed:
        raise ValidationError({"export": "Use " + " or ".join(allowed) + "."})
    return kind


# Reports that can also be downloaded as a real Excel file.
WITH_EXCEL = ("csv", "xlsx", "pdf")


class DailyReportView(APIView):
    """
    GET /api/reports/daily/?date=YYYY-MM-DD  (or date_from & date_to)
    Filters: subject, teacher, classroom, semester, status, department (admin only).
    Add &export=csv, xlsx or pdf to download.
    """

    permission_classes = [IsAdminOrHOD]

    def get(self, request):
        """Return the report as JSON, or as a CSV / PDF file when ?export= is given."""
        kind = export_kind(request, WITH_EXCEL)
        rows, summary = services.daily_report(request.user, request.query_params, kind or "json")
        if kind:
            subtitle = f'{summary["date_from"]} to {summary["date_to"]}  |  Lectures: {summary["lectures"]}, ' \
                       f'Submitted: {summary["submitted"]}, Pending: {summary["pending"]}'
            return exporters.export_response(
                kind, f'attendance-report-{summary["date_from"]}-to-{summary["date_to"]}', "Daily Attendance Report",
                subtitle, services.DAILY_HEADERS, services.daily_rows_for_export(rows),
            )
        return Response({"summary": summary, "results": rows})


class StudentSummaryView(APIView):
    """GET /api/reports/student-summary/?classroom=ID[&subject=ID&date_from=&date_to=][&export=csv|pdf]"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        class_id = request.query_params.get("classroom")
        if not class_id or not class_id.isdigit():
            raise ValidationError({"classroom": "This field is required."})
        classroom = get_object_or_404(ClassRoom, pk=class_id)
        user = request.user
        # the Admin may see any class; HOD and teacher only classes they are allowed to access
        allowed = user.role == User.Role.ADMIN or can_access_classroom(user, classroom)
        if not allowed:
            raise NotFound()  # 404 instead of 403, so the existence of other classes is not revealed
        rows = services.student_summary(user, classroom, request.query_params)
        kind = export_kind(request, WITH_EXCEL)
        if kind:
            return exporters.export_response(
                kind, f"student-attendance-{classroom.pk}", "Student-wise Attendance", classroom.label,
                ["Roll No", "Name", "Present", "Absent", "Total", "Attendance %"],
                [[r["roll_number"], r["name"], r["present"], r["absent"], r["total"],
                  "" if r["percentage"] is None else f'{r["percentage"]}%'] for r in rows],
            )
        return Response({"classroom": classroom.label, "threshold": settings.LOW_ATTENDANCE_THRESHOLD, "results": rows})


class MonthlySheetView(APIView):
    """GET /api/reports/monthly-sheet/?classroom=ID&month=YYYY-MM[&subject=ID][&export=csv|pdf]"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        class_id = request.query_params.get("classroom")
        if not class_id or not class_id.isdigit():
            raise ValidationError({"classroom": "This field is required."})
        classroom = get_object_or_404(ClassRoom, pk=class_id)
        user = request.user
        # same access rule as the student summary: Admin sees all, others only their own classes
        if not (user.role == User.Role.ADMIN or can_access_classroom(user, classroom)):
            raise NotFound()
        sheet = services.monthly_sheet(user, classroom, request.query_params)
        kind = export_kind(request, WITH_EXCEL)
        if kind:
            headers, rows = services.monthly_sheet_export(sheet)
            if kind == "pdf":  # the PDF is drawn as a register page; the CSV is a plain table
                title = f'Monthly Attendance Sheet: {sheet["month_label"]}'
                subtitle = f"{classroom.label}  |  Lectures: {len(sheet['columns'])}  |  P = present, A = absent (shown in red)"
                return exporters.export_sheet_pdf(
                    f'monthly-sheet-{classroom.pk}-{sheet["month"]}', title, subtitle,
                    [(f'{c["day"]:02d}', c["subject_code"]) for c in sheet["columns"]],
                    [(r["roll_number"], r["name"], [r["marks"].get(str(c["id"]), "") for c in sheet["columns"]],
                      [r["present"], r["total"], "" if r["percentage"] is None else f'{r["percentage"]}%']) for r in sheet["results"]],
                )
            return exporters.export_response(
                kind, f'monthly-sheet-{classroom.pk}-{sheet["month"]}', f'Monthly Attendance Sheet: {sheet["month_label"]}',
                classroom.label, headers, rows,
            )
        sheet["classroom"] = classroom.label
        sheet["threshold"] = settings.LOW_ATTENDANCE_THRESHOLD
        return Response(sheet)


class DashboardView(APIView):
    """Role-specific statistics, all calculated live from the database."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Pick the dashboard that matches the role of the person asking."""
        user = request.user
        if user.role == User.Role.ADMIN:
            return Response(self.admin_stats())
        if user.role == User.Role.HOD:
            return Response(self.hod_stats(user))
        return Response(self.staff_stats(user))

    # --- College admin ------------------------------------------------------
    def admin_stats(self):
        """Whole-college numbers: the top cards, one row per department and the trend chart."""
        today = timezone.localdate()
        # Stored per-lecture totals are added up, so this stays fast with millions of attendance rows.
        submitted = Lecture.objects.filter(status=Lecture.Status.SUBMITTED)
        lectures_today = Lecture.objects.filter(lecture_date=today).exclude(dead_draft_q())
        per_dept = {
            row[LECTURE_DEPT]: row
            for row in submitted.values(LECTURE_DEPT).annotate(present=Sum("present_total"), absent=Sum("absent_total"))
        }
        # pending = draft lectures of today that the teacher can still submit
        pending_today = dict(lectures_today.filter(pending_q()).values_list(LECTURE_DEPT).annotate(c=Count("id")))
        hods = {
            u.department_id: u.get_full_name() or u.username
            for u in User.objects.filter(role=User.Role.HOD, is_active=True)
        }
        departments = Department.objects.annotate(
            staff_count=count_by(User.objects.filter(role=User.Role.STAFF, is_active=True), "department"),
            student_count=count_by(Student.objects.filter(is_active=True), "department"),
        ).order_by("name")
        table = []  # one row per department for the admin table
        for dept in departments:
            stat = per_dept.get(dept.id, {"present": 0, "absent": 0})
            table.append({
                "id": dept.id, "name": dept.name, "code": dept.code, "is_active": dept.is_active,
                "hod": hods.get(dept.id),
                "staff_count": dept.staff_count, "student_count": dept.student_count,
                "attendance_percentage": services.percentage(stat["present"], stat["present"] + stat["absent"]),
                "pending_today": pending_today.get(dept.id, 0),
            })
        totals = submitted.aggregate(present=Sum("present_total"), absent=Sum("absent_total"))
        present, absent = totals["present"] or 0, totals["absent"] or 0
        return {
            "role": "ADMIN",
            "cards": {
                "departments": Department.objects.count(),
                "active_departments": Department.objects.filter(is_active=True).count(),
                "active_hods": User.objects.filter(role=User.Role.HOD, is_active=True).count(),
                "staff": User.objects.filter(role=User.Role.STAFF, is_active=True).count(),
                "staff_on_leave": StaffAttendance.objects.filter(
                    date=today, status=StaffAttendance.Status.LEAVE, user__is_active=True, user__role=User.Role.STAFF).count(),
                "students": Student.objects.filter(is_active=True).count(),
                "lectures_today": lectures_today.exclude(status=Lecture.Status.CANCELLED).count(),
                "submitted_today": lectures_today.filter(status=Lecture.Status.SUBMITTED).count(),
                "pending_today": lectures_today.filter(pending_q()).count(),
                "overdue_pending": services.overdue_pending_count(Lecture.objects.all(), today),
                "attendance_percentage": services.percentage(present, present + absent),
            },
            "departments": table,
            "trend": services.daily_trend(Lecture.objects.all(), today),
            **self.admin_extras(today),
        }

    def admin_extras(self, today):
        """The extra boxes of the admin page: staff today, leave by type this month, things waiting, low attendance, recent activity."""
        from core.models import AuditLog
        from students.models import ClassPromotionRequest, StudentRemovalRequest

        St = StaffAttendance.Status
        staff_total = User.objects.filter(role=User.Role.STAFF, is_active=True).count()
        counts = dict(StaffAttendance.objects.filter(date=today, user__is_active=True, user__role=User.Role.STAFF)
                      .values_list("status").annotate(c=Count("id")))
        marked = sum(counts.values())
        staff_today = {
            "present": counts.get(St.PRESENT, 0), "late": counts.get(St.LATE, 0), "half_day": counts.get(St.HALF_DAY, 0),
            "absent": counts.get(St.ABSENT, 0), "leave": counts.get(St.LEAVE, 0), "not_marked": max(0, staff_total - marked),
        }
        first = today.replace(day=1)
        kinds = dict(LeaveRequest.Kind.choices)
        # count approved leave that touches this month, per type of leave, and how many different teachers took it
        by_type = {k: {"type": k, "label": v, "requests": 0, "teachers": set()} for k, v in kinds.items()}
        for lr in LeaveRequest.objects.filter(status=LeaveRequest.Status.APPROVED, from_date__lte=today, to_date__gte=first):
            row = by_type[lr.leave_type]
            row["requests"] += 1
            row["teachers"].add(lr.user_id)
        leave_month = [{"type": r["type"], "label": r["label"], "requests": r["requests"], "teachers": len(r["teachers"])}
                       for r in by_type.values() if r["requests"]]
        low = []  # the worst few students of every department, merged into one list
        low_total = 0
        for dept in Department.objects.filter(is_active=True).order_by("name"):
            count, rows = services.low_attendance_students(dept.id, today, limit=3)
            low_total += count
            low += [{**r, "department": dept.name} for r in rows]
        low.sort(key=lambda r: r["percentage"])
        recent = [{"when": a.created_at, "who": (a.actor.get_full_name() or a.actor.username) if a.actor else "System",
                   "action": a.action.replace("_", " ").capitalize(), "department": a.department.name if a.department else ""}
                  for a in AuditLog.objects.select_related("actor", "department").order_by("-created_at", "-id")[:8]]
        return {
            "staff_today": staff_today,
            "leave_month": leave_month,
            "waiting": {
                "leave_requests": LeaveRequest.objects.filter(status__in=LeaveRequest.OPEN).count(),
                "corrections": AttendanceCorrectionRequest.objects.filter(status="PENDING").count(),
                "student_removals": StudentRemovalRequest.objects.filter(status="PENDING").count(),
                "promotions": ClassPromotionRequest.objects.filter(status="WAITING_ADMIN").count(),
            },
            "low_attendance": {"count": low_total, "students": low[:8]},
            "recent_activity": recent,
        }

    # --- HOD ---------------------------------------------------------------
    def hod_stats(self, user):
        """Numbers for one department (the HOD's own): cards, subject-wise attendance, trend and low-attendance students."""
        today = timezone.localdate()
        dept_id = user.department_id
        lectures = Lecture.objects.filter(**{LECTURE_DEPT: dept_id}).exclude(dead_draft_q())
        lectures_today = lectures.filter(lecture_date=today)
        submitted = lectures.filter(status=Lecture.Status.SUBMITTED)
        subject_key = "teacher_subject_assignment__subject__name"
        subject_wise = []
        for row in submitted.values(subject_key).annotate(
                present=Sum("present_total"), absent=Sum("absent_total")).order_by(subject_key):
            total = row["present"] + row["absent"]
            subject_wise.append({
                "subject": row[subject_key], "present": row["present"], "total": total,
                "percentage": services.percentage(row["present"], total),
            })
        totals = submitted.aggregate(present=Sum("present_total"), absent=Sum("absent_total"))
        present, absent = totals["present"] or 0, totals["absent"] or 0
        low_count, low_students = services.low_attendance_students(dept_id, today)
        return {
            "role": "HOD",
            "department": user.department.name,
            "cards": {
                "staff": User.objects.filter(role=User.Role.STAFF, department_id=dept_id, is_active=True).count(),
                "classes": ClassRoom.objects.filter(department_id=dept_id, is_active=True).count(),
                "subjects": Subject.objects.filter(department_id=dept_id, is_active=True).count(),
                "students": Student.objects.filter(department_id=dept_id, is_active=True).count(),
                "lectures_today": lectures_today.exclude(status=Lecture.Status.CANCELLED).count(),
                "submitted_today": lectures_today.filter(status=Lecture.Status.SUBMITTED).count(),
                "pending_today": lectures_today.filter(pending_q()).count(),
                "overdue_pending": services.overdue_pending_count(lectures, today),
                "pending_corrections": AttendanceCorrectionRequest.objects.filter(
                    status="PENDING", **{"record__lecture__" + LECTURE_DEPT: dept_id}).count(),
                "low_attendance_students": low_count,
                "attendance_percentage": services.percentage(present, present + absent),
            },
            "subject_wise": subject_wise,
            "trend": services.daily_trend(lectures, today),
            "low_attendance": {
                "threshold": settings.LOW_ATTENDANCE_THRESHOLD, "days": settings.LOW_ATTENDANCE_DAYS,
                "min_lectures": settings.LOW_ATTENDANCE_MIN_LECTURES, "students": low_students,
            },
        }

    # --- Staff -------------------------------------------------------------
    def staff_stats(self, user):
        """Numbers for one teacher: only their own lectures, assignments and correction requests."""
        today = timezone.localdate()
        mine = Lecture.objects.filter(teacher_subject_assignment__teacher=user)
        todays = mine.filter(lecture_date=today).select_related(
            "teacher_subject_assignment__subject", "teacher_subject_assignment__classroom__department",
            "teacher_subject_assignment__teacher").order_by("start_time")
        assignments = TeacherSubjectAssignment.objects.filter(teacher=user, is_active=True)
        corrections = AttendanceCorrectionRequest.objects.filter(requested_by=user)
        totals = mine.filter(status=Lecture.Status.SUBMITTED).aggregate(
            present=Sum("present_total"), absent=Sum("absent_total"))
        present, absent = totals["present"] or 0, totals["absent"] or 0
        return {
            "role": "STAFF",
            "cards": {
                "assignments": assignments.count(),
                "classes": assignments.values("classroom").distinct().count(),
                "students": Student.objects.filter(
                    classroom__in=assignments.values("classroom"), is_active=True).count(),
                "lectures_today": todays.exclude(status=Lecture.Status.CANCELLED).count(),
                "pending_lectures": mine.filter(
                    status=Lecture.Status.DRAFT, lecture_date__lte=today, teacher_subject_assignment__is_active=True).count(),
                "pending_corrections": corrections.filter(status="PENDING").count(),
                "attendance_percentage": services.percentage(present, present + absent),
            },
            "today": LectureSerializer(todays, many=True).data,
        }
