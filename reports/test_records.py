import datetime

from core.models import AuditLog
from core.testing import BaseTestCase

URL = "/api/reports/student-records/"


class StudentRecordsTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.d1 = self.today.replace(day=1)
        # two lectures this month: students 1-3 present in the first, 1-2 in the second
        self.submit(self.make_lecture(self.assign_py, date=self.d1, start="09:00", end="10:00"), self.t1, 3)
        self.submit(self.make_lecture(self.assign_py, date=self.d1, start="10:00", end="11:00"), self.t1, 2)
        self.submit(self.make_lecture(self.assign_math, date=self.d1, start="09:00", end="10:00"), self.t_math, 3, students=self.math_students)

    def rows(self, query="", user=None):
        self.as_user(user or self.admin)
        r = self.client.get(f"{URL}?period=month&month={self.d1:%Y-%m}{query}")
        self.assertEqual(r.status_code, 200, r.data)
        return r.data

    def test_admin_sees_every_department_with_totals(self):
        d = self.rows("&page_size=100")
        self.assertEqual(d["count"], 8)  # 5 CS + 3 Maths
        by = {(r["department"], r["roll_number"]): r for r in d["results"]}
        s1 = by[("Computer Science", "1")]
        self.assertEqual((s1["present"], s1["absent"], s1["total"], s1["percentage"]), (2, 0, 2, 100.0))
        s3 = by[("Computer Science", "3")]
        self.assertEqual((s3["present"], s3["absent"], s3["percentage"], s3["low"]), (1, 1, 50.0, True))
        self.assertEqual(d["summary"]["students"], 8)
        self.assertEqual(d["period"], self.d1.strftime("%B %Y"))

    def test_hod_only_own_department_and_teacher_refused(self):
        d = self.rows("&page_size=100", self.hod_cs)
        self.assertEqual({r["department"] for r in d["results"]}, {"Computer Science"})
        self.as_user(self.t1)
        self.assertEqual(self.client.get(URL).status_code, 403)
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.get(f"{URL}{self.math_students[0].id}/?year={self.d1.year}").status_code, 404)

    def test_filters_paging_and_other_months_are_empty(self):
        self.assertEqual(self.rows(f"&department={self.math.id}")["count"], 3)
        self.assertEqual(self.rows(f"&classroom={self.class_cs.id}&low_only=1")["count"], 3)  # students 3, 4, 5: 50% or 0%
        d = self.rows("&page_size=3&page=2")
        self.assertEqual((len(d["results"]), d["count"]), (3, 8))
        self.assertEqual(self.rows("&search=Math Student 2")["count"], 1)
        self.as_user(self.admin)
        other = (self.d1 - datetime.timedelta(days=1)).strftime("%Y-%m")
        d = self.client.get(f"{URL}?period=month&month={other}").data
        self.assertEqual((d["summary"]["total"], d["summary"]["no_record"]), (0, 8))

    def test_year_and_range_periods(self):
        self.as_user(self.admin)
        y = self.client.get(f"{URL}?period=year&year={self.d1.year}").data
        self.assertEqual(y["summary"]["total"], 5 + 5 + 3)
        r = self.client.get(f"{URL}?period=range&date_from={self.d1}&date_to={self.d1}").data
        self.assertEqual(r["summary"]["total"], 13)
        for bad in ("?period=year&year=abc", "?period=range&date_from=x&date_to=y", "?period=decade", f"?period=range&date_from={self.d1}&date_to=2000-01-01"):
            self.assertEqual(self.client.get(URL + bad).status_code, 400, bad)

    def test_inactive_student_with_marks_stays_in_the_record(self):
        self.students[4].is_active = False
        self.students[4].save()
        self.assertEqual(self.rows()["count"], 8)  # he has marks this month
        other = (self.d1 - datetime.timedelta(days=1)).strftime("%Y-%m")
        self.as_user(self.admin)
        self.assertEqual(self.client.get(f"{URL}?period=month&month={other}").data["count"], 7)

    def test_downloads_csv_and_pdf_and_audit(self):
        self.as_user(self.admin)
        r = self.client.get(f"{URL}?period=month&month={self.d1:%Y-%m}&export=csv")
        t = r.content.decode("utf-8")
        self.assertTrue(t.startswith("﻿Department,Class,Roll No,Name"))
        self.assertEqual(len(t.strip().splitlines()), 9)
        self.assertEqual(self.client.get(f"{URL}?export=pdf").content[:4], b"%PDF")
        self.assertEqual(self.client.get(f"{URL}?export=xml").status_code, 400)
        self.assertTrue(AuditLog.objects.filter(action="DATA_DOWNLOADED", new_value__list="student-records").exists())

    def test_one_student_year_month_by_month(self):
        self.as_user(self.admin)
        r = self.client.get(f"{URL}{self.students[2].id}/?year={self.d1.year}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.data["months"]), 12)
        m = r.data["months"][self.d1.month - 1]
        self.assertEqual((m["present"], m["absent"], m["percentage"]), (1, 1, 50.0))
        self.assertEqual(r.data["subjects"][0]["subject"], "Python")
        self.assertEqual(r.data["total"]["total"], 2)
        self.assertEqual(self.client.get(f"{URL}999999/").status_code, 404)
