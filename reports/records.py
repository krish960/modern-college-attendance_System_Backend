"""
Student records for the whole college (admin) or a department (HOD): attendance per student for a month, a year or any
dates, and one student's year month by month. Read-only, calculated from submitted lectures only.
"""
import calendar
from datetime import date

from django.conf import settings
from django.db.models import Count, Q
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from accounts.models import User
from accounts.permissions import IsAdminOrHOD
from attendance.models import AttendanceRecord, Lecture
from core.audit import log_action
from core.ordering import roll_order
from core.scoping import department_scope
from students.models import Student

from . import exporters
from .services import parse_month, percentage

# the most students one downloaded file may contain (a PDF is smaller because it is slow to draw)
MAX_CSV_ROWS = 50000
MAX_PDF_ROWS = 1500


def _period(params):
    """-> (first day, last day, label). period=month&month=YYYY-MM | year&year=YYYY | range&date_from&date_to."""
    kind = params.get("period") or "month"
    if kind == "month":
        first, last = parse_month(params.get("month"))
        return first, last, first.strftime("%B %Y")
    if kind == "year":
        year = params.get("year") or str(date.today().year)
        if not (str(year).isdigit() and 2000 <= int(year) <= 2100):
            raise ValidationError({"year": "Use a year like 2026."})
        year = int(year)
        return date(year, 1, 1), date(year, 12, 31), f"Year {year}"
    if kind == "range":
        try:
            first, last = date.fromisoformat(params.get("date_from", "")), date.fromisoformat(params.get("date_to", ""))
        except ValueError:
            raise ValidationError({"date_from": "Choose both dates (YYYY-MM-DD)."})
        if last < first:
            raise ValidationError({"date_to": "The last date cannot be before the first."})
        return first, last, f"{first:%d %b %Y} to {last:%d %b %Y}"
    raise ValidationError({"period": "Use month, year or range."})


def build_rows(user, params):
    """One row per student with present / absent / percentage for the chosen period. Returns (rows, label, first, last)."""
    first, last, label = _period(params)
    records = AttendanceRecord.objects.filter(lecture__status=Lecture.Status.SUBMITTED, lecture__lecture_date__range=(first, last))
    records = department_scope(records, user, "student__department_id")  # an HOD only sees their own department
    students = department_scope(Student.objects.defer("photo").select_related("classroom", "department"), user)
    if user.role == User.Role.ADMIN and str(params.get("department", "")).isdigit():
        records = records.filter(student__department_id=params["department"])
        students = students.filter(department_id=params["department"])
    if str(params.get("classroom", "")).isdigit():
        records = records.filter(student__classroom_id=params["classroom"])
        students = students.filter(classroom_id=params["classroom"])
    if str(params.get("subject", "")).isdigit():
        records = records.filter(lecture__teacher_subject_assignment__subject_id=params["subject"])
    # present and absent counts per student id, from submitted lectures only
    totals = {
        r["student"]: r
        for r in records.values("student").annotate(present=Count("id", filter=Q(status="PRESENT")), absent=Count("id", filter=Q(status="ABSENT")))
    }
    search = (params.get("search") or "").strip()
    if search:
        students = students.filter(Q(name__icontains=search) | Q(roll_number__icontains=search))
    # active students, plus anyone who has marks in the period (left or moved later: the old record stays)
    students = students.filter(Q(is_active=True) | Q(id__in=list(totals))).order_by("department__name", "classroom__name", "classroom_id", *roll_order())
    low_only = str(params.get("low_only", "")).lower() in ("1", "true", "yes")
    rows = []
    for st in students:
        t = totals.get(st.id, {"present": 0, "absent": 0})
        total = t["present"] + t["absent"]
        pct = percentage(t["present"], total)
        low = pct is not None and pct < settings.LOW_ATTENDANCE_THRESHOLD  # below the college limit
        if low_only and not low:
            continue
        rows.append({
            "student": st.id, "roll_number": st.roll_number, "name": st.name, "is_active": st.is_active,
            "department": st.department.name, "class_name": st.classroom.label,
            "present": t["present"], "absent": t["absent"], "total": total, "percentage": pct, "low": low,
        })
    return rows, label, first, last


