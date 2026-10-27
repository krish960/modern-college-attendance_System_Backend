"""
Business rules for attendance. Views stay thin and call these functions, which
run inside database transactions.
"""
from collections import defaultdict
from datetime import timedelta

from django.db import transaction
from django.db.models import F, Q
from django.utils import timezone
from rest_framework.exceptions import PermissionDenied, ValidationError

from accounts.models import User
from core.audit import log_action
from core.notifications import notify, notify_admins, notify_many
from core.ordering import roll_order
from students.models import Student

from .models import AttendanceCorrectionRequest, AttendanceRecord, Lecture, SubjectExclusion, TimetableSlot

# A DRAFT lecture of a deactivated teacher or assignment is not "pending": nobody can submit it.
# Use as  qs.filter(pending_q())  /  qs.exclude(dead_draft_q())


def pending_q(prefix=""):
    """Lectures that are waiting for a teacher who can still submit them."""
    return Q(**{
        prefix + "status": Lecture.Status.DRAFT,
        prefix + "teacher_subject_assignment__is_active": True,
        prefix + "teacher_subject_assignment__teacher__is_active": True,
    })


def dead_draft_q(prefix=""):
    """Draft lectures that can never be submitted (assignment or teacher deactivated)."""
    return Q(**{prefix + "status": Lecture.Status.DRAFT}) & (
        Q(**{prefix + "teacher_subject_assignment__is_active": False})
        | Q(**{prefix + "teacher_subject_assignment__teacher__is_active": False})
    )

# Longest date range the HOD may generate lectures for in one go (stops huge accidental requests).
MAX_GENERATE_DAYS = 62


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------
def lecture_department(lecture):
    """Return the department that owns this lecture (through its class)."""
    return lecture.teacher_subject_assignment.classroom.department


# Role rule: only the teacher who owns the assignment marks attendance.
# Even the HOD and Admin cannot mark for a teacher.
def assert_teacher_owns(lecture, user):
    """Only the teacher who owns the (active) assignment may mark attendance."""
    assignment = lecture.teacher_subject_assignment
    if user.role != User.Role.STAFF or assignment.teacher_id != user.id:
        raise PermissionDenied("You can only mark attendance for subjects and classes assigned to you.")
    if not assignment.is_active:
        raise PermissionDenied("This teaching assignment is no longer active.")


# The roster is the list of students shown on the marking screen.
# Elective students (SubjectExclusion) are left out, but anyone who already has a mark
# stays so old data is never hidden.
def roster_students(lecture):
    """Active students of the class who take this subject, plus every student already marked in this lecture (even if deactivated or moved to another class later)."""
    classroom_id = lecture.teacher_subject_assignment.classroom_id
    subject_id = lecture.teacher_subject_assignment.subject_id
    already_marked = AttendanceRecord.objects.filter(lecture=lecture).values("student_id")
    not_in_subject = SubjectExclusion.objects.filter(subject_id=subject_id).values("student_id")
    return (
        Student.objects.defer("photo")
        .filter(Q(classroom_id=classroom_id, is_active=True) & ~Q(id__in=not_in_subject) | Q(id__in=already_marked))
        .order_by(*roll_order())
    )


def attendance_counts(lecture):
    """Count PRESENT and ABSENT records of a lecture. Returns {present, absent, total}."""
    records = list(lecture.records.values_list("status", flat=True))
    present = records.count(AttendanceRecord.Status.PRESENT)
    return {"present": present, "absent": len(records) - present, "total": len(records)}


