from accounts.models import User
from core.models import Notification
from core.testing import BaseTestCase, make_user

from .models import ClassRoom, TeacherSubjectAssignment


class ClassAndSubjectTests(BaseTestCase):
    def test_hod_creates_class_in_own_department_only(self):
        self.as_user(self.hod_cs)
        payload = {"name": "MCS Part II", "semester": 3, "division": "b", "academic_year": "2026-27",
                   "department": self.math.id}
        response = self.client.post("/api/classes/", payload, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        created = ClassRoom.objects.get(pk=response.data["id"])
        self.assertEqual(created.department, self.cs)
        self.assertEqual(created.division, "B")

    def test_duplicate_class_is_rejected(self):
        self.as_user(self.hod_cs)
        payload = {"name": "mcs", "semester": 1, "division": "A", "academic_year": "2026-27"}
        self.assertEqual(self.client.post("/api/classes/", payload, format="json").status_code, 400)

    def test_invalid_academic_year_and_semester_are_rejected(self):
        self.as_user(self.hod_cs)
        bad = {"name": "X", "semester": 13, "division": "A", "academic_year": "2026"}
        response = self.client.post("/api/classes/", bad, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("academic_year", response.data)
        self.assertIn("semester", response.data)

    def test_only_hod_can_write_classes_and_subjects(self):
        for user in (self.t1, self.admin):
            self.as_user(user)
            self.assertEqual(self.client.post("/api/classes/", {"name": "N", "semester": 1, "division": "A",
                                                                "academic_year": "2026-27"}, format="json").status_code, 403)
            self.assertEqual(self.client.post("/api/subjects/", {"name": "N", "code": "N1"}, format="json").status_code, 403)

    def test_subject_code_spellings_are_the_same_code(self):
        self.as_user(self.hod_cs)
        post = lambda name, code: self.client.post("/api/subjects/", {"name": name, "code": code}, format="json")
        r = post("Operating Systems", "cs 101")
        self.assertEqual((r.status_code, r.data["code"]), (201, "CS101"))  # spaces removed, capitals
        for same in ("CS 101", "CS-101", "cs101", "Cs_101", "C.S.101", " cs  101 "):
            r = post(f"Other {same}", same)
            self.assertEqual(r.status_code, 400, same)
            self.assertIn("code", r.data)
        self.assertEqual(post("Networks", "CS102").status_code, 201)
        self.assertEqual(post("Nothing", "--").status_code, 400)  # no letter or number
        # editing keeps its own code, but cannot become another subject's code
        sid = self.client.get("/api/subjects/?search=Networks").data["results"][0]["id"]
        self.assertEqual(self.client.patch(f"/api/subjects/{sid}/", {"code": "cs-102"}, format="json").status_code, 200)
        self.assertEqual(self.client.patch(f"/api/subjects/{sid}/", {"code": "CS 101"}, format="json").status_code, 400)
        # another department may use the same code
        self.as_user(self.hod_math)
        self.assertEqual(post("Operating Systems", "CS-101").status_code, 201)

    def test_subject_codes_and_names_are_unique_per_department(self):
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.post("/api/subjects/", {"name": "New", "code": "py101"}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/subjects/", {"name": "python", "code": "NEW1"}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/subjects/", {"name": "Networking", "code": "NW101"}, format="json").status_code, 201)
        # the same code may exist in another department
        self.as_user(self.hod_math)
        self.assertEqual(self.client.post("/api/subjects/", {"name": "Python", "code": "PY101"}, format="json").status_code, 201)

    def test_hod_cannot_see_or_edit_other_departments_classes_and_subjects(self):
        self.as_user(self.hod_cs)
        ids = {c["id"] for c in self.client.get("/api/classes/").data["results"]}
        self.assertEqual(ids, {self.class_cs.id})
        self.assertEqual(self.client.get(f"/api/classes/{self.class_math.id}/").status_code, 404)
        self.assertEqual(self.client.patch(f"/api/subjects/{self.algebra.id}/", {"name": "Hacked"}, format="json").status_code, 404)

    def test_teacher_only_sees_classes_and_subjects_they_teach(self):
        self.as_user(self.t1)
        self.assertEqual({s["id"] for s in self.client.get("/api/subjects/").data["results"]}, {self.python.id})


class AssignmentTests(BaseTestCase):
    def payload(self, **extra):
        return {"teacher": self.t1.id, "subject": self.dbms.id, "classroom": self.class_cs.id, **extra}

    def test_hod_assigns_teacher_defaulting_academic_year_and_teacher_is_notified(self):
        self.as_user(self.hod_cs)
        response = self.client.post("/api/assignments/", self.payload(), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["academic_year"], "2026-27")
        self.assertTrue(Notification.objects.filter(user=self.t1, title="New teaching assignment").exists())

    def test_duplicate_assignment_is_rejected(self):
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.post("/api/assignments/", self.payload(subject=self.python.id), format="json").status_code, 400)

    def test_cannot_mix_departments(self):
        self.as_user(self.hod_cs)
        for extra in ({"teacher": self.t_math.id}, {"subject": self.algebra.id}, {"classroom": self.class_math.id}):
            response = self.client.post("/api/assignments/", self.payload(**extra), format="json")
            self.assertEqual(response.status_code, 400, extra)

    def test_cannot_assign_hod_or_inactive_staff(self):
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.post("/api/assignments/", self.payload(teacher=self.hod_cs.id), format="json").status_code, 400)
        inactive = make_user("gone", User.Role.STAFF, self.cs, is_active=False)
        self.assertEqual(self.client.post("/api/assignments/", self.payload(teacher=inactive.id), format="json").status_code, 400)

    def test_only_hod_can_create_or_change_assignments(self):
        for user in (self.t1, self.admin):
            self.as_user(user)
            self.assertEqual(self.client.post("/api/assignments/", self.payload(), format="json").status_code, 403)
            self.assertEqual(self.client.post(f"/api/assignments/{self.assign_py.id}/deactivate/").status_code, 403)

    def test_assignments_are_not_editable_only_deactivated(self):
        self.as_user(self.hod_cs)
        self.assertEqual(self.client.patch(f"/api/assignments/{self.assign_py.id}/", {"teacher": self.t2.id}, format="json").status_code, 405)
        self.assertEqual(self.client.delete(f"/api/assignments/{self.assign_py.id}/").status_code, 405)
        self.assertEqual(self.client.post(f"/api/assignments/{self.assign_py.id}/deactivate/").status_code, 200)
        self.assign_py.refresh_from_db()
        self.assertFalse(self.assign_py.is_active)

    def test_teacher_sees_only_own_assignments(self):
        self.as_user(self.t1)
        ids = {a["id"] for a in self.client.get("/api/assignments/").data["results"]}
        self.assertEqual(ids, {self.assign_py.id})
        self.assertEqual(self.client.get(f"/api/assignments/{self.assign_db.id}/").status_code, 404)

    def test_hod_cannot_see_other_department_assignments(self):
        self.as_user(self.hod_cs)
        ids = {a["id"] for a in self.client.get("/api/assignments/").data["results"]}
        self.assertNotIn(self.assign_math.id, ids)
        self.assertEqual(self.client.post(f"/api/assignments/{self.assign_math.id}/deactivate/").status_code, 404)
        self.assertEqual(TeacherSubjectAssignment.objects.get(pk=self.assign_math.id).is_active, True)
