"""
"Save my data" downloads.
  students : admin = every student, HOD = the department's, teacher = the classes he teaches   (CSV or PDF)
  staff    : admin = every teacher, HOD = the department's; teachers may not                    (CSV or PDF)
  hods     : admin only                                                                          (CSV or PDF)
  all      : admin only, one ZIP with everything (people, classes, subjects, lectures, every attendance mark, staff days, leave)
Every download is written to the audit log (who took which data and how many rows).
"""
import csv
import tempfile
import zipfile
from io import StringIO

from django.http import FileResponse
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from academics.models import ClassRoom, Subject, TeacherSubjectAssignment
from accounts.models import User
from attendance.models import AttendanceRecord, Lecture
from core.audit import log_action
from core.scoping import department_scope
from departments.models import Department
from staffattendance.models import LeaveRequest, StaffAttendance
from core.ordering import roll_order
from students.models import Student

from . import exporters

# the people lists that can be downloaded: key -> (title, column headings)
PEOPLE = {
    "students": ("Students", ["Department", "Class", "Roll No", "Name", "ID card number", "Status"]),
    "staff": ("Teachers", ["Department", "Staff ID", "Name", "Username", "Designation", "Employment", "Email", "Phone", "ID card number", "Status"]),
    "hods": ("Heads of department", ["Department", "Name", "Username", "Email", "Phone", "ID card number", "Status"]),
}


def _status(active):
    """Show a true/false is_active flag as the word Active or Inactive."""
    return "Active" if active else "Inactive"


def student_rows(user, params):
    """Rows for the students list. Department scoping and, for a teacher, only the classes they teach."""
    qs = Student.objects.select_related("classroom", "department").defer("photo")
    qs = department_scope(qs, user)
    if user.role == User.Role.STAFF:
        mine = TeacherSubjectAssignment.objects.filter(teacher=user, is_active=True).values("classroom_id")
        qs = qs.filter(classroom_id__in=mine)
    if user.role == User.Role.ADMIN and str(params.get("department", "")).isdigit():
        qs = qs.filter(department_id=params["department"])
    if str(params.get("classroom", "")).isdigit():
        qs = qs.filter(classroom_id=params["classroom"])
    if params.get("status") in ("active", "inactive"):
        qs = qs.filter(is_active=params["status"] == "active")
    qs = qs.order_by("department__name", "classroom_id", *roll_order())
    return [[s.department.name, s.classroom.label, s.roll_number, s.name, s.card_code, _status(s.is_active)] for s in qs]


def account_rows(user, role, params):
    """Rows for the staff list or the HOD list (role decides which). An HOD only gets their own department."""
    qs = User.objects.filter(role=role).select_related("department", "profile").order_by("department__name", "first_name", "last_name", "id")
    if user.role == User.Role.HOD:
        qs = qs.filter(department_id=user.department_id)
    elif str(params.get("department", "")).isdigit():
        qs = qs.filter(department_id=params["department"])
    if params.get("status") in ("active", "inactive"):
        qs = qs.filter(is_active=params["status"] == "active")
    rows = []
    for u in qs:
        p = getattr(u, "profile", None)  # the profile may be missing, so every profile field is checked
        name = u.get_full_name() or u.username
        dept = u.department.name if u.department else ""
        if role == User.Role.STAFF:
            rows.append([dept, p.employee_id if p else "", name, u.username, p.designation if p else "", p.get_employment_display() if p else "", u.email, u.phone,
                         p.card_code if p else "", _status(u.is_active)])
        else:
            rows.append([dept, name, u.username, u.email, u.phone, p.card_code if p else "", _status(u.is_active)])
    return rows


class PeopleDownloadView(APIView):
    """GET /api/downloads/<students|staff|hods>/?export=csv|pdf[&department=][&classroom=][&status=active|inactive]"""

    permission_classes = [IsAuthenticated]

    def get(self, request, what):
        """Check the role may download this list, build the rows, write the audit log and send the file."""
        user = request.user
        if what not in PEOPLE:
            raise ValidationError({"detail": "Unknown list."})
        if what == "hods" and user.role != User.Role.ADMIN:
            raise PermissionDenied("Only the college admin can download the list of heads of department.")
        if what == "staff" and user.role not in (User.Role.ADMIN, User.Role.HOD):
            raise PermissionDenied("Only the HOD or the college admin can download the staff list.")
        kind = (request.query_params.get("export") or "csv").lower()
        if kind not in ("csv", "xlsx", "pdf"):
            raise ValidationError({"export": "Use csv, xlsx or pdf."})
        title, headers = PEOPLE[what]
        if what == "students":
            rows = student_rows(user, request.query_params)
        else:
            rows = account_rows(user, User.Role.STAFF if what == "staff" else User.Role.HOD, request.query_params)
        scope = "All departments" if user.role == User.Role.ADMIN else (user.department.name if user.department else "")
        log_action(user, "DATA_DOWNLOADED", user, department=user.department, new={"list": what, "format": kind, "rows": len(rows)})
        stamp = timezone.localdate().isoformat()
        return exporters.export_response(kind, f"{what}-{stamp}", f"{title} ({len(rows)})", f"{scope}  |  {stamp}", headers, rows)


