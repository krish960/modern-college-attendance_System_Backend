"""Report calculations. Everything is computed from the database - nothing is hard coded.

The views in views.py call these functions to build the daily report, the student-wise summary, the monthly
register sheet and the dashboard numbers (trend chart and low-attendance students)."""
from datetime import date, timedelta

from django.conf import settings
from django.db.models import Count, Q, Sum
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from accounts.models import User
from attendance.models import AttendanceRecord, Lecture, SubjectExclusion
from attendance.services import dead_draft_q, pending_q
from core.ordering import roll_order
from core.scoping import department_scope
from students.models import Student

# the longest date range one daily report may cover (about three months)
MAX_RANGE_DAYS = 92
# database path from a Lecture to its department, used to limit lectures to one department
LECTURE_DEPT = "teacher_subject_assignment__classroom__department_id"


def percentage(present, total):
    """Present as a percent of total, rounded to 1 decimal. Returns None (not 0) when there is nothing to divide by."""
    return round(present * 100 / total, 1) if total else None


def parse_date(value, field):
    """Turn a YYYY-MM-DD text into a date. Empty gives None; a bad text raises a ValidationError naming the field."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValidationError({field: "Use the format YYYY-MM-DD."})


def date_range(params):
    """`date` or `date_from`/`date_to`; defaults to today."""
    single = parse_date(params.get("date"), "date")
    if single:
        return single, single
    start = parse_date(params.get("date_from"), "date_from")
    end = parse_date(params.get("date_to"), "date_to")
    if not start and not end:
        start = end = timezone.localdate()
    start = start or end  # only one side given: use it for both sides
    end = end or start
    if end < start:
        raise ValidationError({"date_to": "End date must not be before the start date."})
    # +1 because both the first and the last day are counted
    if (end - start).days + 1 > MAX_RANGE_DAYS:
        raise ValidationError(f"Choose a range of at most {MAX_RANGE_DAYS} days.")
    return start, end


def submission_status(lecture, today):
    """
    SUBMITTED / CANCELLED come straight from the lecture.
    A DRAFT lecture is PENDING once its date has arrived, SCHEDULED if it is still in the future.
    Dates with no lecture at all simply have no rows (nothing is invented as pending).
    """
    if lecture.status == Lecture.Status.SUBMITTED:
        return "SUBMITTED"
    if lecture.status == Lecture.Status.CANCELLED:
        return "CANCELLED"
    return "PENDING" if lecture.lecture_date <= today else "SCHEDULED"


# the most lectures one report may contain, per output type (a PDF is limited the most because it is slow to draw)
MAX_ROWS = {"json": 20000, "csv": 50000, "xlsx": 50000, "pdf": 2000}


def daily_report(user, params, output="json"):
    """Build the daily report: one row per lecture in the date range plus a summary. Returns (rows, summary).

    The HOD sees only their department, the Admin sees all (and may filter by department). `output` decides the row limit."""
    start, end = date_range(params)
    today = timezone.localdate()

    # Submitted lectures carry their own present/absent totals, so no attendance rows are scanned here.
    qs = Lecture.objects.filter(lecture_date__range=(start, end)).exclude(dead_draft_q()).select_related(
        "teacher_subject_assignment__teacher", "teacher_subject_assignment__subject",
        "teacher_subject_assignment__classroom__department",
    )
    qs = department_scope(qs, user, LECTURE_DEPT)  # role scoping: an HOD only gets their own department
    if user.role == User.Role.ADMIN and params.get("department"):
        qs = qs.filter(**{LECTURE_DEPT: params["department"]})
    # optional filters from the query string; each one must be a number (an id) before it is used
    for param, lookup in (
        ("subject", "teacher_subject_assignment__subject"),
        ("teacher", "teacher_subject_assignment__teacher"),
        ("classroom", "teacher_subject_assignment__classroom"),
        ("semester", "teacher_subject_assignment__classroom__semester"),
    ):
        if params.get(param):
            if not str(params[param]).isdigit():
                raise ValidationError({param: "Must be a number."})
            qs = qs.filter(**{lookup: params[param]})
    search = (params.get("search") or "").strip()
    if search:  # free text: matches subject name/code, class name or teacher name
        a = "teacher_subject_assignment__"
        qs = qs.filter(
            Q(**{a + "subject__name__icontains": search}) | Q(**{a + "subject__code__icontains": search})
            | Q(**{a + "classroom__name__icontains": search})
            | Q(**{a + "teacher__first_name__icontains": search}) | Q(**{a + "teacher__last_name__icontains": search})
        )
    qs = qs.order_by("lecture_date", "teacher_subject_assignment__classroom__name", "start_time")

    limit = MAX_ROWS[output]
    lectures = list(qs[: limit + 1])  # fetch one extra row so we can tell the report is too big
    if len(lectures) > limit:
        hint = " Use the Excel or CSV export or" if output == "pdf" else ""
        raise ValidationError(f"This report has more than {limit} lectures.{hint} Narrow the dates or add a filter.")
    # for lectures not yet submitted we show how many students the class has now (active students only)
    class_ids = {lec.teacher_subject_assignment.classroom_id for lec in lectures}
    active_counts = dict(
        Student.objects.filter(classroom_id__in=class_ids, is_active=True)
        .values_list("classroom_id").annotate(c=Count("id"))
    )

    left_out = {}  # (class, subject) -> students of that class who do not take the subject
    for cid, sid in SubjectExclusion.objects.filter(student__is_active=True, student__classroom_id__in=class_ids).values_list("student__classroom_id", "subject_id"):
        left_out[(cid, sid)] = left_out.get((cid, sid), 0) + 1

    wanted_status = (params.get("status") or "").upper()
    rows = []
    for lec in lectures:
        assignment = lec.teacher_subject_assignment
        state = submission_status(lec, today)
        if wanted_status and state != wanted_status:
            continue
        submitted = state == "SUBMITTED"
        # a submitted lecture shows its saved totals; the others show the class size minus students who skip this subject
        rows.append({
            "lecture_id": lec.id,
            "date": lec.lecture_date.isoformat(),
            "start_time": lec.start_time.strftime("%H:%M"),
            "end_time": lec.end_time.strftime("%H:%M"),
            "department": assignment.classroom.department.name,
            "class_name": assignment.classroom.label,
            "subject": assignment.subject.name,
            "teacher": assignment.teacher.display_name,
            "total_students": lec.present_total + lec.absent_total if submitted else active_counts.get(assignment.classroom_id, 0) - left_out.get((assignment.classroom_id, assignment.subject_id), 0),
            "present": lec.present_total if submitted else None,
            "absent": lec.absent_total if submitted else None,
            "percentage": percentage(lec.present_total, lec.present_total + lec.absent_total) if submitted else None,
            "status": state,
        })

    # the summary adds up only submitted lectures, because only they have real present/absent numbers
    submitted_rows = [r for r in rows if r["status"] == "SUBMITTED"]
    present = sum(r["present"] for r in submitted_rows)
    absent = sum(r["absent"] for r in submitted_rows)
    summary = {
        "date_from": start.isoformat(), "date_to": end.isoformat(),
        "lectures": len(rows),
        "submitted": len(submitted_rows),
        "pending": sum(r["status"] == "PENDING" for r in rows),
        "scheduled": sum(r["status"] == "SCHEDULED" for r in rows),
        "cancelled": sum(r["status"] == "CANCELLED" for r in rows),
        "present": present, "absent": absent, "percentage": percentage(present, present + absent),
    }
    return rows, summary


# column titles of the daily report in CSV / PDF files (same order as daily_rows_for_export)
DAILY_HEADERS = [
    "Date", "Time", "Department", "Class", "Subject", "Teacher", "Total Students",
    "Present", "Absent", "Attendance %", "Status",
]


def daily_rows_for_export(rows):
    """Turn the daily report rows into plain lists of text for CSV / PDF. Missing numbers become empty cells."""
    label = {"SUBMITTED": "Submitted", "PENDING": "Pending", "SCHEDULED": "Scheduled", "CANCELLED": "Cancelled"}
    return [
        [r["date"], f'{r["start_time"]}-{r["end_time"]}', r["department"], r["class_name"], r["subject"], r["teacher"],
         r["total_students"], "" if r["present"] is None else r["present"], "" if r["absent"] is None else r["absent"],
         "" if r["percentage"] is None else f'{r["percentage"]}%', label[r["status"]]]
        for r in rows
    ]


def student_summary(user, classroom, params):
    """Per-student present/absent totals for one class (optionally one subject / date range)."""
    records = AttendanceRecord.objects.filter(
        lecture__status=Lecture.Status.SUBMITTED, lecture__teacher_subject_assignment__classroom=classroom
    )
    if user.role == User.Role.STAFF:  # a teacher sees only the marks of their own lectures
        records = records.filter(lecture__teacher_subject_assignment__teacher=user)
    if params.get("subject"):
        if not str(params["subject"]).isdigit():
            raise ValidationError({"subject": "Must be a number."})
        records = records.filter(lecture__teacher_subject_assignment__subject=params["subject"])
    start = parse_date(params.get("date_from"), "date_from")
    end = parse_date(params.get("date_to"), "date_to")
    if start:
        records = records.filter(lecture__lecture_date__gte=start)
    if end:
        records = records.filter(lecture__lecture_date__lte=end)

    # one query: present and absent counts for every student, keyed by student id
    totals = {
        row["student"]: row
        for row in records.values("student").annotate(
            present=Count("id", filter=Q(status="PRESENT")), absent=Count("id", filter=Q(status="ABSENT"))
        )
    }
    rows = []
    current = Q(classroom=classroom, is_active=True)
    if params.get("subject"):  # students who do not take this subject are left out, unless they already have marks in it
        current &= ~Q(id__in=SubjectExclusion.objects.filter(subject=params["subject"]).values("student_id"))
    # current students of the class, plus anyone who has marks in it (left, deactivated or moved later): their old results stay
    for student in Student.objects.filter(current | Q(id__in=list(totals))).defer("photo").order_by(*roll_order()):
        t = totals.get(student.id, {"present": 0, "absent": 0})
        total = t["present"] + t["absent"]
        pct = percentage(t["present"], total)
        rows.append({
            "student": student.id, "roll_number": student.roll_number, "name": student.name, "is_active": student.is_active,
            "present": t["present"], "absent": t["absent"], "total": total,
            "percentage": pct, "low": pct is not None and pct < settings.LOW_ATTENDANCE_THRESHOLD,
        })
    if str(params.get("low_only", "")).lower() in ("1", "true", "yes"):  # keep only students below the low-attendance limit
        rows = [r for r in rows if r["low"]]
    return rows


# the most lecture columns shown in one monthly sheet
MONTHLY_MAX_LECTURES = 120


def parse_month(value):
    """'2026-10' -> (first day, last day). Defaults to the current month."""
    import calendar
    if not value:
        today = timezone.localdate()
        year, month = today.year, today.month
    else:
        try:
            year_text, month_text = str(value).split("-")
            year, month = int(year_text), int(month_text)
            date(year, month, 1)  # only a check: this raises ValueError for a month like 13
        except (ValueError, TypeError):
            raise ValidationError({"month": "Use the form YYYY-MM, for example 2026-10."})
    return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])


def monthly_sheet(user, classroom, params):
    """
    The classic register page for one class and month: a row per student, a column per submitted lecture,
    P / A in the cells and the month's totals at the right. Teachers only see their own lectures.
    """
    first, last = parse_month(params.get("month"))
    lectures = Lecture.objects.filter(
        status=Lecture.Status.SUBMITTED, teacher_subject_assignment__classroom=classroom,
        lecture_date__gte=first, lecture_date__lte=last,
    ).select_related("teacher_subject_assignment__subject", "teacher_subject_assignment__teacher")
    if user.role == User.Role.STAFF:
        lectures = lectures.filter(teacher_subject_assignment__teacher=user)
    subject = params.get("subject")
    if subject:
        if not str(subject).isdigit():
            raise ValidationError({"subject": "Must be a number."})
        lectures = lectures.filter(teacher_subject_assignment__subject_id=subject)
    # read one extra lecture to learn whether the month was cut off at the limit
    lectures = list(lectures.order_by("lecture_date", "start_time", "id")[: MONTHLY_MAX_LECTURES + 1])
    truncated = len(lectures) > MONTHLY_MAX_LECTURES
    lectures = lectures[:MONTHLY_MAX_LECTURES]

    marks = {}  # student id -> {lecture id -> "P" or "A"}
    for student_id, lecture_id, status in AttendanceRecord.objects.filter(lecture__in=[l.id for l in lectures]).values_list(
        "student_id", "lecture_id", "status"
    ):
        marks.setdefault(student_id, {})[lecture_id] = "P" if status == "PRESENT" else "A"

    # a student who was moved to another class or deactivated keeps his marks in this class's sheet
    current = Q(classroom=classroom, is_active=True)
    if subject:
        current &= ~Q(id__in=SubjectExclusion.objects.filter(subject_id=subject).values("student_id"))
    students = Student.objects.defer("photo").filter(current | Q(id__in=list(marks))).order_by(*roll_order())
    rows = []  # one row per student: the P/A marks plus the month totals
    for st in students:
        own = marks.get(st.id, {})
        present = sum(1 for v in own.values() if v == "P")
        total = len(own)
        pct = percentage(present, total)
        rows.append({
            "student": st.id, "roll_number": st.roll_number, "name": st.name, "is_active": st.is_active,
            "marks": {str(k): v for k, v in own.items()}, "present": present, "total": total, "percentage": pct,
            "low": pct is not None and pct < settings.LOW_ATTENDANCE_THRESHOLD,
        })
    columns = []  # one column per lecture, with how many were present/absent in it
    for l in lectures:
        a = l.teacher_subject_assignment
        columns.append({
            "id": l.id, "date": l.lecture_date.isoformat(), "day": l.lecture_date.day,
            "start": l.start_time.strftime("%H:%M"), "end": l.end_time.strftime("%H:%M"),
            "subject": a.subject.name, "subject_code": a.subject.code, "teacher": a.teacher.get_full_name() or a.teacher.username,
            "present": sum(1 for r in rows if r["marks"].get(str(l.id)) == "P"),
            "absent": sum(1 for r in rows if r["marks"].get(str(l.id)) == "A"),
        })
    return {"month": first.strftime("%Y-%m"), "month_label": first.strftime("%B %Y"), "columns": columns, "results": rows, "truncated": truncated}


def monthly_sheet_export(sheet):
    """Turn the monthly sheet into (headers, rows) of plain values for CSV / PDF."""
    headers = ["Roll", "Name"] + [f'{c["day"]:02d} {c["subject_code"]}' for c in sheet["columns"]] + ["P", "Total", "%"]
    rows = [
        [r["roll_number"], r["name"]] + [r["marks"].get(str(c["id"]), "") for c in sheet["columns"]]
        + [r["present"], r["total"], "" if r["percentage"] is None else f'{r["percentage"]}%']
        for r in sheet["results"]
    ]
    return headers, rows


def overdue_pending_count(qs, today):
    """Draft lectures from earlier dates whose teacher can still submit them."""
    return qs.filter(pending_q(), lecture_date__lt=today).count()


def week_ago(today):
    """The date 7 days before `today`."""
    return today - timedelta(days=7)


# ----------------------------------------------------------------------------
# Dashboard helpers: trend chart and low-attendance students
# ----------------------------------------------------------------------------
TREND_DAYS = 14


def daily_trend(lectures, today, days=TREND_DAYS):
    """Attendance % per day for the last `days` days, from stored lecture totals (None = nothing submitted)."""
    start = today - timedelta(days=days - 1)  # days - 1 because today is also one of the days
    sums = {
        row["lecture_date"]: row
        for row in lectures.filter(status=Lecture.Status.SUBMITTED, lecture_date__range=(start, today))
        .values("lecture_date").annotate(present=Sum("present_total"), absent=Sum("absent_total"))
    }
    out = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        row = sums.get(day)
        present, absent = (row["present"], row["absent"]) if row else (0, 0)
        out.append({
            "date": day.isoformat(), "percentage": percentage(present, present + absent),
            "present": present, "total": present + absent,
        })
    return out


def low_attendance_students(department_id, today, limit=8):
    """
    Students below the threshold over the last LOW_ATTENDANCE_DAYS days (only those with at least
    LOW_ATTENDANCE_MIN_LECTURES marked lectures, so one absence on day one is not an alarm).
    Returns (count, worst `limit` students).
    """
    start = today - timedelta(days=settings.LOW_ATTENDANCE_DAYS - 1)
    grouped = (
        AttendanceRecord.objects.filter(
            lecture__status=Lecture.Status.SUBMITTED, lecture__lecture_date__range=(start, today),
            student__department_id=department_id, student__is_active=True,
        )
        .values("student_id")
        .annotate(total=Count("id"), present=Count("id", filter=Q(status=AttendanceRecord.Status.PRESENT)))
    )
    low = []
    for row in grouped:
        if row["total"] < settings.LOW_ATTENDANCE_MIN_LECTURES:
            continue
        pct = percentage(row["present"], row["total"])
        if pct < settings.LOW_ATTENDANCE_THRESHOLD:
            low.append((pct, row["student_id"], row["present"], row["total"]))
    low.sort()  # the lowest percentage comes first
    worst = low[:limit]
    students = {s.id: s for s in Student.objects.filter(id__in=[w[1] for w in worst]).select_related("classroom")}
    return len(low), [
        {"student": sid, "roll_number": students[sid].roll_number, "name": students[sid].name,
         "class_name": students[sid].classroom.label, "present": present, "total": total, "percentage": pct}
        for pct, sid, present, total in worst
    ]
