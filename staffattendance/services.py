"""Helpers for staff attendance: late cut-off time, date parsing, who an HOD/Admin may see, and the day / month sheets.

The views in views.py call these functions."""
import calendar
from datetime import date, datetime

from django.conf import settings
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from accounts.models import User
from core.audit import log_action

from .models import StaffAttendance

ST = StaffAttendance.Status
# one-letter code of each status in the monthly sheet (V = on leave)
LETTER = {ST.PRESENT: "P", ST.LATE: "L", ST.HALF_DAY: "H", ST.ABSENT: "A", ST.LEAVE: "V"}
# statuses that count as a full present day in the monthly total (half day is counted separately as 0.5)
COUNTS_AS_PRESENT = {ST.PRESENT, ST.LATE}


def late_cutoff():
    """The late cut-off from settings (STAFF_LATE_AFTER, like "10:00") as (hour, minute)."""
    hh, mm = settings.STAFF_LATE_AFTER.split(":")
    return int(hh), int(mm)


def present_or_late(now=None):
    """PRESENT before the cut-off time, LATE after it (local time)."""
    now = timezone.localtime(now or timezone.now())
    hh, mm = late_cutoff()
    return ST.LATE if (now.hour, now.minute) > (hh, mm) else ST.PRESENT


def parse_day(value, field="date"):
    """Read a YYYY-MM-DD text as a date; empty means today; a bad text is a validation error."""
    if not value:
        return timezone.localdate()
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ValidationError({field: "Use the form YYYY-MM-DD."})


def parse_month(value):
    """Read "YYYY-MM" and return (first day, last day) of that month; empty means the current month."""
    if not value:
        today = timezone.localdate()
        return date(today.year, today.month, 1), date(today.year, today.month, calendar.monthrange(today.year, today.month)[1])
    try:
        y, m = (int(x) for x in str(value).split("-"))
        first = date(y, m, 1)
    except (ValueError, TypeError):
        raise ValidationError({"month": "Use the form YYYY-MM, for example 2026-10."})
    return first, date(y, m, calendar.monthrange(y, m)[1])


def department_staff(user, department_id=None):
    """Active teachers whose attendance this user may see: the HOD's own department (admin chooses one)."""
    qs = User.objects.filter(role=User.Role.STAFF, is_active=True).select_related("profile")
    if user.role == User.Role.ADMIN:  # the Admin must say which department to look at
        if not department_id or not str(department_id).isdigit():
            raise ValidationError({"department": "Choose a department."})
        return qs.filter(department_id=department_id)
    if user.role == User.Role.HOD:  # an HOD is fixed to their own department
        return qs.filter(department_id=user.department_id)
    raise PermissionDenied("Not allowed.")


def row_for(staff, record):
    """One teacher's line in the day sheet. `record` may be None (not marked yet), then status and source are None."""
    profile = getattr(staff, "profile", None)
    return {
        "user": staff.id, "name": staff.get_full_name() or staff.username,
        "employee_id": profile.employee_id if profile else "", "card_code": profile.card_code if profile else "",
        "status": record.status if record else None, "source": record.source if record else None,
        "check_in_at": record.check_in_at.isoformat() if record and record.check_in_at else None,
        "note": record.note if record else "",
        "marked_by_name": (record.marked_by.get_full_name() or record.marked_by.username) if record and record.marked_by else None,
    }


def day_sheet(user, day, department_id=None):
    """Every teacher of the department with their mark for `day`, plus counts per status. Returns (rows, summary)."""
    staff = list(department_staff(user, department_id).order_by("first_name", "last_name", "id"))
    records = {r.user_id: r for r in StaffAttendance.objects.filter(date=day, user__in=staff).select_related("marked_by")}
    rows = [row_for(s, records.get(s.id)) for s in staff]
    summary = {"staff": len(rows), "marked": sum(1 for r in rows if r["status"]), "not_marked": sum(1 for r in rows if not r["status"])}
    for status in ST.values:
        summary[status.lower()] = sum(1 for r in rows if r["status"] == status)
    return rows, summary


def monthly_sheet(user, month, department_id=None):
    """A grid for a month: a row per teacher, a column per day, with a letter for the status and the totals."""
    first, last = parse_month(month)
    staff = list(department_staff(user, department_id).order_by("first_name", "last_name", "id"))
    marks = {}  # teacher id -> {day of month -> status}
    for uid, d, status in StaffAttendance.objects.filter(user__in=staff, date__gte=first, date__lte=last).values_list("user_id", "date", "status"):
        marks.setdefault(uid, {})[d.day] = status
    days = list(range(1, last.day + 1))
    rows = []
    for s in staff:
        own = marks.get(s.id, {})
        profile = getattr(s, "profile", None)
        # present and late count as 1 day each, a half day counts as 0.5
        present = sum(1 for v in own.values() if v in COUNTS_AS_PRESENT) + 0.5 * sum(1 for v in own.values() if v == ST.HALF_DAY)
        rows.append({
            "user": s.id, "name": s.get_full_name() or s.username, "employee_id": profile.employee_id if profile else "",
            "marks": {str(d): LETTER[ST(v)] for d, v in own.items()},
            "present": present, "late": sum(1 for v in own.values() if v == ST.LATE),
            "absent": sum(1 for v in own.values() if v == ST.ABSENT), "leave": sum(1 for v in own.values() if v == ST.LEAVE),
            "marked_days": len(own),
        })
    # two-letter weekday names (Mo, Tu ...) for the sheet header
    weekdays = [calendar.day_abbr[date(first.year, first.month, d).weekday()][:2] for d in days]
    return {"month": first.strftime("%Y-%m"), "month_label": first.strftime("%B %Y"), "days": days, "weekdays": weekdays, "results": rows}


def record_lecture_presence(teacher, day, lecture):
    """
    A teacher who has submitted at least one lecture on a day was obviously at college: that day is marked Present
    automatically (nothing is changed if the teacher or the HOD already recorded Present, Late or Half day; an Absent or
    On leave mark is replaced, because the lecture proves otherwise, and the change is written to the audit log).
    """
    if teacher is None or teacher.role != User.Role.STAFF:  # only teachers have staff attendance
        return
    record, created = StaffAttendance.objects.get_or_create(
        user=teacher, date=day,
        defaults={"status": ST.PRESENT, "source": StaffAttendance.Source.LECTURE, "check_in_at": timezone.now(), "note": "Took a lecture"},
    )
    old = None
    if not created:
        if record.status not in (ST.ABSENT, ST.LEAVE):  # already Present / Late / Half day: leave it as it is
            return
        old = record.status
        record.status, record.source = ST.PRESENT, StaffAttendance.Source.LECTURE
        record.note = f"Took a lecture (was {old.lower()})"
        record.save(update_fields=["status", "source", "note", "updated_at"])
    log_action(teacher, "STAFF_ATTENDANCE_AUTO", record, department=teacher.department,
               old={"status": old} if old else None, new={"status": ST.PRESENT, "date": str(day)}, reason="Submitted a lecture")