# column titles of the leave download (same order as the rows in leave_rows)
LEAVE_HEADERS = ["Department", "Teacher", "Staff ID", "Type of leave", "From", "To", "Working days", "Reason", "Status", "Reviewed by", "Remarks", "Sent on"]


def leave_rows(user, params):
    """Rows of leave requests: Admin all, HOD their department, a teacher only their own."""
    from staffattendance.leave import leave_days

    qs = LeaveRequest.objects.select_related("user__department", "user__profile", "reviewed_by").order_by("-from_date", "-id")
    qs = department_scope(qs, user, "user__department_id")
    if user.role == User.Role.STAFF:
        qs = qs.filter(user=user)
    if params.get("status") in LeaveRequest.Status.values:
        qs = qs.filter(status=params["status"])
    if params.get("leave_type") in LeaveRequest.Kind.values:
        qs = qs.filter(leave_type=params["leave_type"])
    if str(params.get("year", "")).isdigit():
        qs = qs.filter(from_date__year=int(params["year"]))
    rows = []
    for l in qs:
        p = getattr(l.user, "profile", None)
        rows.append([l.user.department.name if l.user.department else "", l.user.get_full_name() or l.user.username, p.employee_id if p else "",
                     l.get_leave_type_display(), l.from_date, l.to_date, len(leave_days(l)), l.reason, l.get_status_display(),
                     (l.reviewed_by.get_full_name() or l.reviewed_by.username) if l.reviewed_by else "", l.remarks,
                     timezone.localtime(l.created_at).strftime("%Y-%m-%d %H:%M")])
    return rows


class LeaveDownloadView(APIView):
    """GET /api/downloads/leave/?export=csv|pdf[&status=][&leave_type=][&year=]: admin = all, HOD = department, teacher = own."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Send the leave list as CSV or PDF and record the download in the audit log."""
        kind = (request.query_params.get("export") or "csv").lower()
        if kind not in ("csv", "xlsx", "pdf"):
            raise ValidationError({"export": "Use csv, xlsx or pdf."})
        rows = leave_rows(request.user, request.query_params)
        scope = {User.Role.ADMIN: "All departments", User.Role.HOD: request.user.department.name if request.user.department else ""}.get(request.user.role, "My leave")
        log_action(request.user, "DATA_DOWNLOADED", request.user, department=request.user.department, new={"list": "leave", "format": kind, "rows": len(rows)})
        stamp = timezone.localdate().isoformat()
        return exporters.export_response(kind, f"leave-{stamp}", f"Leave requests ({len(rows)})", f"{scope}  |  {stamp}", LEAVE_HEADERS, rows)


def _csv_text(headers, rows):
    """Make CSV text in memory (cells are made formula-safe first)."""
    buf = StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    for r in rows:
        w.writerow([exporters._csv_safe(c) for c in r])
    return buf.getvalue()


