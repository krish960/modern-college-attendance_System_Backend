import datetime

from .testing import PASSWORD, BaseTestCase


class CrossDepartmentIsolationTests(BaseTestCase):
    """A CS user must never read or change Mathematics data (and vice-versa) on any endpoint."""

    def setUp(self):
        super().setUp()
        self.math_lecture = self.submit(self.make_lecture(self.assign_math), self.t_math, 2, self.math_students)
        self.math_record = self.math_lecture.records.first()
        self.as_user(self.t_math)
        self.math_correction = self.client.post(
            "/api/corrections/", {"record": self.math_record.id, "requested_status": "PRESENT" if self.math_record.status == "ABSENT" else "ABSENT",
                                  "reason": "Marked wrongly"}, format="json").data["id"]

    def detail_urls(self):
        return [
            f"/api/departments/{self.math.id}/", f"/api/classes/{self.class_math.id}/",
            f"/api/subjects/{self.algebra.id}/", f"/api/assignments/{self.assign_math.id}/",
            f"/api/students/{self.math_students[0].id}/", f"/api/lectures/{self.math_lecture.id}/",
            f"/api/lectures/{self.math_lecture.id}/roster/", f"/api/attendance-records/{self.math_record.id}/",
            f"/api/corrections/{self.math_correction}/", f"/api/staff/{self.t_math.id}/",
        ]

    def test_cs_hod_gets_404_for_every_math_object(self):
        self.as_user(self.hod_cs)
        for url in self.detail_urls():
            if url.startswith("/api/staff/"):
                self.assertEqual(self.client.get(url).status_code, 404, url)
            else:
                self.assertEqual(self.client.get(url).status_code, 404, url)

    def test_cs_staff_gets_404_for_every_math_object(self):
        self.as_user(self.t1)
        for url in self.detail_urls():
            self.assertIn(self.client.get(url).status_code, (403, 404), url)
            self.assertNotEqual(self.client.get(url).status_code, 200, url)

    def test_list_endpoints_never_leak_other_department_rows(self):
        math_ids = {
            "classes": {self.class_math.id}, "subjects": {self.algebra.id}, "assignments": {self.assign_math.id},
            "students": {s.id for s in self.math_students}, "lectures": {self.math_lecture.id},
            "attendance-records": set(self.math_lecture.records.values_list("id", flat=True)),
            "corrections": {self.math_correction}, "staff": {self.t_math.id},
        }
        for user in (self.hod_cs, self.t1):
            self.as_user(user)
            for endpoint, forbidden in math_ids.items():
                response = self.client.get(f"/api/{endpoint}/?page_size=200")
                if response.status_code != 200:  # e.g. staff list is HOD-only
                    self.assertEqual(response.status_code, 403, endpoint)
                    continue
                ids = {row["id"] for row in response.data["results"]}
                self.assertFalse(ids & forbidden, f"{user.username} saw math rows on /{endpoint}/")

    def test_cs_hod_cannot_change_math_data(self):
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.patch(f"/api/staff/{self.t_math.id}/", {"first_name": "X"}, format="json").status_code, 404)
        self.assertEqual(self.client.post(f"/api/staff/{self.t_math.id}/deactivate/").status_code, 404)
        self.assertEqual(self.client.patch(f"/api/classes/{self.class_math.id}/", {"name": "X"}, format="json").status_code, 404)
        self.assertEqual(self.client.patch(f"/api/students/{self.math_students[0].id}/", {"name": "X"}, format="json").status_code, 404)
        self.assertEqual(self.client.post(f"/api/corrections/{self.math_correction}/approve/", {"remarks": "ok"}, format="json").status_code, 404)
        self.assertEqual(self.client.post(f"/api/lectures/{self.math_lecture.id}/cancel/").status_code, 404)

    def test_dashboard_numbers_do_not_include_other_department(self):
        self.as_user(self.hod_cs)
        cards = self.client.get("/api/dashboard/").data["cards"]
        self.assertEqual(cards["students"], 5)
        self.assertEqual(cards["lectures_today"], 0)
        self.assertEqual(cards["pending_corrections"], 0)


