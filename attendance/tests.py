"""Tests for the attendance app: marking, submitting, lectures, timetable, corrections, history and notifications."""
import datetime

from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction

from academics.models import ClassRoom
from core.models import AuditLog, Notification
from core.testing import BaseTestCase

from .models import AttendanceCorrectionRequest, AttendanceRecord, Lecture, TimetableSlot


class MarkingAndSubmissionTests(BaseTestCase):
    """Teachers saving drafts and submitting attendance, and the rules that protect submitted marks."""
    def test_teacher_submits_attendance_and_counts_are_correct(self):
        """Submitting locks the lecture and stores the right present and absent totals."""
        lecture = self.make_lecture()
        self.as_user(self.t1)
        present = {s.id for s in self.students[:3]}
        response = self.client.post(f"/api/lectures/{lecture.id}/submit/", self.marks(self.students, present), format="json")
        self.assertEqual(response.status_code, 200, response.data)
        lecture.refresh_from_db()
        self.assertEqual(lecture.status, Lecture.Status.SUBMITTED)
        self.assertEqual(lecture.submitted_by, self.t1)
        self.assertIsNotNone(lecture.submitted_at)
        self.assertEqual(response.data["lecture"]["present_count"], 3)
        self.assertEqual(response.data["lecture"]["absent_count"], 2)
        self.assertEqual(AttendanceRecord.objects.filter(lecture=lecture).count(), 5)
        self.assertFalse(response.data["editable"])
        log = AuditLog.objects.get(action="ATTENDANCE_SUBMITTED")
        self.assertEqual(log.new_value["present"], 3)

    def test_draft_marks_can_be_saved_and_resumed(self):
        """A teacher can save part of a register as a draft and come back to finish it."""
        lecture = self.make_lecture()
        self.as_user(self.t1)
        first = self.client.post(f"/api/lectures/{lecture.id}/mark/", self.marks(self.students[:2], {self.students[0].id}), format="json")
        self.assertEqual(first.status_code, 200)
        self.assertTrue(first.data["editable"])
        roster = self.client.get(f"/api/lectures/{lecture.id}/roster/").data
        statuses = {s["id"]: s["status"] for s in roster["students"]}
        self.assertEqual(statuses[self.students[0].id], "PRESENT")
        self.assertEqual(statuses[self.students[1].id], "ABSENT")
        self.assertIsNone(statuses[self.students[4].id])
        # changing a draft mark updates the same record (no duplicate)
        self.client.post(f"/api/lectures/{lecture.id}/mark/", {"records": [{"student": self.students[0].id, "status": "ABSENT"}]}, format="json")
        self.assertEqual(AttendanceRecord.objects.filter(lecture=lecture, student=self.students[0]).count(), 1)
        self.assertEqual(AttendanceRecord.objects.get(lecture=lecture, student=self.students[0]).status, "ABSENT")

    def test_cannot_submit_until_every_student_is_marked(self):
        """Submit is refused while any active student is still unmarked."""
        lecture = self.make_lecture()
        self.as_user(self.t1)
        response = self.client.post(f"/api/lectures/{lecture.id}/submit/", self.marks(self.students[:4]), format="json")
        self.assertEqual(response.status_code, 400)
        lecture.refresh_from_db()
        self.assertEqual(lecture.status, Lecture.Status.DRAFT)

    def test_resubmitting_or_editing_a_submitted_lecture_is_refused(self):
        """A submitted lecture cannot be submitted or edited again."""
        lecture = self.submit(self.make_lecture(), self.t1)
        for action in ("submit", "mark"):
            response = self.client.post(f"/api/lectures/{lecture.id}/{action}/", self.marks(self.students, set()), format="json")
            self.assertEqual(response.status_code, 400, action)
        self.assertEqual(AttendanceRecord.objects.filter(lecture=lecture, status="PRESENT").count(), 3)

    def test_submitted_attendance_cannot_be_edited_or_deleted_directly(self):
        """Submitted marks are locked at the model level; only an approved correction may change them."""
        lecture = self.submit(self.make_lecture(), self.t1)
        record = lecture.records.first()
        for user in (self.t1, self.hod_cs, self.admin):
            self.as_user(user)
            self.assertEqual(self.client.patch(f"/api/attendance-records/{record.id}/", {"status": "PRESENT"}, format="json").status_code, 405)
            self.assertEqual(self.client.put(f"/api/attendance-records/{record.id}/", {"status": "PRESENT"}, format="json").status_code, 405)
            self.assertEqual(self.client.delete(f"/api/attendance-records/{record.id}/").status_code, 405)
            self.assertEqual(self.client.post("/api/attendance-records/", {"lecture": lecture.id, "student": 1, "status": "PRESENT"}, format="json").status_code, 405)
            self.assertEqual(self.client.delete(f"/api/lectures/{lecture.id}/").status_code, 405)
            self.assertEqual(self.client.patch(f"/api/lectures/{lecture.id}/", {"status": "DRAFT"}, format="json").status_code, 405)

    def test_only_the_owning_teacher_can_mark(self):
        """Another teacher (or an HOD) cannot mark someone else's lecture."""
        lecture = self.make_lecture()
        payload = self.marks(self.students)
        self.as_user(self.t2)  # teaches the same class, but a different subject
        self.assertEqual(self.client.post(f"/api/lectures/{lecture.id}/submit/", payload, format="json").status_code, 404)
        self.as_user(self.t_math)
        self.assertEqual(self.client.post(f"/api/lectures/{lecture.id}/submit/", payload, format="json").status_code, 404)
        for user in (self.hod_cs, self.admin):
            self.as_user(user)
            self.assertEqual(self.client.post(f"/api/lectures/{lecture.id}/submit/", payload, format="json").status_code, 403)
        self.assertEqual(AttendanceRecord.objects.count(), 0)

    def test_service_layer_also_checks_ownership(self):
        """The ownership rule also holds when the service function is called directly, not just through the API."""
        from rest_framework.exceptions import PermissionDenied

        from .services import save_attendance
        lecture = self.make_lecture()
        with self.assertRaises(PermissionDenied):
            save_attendance(lecture.id, self.t2, [], submit=False)

    def test_deactivated_assignment_blocks_marking(self):
        """If the teacher's subject assignment is switched off, marking is blocked."""
        lecture = self.make_lecture()
        self.assign_py.is_active = False
        self.assign_py.save()
        self.as_user(self.t1)
        self.assertEqual(self.client.post(f"/api/lectures/{lecture.id}/submit/", self.marks(self.students), format="json").status_code, 403)

    def test_student_from_another_class_is_rejected(self):
        """A student of a different class cannot be marked in this lecture."""
        lecture = self.make_lecture()
        self.as_user(self.t1)
        payload = self.marks(self.students)
        payload["records"][0]["student"] = self.math_students[0].id
        response = self.client.post(f"/api/lectures/{lecture.id}/submit/", payload, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(AttendanceRecord.objects.count(), 0)

    def test_duplicate_student_in_one_request_is_rejected(self):
        """The same student listed twice in one request is an error."""
        lecture = self.make_lecture()
        self.as_user(self.t1)
        payload = {"records": [{"student": self.students[0].id, "status": "PRESENT"}] * 2}
        self.assertEqual(self.client.post(f"/api/lectures/{lecture.id}/mark/", payload, format="json").status_code, 400)

    def test_invalid_status_is_rejected(self):
        """Only PRESENT or ABSENT are accepted as a mark."""
        lecture = self.make_lecture()
        self.as_user(self.t1)
        payload = {"records": [{"student": self.students[0].id, "status": "LATE"}]}
        self.assertEqual(self.client.post(f"/api/lectures/{lecture.id}/mark/", payload, format="json").status_code, 400)

    def test_future_lectures_cannot_be_marked(self):
        """Attendance cannot be taken for a lecture that has not happened yet."""
        lecture = self.make_lecture(date=self.today + datetime.timedelta(days=1))
        self.as_user(self.t1)
        self.assertEqual(self.client.post(f"/api/lectures/{lecture.id}/submit/", self.marks(self.students), format="json").status_code, 400)

    def test_database_blocks_duplicate_record_per_lecture_and_student(self):
        """The database unique rule stops two records for one student in one lecture."""
        lecture = self.make_lecture()
        AttendanceRecord.objects.create(lecture=lecture, student=self.students[0], status="PRESENT", marked_by=self.t1)
        with self.assertRaises(IntegrityError), transaction.atomic():
            AttendanceRecord.objects.create(lecture=lecture, student=self.students[0], status="ABSENT", marked_by=self.t1)

    def test_model_rejects_student_outside_the_lectures_class(self):
        """The model's own check refuses a student who is not in the lecture's class."""
        lecture = self.make_lecture()
        with self.assertRaises(DjangoValidationError):
            AttendanceRecord.objects.create(lecture=lecture, student=self.math_students[0], status="PRESENT", marked_by=self.t1)

    def test_inactive_students_are_not_required(self):
        """Inactive (removed) students do not have to be marked before submitting."""
        self.students[4].is_active = False
        self.students[4].save()
        lecture = self.make_lecture()
        self.as_user(self.t1)
        response = self.client.post(f"/api/lectures/{lecture.id}/submit/", self.marks(self.students[:4]), format="json")
        self.assertEqual(response.status_code, 200, response.data)


class LectureTests(BaseTestCase):
    """Creating, listing and cancelling lectures, and who may do each."""
    payload = {"lecture_date": None, "start_time": "11:00", "end_time": "12:00"}

    def body(self, assignment, **extra):
        """Build the JSON body for creating a lecture, with optional overrides."""
        return {"teacher_subject_assignment": assignment.id, "lecture_date": str(self.today),
                "start_time": "11:00", "end_time": "12:00", **extra}

    def test_teacher_creates_lecture_for_own_assignment_only(self):
        """A teacher can schedule lectures only for their own subject assignments."""
        self.as_user(self.t1)
        self.assertEqual(self.client.post("/api/lectures/", self.body(self.assign_py), format="json").status_code, 201)
        self.assertEqual(self.client.post("/api/lectures/", self.body(self.assign_db, start_time="14:00", end_time="15:00"), format="json").status_code, 400)

    def test_hod_schedules_lecture_only_in_own_department(self):
        """An HOD can schedule lectures only inside their own department."""
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.post("/api/lectures/", self.body(self.assign_py), format="json").status_code, 201)
        self.assertEqual(self.client.post("/api/lectures/", self.body(self.assign_math), format="json").status_code, 400)

    def test_overlapping_lectures_for_same_teacher_or_class_are_refused(self):
        """Two lectures at overlapping times for one teacher or one class are refused."""
        self.make_lecture(self.assign_py, start="11:00", end="12:00")
        self.as_user(self.hod_cs)
        same_teacher = self.body(self.assign_py, start_time="11:30", end_time="12:30")
        self.assertEqual(self.client.post("/api/lectures/", same_teacher, format="json").status_code, 400)
        same_class = self.body(self.assign_db, start_time="11:00", end_time="11:45")
        self.assertEqual(self.client.post("/api/lectures/", same_class, format="json").status_code, 400)
        later = self.body(self.assign_db, start_time="12:00", end_time="13:00")
        self.assertEqual(self.client.post("/api/lectures/", later, format="json").status_code, 201)

    def test_end_time_must_follow_start_time(self):
        """A lecture that ends before it starts is refused."""
        self.as_user(self.hod_cs)
        response = self.client.post("/api/lectures/", self.body(self.assign_py, start_time="12:00", end_time="11:00"), format="json")
        self.assertEqual(response.status_code, 400)

    def test_lectures_are_scoped_by_role(self):
        """Each role sees only its own lectures (teacher own, HOD department, Admin all)."""
        mine = self.make_lecture(self.assign_py)
        theirs = self.make_lecture(self.assign_db, start="10:00", end="11:00")
        other_dept = self.make_lecture(self.assign_math)
        ids = lambda user: {l["id"] for l in (self.as_user(user), self.client.get("/api/lectures/").data["results"])[1]}  # noqa: E731
        self.assertEqual(ids(self.t1), {mine.id})
        self.assertEqual(ids(self.hod_cs), {mine.id, theirs.id})
        self.assertEqual(ids(self.hod_math), {other_dept.id})
        self.assertEqual(ids(self.admin), {mine.id, theirs.id, other_dept.id})
        self.as_user(self.t1)
        self.assertEqual(self.client.get(f"/api/lectures/{theirs.id}/").status_code, 404)

    def test_cancel_draft_lecture_by_owner_or_hod_but_not_submitted(self):
        """Only draft lectures can be cancelled, and only by the owner or the department HOD."""
        lecture = self.make_lecture()
        self.as_user(self.t2)
        self.assertEqual(self.client.post(f"/api/lectures/{lecture.id}/cancel/").status_code, 404)
        self.as_user(self.hod_math)
        self.assertEqual(self.client.post(f"/api/lectures/{lecture.id}/cancel/").status_code, 404)
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.post(f"/api/lectures/{lecture.id}/cancel/", {"reason": "Holiday"}, format="json").status_code, 200)
        lecture.refresh_from_db()
        self.assertEqual(lecture.status, Lecture.Status.CANCELLED)
        submitted = self.submit(self.make_lecture(start="13:00", end="14:00"), self.t1)
        self.assertEqual(self.client.post(f"/api/lectures/{submitted.id}/cancel/").status_code, 400)