class EverythingDownloadView(APIView):
    """GET /api/downloads/all/ : the college admin's own copy of all the data, as one ZIP of CSV files (opens in Excel)."""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        """Admin only: put every table into one ZIP of CSV files, log it and send it."""
        user = request.user
        if user.role != User.Role.ADMIN:
            raise PermissionDenied("Only the college admin can download all the data.")
        tmp = tempfile.TemporaryFile()  # the ZIP is built in a temporary file, not in memory
        counts = {}  # file name -> number of rows, written to the audit log at the end
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            def add(name, headers, rows):
                """Add one CSV file to the ZIP (a byte-order mark first, so Excel reads UTF-8 names)."""
                rows = list(rows)
                counts[name] = len(rows)
                z.writestr(f"{name}.csv", "﻿" + _csv_text(headers, rows))

            add("departments", ["Department", "Code", "Status"], [[d.name, d.code, _status(d.is_active)] for d in Department.objects.order_by("name")])
            add("hods", PEOPLE["hods"][1], account_rows(user, User.Role.HOD, {}))
            add("staff", PEOPLE["staff"][1], account_rows(user, User.Role.STAFF, {}))
            add("students", PEOPLE["students"][1], student_rows(user, {}))
            add("classes", ["Department", "Class", "Semester", "Division", "Academic year", "Status"],
                [[c.department.name, c.name, c.semester, c.division, c.academic_year, _status(c.is_active)]
                 for c in ClassRoom.objects.select_related("department").order_by("department__name", "name", "division")])
            add("subjects", ["Department", "Subject", "Code", "Semester", "Status"],
                [[s.department.name, s.name, s.code, s.semester or "", _status(s.is_active)]
                 for s in Subject.objects.select_related("department").order_by("department__name", "name")])
            add("assignments", ["Department", "Teacher", "Subject", "Class", "Academic year", "Status"],
                [[a.classroom.department.name, a.teacher.get_full_name() or a.teacher.username, a.subject.name, a.classroom.label,
                  a.academic_year, _status(a.is_active)]
                 for a in TeacherSubjectAssignment.objects.select_related("teacher", "subject", "classroom__department")])
            add("lectures", ["Date", "Start", "End", "Department", "Class", "Subject", "Teacher", "Status", "Present", "Absent"],
                [[l.lecture_date, l.start_time.strftime("%H:%M"), l.end_time.strftime("%H:%M"),
                  l.teacher_subject_assignment.classroom.department.name, l.teacher_subject_assignment.classroom.label,
                  l.teacher_subject_assignment.subject.name,
                  l.teacher_subject_assignment.teacher.get_full_name() or l.teacher_subject_assignment.teacher.username,
                  l.status, l.present_total, l.absent_total]
                 for l in Lecture.objects.select_related("teacher_subject_assignment__classroom__department", "teacher_subject_assignment__subject",
                                                         "teacher_subject_assignment__teacher").order_by("lecture_date", "start_time", "id").iterator(chunk_size=2000)])
            # every single mark, streamed so a big college does not fill the memory
            records = (AttendanceRecord.objects.filter(lecture__status=Lecture.Status.SUBMITTED)
                       .values_list("lecture__lecture_date", "lecture__start_time", "lecture__teacher_subject_assignment__classroom__department__name",
                                    "lecture__teacher_subject_assignment__classroom__name", "lecture__teacher_subject_assignment__subject__name",
                                    "student__roll_number", "student__name", "status")
                       .order_by("lecture__lecture_date", "lecture__start_time", "lecture_id", "student_id"))
            n = 0  # rows written so far
            with z.open("attendance_marks.csv", "w") as f:
                f.write("﻿".encode("utf-8"))
                head = StringIO()
                csv.writer(head).writerow(["Date", "Start", "Department", "Class", "Subject", "Roll No", "Student", "Mark"])
                f.write(head.getvalue().encode("utf-8"))
                buf = StringIO()
                w = csv.writer(buf)
                for row in records.iterator(chunk_size=5000):
                    w.writerow([exporters._csv_safe(c) for c in (row[0], row[1].strftime("%H:%M"), *row[2:])])
                    n += 1
                    if n % 5000 == 0:  # write to the file every 5000 rows and empty the buffer
                        f.write(buf.getvalue().encode("utf-8"))
                        buf.seek(0), buf.truncate()
                f.write(buf.getvalue().encode("utf-8"))
            counts["attendance_marks"] = n
            add("staff_attendance", ["Date", "Department", "Teacher", "Status", "Source", "Note"],
                [[a.date, a.user.department.name if a.user.department else "", a.user.get_full_name() or a.user.username, a.status, a.source, a.note]
                 for a in StaffAttendance.objects.select_related("user__department").order_by("date", "user__first_name")])
            add("leave_requests", LEAVE_HEADERS, leave_rows(user, {}))
            z.writestr("README.txt", "College attendance data, saved on %s.\r\nEvery file opens in Excel (UTF-8, Marathi names included).\r\nIt contains personal data: keep it safe.\r\n" % timezone.localtime().strftime("%d %b %Y %I:%M %p"))
        tmp.seek(0)
        log_action(user, "DATA_DOWNLOADED", user, new={"list": "everything", "format": "zip", "rows": counts})
        return FileResponse(tmp, as_attachment=True, filename=f"college-data-{timezone.localdate().isoformat()}.zip", content_type="application/zip")