class ScaleAndHardeningTests(BaseTestCase):
    """Behaviour added for large, many-department colleges (stored totals, fair throttling, safer edits)."""

    # --- stored lecture totals -------------------------------------------------
    def test_submit_stores_totals_equal_to_the_records(self):
        lecture = self.submit(self.make_lecture(), self.t1, present_count=3)
        self.assertEqual((lecture.present_total, lecture.absent_total), (3, 2))

    def test_approved_correction_moves_the_stored_totals(self):
        from attendance.models import AttendanceRecord, Lecture
        lecture = self.submit(self.make_lecture(), self.t1, present_count=3)
        record = AttendanceRecord.objects.get(lecture=lecture, student=self.students[4])  # absent
        self.as_user(self.t1)
        created = self.client.post("/api/corrections/", {"record": record.id, "requested_status": "PRESENT",
                                                          "reason": "Was present in class"}, format="json")
        self.assertEqual(created.status_code, 201, created.data)
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.post(f"/api/corrections/{created.data['id']}/approve/",
                                          {"remarks": "ok"}, format="json").status_code, 200)
        lecture = Lecture.objects.get(pk=lecture.pk)
        self.assertEqual((lecture.present_total, lecture.absent_total), (4, 1))
        self.assertEqual(AttendanceRecord.objects.filter(lecture=lecture, status="PRESENT").count(), 4)

    def test_recount_command_repairs_totals(self):
        from django.core.management import call_command
        from attendance.models import Lecture
        lecture = self.submit(self.make_lecture(), self.t1, present_count=2)
        Lecture.objects.filter(pk=lecture.pk).update(present_total=99, absent_total=99)
        call_command("recount_attendance", verbosity=0)
        lecture.refresh_from_db()
        self.assertEqual((lecture.present_total, lecture.absent_total), (2, 3))

    def test_dashboard_and_report_use_the_same_totals(self):
        self.submit(self.make_lecture(), self.t1, present_count=3)
        self.as_user(self.hod_cs)
        card = self.client.get("/api/dashboard/").data["cards"]["attendance_percentage"]
        report = self.client.get(f"/api/reports/daily/?date={self.today}").data["summary"]["percentage"]
        self.assertEqual((card, report), (60.0, 60.0))

    # --- submitted attendance cannot be touched from code either ----------------
    def test_submitted_record_cannot_be_saved_or_deleted_directly(self):
        from django.core.exceptions import ValidationError
        from attendance.models import AttendanceRecord
        lecture = self.submit(self.make_lecture(), self.t1)
        record = AttendanceRecord.objects.filter(lecture=lecture).first()
        record.status = "ABSENT" if record.status == "PRESENT" else "PRESENT"
        with self.assertRaises(ValidationError):
            record.save()
        with self.assertRaises(ValidationError):
            record.delete()

    # --- pending ignores lectures nobody can submit -----------------------------
    def test_draft_of_a_deactivated_teacher_is_not_pending(self):
        self.make_lecture(self.assign_py, start="09:00", end="10:00")
        self.make_lecture(self.assign_db, start="10:30", end="11:30")
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.get("/api/dashboard/").data["cards"]["pending_today"], 2)
        self.t2.is_active = False
        self.t2.save()
        self.assertEqual(self.client.get("/api/dashboard/").data["cards"]["pending_today"], 1)
        rows = self.client.get(f"/api/reports/daily/?date={self.today}").data
        self.assertEqual([r["subject"] for r in rows["results"]], ["Python"])
        self.assertEqual(rows["summary"]["pending"], 1)

    def test_generate_skips_deactivated_teachers(self):
        from attendance.models import TimetableSlot
        monday = self.today - datetime.timedelta(days=self.today.weekday())
        TimetableSlot.objects.create(assignment=self.assign_py, weekday=0, start_time=datetime.time(9), end_time=datetime.time(10))
        TimetableSlot.objects.create(assignment=self.assign_db, weekday=0, start_time=datetime.time(10, 15), end_time=datetime.time(11, 15))
        self.t2.is_active = False
        self.t2.save()
        self.as_user(self.hod_cs)
        response = self.client.post("/api/timetable/generate/", {"date_from": str(monday), "date_to": str(monday)}, format="json")
        self.assertEqual(response.data["created"], 1)

    # --- timetable clashes --------------------------------------------------------
    def test_timetable_rejects_a_clashing_teacher_or_class(self):
        self.as_user(self.hod_cs)
        slot = {"assignment": self.assign_py.id, "weekday": 1, "start_time": "09:00", "end_time": "10:00"}
        self.assertEqual(self.client.post("/api/timetable/", slot, format="json").status_code, 201)
        # same class (CS), different teacher, overlapping time -> clash
        other_teacher = dict(slot, assignment=self.assign_db.id, start_time="09:30", end_time="10:30")
        self.assertEqual(self.client.post("/api/timetable/", other_teacher, format="json").status_code, 400)
        # back-to-back is fine
        after = dict(slot, assignment=self.assign_db.id, start_time="10:00", end_time="11:00")
        self.assertEqual(self.client.post("/api/timetable/", after, format="json").status_code, 201)

    # --- accounts -------------------------------------------------------------------
    def test_usernames_are_unique_ignoring_case(self):
        self.as_user(self.hod_cs)
        body = {"username": "PROF_A", "first_name": "X", "last_name": "Y", "password": "Another#Pass2026"}
        response = self.client.post("/api/staff/", body, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("username", response.data)

    def test_email_on_profile_must_be_unique(self):
        self.t2.email = "taken@college.example"
        self.t2.save()
        self.as_user(self.t1)
        response = self.client.patch("/api/auth/me/", {"email": "TAKEN@college.example"}, format="json")
        self.assertEqual(response.status_code, 400)

    def test_login_limit_is_per_username_so_one_user_cannot_lock_out_others(self):
        for _ in range(10):
            self.client.post("/api/auth/login/", {"username": "prof_a", "password": "bad"}, format="json")
        blocked = self.client.post("/api/auth/login/", {"username": "prof_a", "password": "bad"}, format="json")
        self.assertEqual(blocked.status_code, 429)
        ok = self.client.post("/api/auth/login/", {"username": "prof_b", "password": PASSWORD}, format="json")
        self.assertEqual(ok.status_code, 200)

    # --- students -------------------------------------------------------------------
    def test_only_the_hod_can_deactivate_a_student(self):
        student = self.students[0]
        self.as_user(self.t1)
        denied = self.client.patch(f"/api/students/{student.id}/", {"is_active": False}, format="json")
        self.assertEqual(denied.status_code, 400)
        self.as_user(self.hod_cs)
        allowed = self.client.patch(f"/api/students/{student.id}/", {"is_active": False}, format="json")
        self.assertEqual(allowed.status_code, 200)
        self.assertFalse(allowed.data["is_active"])

    # --- lectures -------------------------------------------------------------------
    def test_teacher_cannot_add_a_very_old_lecture(self):
        self.as_user(self.t1)
        old = self.today - datetime.timedelta(days=90)
        body = {"teacher_subject_assignment": self.assign_py.id, "lecture_date": str(old),
                "start_time": "09:00", "end_time": "10:00"}
        self.assertEqual(self.client.post("/api/lectures/", body, format="json").status_code, 400)

    def test_correction_for_someone_elses_record_looks_like_a_missing_record(self):
        from attendance.models import AttendanceRecord
        lecture = self.submit(self.make_lecture(self.assign_py), self.t1)
        record = AttendanceRecord.objects.filter(lecture=lecture).first()
        self.as_user(self.t2)
        mine = self.client.post("/api/corrections/", {"record": 999999, "requested_status": "ABSENT", "reason": "No such"}, format="json")
        theirs = self.client.post("/api/corrections/", {"record": record.id, "requested_status": "ABSENT", "reason": "Not mine"}, format="json")
        self.assertEqual(mine.status_code, theirs.status_code)

    # --- misc -----------------------------------------------------------------------
    def test_report_refuses_an_unreasonably_large_result(self):
        from unittest import mock
        from reports import services
        self.make_lecture(self.assign_py, start="09:00", end="10:00")
        self.make_lecture(self.assign_db, start="10:30", end="11:30")
        self.as_user(self.hod_cs)
        with mock.patch.dict(services.MAX_ROWS, {"json": 1}):
            response = self.client.get(f"/api/reports/daily/?date={self.today}")
        self.assertEqual(response.status_code, 400)

    def test_django_admin_site_is_not_exposed(self):
        self.assertEqual(self.client.get("/admin/").status_code, 404)

    def test_department_counts_are_not_multiplied_by_joins(self):
        # CS has 4 users, 5 students, 1 class: the counts must be 2 staff / 5 students / 1 class.
        self.as_user(self.admin)
        rows = {d["code"]: d for d in self.client.get("/api/departments/").data["results"]}
        self.assertEqual((rows["CS"]["staff_count"], rows["CS"]["student_count"], rows["CS"]["class_count"]), (2, 5, 1))


class LowAttendanceTests(BaseTestCase):
    def three_lectures(self, present_count=3):
        for start, end in (("08:00", "09:00"), ("09:00", "10:00"), ("10:00", "11:00")):
            self.submit(self.make_lecture(start=start, end=end), self.t1, present_count=present_count)

    def test_summary_flags_students_below_the_limit(self):
        self.three_lectures()  # students 1-3 always present, 4-5 never
        self.as_user(self.hod_cs)
        data = self.client.get(f"/api/reports/student-summary/?classroom={self.class_cs.id}").data
        self.assertEqual(data["threshold"], 75)
        low = {r["roll_number"] for r in data["results"] if r["low"]}
        self.assertEqual(low, {"4", "5"})
        only = self.client.get(f"/api/reports/student-summary/?classroom={self.class_cs.id}&low_only=true").data
        self.assertEqual({r["roll_number"] for r in only["results"]}, {"4", "5"})

    def test_student_with_no_lectures_is_not_flagged(self):
        self.as_user(self.hod_cs)
        rows = self.client.get(f"/api/reports/student-summary/?classroom={self.class_cs.id}").data["results"]
        self.assertTrue(all(r["percentage"] is None and not r["low"] for r in rows))

    def test_hod_dashboard_lists_low_students_worst_first(self):
        self.three_lectures()
        self.as_user(self.hod_cs)
        data = self.client.get("/api/dashboard/").data
        self.assertEqual(data["cards"]["low_attendance_students"], 2)
        students = data["low_attendance"]["students"]
        self.assertEqual({s["roll_number"] for s in students}, {"4", "5"})
        self.assertEqual(students[0]["percentage"], 0.0)
        self.assertEqual(students[0]["total"], 3)

    def test_dashboard_needs_the_minimum_number_of_lectures(self):
        for start, end in (("08:00", "09:00"), ("09:00", "10:00")):  # only 2 lectures
            self.submit(self.make_lecture(start=start, end=end), self.t1, present_count=3)
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.get("/api/dashboard/").data["cards"]["low_attendance_students"], 0)

    def test_low_students_of_other_departments_are_never_listed(self):
        self.three_lectures()
        self.as_user(self.hod_math)
        data = self.client.get("/api/dashboard/").data
        self.assertEqual(data["cards"]["low_attendance_students"], 0)
        self.assertEqual(data["low_attendance"]["students"], [])