class TimetableTests(BaseTestCase):
    """The weekly timetable and generating dated lectures from it."""
    def slot(self, **extra):
        """Helper that creates a timetable slot through the API."""
        return {"assignment": self.assign_py.id, "weekday": 0, "start_time": "09:00", "end_time": "10:00", **extra}

    def test_hod_builds_timetable_and_generates_lectures_idempotently(self):
        """The HOD generates lectures from slots, and running it twice creates no duplicates."""
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.post("/api/timetable/", self.slot(), format="json").status_code, 201)
        self.assertEqual(self.client.post("/api/timetable/", self.slot(), format="json").status_code, 400)  # duplicate
        monday = self.today - datetime.timedelta(days=self.today.weekday())
        rng = {"date_from": str(monday), "date_to": str(monday + datetime.timedelta(days=13))}
        first = self.client.post("/api/timetable/generate/", rng, format="json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.data["created"], 2)
        self.assertEqual(self.client.post("/api/timetable/generate/", rng, format="json").data["created"], 0)
        self.assertEqual(Lecture.objects.filter(status="DRAFT").count(), 2)

    def test_generate_validates_range(self):
        """Generating needs a sensible date range (not backwards, not too long)."""
        self.as_user(self.hod_cs)
        bad = {"date_from": "2026-10-10", "date_to": "2026-10-01"}
        self.assertEqual(self.client.post("/api/timetable/generate/", bad, format="json").status_code, 400)
        huge = {"date_from": "2026-01-01", "date_to": "2026-12-31"}
        self.assertEqual(self.client.post("/api/timetable/generate/", huge, format="json").status_code, 400)

    def test_hod_cannot_use_other_departments_assignment(self):
        """An HOD cannot put another department's assignment on the timetable."""
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.post("/api/timetable/", self.slot(assignment=self.assign_math.id), format="json").status_code, 400)

    def test_generation_only_touches_own_department(self):
        """Generating lectures only creates them for the HOD's own department."""
        TimetableSlot.objects.create(assignment=self.assign_math, weekday=0, start_time="09:00", end_time="10:00")
        self.as_user(self.hod_cs)
        monday = self.today - datetime.timedelta(days=self.today.weekday())
        self.client.post("/api/timetable/generate/", {"date_from": str(monday), "date_to": str(monday + datetime.timedelta(days=6))}, format="json")
        self.assertEqual(Lecture.objects.count(), 0)

    def test_staff_and_admin_cannot_edit_timetable(self):
        """Only the HOD may change the timetable; teachers and the Admin cannot."""
        for user in (self.t1, self.admin):
            self.as_user(user)
            self.assertEqual(self.client.post("/api/timetable/", self.slot(), format="json").status_code, 403)
            self.assertEqual(self.client.post("/api/timetable/generate/", {"date_from": "2026-10-01", "date_to": "2026-10-02"}, format="json").status_code, 403)

    def test_inactive_slots_are_skipped(self):
        """Slots that are switched off do not produce lectures."""
        TimetableSlot.objects.create(assignment=self.assign_py, weekday=0, start_time="09:00", end_time="10:00", is_active=False)
        self.as_user(self.hod_cs)
        monday = self.today - datetime.timedelta(days=self.today.weekday())
        response = self.client.post("/api/timetable/generate/", {"date_from": str(monday), "date_to": str(monday + datetime.timedelta(days=6))}, format="json")
        self.assertEqual(response.data["created"], 0)


