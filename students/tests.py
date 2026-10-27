from django.db import IntegrityError, transaction

from academics.models import ClassRoom, TeacherSubjectAssignment
from core.models import AuditLog
from core.testing import BaseTestCase

from .models import Student


class StudentRegistrationTests(BaseTestCase):
    def post(self, user, **data):
        self.as_user(user)
        payload = {"roll_number": "21", "name": "New Student", "classroom": self.class_cs.id, **data}
        return self.client.post("/api/students/", payload, format="json")

    def test_teacher_registers_student_in_assigned_class(self):
        response = self.post(self.t1, roll_number=" a-21 ")
        self.assertEqual(response.status_code, 201, response.data)
        student = Student.objects.get(pk=response.data["id"])
        self.assertEqual(student.roll_number, "A-21")  # normalised
        self.assertEqual(student.department, self.cs)  # derived from the class
        self.assertEqual(student.created_by, self.t1)

    def test_department_cannot_be_forced_by_the_client(self):
        response = self.post(self.t1, department=self.math.id)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(Student.objects.get(pk=response.data["id"]).department, self.cs)

    def test_duplicate_roll_number_returns_existing_student_instead_of_creating(self):
        # Prof A registered student "1" in the fixture; Prof B also teaches this class.
        before = Student.objects.count()
        response = self.post(self.t2, roll_number="1", name="Different Name")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data["code"], "duplicate_student")
        self.assertEqual(response.data["existing_student"]["id"], self.students[0].id)
        self.assertEqual(response.data["existing_student"]["name"], "Student 1")
        self.assertEqual(Student.objects.count(), before)

    def test_duplicate_detection_ignores_case_and_spaces(self):
        Student.objects.create(classroom=self.class_cs, roll_number="ab12", name="X")
        self.assertEqual(self.post(self.t1, roll_number=" AB 12 ").status_code, 409)

    def test_same_roll_number_is_allowed_in_another_class(self):
        other = ClassRoom.objects.create(department=self.cs, name="MCS", semester=2, division="A", academic_year="2026-27")
        TeacherSubjectAssignment.objects.create(teacher=self.t1, subject=self.python, classroom=other, academic_year="2026-27")
        response = self.post(self.t1, roll_number="1", classroom=other.id)
        self.assertEqual(response.status_code, 201, response.data)

    def test_database_enforces_unique_roll_per_class(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            Student.objects.create(classroom=self.class_cs, roll_number="1", name="Clone")

    def test_teacher_cannot_register_in_unassigned_or_foreign_class(self):
        response = self.post(self.t1, classroom=self.class_math.id)
        self.assertEqual(response.status_code, 400)
        self.assertIn("classroom", response.data)
        self.assertEqual(Student.objects.filter(classroom=self.class_math).count(), 3)

    def test_hod_registers_but_only_in_own_department(self):
        self.assertEqual(self.post(self.hod_cs).status_code, 201)
        self.assertEqual(self.post(self.hod_cs, roll_number="77", classroom=self.class_math.id).status_code, 400)

    def test_admin_cannot_register_but_can_edit_and_move_within_department(self):
        self.assertEqual(self.post(self.admin).status_code, 403)
        self.as_user(self.admin)
        s = self.students[0]
        r = self.client.patch(f"/api/students/{s.id}/", {"name": "Corrected Name", "is_active": False}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        s.refresh_from_db()
        self.assertEqual((s.name, s.is_active), ("Corrected Name", False))
        # moving to another department's class is refused
        r = self.client.patch(f"/api/students/{s.id}/", {"classroom": self.class_math.id}, format="json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.client.post("/api/students/import-csv/", {}, format="json").status_code, 403)
        self.assertEqual(self.client.get("/api/students/").data["count"], 8)

    def test_required_fields_are_validated(self):
        self.as_user(self.t1)
        response = self.client.post("/api/students/", {"roll_number": "", "name": "", "classroom": None}, format="json")
        self.assertEqual(response.status_code, 400)
        for field in ("roll_number", "name", "classroom"):
            self.assertIn(field, response.data)


class StudentAccessTests(BaseTestCase):
    def test_teacher_sees_students_of_taught_classes_including_ones_added_by_colleagues(self):
        self.as_user(self.t2)
        self.assertEqual(self.client.get("/api/students/").data["count"], 5)

    def test_teacher_cannot_see_other_departments_students(self):
        self.as_user(self.t1)
        ids = {s["id"] for s in self.client.get("/api/students/").data["results"]}
        self.assertFalse(ids & {s.id for s in self.math_students})
        self.assertEqual(self.client.get(f"/api/students/{self.math_students[0].id}/").status_code, 404)
        self.assertEqual(self.client.patch(f"/api/students/{self.math_students[0].id}/", {"name": "Hacked"}, format="json").status_code, 404)

    def test_hod_sees_whole_department_with_registering_teacher(self):
        self.as_user(self.hod_cs)
        response = self.client.get("/api/students/")
        self.assertEqual(response.data["count"], 5)
        self.assertEqual(response.data["results"][0]["created_by_name"], self.t1.display_name)
        self.assertEqual(self.client.get(f"/api/students/{self.math_students[0].id}/").status_code, 404)

    def test_teacher_without_assignment_in_class_cannot_see_its_students(self):
        self.assign_db.is_active = False
        self.assign_db.save()
        self.as_user(self.t2)
        self.assertEqual(self.client.get("/api/students/").data["count"], 0)

    def test_search_and_class_filter(self):
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.get("/api/students/?search=Student 3").data["count"], 1)
        self.assertEqual(self.client.get(f"/api/students/?classroom={self.class_cs.id}").data["count"], 5)

    def test_edit_student_details_but_not_class_or_into_duplicate_roll(self):
        self.as_user(self.t1)
        sid = self.students[0].id
        self.assertEqual(self.client.patch(f"/api/students/{sid}/", {"name": "Renamed Student"}, format="json").status_code, 200)
        clash = self.client.patch(f"/api/students/{sid}/", {"roll_number": "2"}, format="json")
        self.assertEqual(clash.status_code, 400)
        move = self.client.patch(f"/api/students/{sid}/", {"classroom": self.class_math.id}, format="json")
        self.assertEqual(move.status_code, 400)  # a teacher cannot move a student
        self.students[0].refresh_from_db()
        self.assertEqual(self.students[0].name, "Renamed Student")
        self.assertEqual(self.students[0].classroom, self.class_cs)

    def test_hod_can_move_a_student_to_another_class_of_the_department(self):
        from academics.models import ClassRoom
        part2 = ClassRoom.objects.create(department=self.cs, name="MCS Part II", semester=3, division="A", academic_year="2026-27")
        s = self.students[0]
        self.as_user(self.hod_cs)
        r = self.client.patch(f"/api/students/{s.id}/", {"classroom": part2.id}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        s.refresh_from_db()
        self.assertEqual(s.classroom, part2)
        self.assertEqual(s.department_id, self.cs.id)
        self.assertEqual(self.client.get(f"/api/students/?classroom={self.class_cs.id}").data["count"], 4)
        log = AuditLog.objects.filter(action="STUDENT_UPDATED").latest("id")
        self.assertIn("MCS Part II", str(log.new_value))

    def test_move_is_refused_for_other_department_or_a_clashing_roll_number(self):
        from academics.models import ClassRoom
        part2 = ClassRoom.objects.create(department=self.cs, name="MCS Part II", semester=3, division="A", academic_year="2026-27")
        Student.objects.create(classroom=part2, roll_number=self.students[0].roll_number, name="Already There")
        self.as_user(self.hod_cs)
        sid = self.students[0].id
        self.assertEqual(self.client.patch(f"/api/students/{sid}/", {"classroom": self.class_math.id}, format="json").status_code, 400)
        clash = self.client.patch(f"/api/students/{sid}/", {"classroom": part2.id}, format="json")
        self.assertEqual(clash.status_code, 400)
        ok = self.client.patch(f"/api/students/{sid}/", {"classroom": part2.id, "roll_number": "901"}, format="json")
        self.assertEqual(ok.status_code, 200, ok.data)

    def test_students_cannot_be_deleted(self):
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.delete(f"/api/students/{self.students[0].id}/").status_code, 405)


class StudentOrderingTests(BaseTestCase):
    def test_roll_numbers_are_listed_in_natural_order(self):
        for roll in ("5000", "700", "60000", "900"):
            Student.objects.create(classroom=self.class_cs, roll_number=roll, name=f"S{roll}")
        self.as_user(self.t1)
        response = self.client.get(f"/api/students/?classroom={self.class_cs.id}&page_size=200")
        self.assertEqual(response.status_code, 200)
        numeric = [int(r["roll_number"]) for r in response.data["results"] if r["roll_number"].isdigit()]
        self.assertEqual(numeric, sorted(numeric))


class StudentPhotoTests(BaseTestCase):
    def png(self, size=(900, 600), color=(200, 30, 30), fmt="PNG"):
        from io import BytesIO
        from PIL import Image
        buf = BytesIO()
        Image.new("RGB", size, color).save(buf, format=fmt)
        buf.seek(0)
        buf.name = "me." + fmt.lower()
        return buf

    def upload(self, student=None, file=None):
        return self.client.post(f"/api/students/{(student or self.students[0]).id}/photo/", {"photo": file or self.png()}, format="multipart")

    def test_teacher_uploads_photo_it_is_shrunk_to_jpeg_and_served_back(self):
        from io import BytesIO
        from PIL import Image
        self.as_user(self.t1)
        r = self.upload()
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data["has_photo"])
        self.assertGreater(r.data["photo_version"], 0)
        got = self.client.get(f"/api/students/{self.students[0].id}/photo/")
        self.assertEqual(got.status_code, 200)
        self.assertEqual(got["Content-Type"], "image/jpeg")
        img = Image.open(BytesIO(got.content))
        self.assertEqual(img.format, "JPEG")
        self.assertLessEqual(max(img.size), 480)

    def test_student_list_and_roster_report_has_photo_without_the_bytes(self):
        self.as_user(self.t1)
        self.upload()
        rows = self.client.get("/api/students/", {"search": self.students[0].name}).data["results"]
        self.assertTrue(rows[0]["has_photo"])
        self.assertNotIn("photo", rows[0])
        lecture = self.make_lecture(self.assign_py)
        roster = self.client.get(f"/api/lectures/{lecture.id}/roster/").data["students"]
        self.assertTrue({s["id"]: s["has_photo"] for s in roster}[self.students[0].id])

    def test_bad_files_are_refused(self):
        from io import BytesIO
        self.as_user(self.t1)
        junk = BytesIO(b"this is not an image"); junk.name = "x.png"
        self.assertEqual(self.upload(file=junk).status_code, 400)
        gif = self.png(fmt="GIF")
        self.assertEqual(self.upload(file=gif).status_code, 400)
        self.assertEqual(self.client.post(f"/api/students/{self.students[0].id}/photo/", {}, format="multipart").status_code, 400)
        self.students[0].refresh_from_db()
        self.assertIsNone(self.students[0].photo_updated_at)

    def test_photo_without_upload_is_404_and_remove_works(self):
        self.as_user(self.t1)
        self.assertEqual(self.client.get(f"/api/students/{self.students[0].id}/photo/").status_code, 404)
        self.upload()
        self.assertEqual(self.client.delete(f"/api/students/{self.students[0].id}/photo/").status_code, 200)
        self.assertEqual(self.client.get(f"/api/students/{self.students[0].id}/photo/").status_code, 404)
        from core.models import AuditLog
        self.assertTrue(AuditLog.objects.filter(action="STUDENT_PHOTO_UPDATED").exists())
        self.assertTrue(AuditLog.objects.filter(action="STUDENT_PHOTO_REMOVED").exists())

    def test_scoping_and_login_required(self):
        self.as_user(self.t1)
        self.upload()
        url = f"/api/students/{self.students[0].id}/photo/"
        self.as_user(self.t_math)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertEqual(self.upload().status_code, 404)
        self.as_user(self.hod_math)
        self.assertEqual(self.client.get(url).status_code, 404)
        self.as_user(self.admin)
        self.assertEqual(self.client.get(url).status_code, 200)   # admin may look
        self.assertEqual(self.upload().status_code, 200)          # and, since 5.5, may also set it
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(url).status_code, 401)