class StudentRecordsView(APIView):
    """GET /api/reports/student-records/?period=month|year|range ... [&export=csv|pdf]  (admin: all, HOD: own department)"""

    permission_classes = [IsAdminOrHOD]

    def get(self, request):
        """Return one page of the student records as JSON, or the whole list as a CSV / PDF file."""
        rows, label, first, last = build_rows(request.user, request.query_params)
        kind = (request.query_params.get("export") or "").lower()
        if kind:
            if kind not in ("csv", "pdf"):
                raise ValidationError({"export": "Use csv or pdf."})
            # refuse a file that is too big instead of making the server slow
            cap = MAX_PDF_ROWS if kind == "pdf" else MAX_CSV_ROWS
            if len(rows) > cap:
                raise ValidationError(f"{len(rows)} students: too many for a {kind.upper()}. Choose a department or class" + (" or use Excel." if kind == "pdf" else "."))
            log_action(request.user, "DATA_DOWNLOADED", request.user, department=request.user.department,
                       new={"list": "student-records", "format": kind, "rows": len(rows), "period": label})
            scope = "All departments" if request.user.role == User.Role.ADMIN else request.user.department.name
            return exporters.export_response(
                kind, f"student-records-{first:%Y%m%d}-{last:%Y%m%d}", f"Student attendance records: {label}", f"{scope}  |  {len(rows)} students",
                ["Department", "Class", "Roll No", "Name", "Present", "Absent", "Total", "Attendance %"],
                [[r["department"], r["class_name"], r["roll_number"], r["name"] + ("" if r["is_active"] else " (inactive)"), r["present"], r["absent"],
                  r["total"], "" if r["percentage"] is None else f'{r["percentage"]}%'] for r in rows],
            )
        try:  # simple paging: page size is kept between 1 and 200
            page = max(1, int(request.query_params.get("page", 1)))
            size = min(200, max(1, int(request.query_params.get("page_size", 25))))
        except ValueError:
            raise ValidationError("page and page_size must be numbers.")
        present = sum(r["present"] for r in rows)
        total = sum(r["total"] for r in rows)
        return Response({
            "period": label, "from": first, "to": last, "threshold": settings.LOW_ATTENDANCE_THRESHOLD,
            "count": len(rows), "page": page, "page_size": size,
            "summary": {"students": len(rows), "present": present, "total": total, "percentage": percentage(present, total),
                        "low": sum(1 for r in rows if r["low"]), "no_record": sum(1 for r in rows if r["total"] == 0)},
            "results": rows[(page - 1) * size: page * size],
        })


class StudentYearView(APIView):
    """GET /api/reports/student-records/<student id>/?year=YYYY : one student's year, month by month and subject by subject."""

    permission_classes = [IsAdminOrHOD]

    def get(self, request, pk):
        """Return one student's year: present/absent for each month and for each subject."""
        student = department_scope(Student.objects.defer("photo").select_related("classroom", "department"), request.user).filter(pk=pk).first()
        if student is None:
            raise NotFound()
        year = request.query_params.get("year") or str(date.today().year)
        if not (str(year).isdigit() and 2000 <= int(year) <= 2100):
            raise ValidationError({"year": "Use a year like 2026."})
        year = int(year)
        records = AttendanceRecord.objects.filter(student=student, lecture__status=Lecture.Status.SUBMITTED, lecture__lecture_date__year=year)
        by_month = {}  # month number -> [present count, absent count]
        for d, status in records.values_list("lecture__lecture_date", "status"):
            row = by_month.setdefault(d.month, [0, 0])
            row[0 if status == "PRESENT" else 1] += 1
        months = []
        for m in range(1, 13):
            p, a = by_month.get(m, [0, 0])
            months.append({"month": f"{year}-{m:02d}", "label": calendar.month_name[m], "present": p, "absent": a, "total": p + a, "percentage": percentage(p, p + a)})
        subj = "lecture__teacher_subject_assignment__subject__name"
        subjects = [
            {"subject": r[subj], "present": r["present"], "total": r["present"] + r["absent"], "percentage": percentage(r["present"], r["present"] + r["absent"])}
            for r in records.values(subj).annotate(present=Count("id", filter=Q(status="PRESENT")), absent=Count("id", filter=Q(status="ABSENT"))).order_by(subj)
        ]
        p = sum(m["present"] for m in months)
        t = sum(m["total"] for m in months)
        return Response({
            "student": {"id": student.id, "name": student.name, "roll_number": student.roll_number, "class_name": student.classroom.label,
                        "department": student.department.name, "is_active": student.is_active},
            "year": year, "months": months, "subjects": subjects,
            "total": {"present": p, "absent": t - p, "total": t, "percentage": percentage(p, t)},
        })