# ----------------------------------------------------------------------------
# Marking and submitting
# ----------------------------------------------------------------------------
@transaction.atomic
# Main marking function. Locks the lecture row (select_for_update) so two requests cannot
# save at the same time. Saving without submit keeps the lecture as DRAFT.
def save_attendance(lecture_id, user, items, submit=False):
    """
    Save PRESENT/ABSENT marks for a DRAFT lecture, optionally submitting it.
    `items` is a list of {"student": id, "status": "PRESENT"|"ABSENT"}.
    """
    lecture = (
        Lecture.objects.select_for_update()
        .select_related("teacher_subject_assignment__classroom", "teacher_subject_assignment__subject")
        .get(pk=lecture_id)
    )
    assert_teacher_owns(lecture, user)

    if lecture.status != Lecture.Status.DRAFT:
        raise ValidationError(
            "Only draft lectures can be marked. Submitted attendance can only be changed through a correction request."
        )
    # Business rule: no marking in advance.
    if lecture.lecture_date > timezone.localdate():
        raise ValidationError("Attendance cannot be marked for a future date.")

    # Check every student sent by the client really belongs to this lecture's roster.
    roster = {s.id: s for s in roster_students(lecture)}
    wanted = {}
    for item in items:
        sid = item["student"]
        if sid in wanted:
            raise ValidationError(f"Student {sid} appears more than once.")
        if sid not in roster:
            raise ValidationError(f"Student {sid} does not belong to this lecture's class.")
        wanted[sid] = item["status"]

    now = timezone.now()
    existing = {r.student_id: r for r in lecture.records.all()}
    to_create, to_update = [], []
    for sid, status in wanted.items():
        record = existing.get(sid)
        if record is None:
            to_create.append(AttendanceRecord(lecture=lecture, student_id=sid, status=status, marked_by=user, marked_at=now))
        elif record.status != status:
            record.status, record.marked_by, record.marked_at = status, user, now
            to_update.append(record)
    AttendanceRecord.objects.bulk_create(to_create)  # students were validated against the class above
    AttendanceRecord.objects.bulk_update(to_update, ["status", "marked_by", "marked_at"])

    # Submitting locks the lecture. Every active student must be marked first.
    if submit:
        marked = set(existing) | set(wanted)
        required = {sid for sid, s in roster.items() if s.is_active}
        if not required:
            raise ValidationError("This class has no active students.")
        missing = required - marked
        if missing:
            raise ValidationError(f"Mark every student before submitting. {len(missing)} student(s) are still unmarked.")
        counts = attendance_counts(lecture)
        lecture.status = Lecture.Status.SUBMITTED
        lecture.submitted_at = now
        lecture.submitted_by = user
        # Store the totals now so reports do not have to count records later.
        lecture.present_total, lecture.absent_total = counts["present"], counts["absent"]
        lecture.save(update_fields=["status", "submitted_at", "submitted_by", "present_total", "absent_total"])
        log_action(user, "ATTENDANCE_SUBMITTED", lecture, department=lecture_department(lecture),
                   new={"status": "SUBMITTED", **counts})
        from staffattendance.services import record_lecture_presence  # a lecture taken = the teacher was in college
        record_lecture_presence(user, lecture.lecture_date, lecture)
    return lecture


@transaction.atomic
# A lecture can be cancelled only while DRAFT, by its own teacher or by the HOD of the department.
def cancel_lecture(lecture_id, user, reason=""):
    """Mark a DRAFT lecture as CANCELLED and throw away any half-saved marks."""
    lecture = Lecture.objects.select_for_update().select_related(
        "teacher_subject_assignment__classroom__department").get(pk=lecture_id)
    assignment = lecture.teacher_subject_assignment
    is_owner = user.role == User.Role.STAFF and assignment.teacher_id == user.id
    is_hod = user.role == User.Role.HOD and user.department_id == assignment.classroom.department_id
    if not (is_owner or is_hod):
        raise PermissionDenied("You cannot cancel this lecture.")
    if lecture.status != Lecture.Status.DRAFT:
        raise ValidationError("Only draft lectures can be cancelled.")
    lecture.status = Lecture.Status.CANCELLED
    lecture.save(update_fields=["status"])
    lecture.records.all().delete()  # any partially saved draft marks are discarded
    # (Deleting is allowed here because the lecture is no longer SUBMITTED.)
    log_action(user, "LECTURE_CANCELLED", lecture, department=assignment.classroom.department,
               old={"status": "DRAFT"}, new={"status": "CANCELLED"}, reason=reason)
    return lecture


# ----------------------------------------------------------------------------
# Timetable -> lectures
# ----------------------------------------------------------------------------
@transaction.atomic
# The HOD turns weekly timetable slots into dated DRAFT lectures for their own department.
# Running it twice does not make duplicates (get_or_create).
def generate_lectures(hod, date_from, date_to):
    """Create DRAFT lectures for every active timetable slot between two dates (idempotent)."""
    if date_to < date_from:
        raise ValidationError({"date_to": "End date must not be before the start date."})
    if (date_to - date_from).days + 1 > MAX_GENERATE_DAYS:
        raise ValidationError(f"Choose a range of at most {MAX_GENERATE_DAYS} days.")

    slots = TimetableSlot.objects.filter(
        is_active=True, assignment__is_active=True, assignment__teacher__is_active=True,
        assignment__classroom__department=hod.department,
    ).select_related("assignment")
    by_weekday = defaultdict(list)
    for slot in slots:
        by_weekday[slot.weekday].append(slot)

    created = 0
    day = date_from
    while day <= date_to:
        for slot in by_weekday[day.weekday()]:
            _, was_created = Lecture.objects.get_or_create(
                teacher_subject_assignment=slot.assignment, lecture_date=day, start_time=slot.start_time,
                defaults={"end_time": slot.end_time},
            )
            created += int(was_created)
        day += timedelta(days=1)
    return created