class TrendTests(BaseTestCase):
    def test_trend_has_one_point_per_day_with_real_percentages(self):
        self.submit(self.make_lecture(), self.t1, present_count=3)  # 3 of 5 = 60 %
        self.as_user(self.hod_cs)
        trend = self.client.get("/api/dashboard/").data["trend"]
        self.assertEqual(len(trend), 14)
        self.assertEqual(trend[-1]["date"], str(self.today))
        self.assertEqual((trend[-1]["percentage"], trend[-1]["present"], trend[-1]["total"]), (60.0, 3, 5))
        self.assertTrue(all(p["percentage"] is None for p in trend[:-1]))

    def test_admin_trend_covers_all_departments_and_hod_trend_only_its_own(self):
        self.submit(self.make_lecture(), self.t1, present_count=5)
        self.submit(self.make_lecture(self.assign_math), self.t_math, present_count=0, students=self.math_students)
        self.as_user(self.admin)
        last = self.client.get("/api/dashboard/").data["trend"][-1]
        self.assertEqual((last["present"], last["total"]), (5, 8))
        self.as_user(self.hod_math)
        last = self.client.get("/api/dashboard/").data["trend"][-1]
        self.assertEqual((last["present"], last["total"]), (0, 3))


from core.models import AuditLog  # noqa: E402