class CorrectionTests(BaseTestCase):
    """The correction flow: teacher asks, HOD approves or rejects, everything is audited."""
    def setUp(self):
        """Prepare a submitted lecture with marks that can be corrected."""
        super().setUp()
        self.lecture = self.submit(self.make_lecture(), self.t1, present_count=3)
        self.absent_record = AttendanceRecord.objects.get(lecture=self.lecture, student=self.students[4])
        self.reason = "Student was present, marked absent by mistake."

    def request_correction(self, user=None, **extra):
        """Helper that sends a correction request as the teacher (or another user)."""
        self.as_user(user or self.t1)
        payload = {"record": self.absent_record.id, "requested_status": "PRESENT", "reason": self.reason, **extra}
        return self.client.post("/api/corrections/", payload, format="json")

    def test_teacher_requests_correction_with_reason_and_hod_is_notified(self):
        """A valid request is saved as PENDING and the HOD gets a notification."""
        response = self.request_correction()
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["status"], "PENDING")
        self.assertEqual(response.data["old_status"], "ABSENT")
        self.assertTrue(Notification.objects.filter(user=self.hod_cs, title="New correction request").exists())
        self.absent_record.refresh_from_db()
        self.assertEqual(self.absent_record.status, "ABSENT")  # nothing changes silently

    def test_reason_is_mandatory(self):
        """A correction request without a reason is refused."""
        self.assertEqual(self.request_correction(reason="").status_code, 400)
        self.assertEqual(self.request_correction(reason="   ").status_code, 400)
        self.assertEqual(self.request_correction(reason="no").status_code, 400)
        self.assertEqual(AttendanceCorrectionRequest.objects.count(), 0)

    def test_invalid_requests_are_refused(self):
        """Requests that make no sense (same status, other teacher's lecture, duplicates) are refused."""
        self.assertEqual(self.request_correction(requested_status="ABSENT").status_code, 400)  # no change
        self.assertEqual(self.request_correction(requested_status="LATE").status_code, 400)
        self.assertEqual(self.request_correction().status_code, 201)
        self.assertEqual(self.request_correction().status_code, 400)  # already pending
        # another teacher cannot request changes for this teacher's lecture
        self.assertEqual(self.request_correction(user=self.t2).status_code, 400)
        # HOD and admin do not raise correction requests
        self.assertEqual(self.request_correction(user=self.hod_cs).status_code, 403)

    def test_draft_lecture_records_cannot_be_corrected(self):
        """Only submitted attendance can be corrected; drafts can simply be edited."""
        draft = self.make_lecture(start="14:00", end="15:00")
        self.as_user(self.t1)
        self.client.post(f"/api/lectures/{draft.id}/mark/", self.marks(self.students[:1]), format="json")
        record = AttendanceRecord.objects.get(lecture=draft)
        response = self.client.post("/api/corrections/", {"record": record.id, "requested_status": "PRESENT", "reason": "Please change"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_hod_approves_record_updates_and_audit_log_is_complete(self):
        """Approval changes the mark and totals, and the audit log keeps the old and new value."""
        correction_id = self.request_correction().data["id"]
        self.as_user(self.hod_cs)
        response = self.client.post(f"/api/corrections/{correction_id}/approve/", {"remarks": "Verified with register."}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "APPROVED")
        self.absent_record.refresh_from_db()
        self.assertEqual(self.absent_record.status, "PRESENT")

        log = AuditLog.objects.get(action="ATTENDANCE_CORRECTION_APPROVED")
        self.assertEqual(log.old_value, {"status": "ABSENT"})
        self.assertEqual(log.new_value, {"status": "PRESENT"})
        self.assertEqual(log.actor, self.hod_cs)          # approver
        self.assertEqual(log.extra["requested_by"], "prof_a")  # requester
        self.assertEqual(log.extra["approved_by"], "hod_cs")
        self.assertEqual(log.reason, self.reason)         # teacher's reason
        self.assertEqual(log.extra["remarks"], "Verified with register.")
        self.assertIsNotNone(log.created_at)              # timestamp
        self.assertEqual(log.entity_id, str(self.absent_record.id))

        # the teacher sees the decision and is notified
        self.as_user(self.t1)
        mine = self.client.get("/api/corrections/").data["results"]
        self.assertEqual(mine[0]["status"], "APPROVED")
        self.assertEqual(mine[0]["remarks"], "Verified with register.")
        self.assertTrue(Notification.objects.filter(user=self.t1, title="Correction approved").exists())

    def test_hod_rejects_record_unchanged_and_decision_logged(self):
        """Rejection leaves the mark as it was and the decision is logged."""
        correction_id = self.request_correction().data["id"]
        self.as_user(self.hod_cs)
        response = self.client.post(f"/api/corrections/{correction_id}/reject/", {"remarks": "Register shows absent."}, format="json")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "REJECTED")
        self.absent_record.refresh_from_db()
        self.assertEqual(self.absent_record.status, "ABSENT")
        log = AuditLog.objects.get(action="ATTENDANCE_CORRECTION_REJECTED")
        self.assertEqual(log.old_value, log.new_value)
        self.assertEqual(log.extra["rejected_by"], "hod_cs")
        self.assertTrue(Notification.objects.filter(user=self.t1, title="Correction rejected").exists())

    def test_remarks_are_required_to_review(self):
        """The HOD must write remarks when approving or rejecting."""
        correction_id = self.request_correction().data["id"]
        self.as_user(self.hod_cs)
        for action in ("approve", "reject"):
            self.assertEqual(self.client.post(f"/api/corrections/{correction_id}/{action}/", {"remarks": ""}, format="json").status_code, 400)
            self.assertEqual(self.client.post(f"/api/corrections/{correction_id}/{action}/", {}, format="json").status_code, 400)
        self.assertEqual(AttendanceCorrectionRequest.objects.get(pk=correction_id).status, "PENDING")

    def test_a_request_can_only_be_reviewed_once(self):
        """A request that is already approved or rejected cannot be decided again."""
        correction_id = self.request_correction().data["id"]
        self.as_user(self.hod_cs)
        self.client.post(f"/api/corrections/{correction_id}/reject/", {"remarks": "No"}, format="json")
        self.assertEqual(self.client.post(f"/api/corrections/{correction_id}/approve/", {"remarks": "Changed mind"}, format="json").status_code, 400)
        self.absent_record.refresh_from_db()
        self.assertEqual(self.absent_record.status, "ABSENT")

    def test_after_a_decision_a_new_request_is_possible(self):
        """Once a request is decided, the teacher may ask again for the same mark."""
        correction_id = self.request_correction().data["id"]
        self.as_user(self.hod_cs)
        self.client.post(f"/api/corrections/{correction_id}/reject/", {"remarks": "No"}, format="json")
        self.assertEqual(self.request_correction().status_code, 201)

    def test_only_the_departments_hod_may_review(self):
        """Only the HOD of the same department can decide; others get a refusal."""
        correction_id = self.request_correction().data["id"]
        self.as_user(self.hod_math)
        self.assertEqual(self.client.post(f"/api/corrections/{correction_id}/approve/", {"remarks": "ok"}, format="json").status_code, 404)
        self.assertEqual(self.client.get(f"/api/corrections/{correction_id}/").status_code, 404)
        for user in (self.t1, self.t2, self.admin):
            self.as_user(user)
            self.assertEqual(self.client.post(f"/api/corrections/{correction_id}/approve/", {"remarks": "ok"}, format="json").status_code, 403)
        self.absent_record.refresh_from_db()
        self.assertEqual(self.absent_record.status, "ABSENT")

    def test_visibility_of_correction_requests(self):
        """Teachers see their own requests, HODs their department's, the Admin all."""
        correction_id = self.request_correction().data["id"]
        self.as_user(self.t2)
        self.assertEqual(self.client.get("/api/corrections/").data["count"], 0)
        self.assertEqual(self.client.get(f"/api/corrections/{correction_id}/").status_code, 404)
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.get("/api/corrections/?status=PENDING").data["count"], 1)
        self.as_user(self.hod_math)
        self.assertEqual(self.client.get("/api/corrections/").data["count"], 0)

    def test_audit_log_entries_are_immutable(self):
        """Audit log rows cannot be changed or deleted once written."""
        self.request_correction()
        log = AuditLog.objects.first()
        with self.assertRaises(PermissionError):
            log.reason = "tampered"
            log.save()
        with self.assertRaises(PermissionError):
            log.delete()

    def test_audit_logs_are_scoped_to_department(self):
        """An HOD can read audit entries of their own department only."""
        correction_id = self.request_correction().data["id"]
        self.as_user(self.hod_cs)
        self.client.post(f"/api/corrections/{correction_id}/approve/", {"remarks": "ok"}, format="json")
        self.assertGreater(self.client.get("/api/audit-logs/?action=ATTENDANCE_CORRECTION_APPROVED").data["count"], 0)
        self.as_user(self.hod_math)
        self.assertEqual(self.client.get("/api/audit-logs/?action=ATTENDANCE_CORRECTION_APPROVED").data["count"], 0)
        self.as_user(self.t1)
        self.assertEqual(self.client.get("/api/audit-logs/").status_code, 403)
        self.as_user(self.admin)
        self.assertGreater(self.client.get("/api/audit-logs/?action=ATTENDANCE_CORRECTION_APPROVED").data["count"], 0)