# ----------------------------------------------------------------------------
# Corrections
# ----------------------------------------------------------------------------
@transaction.atomic
# A teacher asks to change one mark of a SUBMITTED lecture. The HOD is notified and the
# request is audit logged. Only one PENDING request per record is allowed.
def create_correction(user, record, requested_status, reason):
    """Save a correction request, write it to the audit log and tell the department's HOD."""
    lecture = record.lecture
    assignment = lecture.teacher_subject_assignment
    if assignment.teacher_id != user.id:
        raise PermissionDenied("You can only request corrections for your own lectures.")
    if lecture.status != Lecture.Status.SUBMITTED:
        raise ValidationError("Corrections apply to submitted attendance only.")
    if requested_status == record.status:
        raise ValidationError({"requested_status": "The record already has this status."})
    if record.correction_requests.filter(status=AttendanceCorrectionRequest.Status.PENDING).exists():
        raise ValidationError("A correction request for this record is already pending.")

    correction = AttendanceCorrectionRequest.objects.create(
        record=record, requested_by=user, old_status=record.status, requested_status=requested_status, reason=reason
    )
    department = assignment.classroom.department
    log_action(user, "CORRECTION_REQUESTED", correction, department=department,
               old={"status": record.status}, new={"status": requested_status}, reason=reason,
               extra={"record_id": record.id, "student": record.student.name, "lecture_id": lecture.id})
    hods = User.objects.filter(role=User.Role.HOD, department=department, is_active=True)
    notify_many(hods, "New correction request",
                f"{user.display_name} asked to change {record.student.name} on {lecture.lecture_date} "
                f"({assignment.subject.name}) from {record.status} to {requested_status}.")
    return correction


@transaction.atomic
# The HOD of the department approves or rejects a correction (remarks are required).
# On approval the mark is changed, the lecture totals are adjusted, and the Admin is
# notified. Both outcomes go to the audit log with the old value.
def review_correction(correction_id, reviewer, approve, remarks):
    """Approve or reject a correction. Only the HOD of the same department may do this, and only once."""
    correction = (
        AttendanceCorrectionRequest.objects.select_for_update()
        .select_related("record__lecture__teacher_subject_assignment__classroom__department",
                        "record__lecture__teacher_subject_assignment__subject", "record__student", "requested_by")
        .get(pk=correction_id)
    )
    record = correction.record
    assignment = record.lecture.teacher_subject_assignment
    department = assignment.classroom.department
    if reviewer.role != User.Role.HOD or reviewer.department_id != department.id:
        raise PermissionDenied("Only the HOD of this department can review corrections.")
    if correction.status != AttendanceCorrectionRequest.Status.PENDING:
        raise ValidationError("This request has already been reviewed.")
    remarks = (remarks or "").strip()
    if not remarks:
        raise ValidationError({"remarks": "Remarks are required."})

    now = timezone.now()
    old_status = record.status
    extra = {
        "correction_request_id": correction.id, "record_id": record.id, "student": record.student.name,
        "lecture_id": record.lecture_id, "requested_by": correction.requested_by.username,
        "approved_by" if approve else "rejected_by": reviewer.username,
        "remarks": remarks, "reviewed_at": now.isoformat(),
    }
    if approve:
        record.status = correction.requested_status
        record.save(update_fields=["status"], allow_submitted_edit=True)
        if record.status != old_status:  # keep the lecture's stored totals in step with its records
        # delta +1 means ABSENT became PRESENT, -1 means the opposite.
            delta = 1 if record.status == AttendanceRecord.Status.PRESENT else -1
            Lecture.objects.filter(pk=record.lecture_id).update(
                present_total=F("present_total") + delta, absent_total=F("absent_total") - delta)
        correction.status = AttendanceCorrectionRequest.Status.APPROVED
        log_action(reviewer, "ATTENDANCE_CORRECTION_APPROVED", record, department=department,
                   old={"status": old_status}, new={"status": record.status}, reason=correction.reason, extra=extra)
    else:
        correction.status = AttendanceCorrectionRequest.Status.REJECTED
        log_action(reviewer, "ATTENDANCE_CORRECTION_REJECTED", record, department=department,
                   old={"status": old_status}, new={"status": old_status}, reason=correction.reason, extra=extra)
    correction.reviewed_by = reviewer
    correction.reviewed_at = now
    correction.remarks = remarks
    correction.save(update_fields=["status", "reviewed_by", "reviewed_at", "remarks"])

    verdict = "approved" if approve else "rejected"
    notify(correction.requested_by, f"Correction {verdict}",
           f"Your request for {record.student.name} ({assignment.subject.name}, {record.lecture.lecture_date}) "
           f"was {verdict}. Remarks: {remarks}")
    if approve:  # a submitted result was changed: the admin is told at once (the old value is in the audit log)
        notify_admins("Attendance corrected",
                      f"{department.name}: {record.student.name} ({assignment.subject.name}, {record.lecture.lecture_date}) "
                      f"changed from {old_status.title()} to {record.status.title()} by {reviewer.display_name}. Reason: {correction.reason}")
    return correction