class CsvImportTests(BaseTestCase):
    URL = "/api/students/import/"

    def upload(self, user, text, classroom=None, dry_run=False, name="students.csv"):
        from django.core.files.uploadedfile import SimpleUploadedFile
        self.as_user(user)
        data = {"classroom": (classroom or self.class_cs).id, "file": SimpleUploadedFile(name, text.encode("utf-8-sig"), "text/csv")}
        if dry_run:
            data["dry_run"] = "true"
        return self.client.post(self.URL, data, format="multipart")

    def test_hod_imports_students_and_skips_existing_and_repeated_rolls(self):
        from students.models import Student
        csv_text = "roll_number,name\n1,Already Here\n101,Amit Jadhav\n102,  Priya   Shinde \n101,Repeated\n103,Rohan Kulkarni\n"
        response = self.upload(self.hod_cs, csv_text)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["created"], 3)
        self.assertEqual(len(response.data["skipped"]), 2)
        student = Student.objects.get(classroom=self.class_cs, roll_number="102")
        self.assertEqual((student.name, student.created_by_id, student.department_id), ("Priya Shinde", self.hod_cs.id, self.hod_cs.department_id))
        self.assertEqual(Student.objects.get(classroom=self.class_cs, roll_number="1").name, "Student 1")  # untouched

    def test_member_id_column_links_the_id_cards(self):
        from students.models import Student
        # student 1 already exists without a card; 5 has one; "079968" keeps its leading zero (text) and spaces are removed
        Student.objects.filter(pk=self.students[4].pk).update(card_code="HAVE5")
        csv_text = ("roll_number,name,Member ID\n1,Student 1,018822\n5,Student 5,CHANGED\n101,Amit Jadhav,079 968\n"
                    "102,Priya Shinde,079968\n103,Rohan Kulkarni,HAVE5\n104,No Card,\n")
        r = self.upload(self.hod_cs, csv_text, dry_run=True)
        self.assertEqual((r.data["created"], r.data["cards_linked"], len(r.data["card_problems"])), (4, 2, 2))
        self.assertEqual(Student.objects.filter(card_code="079968").count(), 0)  # a dry run saves nothing
        r = self.upload(self.hod_cs, csv_text)
        self.assertEqual((r.data["created"], r.data["cards_linked"]), (4, 2))
        card = lambda roll: Student.objects.get(classroom=self.class_cs, roll_number=roll).card_code
        self.assertEqual(card("1"), "018822")   # added to the old student, nothing else changed
        self.assertEqual(card("5"), "HAVE5")    # a card already there is never overwritten
        self.assertEqual(card("101"), "079968")
        self.assertEqual((card("102"), card("103"), card("104")), ("", "", ""))  # clashes: student made, card not linked
        reasons = {p["roll_number"]: p["reason"] for p in r.data["card_problems"]}
        self.assertEqual(set(reasons), {"102", "103"})
        self.assertTrue(AuditLog.objects.filter(action="STUDENTS_IMPORTED", new_value__cards=2).exists())
        # scanning the card now finds the student
        self.as_user(self.hod_cs)
        self.assertTrue(self.client.post("/api/scan/identify/", {"code": "079968"}, format="json").data["student"])

    def test_dry_run_saves_nothing(self):
        from students.models import Student
        before = Student.objects.count()
        response = self.upload(self.hod_cs, "roll_number,name\n101,Amit Jadhav\n", dry_run=True)
        self.assertEqual((response.status_code, response.data["created"], response.data["dry_run"]), (200, 1, True))
        self.assertEqual(Student.objects.count(), before)

    def test_invalid_rows_are_reported_not_imported(self):
        from students.models import Student
        response = self.upload(self.hod_cs, "roll_number,name\n,No Roll\n201,X\n202,Good Name\n")
        self.assertEqual(response.data["created"], 1)
        self.assertEqual([r["line"] for r in response.data["invalid"]], [2, 3])
        self.assertFalse(Student.objects.filter(roll_number="201").exists())

    def test_import_applies_the_same_text_rules_as_the_form(self):
        from students.models import Student
        csv_text = "roll_number,name,Member ID\n401,Rahul 123,\n402,<b>Bold</b>,\n!!,Good Name,\n403,Good Name,<X>\n404,Sneha Patil,ab-12\n"
        r = self.upload(self.hod_cs, csv_text)
        self.assertEqual(r.data["created"], 1)
        self.assertEqual(len(r.data["invalid"]), 4)
        self.assertEqual(Student.objects.get(classroom=self.class_cs, roll_number="404").card_code, "AB-12")

    def test_headers_in_any_order_and_semicolon_files_work(self):
        response = self.upload(self.hod_cs, "Name;Roll No\nAmit Jadhav;301\n")
        self.assertEqual((response.status_code, response.data["created"]), (200, 1))

    def test_missing_columns_and_empty_files_are_rejected(self):
        self.assertEqual(self.upload(self.hod_cs, "a,b\n1,2\n").status_code, 400)
        self.assertEqual(self.upload(self.hod_cs, "").status_code, 400)

    def test_too_many_rows_are_rejected(self):
        rows = "roll_number,name\n" + "".join(f"{i},Student Number {i}\n" for i in range(1000, 3005))
        self.assertEqual(self.upload(self.hod_cs, rows).status_code, 400)

    def test_teacher_can_import_only_into_own_class(self):
        ok = self.upload(self.t1, "roll_number,name\n401,Amit Jadhav\n")
        self.assertEqual(ok.status_code, 200, ok.data)
        other = self.upload(self.t1, "roll_number,name\n402,Amit Jadhav\n", classroom=self.class_math)
        self.assertEqual(other.status_code, 400)

    def test_other_department_hod_and_admin_cannot_import(self):
        self.assertEqual(self.upload(self.hod_math, "roll_number,name\n501,Amit Jadhav\n").status_code, 400)
        self.assertEqual(self.upload(self.admin, "roll_number,name\n501,Amit Jadhav\n").status_code, 403)

    def test_import_is_written_to_the_audit_log(self):
        from core.models import AuditLog
        self.upload(self.hod_cs, "roll_number,name\n601,Amit Jadhav\n")
        self.assertTrue(AuditLog.objects.filter(action="STUDENTS_IMPORTED", department=self.hod_cs.department).exists())