class HistoryAndNotificationTests(BaseTestCase):
    """The attendance history list and the user's private notifications."""
    def test_history_only_contains_submitted_attendance_in_scope(self):
        """History shows only submitted marks, limited to what the user may see."""
        self.submit(self.make_lecture(), self.t1)
        draft = self.make_lecture(start="13:00", end="14:00")
        self.as_user(self.t1)
        self.client.post(f"/api/lectures/{draft.id}/mark/", self.marks(self.students[:2]), format="json")
        self.assertEqual(self.client.get("/api/attendance-records/").data["count"], 5)
        self.as_user(self.t2)
        self.assertEqual(self.client.get("/api/attendance-records/").data["count"], 0)
        self.as_user(self.hod_math)
        self.assertEqual(self.client.get("/api/attendance-records/").data["count"], 0)
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.get(f"/api/attendance-records/?status=PRESENT&subject={self.python.id}").data["count"], 3)

    def test_notifications_are_private_and_can_be_marked_read(self):
        """Users see only their own notifications and can mark them as read."""
        Notification.objects.create(user=self.t1, title="Hello", message="For prof A")
        Notification.objects.create(user=self.t2, title="Secret", message="For prof B")
        self.as_user(self.t1)
        data = self.client.get("/api/notifications/").data
        self.assertEqual(data["count"], 1)
        self.assertEqual(self.client.get("/api/notifications/unread-count/").data["count"], 1)
        nid = data["results"][0]["id"]
        self.assertEqual(self.client.post(f"/api/notifications/{nid}/read/").data["is_read"], True)
        self.assertEqual(self.client.get("/api/notifications/unread-count/").data["count"], 0)
        other = Notification.objects.get(user=self.t2)
        self.assertEqual(self.client.post(f"/api/notifications/{other.id}/read/").status_code, 404)