class PromoteClassTests(BaseTestCase):
    """Start of a new year: a class moves to the next one only after the HOD and then the admin approve."""

    def setUp(self):
        super().setUp()
        self.next_class = ClassRoom.objects.create(
            department=self.cs, name="MCS Part II", semester=3, division="A", academic_year="2027-28")

    def ask(self, user, **extra):
        self.as_user(user)
        body = {"from_classroom": self.class_cs.id, "to_classroom": self.next_class.id, **extra}
        return self.client.post("/api/class-promotions/", body, format="json")

    def decide(self, user, rid, verb="approve", remarks="Ok"):
        self.as_user(user)
        return self.client.post(f"/api/class-promotions/{rid}/{verb}/", {"remarks": remarks}, format="json")

    def in_old_class(self):
        return Student.objects.filter(classroom=self.class_cs, is_active=True).count()

    def test_teacher_request_needs_hod_then_admin_and_only_then_students_move(self):
        count = self.in_old_class()
        r = self.ask(self.t1)
        self.assertEqual((r.status_code, r.data["status"]), (201, "WAITING_HOD"))
        rid = r.data["id"]
        # the admin cannot jump the queue, and a teacher cannot approve
        self.assertEqual(self.decide(self.admin, rid).status_code, 400)
        self.assertEqual(self.decide(self.t1, rid).status_code, 403)
        self.assertEqual(self.in_old_class(), count)
        hod = self.decide(self.hod_cs, rid)
        self.assertEqual(hod.data["status"], "WAITING_ADMIN")
        self.assertEqual(self.in_old_class(), count)  # still nobody moved
        self.assertEqual(self.decide(self.hod_cs, rid).status_code, 400)  # HOD cannot approve twice or do the admin step
        done = self.decide(self.admin, rid)
        self.assertEqual((done.data["status"], done.data["moved_count"]), ("DONE", count))
        self.assertEqual(Student.objects.filter(classroom=self.next_class).count(), count)
        self.assertEqual(self.in_old_class(), 0)
        self.assertTrue(AuditLog.objects.filter(action="CLASS_PROMOTED").exists())

    def test_hod_request_skips_the_hod_step_and_waits_for_admin(self):
        r = self.ask(self.hod_cs)
        self.assertEqual(r.data["status"], "WAITING_ADMIN")
        self.assertEqual(Student.objects.filter(classroom=self.next_class).count(), 0)
        self.assertEqual(self.decide(self.admin, r.data["id"]).data["status"], "DONE")

    def test_students_left_behind_and_roll_clashes_do_not_move(self):
        stay = Student.objects.filter(classroom=self.class_cs).first()
        Student.objects.create(classroom=self.next_class, roll_number="2", name="Already here", created_by=self.hod_cs)
        rid = self.ask(self.hod_cs, exclude=[stay.id]).data["id"]
        done = self.decide(self.admin, rid).data
        self.assertEqual(done["clash_count"], 1)
        stay.refresh_from_db()
        self.assertEqual(stay.classroom_id, self.class_cs.id)
        self.assertTrue(Student.objects.filter(classroom=self.class_cs, roll_number="2", is_active=True).exists())

    def test_rejection_and_cancel_move_nobody_and_one_open_request_per_class(self):
        count = self.in_old_class()
        rid = self.ask(self.t1).data["id"]
        self.assertEqual(self.ask(self.hod_cs).status_code, 400)  # already one open for this class
        rejected = self.decide(self.hod_cs, rid, "reject", "Not yet")
        self.assertEqual((rejected.data["status"], rejected.data["rejected_by_role"]), ("REJECTED", "HOD"))
        self.assertEqual(self.in_old_class(), count)
        rid2 = self.ask(self.t1).data["id"]
        self.as_user(self.t2)
        self.assertEqual(self.client.post(f"/api/class-promotions/{rid2}/cancel/").status_code, 404)  # not his request
        self.as_user(self.t1)
        self.assertEqual(self.client.post(f"/api/class-promotions/{rid2}/cancel/").data["status"], "CANCELLED")

    def test_who_may_ask_and_where(self):
        self.assertEqual(self.ask(self.admin).status_code, 403)       # the admin approves, he does not ask
        self.assertEqual(self.ask(self.t_math).status_code, 400)      # does not teach this class
        self.assertEqual(self.ask(self.hod_math).status_code, 400)    # other department
        self.assertEqual(self.ask(self.hod_cs, to_classroom=self.class_cs.id).status_code, 400)
        self.assertEqual(self.ask(self.hod_cs, to_classroom=self.class_math.id).status_code, 400)

    def test_preview_changes_nothing(self):
        self.as_user(self.hod_cs)
        r = self.client.post("/api/students/promote/", {"from_classroom": self.class_cs.id, "to_classroom": self.next_class.id}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Student.objects.filter(classroom=self.next_class).count(), 0)

    def test_teacher_can_list_the_classes_to_move_to(self):
        self.as_user(self.t1)
        r = self.client.get(f"/api/class-promotions/targets/?from={self.class_cs.id}")
        self.assertEqual([c["id"] for c in r.data], [self.next_class.id])  # same department only
        self.as_user(self.t_math)
        self.assertEqual(self.client.get(f"/api/class-promotions/targets/?from={self.class_cs.id}").status_code, 400)