class RoleSpecificLoginTests(BaseTestCase):
    def login(self, username, role=None, password=None):
        from rest_framework.test import APIClient
        from .testing import PASSWORD
        body = {"username": username, "password": password or PASSWORD}
        if role:
            body["role"] = role
        return APIClient().post("/api/auth/login/", body, format="json")

    def test_each_account_signs_in_only_on_its_own_role_page(self):
        for username, role in (("admin", "ADMIN"), ("hod_cs", "HOD"), ("prof_a", "STAFF")):
            self.assertEqual(self.login(username, role).status_code, 200, username)
        wrong = self.login("prof_a", "HOD")
        self.assertEqual(wrong.status_code, 401)
        self.assertIn("not a", wrong.data["detail"])
        self.assertEqual(self.login("hod_cs", "STAFF").status_code, 401)
        self.assertEqual(self.login("admin", "HOD").status_code, 401)

    def test_wrong_password_is_reported_before_the_role_so_roles_cannot_be_probed(self):
        wrong_pw_wrong_role = self.login("prof_a", "HOD", password="nope-nope")
        wrong_pw_right_role = self.login("prof_a", "STAFF", password="nope-nope")
        self.assertEqual(wrong_pw_wrong_role.data["detail"], wrong_pw_right_role.data["detail"])

    def test_unknown_role_value_is_rejected(self):
        self.assertEqual(self.login("prof_a", "STUDENT").status_code, 400)


class SearchTests(BaseTestCase):
    def test_lecture_search_finds_by_subject_class_and_teacher_name(self):
        self.make_lecture(self.assign_py, start="09:00", end="10:00")
        self.make_lecture(self.assign_db, start="10:30", end="11:30")
        self.as_user(self.hod_cs)
        def names(term):
            return {r["subject_name"] for r in self.client.get("/api/lectures/", {"search": term}).data["results"]}
        self.assertEqual(names("python"), {self.python.name})
        self.assertEqual(names(self.t2.first_name), {self.assign_db.subject.name})
        self.assertEqual(names("zzz-nothing"), set())

    def test_lecture_search_never_crosses_departments(self):
        self.make_lecture(self.assign_math)
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.get("/api/lectures/", {"search": self.assign_math.subject.name}).data["count"], 0)

    def test_timetable_search(self):
        from attendance.models import TimetableSlot
        TimetableSlot.objects.create(assignment=self.assign_py, weekday=0, start_time=datetime.time(9), end_time=datetime.time(10))
        TimetableSlot.objects.create(assignment=self.assign_db, weekday=1, start_time=datetime.time(9), end_time=datetime.time(10))
        self.as_user(self.hod_cs)
        found = self.client.get("/api/timetable/", {"search": "python"}).data["results"]
        self.assertEqual([r["subject_name"] for r in found], [self.python.name])

    def test_notification_search_only_inside_own_notifications(self):
        from core.notifications import notify
        notify(self.t1, "Correction approved", "Student Amit")
        notify(self.t2, "Correction approved", "Student Amit")
        self.as_user(self.t1)
        self.assertEqual(self.client.get("/api/notifications/", {"search": "amit"}).data["count"], 1)
        self.assertEqual(self.client.get("/api/notifications/", {"search": "nothing-here"}).data["count"], 0)

    def test_daily_report_search_filters_rows_and_summary(self):
        self.submit(self.make_lecture(self.assign_py, start="09:00", end="10:00"), self.t1, present_count=5)
        self.submit(self.make_lecture(self.assign_db, start="10:30", end="11:30"), self.t2, present_count=0)
        self.as_user(self.hod_cs)
        data = self.client.get("/api/reports/daily/", {"date": str(self.today), "search": self.t1.first_name}).data
        self.assertEqual([r["subject"] for r in data["results"]], [self.python.name])
        self.assertEqual(data["summary"]["percentage"], 100.0)


