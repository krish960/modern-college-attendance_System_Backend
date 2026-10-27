"""Tests for electives: leaving a student out of one subject (SubjectExclusion) and its effect on lists, totals and reports."""
from core.models import AuditLog
from core.testing import BaseTestCase
from attendance.models import AttendanceRecord, SubjectExclusion

URL = "/api/subject-students/"


class ElectiveTests(BaseTestCase):
    """A teacher marks which students take an elective, and the rest of the system respects it."""
    def setUp(self):
        """Pick a fifth student to use as the one who is left out."""
        super().setUp()
        self.s5 = self.students[4]

    def leave_out(self, student, takes=False, user=None, assign=None):
        """Helper that posts a take/do-not-take change as the given teacher."""
        a = assign or self.assign_py
        self.as_user(user or self.t1)
        return self.client.post(URL, {"classroom": a.classroom_id, "subject": a.subject_id, "student": student.id, "takes_subject": takes}, format="json")

    def roster_ids(self, lecture, user=None):
        """Helper that returns the student ids shown on a lecture's attendance list."""
        self.as_user(user or self.t1)
        r = self.client.get(f"/api/lectures/{lecture.id}/roster/")
        self.assertEqual(r.status_code, 200)
        return [s["id"] for s in r.data["students"]]

    def test_left_out_student_not_on_that_subjects_list_but_on_others(self):
        """A left-out student disappears from that subject's list only, not from other subjects."""
        self.assertEqual(self.leave_out(self.s5).status_code, 200)
        self.assertNotIn(self.s5.id, self.roster_ids(self.make_lecture(self.assign_py)))
        self.assertIn(self.s5.id, self.roster_ids(self.make_lecture(self.assign_db, start="11:00", end="12:00"), self.t2))

    def test_submit_without_him_works_and_counts_exclude_him(self):
        """A lecture can be submitted without him, and the totals do not count him."""
        self.leave_out(self.s5)
        lec = self.submit(self.make_lecture(self.assign_py), self.t1, 3, students=self.students[:4])
        self.assertEqual((lec.present_total, lec.absent_total), (3, 1))
        self.as_user(self.t1)
        r = self.client.post(f"/api/lectures/{self.make_lecture(self.assign_py, start='12:00', end='13:00').id}/mark/",
                             self.marks([self.s5]), format="json")
        self.assertEqual(r.status_code, 400)

    def test_old_marks_stay_when_left_out_later(self):
        """Marks he already has are kept when he is left out later."""
        lec = self.submit(self.make_lecture(self.assign_py), self.t1, 3)
        self.leave_out(self.s5)
        self.assertTrue(AttendanceRecord.objects.filter(lecture=lec, student=self.s5).exists())
        self.assertIn(self.s5.id, self.roster_ids(lec))

    def test_summary_and_sheet_hide_him_only_for_that_subject(self):
        """The student summary and monthly sheet skip him for that subject only."""
        self.leave_out(self.s5)
        self.as_user(self.hod_cs)
        rows = self.client.get(f"/api/reports/student-summary/?classroom={self.class_cs.id}&subject={self.python.id}").data
        rows = rows["results"] if isinstance(rows, dict) else rows
        self.assertNotIn(self.s5.id, [r["student"] for r in rows])
        rows = self.client.get(f"/api/reports/student-summary/?classroom={self.class_cs.id}&subject={self.dbms.id}").data
        rows = rows["results"] if isinstance(rows, dict) else rows
        self.assertIn(self.s5.id, [r["student"] for r in rows])
        sheet = self.client.get(f"/api/reports/monthly-sheet/?classroom={self.class_cs.id}&subject={self.python.id}").data
        self.assertNotIn(self.s5.id, [r["student"] for r in sheet["results"]])

    def test_daily_report_pending_total_ignores_him(self):
        """The daily report's pending-student count does not include him."""
        self.leave_out(self.s5)
        self.make_lecture(self.assign_py)
        self.as_user(self.hod_cs)
        rows = self.client.get(f"/api/reports/daily/?date={self.today}").data["results"]
        self.assertEqual(rows[0]["total_students"], 4)

    def test_add_back_and_audit(self):
        """Adding him back works, and both changes are written to the audit log."""
        self.leave_out(self.s5)
        self.leave_out(self.s5, takes=True)
        self.assertFalse(SubjectExclusion.objects.exists())
        self.assertIn(self.s5.id, self.roster_ids(self.make_lecture(self.assign_py)))
        self.assertTrue(AuditLog.objects.filter(action="STUDENT_LEFT_OUT_OF_SUBJECT").exists())
        self.assertTrue(AuditLog.objects.filter(action="STUDENT_ADDED_TO_SUBJECT").exists())

    def test_repeat_is_harmless(self):
        """Sending the same change twice is safe and leaves just one exclusion row."""
        self.leave_out(self.s5)
        self.assertEqual(self.leave_out(self.s5).status_code, 200)
        self.assertEqual(SubjectExclusion.objects.count(), 1)

    def test_who_may_change_it(self):
        # another teacher of the same class who does not teach Python
        """Only the teacher assigned to the subject in that class may change the list."""
        self.assertEqual(self.leave_out(self.s5, user=self.t2).status_code, 403)
        # a teacher of another department
        self.assertEqual(self.leave_out(self.s5, user=self.t_math).status_code, 403)
        # HOD of another department
        self.assertEqual(self.leave_out(self.s5, user=self.hod_math).status_code, 403)
        # admin is read-only here
        self.assertEqual(self.leave_out(self.s5, user=self.admin).status_code, 403)
        # the HOD of the department does not manage it either: only the teacher of the subject
        self.assertEqual(self.leave_out(self.s5, user=self.hod_cs).status_code, 403)
        self.assertEqual(SubjectExclusion.objects.count(), 0)
        self.assertEqual(self.leave_out(self.s5, user=self.t1).status_code, 200)
        self.assertEqual(SubjectExclusion.objects.count(), 1)

    def test_bad_input(self):
        """A non-boolean answer, a student of another class or a non-numeric class id all get a 400 error."""
        self.as_user(self.t1)
        a = self.assign_py
        for body in (
            {"classroom": a.classroom_id, "subject": a.subject_id, "student": self.s5.id, "takes_subject": "no"},
            {"classroom": a.classroom_id, "subject": a.subject_id, "student": self.math_students[0].id, "takes_subject": False},
            {"classroom": "x", "subject": a.subject_id, "student": self.s5.id, "takes_subject": False},
        ):
            self.assertEqual(self.client.post(URL, body, format="json").status_code, 400, body)
        self.assertEqual(self.client.get(f"{URL}?classroom={a.classroom_id}&subject={a.subject_id}").status_code, 200)

    def test_listing_shows_who_takes_it(self):
        """The list screen shows each student with true/false for taking the subject."""
        self.leave_out(self.s5)
        r = self.client.get(f"{URL}?classroom={self.assign_py.classroom_id}&subject={self.assign_py.subject_id}")
        self.assertEqual((r.data["taking"], r.data["not_taking"]), (4, 1))
        self.assertFalse([s for s in r.data["students"] if s["id"] == self.s5.id][0]["takes_subject"])