class OldResultsStayTests(BaseTestCase):
    """What was recorded stays visible and unchanged when a student is moved, deactivated or renamed later."""

    def test_a_moved_student_keeps_his_old_marks_in_the_old_class(self):
        lecture = self.make_lecture(self.assign_py, start="09:00", end="09:50")
        self.submit(lecture, self.t1, 2, self.students)
        moved = self.students[0]
        moved.classroom = ClassRoom.objects.create(department=self.cs, name="MCS Part I", semester=1, division="B", academic_year="2026-27")
        moved.save()
        self.as_user(self.hod_cs)
        roster = self.client.get(f"/api/lectures/{lecture.id}/roster/").data
        self.assertIn(moved.id, [s["id"] for s in roster["students"]])
        self.assertEqual(next(s["status"] for s in roster["students"] if s["id"] == moved.id), "PRESENT")
        sheet = self.client.get(f"/api/reports/monthly-sheet/?classroom={self.class_cs.id}&month={self.today:%Y-%m}").data
        self.assertIn(moved.id, [r["student"] for r in sheet["results"]])
        summary = self.client.get(f"/api/reports/student-summary/?classroom={self.class_cs.id}").data["results"]
        self.assertEqual(next(r["total"] for r in summary if r["student"] == moved.id), 1)
        # and he is NOT on a new register of the old class
        new = self.make_lecture(self.assign_py, start="11:00", end="11:50")
        self.assertNotIn(moved.id, [s["id"] for s in self.client.get(f"/api/lectures/{new.id}/roster/").data["students"]])


class AdminHearsAboutChangesTests(BaseTestCase):
    """The Admin is told when a submitted result changes."""
    def test_admin_is_notified_when_a_submitted_result_is_corrected(self):
        """Approving a correction sends the Admin a notification with the details."""
        lecture = self.make_lecture(self.assign_py, start="09:00", end="09:50")
        self.submit(lecture, self.t1, 2, self.students)
        record = AttendanceRecord.objects.filter(lecture=lecture, status="ABSENT").first()
        self.as_user(self.t1)
        cor = self.client.post("/api/corrections/", {"record": record.id, "requested_status": "PRESENT", "reason": "Marked by mistake"}, format="json").data
        self.as_user(self.hod_cs)
        self.client.post(f"/api/corrections/{cor['id']}/approve/", {"remarks": "ok"}, format="json")
        mine = Notification.objects.filter(user=self.admin)
        self.assertEqual(mine.count(), 1)
        self.assertIn("Absent to Present", mine.get().message)
        self.assertIn("Computer Science", mine.get().message)
        self.assertEqual(Notification.objects.filter(user=self.hod_math).count(), 0)  # nobody else is told