class IdCardTests(BaseTestCase):
    def setUp(self):
        super().setUp()
        self.s1, self.s2 = self.students[0], self.students[1]

    def test_teacher_of_the_class_links_a_card_and_it_is_normalised(self):
        self.as_user(self.t1)
        r = self.client.patch(f"/api/students/{self.s1.id}/", {"card_code": " mcs-2025 011 "}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["card_code"], "MCS-2025011")

    def test_one_card_cannot_belong_to_two_students(self):
        self.as_user(self.t1)
        self.client.patch(f"/api/students/{self.s1.id}/", {"card_code": "ABC1"}, format="json")
        r = self.client.patch(f"/api/students/{self.s2.id}/", {"card_code": "abc1"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertIn("card_code", r.data)
        self.assertIn("already registered", str(r.data["card_code"]))
        self.assertIn(self.s1.name, str(r.data["card_code"]))

    def test_clearing_a_card_and_relinking_the_same_student_is_allowed(self):
        self.as_user(self.t1)
        self.client.patch(f"/api/students/{self.s1.id}/", {"card_code": "ABC1"}, format="json")
        self.assertEqual(self.client.patch(f"/api/students/{self.s1.id}/", {"card_code": "ABC1"}, format="json").status_code, 200)
        self.assertEqual(self.client.patch(f"/api/students/{self.s1.id}/", {"card_code": ""}, format="json").status_code, 200)
        self.assertEqual(self.client.patch(f"/api/students/{self.s2.id}/", {"card_code": ""}, format="json").status_code, 200)  # blanks never clash

    def test_other_department_cannot_touch_the_card(self):
        self.as_user(self.t_math)
        self.assertEqual(self.client.patch(f"/api/students/{self.s1.id}/", {"card_code": "ZZ9"}, format="json").status_code, 404)

    def test_roster_includes_the_card_code_for_scanning(self):
        self.as_user(self.t1)
        self.client.patch(f"/api/students/{self.s1.id}/", {"card_code": "ABC1"}, format="json")
        lecture = self.make_lecture(self.assign_py)
        roster = self.client.get(f"/api/lectures/{lecture.id}/roster/").data["students"]
        self.assertEqual({s["id"]: s["card_code"] for s in roster}[self.s1.id], "ABC1")

    def test_register_with_card_rejects_a_card_already_in_use(self):
        self.as_user(self.t1)
        self.client.patch(f"/api/students/{self.s1.id}/", {"card_code": "ABC1"}, format="json")
        r = self.client.post("/api/students/", {"roll_number": "991", "name": "New Student", "classroom": self.class_cs.id, "card_code": "ABC1"}, format="json")
        self.assertEqual(r.status_code, 400)


class StudentRemovalTests(BaseTestCase):
    def ask(self, student=None, reason="Registered by mistake"):
        self.as_user(self.t1)
        return self.client.post("/api/student-removals/", {"student": (student or self.students[0]).id, "reason": reason}, format="json")

    def test_teacher_cannot_hard_delete_a_student(self):
        self.as_user(self.t1)
        self.assertEqual(self.client.delete(f"/api/students/{self.students[0].id}/").status_code, 405)
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.delete(f"/api/students/{self.students[0].id}/").status_code, 405)

    def test_request_notifies_hod_and_waits_for_approval(self):
        from core.models import Notification
        r = self.ask()
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["status"], "PENDING")
        self.students[0].refresh_from_db()
        self.assertTrue(self.students[0].is_active)  # nothing changes until the HOD decides
        self.assertTrue(Notification.objects.filter(user=self.hod_cs, title="Student removal request").exists())

    def test_hod_approves_student_becomes_inactive_history_kept(self):
        self.submit(self.make_lecture(self.assign_py), self.t1, 2, self.students)
        rid = self.ask().data["id"]
        self.as_user(self.hod_cs)
        r = self.client.post(f"/api/student-removals/{rid}/approve/", {"remarks": "Left the course"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.students[0].refresh_from_db()
        self.assertFalse(self.students[0].is_active)
        self.assertTrue(self.students[0].attendance_records.exists())
        from core.models import AuditLog, Notification
        self.assertTrue(AuditLog.objects.filter(action="STUDENT_REMOVAL_APPROVED").exists())
        self.assertTrue(Notification.objects.filter(user=self.t1, title="Removal approved").exists())

    def test_hod_rejects_student_stays_active(self):
        rid = self.ask().data["id"]
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.post(f"/api/student-removals/{rid}/reject/", {"remarks": "Still enrolled"}, format="json").status_code, 200)
        self.students[0].refresh_from_db()
        self.assertTrue(self.students[0].is_active)
        self.as_user(self.hod_cs)
        again = self.client.post(f"/api/student-removals/{rid}/approve/", {"remarks": "x"}, format="json")
        self.assertEqual(again.status_code, 400)  # already reviewed

    def test_remarks_are_required_and_teacher_cannot_review(self):
        rid = self.ask().data["id"]
        self.as_user(self.t1)
        self.assertEqual(self.client.post(f"/api/student-removals/{rid}/approve/", {"remarks": "ok"}, format="json").status_code, 403)
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.post(f"/api/student-removals/{rid}/approve/", {}, format="json").status_code, 400)

    def test_only_one_pending_request_and_short_reason_rejected(self):
        self.assertEqual(self.ask().status_code, 201)
        self.assertEqual(self.ask().status_code, 400)
        self.assertEqual(self.ask(self.students[1], reason="no").status_code, 400)

    def test_scoping(self):
        rid = self.ask().data["id"]
        self.as_user(self.hod_math)
        self.assertEqual(self.client.post(f"/api/student-removals/{rid}/approve/", {"remarks": "x"}, format="json").status_code, 404)
        self.assertEqual(self.client.get("/api/student-removals/").data["count"], 0)
        self.as_user(self.t2)  # a teacher of the same class sees only their own requests
        self.assertEqual(self.client.get("/api/student-removals/").data["count"], 0)
        self.as_user(self.t_math)
        r = self.client.post("/api/student-removals/", {"student": self.students[1].id, "reason": "Not my class"}, format="json")
        self.assertEqual(r.status_code, 400)
        self.as_user(self.admin)
        self.assertEqual(self.client.post("/api/student-removals/", {"student": self.students[1].id, "reason": "Admin tries"}, format="json").status_code, 403)


