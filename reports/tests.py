import csv
import datetime
import io

from attendance.models import AttendanceRecord, Lecture
from core.testing import BaseTestCase

DAILY = "/api/reports/daily/"


class DailyReportTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        # Python: submitted (3 present, 2 absent). DBMS: scheduled today but not submitted.
        self.python_lecture = self.submit(self.make_lecture(self.assign_py, start="09:00", end="10:00"), self.t1, 3)
        self.dbms_lecture = self.make_lecture(self.assign_db, start="10:00", end="11:00")

    def rows(self, query=""):
        response = self.client.get(f"{DAILY}?date={self.today}{query}")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data

    def test_report_shows_counts_percentage_and_pending_status(self):
        self.as_user(self.hod_cs)
        data = self.rows()
        self.assertEqual(len(data["results"]), 2)
        py, db = data["results"]
        self.assertEqual((py["subject"], py["teacher"], py["total_students"]), ("Python", "Prof A Test", 5))
        self.assertEqual((py["present"], py["absent"], py["percentage"], py["status"]), (3, 2, 60.0, "SUBMITTED"))
        self.assertEqual(py["department"], "Computer Science")
        self.assertEqual(py["class_name"], self.class_cs.label)
        self.assertEqual(db["subject"], "DBMS")
        self.assertEqual((db["present"], db["absent"], db["percentage"], db["status"]), (None, None, None, "PENDING"))
        self.assertEqual(db["total_students"], 5)  # students in the class
        self.assertEqual(data["summary"]["submitted"], 1)
        self.assertEqual(data["summary"]["pending"], 1)
        self.assertEqual((data["summary"]["present"], data["summary"]["absent"], data["summary"]["percentage"]), (3, 2, 60.0))

    def test_date_without_any_lecture_has_no_pending_rows(self):
        self.as_user(self.hod_cs)
        empty_day = self.today - datetime.timedelta(days=30)
        data = self.client.get(f"{DAILY}?date={empty_day}").data
        self.assertEqual(data["results"], [])
        self.assertEqual(data["summary"]["pending"], 0)

    def test_future_lectures_are_scheduled_not_pending_and_cancelled_are_not_pending(self):
        tomorrow = self.today + datetime.timedelta(days=1)
        self.make_lecture(self.assign_py, date=tomorrow)
        cancelled = self.make_lecture(self.assign_db, start="12:00", end="13:00")
        self.as_user(self.hod_cs)
        self.client.post(f"/api/lectures/{cancelled.id}/cancel/")
        tomorrow_rows = self.client.get(f"{DAILY}?date={tomorrow}").data
        self.assertEqual([r["status"] for r in tomorrow_rows["results"]], ["SCHEDULED"])
        self.assertEqual(tomorrow_rows["summary"]["pending"], 0)
        today = self.rows()
        self.assertEqual(sorted(r["status"] for r in today["results"]), ["CANCELLED", "PENDING", "SUBMITTED"])
        self.assertEqual(today["summary"]["pending"], 1)

    def test_default_is_today_and_date_range_works(self):
        yesterday = self.today - datetime.timedelta(days=1)
        self.make_lecture(self.assign_py, date=yesterday)
        self.as_user(self.hod_cs)
        self.assertEqual(len(self.client.get(DAILY).data["results"]), 2)
        ranged = self.client.get(f"{DAILY}?date_from={yesterday}&date_to={self.today}").data
        self.assertEqual(len(ranged["results"]), 3)
        self.assertEqual(ranged["results"][0]["date"], str(yesterday))
        self.assertEqual(ranged["results"][0]["status"], "PENDING")  # past and unsubmitted

    def test_filters(self):
        self.as_user(self.hod_cs)
        self.assertEqual(len(self.rows(f"&subject={self.python.id}")["results"]), 1)
        self.assertEqual(len(self.rows(f"&teacher={self.t2.id}")["results"]), 1)
        self.assertEqual(len(self.rows(f"&classroom={self.class_cs.id}")["results"]), 2)
        self.assertEqual(len(self.rows("&semester=2")["results"]), 0)
        self.assertEqual(len(self.rows("&semester=1")["results"]), 2)
        self.assertEqual([r["subject"] for r in self.rows("&status=PENDING")["results"]], ["DBMS"])
        self.assertEqual([r["subject"] for r in self.rows("&status=SUBMITTED")["results"]], ["Python"])

    def test_invalid_parameters_are_rejected(self):
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.get(f"{DAILY}?date=not-a-date").status_code, 400)
        self.assertEqual(self.client.get(f"{DAILY}?date_from=2026-01-01&date_to=2026-12-31").status_code, 400)
        self.assertEqual(self.client.get(f"{DAILY}?date_from=2026-02-01&date_to=2026-01-01").status_code, 400)
        self.assertEqual(self.client.get(f"{DAILY}?subject=abc").status_code, 400)
        self.assertEqual(self.client.get(f"{DAILY}?export=xml").status_code, 400)

    def test_hod_only_sees_own_department_but_admin_sees_all(self):
        self.submit(self.make_lecture(self.assign_math), self.t_math, 2, self.math_students)
        self.as_user(self.hod_cs)
        self.assertEqual({r["department"] for r in self.rows()["results"]}, {"Computer Science"})
        self.assertEqual(len(self.rows(f"&department={self.math.id}")["results"]), 2)  # filter is ignored for HODs
        self.as_user(self.hod_math)
        math_rows = self.rows()["results"]
        self.assertEqual({r["department"] for r in math_rows}, {"Mathematics"})
        self.assertEqual(math_rows[0]["total_students"], 3)
        self.as_user(self.admin)
        self.assertEqual(len(self.rows()["results"]), 3)
        self.assertEqual({r["department"] for r in self.rows(f"&department={self.math.id}")["results"]}, {"Mathematics"})

    def test_teachers_cannot_use_the_hod_report(self):
        self.as_user(self.t1)
        self.assertEqual(self.client.get(DAILY).status_code, 403)

    def test_approved_correction_changes_report_counts(self):
        record = AttendanceRecord.objects.get(lecture=self.python_lecture, student=self.students[4])
        self.as_user(self.t1)
        cid = self.client.post("/api/corrections/", {"record": record.id, "requested_status": "PRESENT", "reason": "Marked wrongly"}, format="json").data["id"]
        self.as_user(self.hod_cs)
        self.client.post(f"/api/corrections/{cid}/approve/", {"remarks": "ok"}, format="json")
        py = self.rows()["results"][0]
        self.assertEqual((py["present"], py["absent"], py["percentage"]), (4, 1, 80.0))

    def test_csv_export(self):
        self.as_user(self.hod_cs)
        response = self.client.get(f"{DAILY}?date={self.today}&export=csv")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response["Content-Type"].startswith("text/csv"))
        self.assertIn("attachment", response["Content-Disposition"])
        rows = list(csv.reader(io.StringIO(response.content.decode())))
        self.assertEqual(rows[0][:2], ["\ufeffDate", "Time"])
        self.assertIn("Attendance %", rows[0])
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1][4], "Python")
        self.assertIn("60.0%", rows[1])
        self.assertEqual(rows[1][-1], "Submitted")
        self.assertEqual(rows[2][-1], "Pending")

    def test_csv_export_neutralises_spreadsheet_formulas(self):
        self.t1.first_name = "=HYPERLINK(1)"
        self.t1.save()
        self.as_user(self.hod_cs)
        text = self.client.get(f"{DAILY}?date={self.today}&export=csv").content.decode()
        self.assertIn("'=HYPERLINK(1)", text)

    def test_pdf_export(self):
        self.as_user(self.hod_cs)
        response = self.client.get(f"{DAILY}?date={self.today}&export=pdf")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertTrue(response.content.startswith(b"%PDF"))
        empty = self.client.get(f"{DAILY}?date={self.today - datetime.timedelta(days=40)}&export=pdf")
        self.assertTrue(empty.content.startswith(b"%PDF"))

    def test_exports_respect_department_isolation(self):
        self.as_user(self.hod_math)
        text = self.client.get(f"{DAILY}?date={self.today}&export=csv").content.decode()
        self.assertNotIn("Python", text)
        self.assertEqual(len(text.strip().splitlines()), 1)  # header only


class StudentSummaryTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.submit(self.make_lecture(self.assign_py, start="09:00", end="10:00"), self.t1, 3)
        self.submit(self.make_lecture(self.assign_py, date=self.today - datetime.timedelta(days=1)), self.t1, 5)

    def test_student_wise_percentages(self):
        self.as_user(self.t1)
        response = self.client.get(f"/api/reports/student-summary/?classroom={self.class_cs.id}&subject={self.python.id}")
        self.assertEqual(response.status_code, 200)
        by_roll = {r["roll_number"]: r for r in response.data["results"]}
        self.assertEqual((by_roll["1"]["present"], by_roll["1"]["total"], by_roll["1"]["percentage"]), (2, 2, 100.0))
        self.assertEqual((by_roll["5"]["present"], by_roll["5"]["absent"], by_roll["5"]["percentage"]), (1, 1, 50.0))

    def test_access_is_limited_to_own_classes(self):
        url = f"/api/reports/student-summary/?classroom={self.class_cs.id}"
        for user, expected in ((self.t_math, 404), (self.hod_math, 404), (self.hod_cs, 200), (self.admin, 200), (self.t2, 200)):
            self.as_user(user)
            self.assertEqual(self.client.get(url).status_code, expected, user.username)
        self.as_user(self.t2)  # teaches DBMS only: sees the class but none of Prof A's lectures
        self.assertTrue(all(r["total"] == 0 for r in self.client.get(url).data["results"]))

    def test_classroom_is_required_and_exports_work(self):
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.get("/api/reports/student-summary/").status_code, 400)
        url = f"/api/reports/student-summary/?classroom={self.class_cs.id}"
        self.assertTrue(self.client.get(url + "&export=csv")["Content-Type"].startswith("text/csv"))
        self.assertTrue(self.client.get(url + "&export=pdf").content.startswith(b"%PDF"))


class DashboardTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.submit(self.make_lecture(self.assign_py), self.t1, 4)  # 4 present, 1 absent
        self.make_lecture(self.assign_db, start="10:00", end="11:00")  # pending today
        self.make_lecture(self.assign_py, date=self.today - datetime.timedelta(days=2))  # overdue

    def test_admin_dashboard_is_calculated_from_data(self):
        self.as_user(self.admin)
        data = self.client.get("/api/dashboard/").data
        cards = data["cards"]
        self.assertEqual((cards["departments"], cards["active_hods"], cards["staff"], cards["students"]), (2, 2, 3, 8))
        self.assertEqual((cards["lectures_today"], cards["submitted_today"], cards["pending_today"]), (2, 1, 1))
        self.assertEqual(cards["overdue_pending"], 1)
        self.assertEqual(cards["attendance_percentage"], 80.0)
        cs_row = next(d for d in data["departments"] if d["code"] == "CS")
        self.assertEqual((cs_row["staff_count"], cs_row["student_count"], cs_row["pending_today"]), (2, 5, 1))
        self.assertEqual(cs_row["hod"], "Hod Cs Test")
        math_row = next(d for d in data["departments"] if d["code"] == "MATH")
        self.assertIsNone(math_row["attendance_percentage"])

    def test_hod_dashboard_is_limited_to_department(self):
        self.as_user(self.hod_cs)
        cards = self.client.get("/api/dashboard/").data["cards"]
        self.assertEqual((cards["staff"], cards["classes"], cards["subjects"], cards["students"]), (2, 1, 2, 5))
        self.assertEqual((cards["submitted_today"], cards["pending_today"], cards["pending_corrections"]), (1, 1, 0))
        self.as_user(self.hod_math)
        cards = self.client.get("/api/dashboard/").data
        self.assertEqual(cards["cards"]["lectures_today"], 0)
        self.assertEqual(cards["subject_wise"], [])

    def test_hod_dashboard_shows_subject_wise_attendance_and_pending_corrections(self):
        record = AttendanceRecord.objects.filter(status="ABSENT").first()
        self.as_user(self.t1)
        self.client.post("/api/corrections/", {"record": record.id, "requested_status": "PRESENT", "reason": "Wrongly marked"}, format="json")
        self.as_user(self.hod_cs)
        data = self.client.get("/api/dashboard/").data
        self.assertEqual(data["cards"]["pending_corrections"], 1)
        self.assertEqual(data["subject_wise"], [{"subject": "Python", "present": 4, "total": 5, "percentage": 80.0}])

    def test_staff_dashboard_shows_own_work(self):
        self.as_user(self.t1)
        data = self.client.get("/api/dashboard/").data
        self.assertEqual(data["cards"]["assignments"], 1)
        self.assertEqual(data["cards"]["students"], 5)
        self.assertEqual(data["cards"]["pending_lectures"], 1)  # the overdue one
        self.assertEqual([l["subject_name"] for l in data["today"]], ["Python"])
        self.as_user(self.t2)
        data = self.client.get("/api/dashboard/").data
        self.assertEqual(data["cards"]["pending_lectures"], 1)
        self.assertEqual(data["cards"]["attendance_percentage"], None)


class MonthlySheetTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.lec = self.submit(self.make_lecture(self.assign_py), self.t1, 2, self.students)
        self.month = self.lec.lecture_date.strftime("%Y-%m")

    def get(self, **params):
        params.setdefault("classroom", self.class_cs.id)
        params.setdefault("month", self.month)
        return self.client.get("/api/reports/monthly-sheet/", params)

    def test_sheet_has_a_column_per_submitted_lecture_and_pa_marks(self):
        self.as_user(self.t1)
        data = self.get().data
        self.assertEqual(len(data["columns"]), 1)
        self.assertEqual(len(data["results"]), len(self.students))
        flat = [v for r in data["results"] for v in r["marks"].values()]
        self.assertEqual(flat.count("P"), 2)
        self.assertEqual(flat.count("A"), len(self.students) - 2)
        self.assertEqual(data["columns"][0]["present"], 2)
        first = data["results"][0]
        self.assertEqual(first["total"], 1)

    def test_drafts_and_other_months_are_left_out(self):
        self.make_lecture(self.assign_py, start="14:00", end="15:00")  # a draft
        self.as_user(self.t1)
        self.assertEqual(len(self.get().data["columns"]), 1)
        self.assertEqual(len(self.get(month="2020-01").data["columns"]), 0)

    def test_teacher_sees_only_own_lectures_hod_sees_all(self):
        self.submit(self.make_lecture(self.assign_db, start="11:00", end="12:00"), self.t2, 3, self.students)
        self.as_user(self.t1)
        self.assertEqual(len(self.get().data["columns"]), 1)
        self.as_user(self.hod_cs)
        self.assertEqual(len(self.get().data["columns"]), 2)
        self.assertEqual(len(self.get(subject=self.python.id).data["columns"]), 1)

    def test_other_department_cannot_read_it(self):
        self.as_user(self.hod_math)
        self.assertEqual(self.get().status_code, 404)
        self.as_user(self.t_math)
        self.assertEqual(self.get().status_code, 404)

    def test_bad_input(self):
        self.as_user(self.hod_cs)
        self.assertEqual(self.get(month="October").status_code, 400)
        self.assertEqual(self.get(month="2026-13").status_code, 400)
        self.assertEqual(self.client.get("/api/reports/monthly-sheet/").status_code, 400)
        self.assertEqual(self.get(subject="x").status_code, 400)

    def test_csv_and_pdf_exports(self):
        self.as_user(self.hod_cs)
        csv_response = self.get(export="csv")
        self.assertEqual(csv_response.status_code, 200)
        lines = csv_response.content.decode().splitlines()
        self.assertTrue(lines[0].startswith("\ufeffRoll,Name,"))
        self.assertEqual(len(lines), 1 + len(self.students))
        pdf = self.get(export="pdf")
        self.assertEqual(pdf.status_code, 200)
        self.assertTrue(pdf.content.startswith(b"%PDF"))