class SecurityHardeningTests(BaseTestCase):
    def test_unexpected_error_is_generic_and_logged_with_a_reference(self):
        from unittest import mock
        self.as_user(self.t1)
        with self.assertLogs("app.errors", level="ERROR") as logs, \
                mock.patch("attendance.views.LectureViewSet.list", side_effect=RuntimeError("secret table detail")):
            r = self.client.get("/api/lectures/")
        self.assertEqual(r.status_code, 500)
        body = r.content.decode()
        self.assertNotIn("secret table detail", body)
        self.assertNotIn("Traceback", body)
        self.assertIn(r.data["reference"], logs.output[0])
        self.assertIn("secret table detail", "\n".join(logs.output))  # the detail is in the server log only

    def test_normal_errors_keep_their_clear_message(self):
        self.as_user(self.t1)
        self.assertEqual(self.client.get("/api/lectures/999999/").status_code, 404)

    def test_failed_login_is_logged_without_the_password(self):
        self.client.force_authenticate(user=None)
        with self.assertLogs("app.security", level="WARNING") as logs:
            r = self.client.post("/api/auth/login/", {"username": "prof_a", "password": "WrongPass#123"}, format="json")
        self.assertEqual(r.status_code, 401)
        text = "\n".join(logs.output)
        self.assertIn("prof_a", text)
        self.assertNotIn("WrongPass#123", text)

    def test_security_headers_are_always_sent(self):
        r = self.client.get("/api/auth/me/")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertEqual(r.headers["Referrer-Policy"], "same-origin")


class EncryptedBackupTests(BaseTestCase):
    def test_backup_is_encrypted_and_restores_with_the_right_key_only(self):
        import io, sqlite3, tempfile
        from pathlib import Path
        from cryptography.fernet import Fernet
        from django.core.management import call_command
        from django.core.management.base import CommandError
        from django.test import override_settings

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "live.sqlite3"
            con = sqlite3.connect(db)
            con.execute("create table t (v text)")
            con.execute("insert into t values ('student-secret-name')")
            con.commit()
            con.close()
            key = Fernet.generate_key().decode()
            settings_patch = dict(BACKUP_ENCRYPTION_KEY=key, BACKUP_DIR=Path(tmp) / "bk", DATABASES={"default": {"NAME": db, "ENGINE": "django.db.backends.sqlite3"}})
            with override_settings(**settings_patch):
                call_command("backup_db", stdout=io.StringIO())
                files = list((Path(tmp) / "bk").glob("*.enc"))
                self.assertEqual(len(files), 1)
                self.assertNotIn(b"student-secret-name", files[0].read_bytes())
                self.assertNotIn(b"SQLite format", files[0].read_bytes())
                out = Path(tmp) / "restored.sqlite3"
                call_command("restore_db", str(files[0]), "--output", str(out), stdout=io.StringIO())
                self.assertEqual(sqlite3.connect(out).execute("select v from t").fetchone()[0], "student-secret-name")
            with override_settings(**{**settings_patch, "BACKUP_ENCRYPTION_KEY": Fernet.generate_key().decode()}):
                with self.assertRaises(CommandError):
                    call_command("restore_db", str(files[0]), "--output", str(Path(tmp) / "x.sqlite3"), stdout=io.StringIO())
            with override_settings(BACKUP_ENCRYPTION_KEY=""):
                with self.assertRaises(CommandError):
                    call_command("backup_db", stdout=io.StringIO())


class ApiNetworkLockTests(BaseTestCase):
    """The optional API_ALLOWED_NETWORKS lock: only listed networks reach /api/."""

    def test_no_lock_by_default(self):
        self.assertEqual(self.client.get("/api/auth/me/").status_code, 401)  # reaches the API, only login is missing

    def test_other_network_is_blocked_even_with_a_token(self):
        import ipaddress
        self.client.force_authenticate(self.admin)
        with self.settings(API_ALLOWED_NETWORKS=[ipaddress.ip_network("10.1.0.0/16")]):
            blocked = self.client.get("/api/auth/me/", REMOTE_ADDR="8.8.8.8")
            allowed = self.client.get("/api/auth/me/", REMOTE_ADDR="10.1.2.3")
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(allowed.status_code, 200)

    def test_faked_forwarded_header_does_not_open_the_lock(self):
        import ipaddress
        with self.settings(API_ALLOWED_NETWORKS=[ipaddress.ip_network("10.1.0.0/16")], BEHIND_PROXY=True):
            r = self.client.get("/api/auth/me/", REMOTE_ADDR="127.0.0.1", HTTP_X_FORWARDED_FOR="10.1.2.3, 8.8.8.8")
        self.assertEqual(r.status_code, 403)  # the last entry (added by nginx) is the real one


class InputValidationTests(BaseTestCase):
    """Wrong text in a field is refused with a clear message (400), never saved and never a server error."""

    def post(self, url, data, user=None):
        self.as_user(user or self.hod_cs)
        return self.client.post(url, data, format="json")

    def staff(self, **over):
        return {"username": "newteacher", "password": "Abcdef#1234x", "first_name": "Ravi", "last_name": "Patil",
                "email": "ravi.new@example.com", "phone": "9876543210", **over}

    def test_good_staff_is_accepted_including_marathi_names(self):
        self.assertEqual(self.post("/api/staff/", self.staff()).status_code, 201)
        self.assertEqual(self.post("/api/staff/", self.staff(username="marathi1", email="m@example.com", first_name="कृष्णा", last_name="ठोंबरे")).status_code, 201)

    def test_names_refuse_numbers_and_symbols(self):
        for bad in ("Ravi123", "<b>Ravi</b>", "R@vi", "12345"):
            r = self.post("/api/staff/", self.staff(first_name=bad))
            self.assertEqual(r.status_code, 400, bad)
            self.assertIn("first_name", r.data)

    def test_phone_refuses_text_and_short_numbers(self):
        for bad in ("abcdefghij", "123", "98765abc10"):
            self.assertEqual(self.post("/api/staff/", self.staff(phone=bad)).status_code, 400, bad)

    def test_staff_id_and_designation(self):
        self.assertEqual(self.post("/api/staff/", self.staff(employee_id="<script>")).status_code, 400)
        self.assertEqual(self.post("/api/staff/", self.staff(designation="12345")).status_code, 400)

    def test_student_name_roll_and_card(self):
        base = {"roll_number": "77", "name": "Test Student", "classroom": self.class_cs.pk}
        self.assertEqual(self.post("/api/students/", {**base, "name": "Rahul 123"}).status_code, 400)
        self.assertEqual(self.post("/api/students/", {**base, "name": "<img src=x>"}).status_code, 400)
        for bad in ("!!", "-5", "0", "00"):
            self.assertEqual(self.post("/api/students/", {**base, "roll_number": bad}).status_code, 400, bad)
        self.assertEqual(self.post("/api/students/", {**base, "card_code": "<X>"}).status_code, 400)
        self.assertEqual(self.post("/api/students/", {**base, "name": "राहुल जोशी", "card_code": "ab-12"}).status_code, 201)

    def test_department_and_class_names(self):
        for name in ("123", "<b>x</b>", "!!"):
            self.assertEqual(self.post("/api/departments/", {"name": name, "code": "ZZ"}, self.admin).status_code, 400, name)
        r = self.post("/api/classes/", {"name": "<x>", "semester": 1, "academic_year": "2026-27"})
        self.assertEqual(r.status_code, 400)

    def test_reasons_need_words(self):
        for bad in ("!!!!!!", "12345", "<script>alert(1)</script>"):
            r = self.post("/api/leave-requests/", {"leave_type": "CASUAL", "from_date": "2026-11-03", "to_date": "2026-11-03", "reason": bad}, self.t1)
            self.assertEqual(r.status_code, 400, bad)

    def test_own_profile_refuses_number_name_and_text_phone(self):
        self.as_user(self.t1)
        self.assertEqual(self.client.patch("/api/auth/me/", {"first_name": "12345"}, format="json").status_code, 400)
        self.assertEqual(self.client.patch("/api/auth/me/", {"phone": "abc"}, format="json").status_code, 400)
